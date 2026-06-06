"""
smc_engine.py
Smart Money Concepts detection — ported from LuxAlgo Pine Script logic.

Detects:
  - Swing pivot highs/lows (leg-based, same method as LuxAlgo)
  - Internal pivots (shorter lookback)
  - Market structure: BOS (Break of Structure) & CHoCH (Change of Character)
  - Order Blocks (the candle that caused the structure break)
  - Whether current price is INSIDE an order block

This runs on OHLC candle data fetched from any price API.
It is a faithful-as-practical port; minor differences vs TradingView
can occur due to floating point and bar indexing, but the structural
logic (pivots, BOS/CHoCH, OB selection) mirrors the original.
"""

from dataclasses import dataclass, field
from typing import Optional


BULLISH = 1
BEARISH = -1


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class Pivot:
    level: float = None
    last_level: float = None
    crossed: bool = False
    bar_index: int = 0
    bar_time: int = 0


@dataclass
class OrderBlock:
    high: float
    low: float
    time: int
    bias: int          # BULLISH or BEARISH
    bar_index: int


@dataclass
class SMCResult:
    pair: str
    price: float
    trend: int                       # current internal trend bias
    in_ob: bool = False              # is price currently inside an OB?
    ob_bias: int = 0                 # bias of the OB price is in
    ob_high: float = None
    ob_low: float = None
    ob_type: str = None              # 'Swing' or 'Internal'
    last_structure: str = None       # 'BOS' or 'CHoCH'
    last_structure_bias: int = 0
    swing_trend: int = 0
    internal_trend: int = 0
    # ── extra confluence factors ──
    fvg: bool = False                # Fair Value Gap supporting the OB direction
    eqhl: bool = False               # Equal highs/lows (liquidity) near the zone
    liquidity_sweep: bool = False    # recent sweep of a prior high/low (stop hunt)
    sweep_level: float = None        # the level that was swept
    confluence: int = 0              # total score 1-5
    factors: list = None             # human-readable list of met factors


class SMCEngine:
    """
    Feed it a list of candles (oldest first) and it computes SMC structure.
    Mirrors LuxAlgo: a 'leg' flips bullish/bearish when price makes a new
    highest-high / lowest-low over `swing_length` bars. The flip marks a pivot.
    """

    def __init__(self, swing_length: int = 50, internal_length: int = 5,
                 atr_period: int = 200, ob_filter_mult: float = 2.0):
        self.swing_length = swing_length
        self.internal_length = internal_length
        self.atr_period = atr_period
        self.ob_filter_mult = ob_filter_mult

    # ── ATR (for OB volatility filter & structure noise) ──
    def _atr(self, candles, period):
        if len(candles) < 2:
            return [0.0] * len(candles)
        trs = [candles[0].high - candles[0].low]
        for i in range(1, len(candles)):
            h, l, pc = candles[i].high, candles[i].low, candles[i-1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        # Wilder-style running mean approximated by simple rolling mean
        atr = []
        for i in range(len(trs)):
            start = max(0, i - period + 1)
            window = trs[start:i+1]
            atr.append(sum(window) / len(window))
        return atr

    # ── Leg detection (the heart of LuxAlgo pivot logic) ──
    def _legs(self, candles, size):
        """
        Returns a list where each element is the current leg state at that bar.
        leg = 0 means bearish leg started, 1 means bullish leg started.
        A new highest-high over `size` bars -> bearish leg (BEARISH_LEG=0)
        A new lowest-low over `size` bars  -> bullish leg (BULLISH_LEG=1)
        (matches LuxAlgo: newLegHigh -> BEARISH_LEG, newLegLow -> BULLISH_LEG)
        """
        legs = []
        leg = 0
        n = len(candles)
        for i in range(n):
            if i < size:
                legs.append(leg)
                continue
            # LuxAlgo: newLegHigh = high[size] > ta.highest(size)
            # high[size] = the bar `size` ago. ta.highest(size) = highest high
            # of the current window (the last `size` bars up to current bar).
            # So we compare the bar `size` ago against the most recent `size` bars.
            ref = candles[i-size]
            window = candles[i-size+1:i+1]   # the `size` most recent bars
            if not window:
                legs.append(leg)
                continue
            window_high = max(c.high for c in window)
            window_low  = min(c.low  for c in window)
            new_leg_high = ref.high > window_high
            new_leg_low  = ref.low  < window_low
            if new_leg_high:
                leg = 0   # BEARISH_LEG (a high was confirmed -> top pivot)
            elif new_leg_low:
                leg = 1   # BULLISH_LEG (a low was confirmed -> bottom pivot)
            legs.append(leg)
        return legs

    def _process_structure(self, candles, size, internal=False):
        """
        Walk candles, find pivots from leg changes, detect BOS/CHoCH when
        close crosses a pivot level, and capture the order block that
        caused the break.
        Returns (swing_high_pivot, swing_low_pivot, trend, order_blocks list)
        """
        n = len(candles)
        legs = self._legs(candles, size)

        piv_high = Pivot()
        piv_low = Pivot()
        trend = 0
        order_blocks = []

        for i in range(1, n):
            # detect leg change = new pivot
            if legs[i] != legs[i-1]:
                # leg changed to bullish (1) => a swing LOW formed `size` bars back
                if legs[i] == 1:
                    idx = i - size
                    if idx >= 0:
                        piv_low.last_level = piv_low.level
                        piv_low.level = candles[idx].low
                        piv_low.crossed = False
                        piv_low.bar_index = idx
                        piv_low.bar_time = candles[idx].time
                # leg changed to bearish (0) => a swing HIGH formed `size` bars back
                else:
                    idx = i - size
                    if idx >= 0:
                        piv_high.last_level = piv_high.level
                        piv_high.level = candles[idx].high
                        piv_high.crossed = False
                        piv_high.bar_index = idx
                        piv_high.bar_time = candles[idx].time

            c = candles[i]

            # ── Bullish structure: close crosses above last pivot high ──
            if piv_high.level is not None and not piv_high.crossed:
                if c.close > piv_high.level:
                    tag = 'CHoCH' if trend == BEARISH else 'BOS'
                    piv_high.crossed = True
                    trend = BULLISH
                    ob = self._find_ob(candles, piv_high.bar_index, i, BULLISH)
                    if ob:
                        order_blocks.insert(0, ob)
                    piv_high.last_structure = tag

            # ── Bearish structure: close crosses below last pivot low ──
            if piv_low.level is not None and not piv_low.crossed:
                if c.close < piv_low.level:
                    tag = 'CHoCH' if trend == BULLISH else 'BOS'
                    piv_low.crossed = True
                    trend = BEARISH
                    ob = self._find_ob(candles, piv_low.bar_index, i, BEARISH)
                    if ob:
                        order_blocks.insert(0, ob)
                    piv_low.last_structure = tag

        return piv_high, piv_low, trend, order_blocks

    def _find_ob(self, candles, from_idx, to_idx, bias):
        """
        LuxAlgo: the order block is the extreme candle in the leg before the break.
        For a BULLISH break -> find the candle with the LOWEST low in the range
        (the last down-move before price broke up) = demand zone.
        For a BEARISH break -> find the candle with the HIGHEST high = supply zone.
        """
        if from_idx < 0 or to_idx <= from_idx:
            return None
        segment = candles[from_idx:to_idx]
        if not segment:
            return None

        if bias == BULLISH:
            ob_candle = min(segment, key=lambda c: c.low)
        else:
            ob_candle = max(segment, key=lambda c: c.high)

        return OrderBlock(
            high=ob_candle.high,
            low=ob_candle.low,
            time=ob_candle.time,
            bias=bias,
            bar_index=candles.index(ob_candle)
        )

    def _prune_mitigated(self, candles, order_blocks):
        """
        Remove order blocks that price has already traded fully through
        (mitigated). LuxAlgo High/Low mitigation:
          bearish OB removed if high > ob.high
          bullish OB removed if low  < ob.low
        We check from the OB's bar forward to the latest bar.
        """
        if not order_blocks:
            return []
        live = []
        last = candles[-1]
        for ob in order_blocks:
            after = candles[ob.bar_index+1:]
            mitigated = False
            for c in after:
                if ob.bias == BEARISH and c.high > ob.high:
                    mitigated = True
                    break
                if ob.bias == BULLISH and c.low < ob.low:
                    mitigated = True
                    break
            if not mitigated:
                live.append(ob)
        return live

    # ── Fair Value Gap detection (LuxAlgo 3-candle method) ──
    def _detect_fvg(self, candles, bias, near_price, tolerance):
        """
        A bullish FVG = gap where candle[i-2].high < candle[i].low (price jumped up
        leaving an unfilled gap). Bearish FVG = candle[i-2].low > candle[i].high.
        We look for an unfilled FVG in the OB direction near the current price.
        Returns True if a supporting FVG exists near the zone.
        """
        n = len(candles)
        for i in range(n - 1, 1, -1):
            c0 = candles[i]       # current
            c2 = candles[i-2]     # two bars back
            if bias == BULLISH:
                # bullish gap: top of gap = c0.low, bottom = c2.high
                if c0.low > c2.high:
                    gap_top, gap_bot = c0.low, c2.high
                    # unfilled = price hasn't traded back below gap bottom since
                    filled = any(candles[j].low < gap_bot for j in range(i+1, n))
                    if not filled and abs(near_price - gap_bot) < tolerance * 3:
                        return True
            else:
                # bearish gap: top = c2.low, bottom = c0.high
                if c2.low > c0.high:
                    gap_top, gap_bot = c2.low, c0.high
                    filled = any(candles[j].high > gap_top for j in range(i+1, n))
                    if not filled and abs(near_price - gap_top) < tolerance * 3:
                        return True
        return False

    # ── Equal Highs / Equal Lows (liquidity pools) ──
    def _detect_eqhl(self, candles, bias, near_price, tolerance):
        """
        Equal highs (resting liquidity above) or equal lows (below).
        For a bullish setup we look for equal LOWS near/below price (buy-side
        liquidity that was or will be swept). For bearish, equal HIGHS.
        Two swing points within `tolerance` of each other count as equal.
        """
        recent = candles[-60:] if len(candles) > 60 else candles
        if bias == BULLISH:
            lows = [c.low for c in recent]
            for i in range(len(lows)):
                for j in range(i+1, len(lows)):
                    if abs(lows[i] - lows[j]) < tolerance * 0.5:
                        if abs(near_price - lows[i]) < tolerance * 4:
                            return True
        else:
            highs = [c.high for c in recent]
            for i in range(len(highs)):
                for j in range(i+1, len(highs)):
                    if abs(highs[i] - highs[j]) < tolerance * 0.5:
                        if abs(near_price - highs[i]) < tolerance * 4:
                            return True
        return False

    # ── Liquidity sweep (stop hunt) ──
    def _detect_sweep(self, candles, bias, tolerance):
        """
        A liquidity sweep = price briefly pierces a prior swing high/low (taking
        stops) then closes back inside, signalling a reversal. This is a strong
        SMC confluence: smart money grabbed liquidity before the real move.
        For a BULLISH setup we want a sweep BELOW a prior low then close back up.
        For a BEARISH setup, a sweep ABOVE a prior high then close back down.
        Checks the last few candles.
        Returns (swept: bool, level: float|None).
        """
        if len(candles) < 12:
            return False, None
        recent = candles[-6:]      # look at last 6 bars for the sweep
        prior  = candles[-30:-6] if len(candles) >= 30 else candles[:-6]
        if not prior:
            return False, None

        if bias == BULLISH:
            prior_low = min(c.low for c in prior)
            for c in recent:
                # wick pierces below prior low but candle closes back above it
                if c.low < prior_low and c.close > prior_low:
                    return True, prior_low
        else:
            prior_high = max(c.high for c in prior)
            for c in recent:
                if c.high > prior_high and c.close < prior_high:
                    return True, prior_high
        return False, None

    def analyze(self, pair, candles_4h, candles_15m) -> Optional[SMCResult]:
        """
        Main entry point.
        - Compute SWING order blocks on the 4H timeframe (HTF AOI)
        - Compute internal trend / structure on the 15M (LTF confirmation)
        - Check if current price is inside a live 4H OB
        - Confirm 15M structure aligns with that OB's bias
        """
        if len(candles_4h) < self.swing_length + 5:
            return None
        if len(candles_15m) < self.internal_length + 5:
            return None

        price = candles_15m[-1].close

        # ── HTF (4H) swing structure + order blocks ──
        sh4, sl4, trend4, obs4_swing = self._process_structure(
            candles_4h, self.swing_length, internal=False)
        obs4_swing = self._prune_mitigated(candles_4h, obs4_swing)

        # also compute 4H internal OBs (shorter lookback) for more zones
        sh4i, sl4i, trend4i, obs4_int = self._process_structure(
            candles_4h, self.internal_length, internal=True)
        obs4_int = self._prune_mitigated(candles_4h, obs4_int)

        # ── LTF (15M) internal structure for confirmation ──
        sh15, sl15, trend15, _ = self._process_structure(
            candles_15m, self.internal_length, internal=True)
        # last 15m structure tag
        last_struct_15 = None
        last_struct_bias_15 = 0
        if getattr(sh15, 'last_structure', None) and trend15 == BULLISH:
            last_struct_15 = sh15.last_structure
            last_struct_bias_15 = BULLISH
        elif getattr(sl15, 'last_structure', None) and trend15 == BEARISH:
            last_struct_15 = sl15.last_structure
            last_struct_bias_15 = BEARISH

        # ── Check if price is inside a live 4H OB ──
        def price_in_ob(obs, ob_type):
            for ob in obs:
                if ob.low <= price <= ob.high:
                    return ob, ob_type
            return None, None

        hit_ob, hit_type = price_in_ob(obs4_swing, 'Swing')
        if hit_ob is None:
            hit_ob, hit_type = price_in_ob(obs4_int, 'Internal')

        result = SMCResult(
            pair=pair,
            price=price,
            trend=trend4,
            swing_trend=trend4,
            internal_trend=trend15,
        )

        if hit_ob is not None:
            result.in_ob = True
            result.ob_bias = hit_ob.bias
            result.ob_high = hit_ob.high
            result.ob_low = hit_ob.low
            result.ob_type = hit_type
            result.last_structure = last_struct_15
            result.last_structure_bias = last_struct_bias_15

            # tolerance based on ATR of the 4H series (zone width proxy)
            atr4 = self._atr(candles_4h, 14)
            tol = atr4[-1] if atr4 else (hit_ob.high - hit_ob.low)

            ob_bull = (hit_ob.bias == BULLISH)

            # Factor 2: 15m structure aligned
            struct_aligned = bool(
                last_struct_15 and (
                    (ob_bull and last_struct_bias_15 == BULLISH) or
                    (not ob_bull and last_struct_bias_15 == BEARISH)
                )
            )

            # Factor 3: FVG supporting the OB direction (check on 15m, near price)
            result.fvg = self._detect_fvg(candles_15m, hit_ob.bias, price, tol)

            # Factor 4: Equal highs/lows liquidity near the zone (on 4H)
            result.eqhl = self._detect_eqhl(candles_4h, hit_ob.bias, price, tol)

            # Factor 5: Liquidity sweep / stop hunt (on 15m for entry timing)
            swept, swept_level = self._detect_sweep(candles_15m, hit_ob.bias, tol)
            result.liquidity_sweep = swept
            result.sweep_level = swept_level

            # ── Total confluence score (max 5) ──
            factors = ['4H OB']
            score = 1
            if struct_aligned:
                score += 1; factors.append('15m ' + (last_struct_15 or 'aligned'))
            if result.fvg:
                score += 1; factors.append('FVG')
            if result.eqhl:
                score += 1; factors.append('EQH/EQL')
            if result.liquidity_sweep:
                score += 1; factors.append('Liquidity Sweep')

            result.confluence = score
            result.factors = factors

        return result
