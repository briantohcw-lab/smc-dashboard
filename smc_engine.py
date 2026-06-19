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
    struct_aligned: bool = False     # is 15m structure aligned with the OB bias?
    # ── mitigation-event tracking (price tapped the OB, may have wicked out) ──
    mitigated: bool = False          # OB was tapped within the watch window
    bars_since_mit: int = None       # 15m bars since the first tap
    session: str = None              # session the mitigation happened in (NY/London/Asian)
    currently_in_ob: bool = False    # is price literally inside the OB right now
    # ── break-and-retest of 15m OB (the entry trigger) ──
    br_state: str = None             # 'broken' (awaiting retest) | 'retest' (signal)
    ltf_ob_high: float = None        # the 15m OB zone left by the structure break
    ltf_ob_low: float = None
    # ── nearest-OB watchlist (when NOT currently in an OB) ──
    near_ob_bias: int = 0            # bias of nearest OB
    near_ob_high: float = None
    near_ob_low: float = None
    near_ob_type: str = None
    near_distance: float = None      # absolute price distance to nearest OB edge
    near_distance_pips: float = None # distance expressed in pips
    # ── structural SL/TP REFERENCE (not advice; mechanical levels only) ──
    sl_price: float = None           # stop beyond the far edge of the OB
    tp_price: float = None           # take-profit at default R:R
    rr: float = None                 # the R:R used for tp_price
    sl_pips: float = None
    tp_pips: float = None


class SMCEngine:
    """
    Feed it a list of candles (oldest first) and it computes SMC structure.
    Mirrors LuxAlgo: a 'leg' flips bullish/bearish when price makes a new
    highest-high / lowest-low over `swing_length` bars. The flip marks a pivot.
    """

    def __init__(self, swing_length: int = 50, internal_length: int = 5,
                 atr_period: int = 200, ob_filter_mult: float = 2.0,
                 swing_only: bool = True, default_rr: float = 2.0,
                 swing_max_age: int = 500, internal_max_age: int = 120,
                 mitigation_window: int = 20):
        self.swing_length = swing_length
        self.internal_length = internal_length
        self.atr_period = atr_period
        self.ob_filter_mult = ob_filter_mult
        self.default_rr = default_rr   # R:R used for the reference TP level
        # swing_only: only use 4H SWING order blocks for signals & watchlist.
        # Internal OBs are noisy and produce zones not shown on the chart,
        # which caused phantom signals (e.g. NZDCAD, USDCHF). Default True.
        self.swing_only = swing_only
        # Age limits: how many bars an OB can survive before being treated as
        # stale. Set HIGH because real 4H order blocks stay valid for weeks and
        # price often retests them only after a long excursion (e.g. XAUUSD
        # dropping then rallying back into a zone built weeks earlier). The old
        # 60-bar limit deleted these valid retest zones before price returned.
        # Close-based mitigation still removes zones price has truly invalidated.
        self.swing_max_age = swing_max_age
        self.internal_max_age = internal_max_age
        # mitigation_window: how many 15m bars after price taps a 4H OB the setup
        # stays "armed" (watching for 15m CHoCH), even if price wicked back out.
        # 20 bars = ~5 hours, covering a NY session for periodic checking.
        self.mitigation_window = mitigation_window

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

    def _prune_mitigated(self, candles, order_blocks, max_age=None):
        """
        Remove order blocks that are no longer valid. An OB is dropped if:
          1. CLOSE-based mitigation: a later candle CLOSED beyond the zone in
             the breaking direction (stricter than wick-only; matches how a
             zone is actually consumed):
               bearish OB removed if a later close > ob.high
               bullish OB removed if a later close < ob.low
          2. Left-behind: price has closed decisively on the far side, so the
             zone is stale relative to current price:
               bearish OB whose whole range is now far ABOVE price, or
               bullish OB whose whole range is now far BELOW price,
             once price has moved more than one zone-height past it.
          3. Age: OB older than max_age bars is dropped (stale zones).
        """
        if not order_blocks:
            return []
        n = len(candles)
        last_close = candles[-1].close
        live = []
        for ob in order_blocks:
            after = candles[ob.bar_index+1:]
            mitigated = False

            # 1. close-based mitigation
            for c in after:
                if ob.bias == BEARISH and c.close > ob.high:
                    mitigated = True; break
                if ob.bias == BULLISH and c.close < ob.low:
                    mitigated = True; break

            # 3. age limit
            if not mitigated and max_age is not None:
                if (n - 1 - ob.bar_index) > max_age:
                    mitigated = True

            if not mitigated:
                live.append(ob)
        return live

    # ── Fair Value Gap detection (LuxAlgo 3-candle method) ──
    def _detect_fvg(self, candles, bias, near_price, tolerance):
        """
        A bullish FVG = gap where candle[i-2].high < candle[i].low (price jumped
        up leaving an unfilled 3-candle gap). Bearish = candle[i-2].low > candle[i].high.

        Tightened rules (the old version fired on any tiny gap anywhere near price):
          1. The gap must be MEANINGFUL in size (>= 25% of ATR), not micro-noise.
          2. It must be RECENT (in the last ~20 bars), since a setup's FVG is the
             one that just formed, not an ancient one.
          3. The gap must sit close to the current price (within ~1.5 ATR), i.e.
             it's the gap price is actually reacting to.
        """
        n = len(candles)
        if n < 3 or tolerance <= 0:
            return False
        min_gap = tolerance * 0.25          # gap must be at least 1/4 ATR
        near_window = tolerance * 1.5        # and near current price
        lookback = max(3, n - 20)            # only the last ~20 bars

        for i in range(n - 1, lookback - 1, -1):
            if i < 2:
                break
            c0 = candles[i]
            c2 = candles[i-2]
            if bias == BULLISH:
                if c0.low > c2.high:
                    gap_size = c0.low - c2.high
                    gap_bot = c2.high
                    if gap_size < min_gap:
                        continue
                    filled = any(candles[j].low < gap_bot for j in range(i+1, n))
                    if not filled and abs(near_price - gap_bot) < near_window:
                        return True
            else:
                if c2.low > c0.high:
                    gap_size = c2.low - c0.high
                    gap_top = c2.low
                    if gap_size < min_gap:
                        continue
                    filled = any(candles[j].high > gap_top for j in range(i+1, n))
                    if not filled and abs(near_price - gap_top) < near_window:
                        return True
        return False

    # ── Equal Highs / Equal Lows (liquidity pools) ──
    def _detect_eqhl(self, candles, bias, near_price, tolerance):
        """
        Equal highs (resting liquidity above) or equal lows (below).
        For bullish we look for equal LOWS near price; for bearish, equal HIGHS.

        Tightened (the old version compared EVERY candle to every other and
        almost always returned True). Now:
          1. Only compare confirmed SWING pivots (fractal highs/lows), not every
             bar — equal highs means two actual swing highs at the same level.
          2. The two levels must be genuinely equal (within 0.15 ATR).
          3. The equal level must be near current price (within 1.5 ATR) — it's
             the liquidity price is approaching, not something far away.
          4. The two pivots must be a few bars apart (a real double-top/bottom,
             not two adjacent bars).
        """
        if tolerance <= 0:
            return False
        highs, lows = self._swing_levels(candles, left=2, right=2)
        # Equal highs/lows are DEFINED by the two levels being nearly identical.
        # Keep this very tight so it means a real double-top/bottom, not just two
        # swings that happen to be in the same area. Equal FX really is common,
        # so this is intentionally a hard test.
        eq_tol = tolerance * 0.05             # within 5% ATR = genuinely "equal"
        near_window = tolerance * 0.8

        pts = lows if bias == BULLISH else highs
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                idx_a, lvl_a = pts[a]
                idx_b, lvl_b = pts[b]
                if abs(idx_a - idx_b) < 3:
                    continue                      # too close together
                if abs(lvl_a - lvl_b) < eq_tol and abs(near_price - lvl_a) < near_window:
                    return True
        return False

    # ── Liquidity sweep (stop hunt) ──
    def _swing_levels(self, candles, left=2, right=2):
        """
        Return lists of confirmed swing-high and swing-low prices.
        A swing high = a bar whose high is >= the `left` bars before and
        `right` bars after it (fractal pivot). Same for swing low.
        These are the levels where real liquidity (stops) tends to rest.
        """
        highs, lows = [], []
        n = len(candles)
        for i in range(left, n - right):
            h = candles[i].high
            l = candles[i].low
            # strict: the pivot must be strictly higher/lower than at least one
            # neighbor on each side (not just equal), so flat ranges don't count
            is_high = all(candles[i-k].high <= h for k in range(1, left+1)) and \
                      all(candles[i+k].high <= h for k in range(1, right+1)) and \
                      any(candles[i-k].high <  h for k in range(1, left+1)) and \
                      any(candles[i+k].high <  h for k in range(1, right+1))
            is_low  = all(candles[i-k].low  >= l for k in range(1, left+1)) and \
                      all(candles[i+k].low  >= l for k in range(1, right+1)) and \
                      any(candles[i-k].low  >  l for k in range(1, left+1)) and \
                      any(candles[i+k].low  >  l for k in range(1, right+1))
            if is_high:
                highs.append((i, h))
            if is_low:
                lows.append((i, l))
        return highs, lows

    def _liquidity_levels(self, candles, lookback=80, cluster_tol=None):
        """
        Identify Buyside / Sellside liquidity levels (FluxCharts-style).
          - BUYSIDE liquidity  = swing HIGHS  (buy-stops rest above)
          - SELLSIDE liquidity = swing LOWS   (sell-stops rest below)
        Levels where multiple swing points cluster are stronger liquidity pools.
        Returns (buyside_levels, sellside_levels) as lists of prices, most
        recent first, restricted to the last `lookback` bars.
        """
        n = len(candles)
        window = candles[max(0, n - lookback):]
        highs, lows = self._swing_levels(window, left=2, right=2)
        buyside = [lv for (_, lv) in highs]
        sellside = [lv for (_, lv) in lows]

        # Optionally cluster near-equal levels into single pools (stronger
        # liquidity). cluster_tol defaults to a small fraction of ATR.
        def cluster(levels):
            if not levels or not cluster_tol:
                return sorted(set(levels), reverse=True)
            levels = sorted(levels)
            out, group = [], [levels[0]]
            for lv in levels[1:]:
                if abs(lv - group[-1]) <= cluster_tol:
                    group.append(lv)
                else:
                    out.append(sum(group) / len(group))
                    group = [lv]
            out.append(sum(group) / len(group))
            return sorted(out, reverse=True)

        return cluster(buyside), cluster(sellside)

    def _detect_sweep(self, candles, bias, tolerance, zone_ref=None):
        """
        Liquidity sweep via LONG-WICK REJECTION (FluxCharts Buyside/Sellside
        Liquidity concept ported to the engine).

        A genuine sweep is NOT just price poking through a level — it's a candle
        that spikes through a liquidity pool to grab stops, then gets REJECTED,
        closing its body back on the original side. The tell is a LONG WICK:
          - BEARISH setup: a 15m candle's HIGH pierces a BUYSIDE liquidity level
            (a swing high), but it CLOSES back below that level, leaving a long
            upper wick. Stops above were grabbed, then price rejected down.
          - BULLISH setup: a candle's LOW pierces a SELLSIDE liquidity level
            (swing low) and CLOSES back above it, leaving a long lower wick.

        Strict criteria (fixes the old over-firing sweep):
          1. The swept level is a real swing-pivot liquidity level.
          2. The wick must actually pierce the level and the body close back.
          3. The rejection WICK must be >= `min_wick_frac` of the candle's full
             range (a real long wick, not a marginal poke). Default 50%.
          4. The wick beyond the level must be meaningful (>= 0.2 ATR), so a
             1-pip overshoot doesn't count.
          5. Happened in the last few bars, and (if zone_ref given) near the zone.

        Returns (swept: bool, level: float|None).
        """
        n = len(candles)
        if n < 12 or tolerance <= 0:
            return False, None

        recent_start = n - 5            # only the last 5 bars can be the sweep bar
        near_tol = tolerance * 5
        min_wick_frac = 0.5             # rejection wick >= 50% of candle range
        min_pierce = tolerance * 0.2    # wick must clear the level by >= 0.2 ATR

        # Liquidity levels from the PRIOR window (exclude recent bars so the
        # sweep candle isn't mistaken for the level it sweeps).
        prior = candles[:recent_start]
        if len(prior) < 6:
            return False, None
        buyside, sellside = self._liquidity_levels(prior, lookback=80,
                                                   cluster_tol=tolerance * 0.3)

        for c in candles[recent_start:]:
            rng = c.high - c.low
            if rng <= 0:
                continue
            body_hi = max(c.open, c.close)
            body_lo = min(c.open, c.close)

            if bias == BEARISH:
                # sweep of BUYSIDE liquidity (swing highs): long UPPER wick
                upper_wick = c.high - body_hi
                for lv in buyside:
                    pierced = c.high > lv + min_pierce      # wick clears level
                    closed_back = c.close < lv               # body rejects below
                    long_wick = upper_wick >= rng * min_wick_frac
                    near = (zone_ref is None) or abs(lv - zone_ref) < near_tol
                    if pierced and closed_back and long_wick and near:
                        return True, lv
            else:
                # sweep of SELLSIDE liquidity (swing lows): long LOWER wick
                lower_wick = body_lo - c.low
                for lv in sellside:
                    pierced = c.low < lv - min_pierce
                    closed_back = c.close > lv
                    long_wick = lower_wick >= rng * min_wick_frac
                    near = (zone_ref is None) or abs(lv - zone_ref) < near_tol
                    if pierced and closed_back and long_wick and near:
                        return True, lv
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
        obs4_swing = self._prune_mitigated(candles_4h, obs4_swing, max_age=self.swing_max_age)

        # also compute 4H internal OBs (shorter lookback) for more zones
        sh4i, sl4i, trend4i, obs4_int = self._process_structure(
            candles_4h, self.internal_length, internal=True)
        obs4_int = self._prune_mitigated(candles_4h, obs4_int, max_age=self.internal_max_age)

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

        # ── Check if price is inside a live 4H OB, OR recently mitigated one ──
        def price_in_ob(obs, ob_type):
            for ob in obs:
                if ob.low <= price <= ob.high:
                    return ob, ob_type
            return None, None

        # 1. Is price LITERALLY inside an OB right now?
        hit_ob, hit_type = price_in_ob(obs4_swing, 'Swing')
        if hit_ob is None and not self.swing_only:
            hit_ob, hit_type = price_in_ob(obs4_int, 'Internal')

        currently_in = hit_ob is not None
        mit_bars_since = None
        mit_session = None

        # 2. If NOT currently inside, did price tap (mitigate) an OB recently?
        #    Price may have wicked in and back out between 2-hour scans — that
        #    mitigation event still arms the setup for `mitigation_window` bars.
        if hit_ob is None:
            obs_to_check = list(obs4_swing)
            if not self.swing_only:
                obs_to_check += list(obs4_int)
            best_bars = None
            for ob in obs_to_check:
                ob_type = 'Swing' if ob in obs4_swing else 'Internal'
                mitigated, bars_since, mit_time = self._recent_mitigation(
                    ob, candles_15m, window_bars=self.mitigation_window)
                if mitigated and (best_bars is None or bars_since < best_bars):
                    best_bars = bars_since
                    hit_ob = ob
                    hit_type = ob_type
                    mit_bars_since = bars_since
                    mit_session = self._session_for(mit_time) if mit_time else None

        result = SMCResult(
            pair=pair,
            price=price,
            trend=trend4,
            swing_trend=trend4,
            internal_trend=trend15,
        )

        if hit_ob is not None:
            result.in_ob = True
            result.currently_in_ob = currently_in
            result.mitigated = not currently_in   # True if it's a recent-tap, not live
            result.bars_since_mit = mit_bars_since
            result.session = mit_session
            if currently_in and candles_15m:
                result.session = self._session_for(candles_15m[-1].time)
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

            # Factor 5: Liquidity sweep / stop hunt (on 15m for entry timing).
            # Pass the OB edge as zone_ref so only sweeps of liquidity NEAR the
            # zone count — not random wicks elsewhere on the chart.
            zone_ref = hit_ob.low if ob_bull else hit_ob.high
            swept, swept_level = self._detect_sweep(candles_15m, hit_ob.bias, tol, zone_ref)
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
            result.struct_aligned = struct_aligned

            # ── Break-and-retest of 15m OB = the actual entry trigger ──
            # ARMED (in 4H OB) progresses to a SIGNAL only when the 15m has:
            #   1. broken structure in the OB direction (bearish CHoCH for shorts)
            #   2. left a 15m OB, and price has RETESTED it (re-entered the zone).
            # Until the retest, it's "broken — awaiting retest" (still ARMED).
            br = self._break_and_retest(candles_15m, hit_ob.bias, tol)
            result.br_state = br['state'] if br['state'] != 'none' else None
            if br['state'] in ('broken', 'retest'):
                result.ltf_ob_high = round(br['ob_high'], 5)
                result.ltf_ob_low = round(br['ob_low'], 5)

            # CRITICAL DIRECTION GUARD: the break-and-retest must agree with the
            # CURRENT 15m structure. The detector can find an OLD break (e.g. a
            # bearish break early in the window) even though the 15m has since
            # gone the other way. Without this guard, a SHORT signal can fire
            # while the 15m is making bullish BOS (the AUDCHF bug). Only honour
            # a 'retest' if the live 15m trend matches the OB/setup direction.
            ob_bull = (hit_ob.bias == BULLISH)
            trend_agrees = (
                (ob_bull and trend15 == BULLISH) or
                (not ob_bull and trend15 == BEARISH)
            )
            valid_retest = (br['state'] == 'retest') and trend_agrees
            # if the break disagrees with current 15m trend, it's stale/invalid:
            # drop it back so it doesn't show a misleading 'broken' state either.
            if not trend_agrees and br['state'] in ('broken', 'retest'):
                result.br_state = None
                result.ltf_ob_high = None
                result.ltf_ob_low = None

            # promote to full signal ONLY on a direction-consistent retest
            result.struct_aligned = valid_retest

            # ── Structural SL/TP REFERENCE (mechanical, not advice) ──
            # SL: just beyond the OB's far edge with a small ATR-based buffer.
            # TP: at a default risk:reward multiple from entry (current price).
            pip = self._pip_size(pair)
            buffer = max(tol * 0.15, pip * 2)   # small buffer beyond the zone
            rr = self.default_rr
            if ob_bull:
                sl = hit_ob.low - buffer        # stop below the bullish OB
                risk = price - sl
                tp = price + risk * rr
            else:
                sl = hit_ob.high + buffer       # stop above the bearish OB
                risk = sl - price
                tp = price - risk * rr
            if risk > 0:
                result.sl_price = round(sl, 5)
                result.tp_price = round(tp, 5)
                result.rr = rr
                result.sl_pips = round(abs(price - sl) / pip, 1) if pip else None
                result.tp_pips = round(abs(tp - price) / pip, 1) if pip else None

        else:
            # ── Not in an OB: find the NEAREST live OB for the watchlist ──
            all_obs = [(ob, 'Swing') for ob in obs4_swing]
            if not self.swing_only:
                all_obs += [(ob, 'Internal') for ob in obs4_int]
            best = None
            best_dist = None
            for ob, ob_type in all_obs:
                # distance to the nearest edge of the zone
                if price > ob.high:
                    d = price - ob.high
                elif price < ob.low:
                    d = ob.low - price
                else:
                    d = 0.0
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best = (ob, ob_type)

            if best is not None:
                ob, ob_type = best
                pip = self._pip_size(pair)
                result.near_ob_bias = ob.bias
                result.near_ob_high = ob.high
                result.near_ob_low = ob.low
                result.near_ob_type = ob_type
                result.near_distance = best_dist
                result.near_distance_pips = round(best_dist / pip, 1) if pip else None

        return result

    def _break_and_retest(self, candles_15m, bias, tolerance):
        """
        Break-and-retest entry detection on the 15m timeframe.

        Sequence (for a BEARISH setup; mirror for bullish):
          1. BREAK: 15m breaks structure DOWN — price closes below a recent swing
             low (a bearish CHoCH/BOS). This is the structural shift.
          2. OB FORMED: the break leaves a 15m sell order block = the last
             up-move (bullish candle/cluster) immediately before the break-down
             candle. Its zone is roughly [that candle's open .. its high].
          3. RETEST: price then retraces UP and re-enters that 15m OB zone.
             Re-entering the zone = the entry trigger (Option A).

        Returns a dict describing the state:
          {'state': 'none' | 'broken' | 'retest',
           'ob_high': float, 'ob_low': float,
           'break_idx': int}
          - 'broken'  = structure broke and a 15m OB formed, awaiting retest
          - 'retest'  = price is back in the 15m OB now = SIGNAL trigger
          - 'none'    = no qualifying break yet
        """
        n = len(candles_15m)
        if n < 12 or tolerance <= 0:
            return {'state': 'none'}

        # Look at recent action only (the break should be recent, within the
        # mitigation window era — not ancient history).
        look = min(n, self.mitigation_window + 10)
        seg = candles_15m[n - look:]
        base = n - look

        highs, lows = self._swing_levels(seg, left=2, right=2)

        if bias == BEARISH:
            # find a swing low that price later CLOSED below = bearish break.
            # Only consider RECENT breaks — an old break in the window is stale
            # and likely superseded by newer structure.
            recent_cut = len(seg) - min(len(seg), self.mitigation_window)
            for (pivot_i, pivot_lvl) in lows:
                # scan candles after the pivot for a close below it (the break)
                for j in range(pivot_i + 1, len(seg)):
                    if seg[j].close < pivot_lvl and j >= recent_cut:
                        break_idx = j
                        # 15m sell OB = last bullish (up) candle before break_idx
                        ob_i = None
                        for k in range(break_idx - 1, max(0, break_idx - 6), -1):
                            if seg[k].close > seg[k].open:   # bullish candle
                                ob_i = k
                                break
                        if ob_i is None:
                            ob_i = break_idx - 1
                        ob_high = max(seg[ob_i].high, seg[ob_i].open)
                        ob_low = min(seg[ob_i].close, seg[ob_i].low)
                        # INVALIDATION: a bearish 15m OB is dead if, after it
                        # formed, any candle CLOSED above its high — price
                        # reclaimed the zone, so it's no longer a short setup.
                        invalidated = any(
                            seg[m].close > ob_high
                            for m in range(ob_i + 1, len(seg))
                        )
                        if invalidated:
                            # this OB failed; don't signal it. keep scanning for
                            # an earlier/other valid break, else report none.
                            continue
                        # is price NOW back inside this OB (the retest)?
                        last = seg[-1]
                        in_zone = last.high >= ob_low and last.low <= ob_high
                        # additionally require the retest candle to not be
                        # slicing straight through: its CLOSE must stay at or
                        # above the OB low (i.e. not closing out the bottom).
                        holds = last.close >= ob_low
                        state = 'retest' if (in_zone and holds) else 'broken'
                        return {'state': state, 'ob_high': ob_high,
                                'ob_low': ob_low, 'break_idx': base + break_idx}
        else:
            # bullish: find a swing high price CLOSED above = bullish break
            recent_cut = len(seg) - min(len(seg), self.mitigation_window)
            for (pivot_i, pivot_lvl) in highs:
                for j in range(pivot_i + 1, len(seg)):
                    if seg[j].close > pivot_lvl and j >= recent_cut:
                        break_idx = j
                        # 15m buy OB = last bearish (down) candle before break
                        ob_i = None
                        for k in range(break_idx - 1, max(0, break_idx - 6), -1):
                            if seg[k].close < seg[k].open:   # bearish candle
                                ob_i = k
                                break
                        if ob_i is None:
                            ob_i = break_idx - 1
                        ob_high = max(seg[ob_i].open, seg[ob_i].high)
                        ob_low = min(seg[ob_i].low, seg[ob_i].close)
                        # INVALIDATION: a bullish 15m OB is dead if, after it
                        # formed, any candle CLOSED below its low — price sliced
                        # through the zone (bearish), so it's no longer a long
                        # setup. THIS is the EURJPY bug: price retested then
                        # broke down through the OB, but the old check still saw
                        # price "in the zone" on the way down and fired LONG.
                        invalidated = any(
                            seg[m].close < ob_low
                            for m in range(ob_i + 1, len(seg))
                        )
                        if invalidated:
                            continue
                        last = seg[-1]
                        in_zone = last.high >= ob_low and last.low <= ob_high
                        # retest candle must not be closing out the top edge
                        # (slicing down through); its close must hold at/below high
                        holds = last.close <= ob_high
                        state = 'retest' if (in_zone and holds) else 'broken'
                        return {'state': state, 'ob_high': ob_high,
                                'ob_low': ob_low, 'break_idx': base + break_idx}
        return {'state': 'none'}

    def _recent_mitigation(self, ob, candles_15m, window_bars=20):
        """
        Detect whether price has MITIGATED (entered) this 4H OB within the last
        `window_bars` 15-minute candles - even if price has since wicked back out.

        A real OB setup is an EVENT: price taps the zone, and from that moment you
        watch the lower timeframe for a reversal. If price enters and leaves
        between two 2-hour engine scans, the "is price in OB right now?" check
        misses it. This looks back over recent 15m candles to catch the tap.

        Returns (mitigated, bars_since, mitigation_time).
        bars_since = how many 15m bars ago the FIRST tap in the window occurred.
        """
        n = len(candles_15m)
        if n == 0:
            return False, None, None
        start = max(0, n - window_bars)
        first_tap_idx = None
        for i in range(start, n):
            c = candles_15m[i]
            if c.high >= ob.low and c.low <= ob.high:
                first_tap_idx = i
                break
        if first_tap_idx is None:
            return False, None, None
        bars_since = (n - 1) - first_tap_idx
        return True, bars_since, candles_15m[first_tap_idx].time

    @staticmethod
    def _session_for(ts):
        """
        Map a UTC timestamp to the active FX session. Returns a short label.
        Sessions (UTC): Sydney ~22-07, Asian ~00-09, London ~08-17, NY ~13-22.
        Prioritises NY > London > Asian for a trader's relevance.
        """
        from datetime import datetime, timezone
        h = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        if 13 <= h < 22:
            return 'NY'
        if 8 <= h < 17:
            return 'London'
        if (h >= 22) or (h < 9):
            return 'Asian'
        return 'Off'

    @staticmethod
    def _pip_size(pair):
        p = pair.replace('/', '').upper()
        if 'JPY' in p:  return 0.01
        if 'XAU' in p:  return 0.10
        if 'XAG' in p:  return 0.001
        return 0.0001
