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

        return result
