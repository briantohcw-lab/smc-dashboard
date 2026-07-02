"""
server.py — Self-contained SMC OB Confluence scanner.

What it does (NO TradingView needed):
  1. Fetches live 4H + 15m candles from Twelve Data (free API)
  2. Runs the SMC engine to detect order blocks & structure
  3. Flags pairs where price is inside a 4H OB AND 15m structure aligns
  4. Serves the dashboard + signals at your Railway URL

Set these as Railway environment variables:
  TWELVE_DATA_KEY  = your free api key from twelvedata.com
  PAIRS            = comma list, e.g. "GBP/JPY,EUR/USD,USD/JPY,XAU/USD"
  SCAN_INTERVAL    = seconds between scans (default 300 = 5 min)
"""

from flask import Flask, jsonify, Response, request
from flask_cors import CORS
from datetime import datetime, timezone
import os, time, threading, urllib.request, urllib.parse, urllib.error, json

from smc_engine import SMCEngine, Candle, BULLISH, BEARISH
try:
    import ai_analysis
except Exception:
    ai_analysis = None




app = Flask(__name__)
CORS(app)

# ── Config from environment ──
API_KEY       = os.environ.get('TWELVE_DATA_KEY', '')
PAIRS         = [p.strip() for p in os.environ.get(
                    'PAIRS', 'GBP/JPY,EUR/USD,USD/JPY,XAU/USD,GBP/USD,AUD/USD'
                ).split(',') if p.strip()]
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', '300'))
HTF_BARS      = int(os.environ.get('HTF_BARS', '300'))   # HTF candles to fetch
LTF_BARS      = int(os.environ.get('LTF_BARS', '120'))   # LTF candles to fetch

# ── Configurable timeframes ──
# Change these env vars to run different HTF/LTF pairings without code edits.
# Valid Twelve Data intervals: 1min,5min,15min,30min,45min,1h,2h,4h,1day,1week
# Common SMC pairings:  HTF=4h LTF=15min (default, swing-intraday)
#                       HTF=1h LTF=5min  (intraday/scalp)
#                       HTF=1day LTF=1h  (pure swing)
HTF_TF = os.environ.get('HTF_TF', '4h').strip()
LTF_TF = os.environ.get('LTF_TF', '15min').strip()

# ── Candle timezone anchoring ──
# This controls where intraday candle boundaries fall. It matters a LOT for 4H
# order blocks: a 4H candle anchored to UTC midnight covers a different window
# than one anchored to the New York close, so OB zones shift.
# TradingView's FX feeds typically anchor to the NY session. Setting this to
# 'America/New_York' makes Twelve Data's 4H boundaries line up closer to FXCM.
# Options: 'UTC', 'America/New_York', or any IANA timezone name.
CANDLE_TZ = os.environ.get('CANDLE_TZ', 'America/New_York').strip()

# first_tap_only: only arm on the FIRST tap of a 4H OB (skip already-mitigated
# zones that price has tapped before). Set env FIRST_TAP_ONLY=0 to disable.
FIRST_TAP_ONLY = os.environ.get('FIRST_TAP_ONLY', '1').strip() not in ('0', 'false', 'False')

# mitigation_window: how many 15m bars an armed setup stays active after price
# taps the 4H OB (waiting for 15m confirmation), even if price wicked out.
# 40 bars = ~10 hours. Raised from 20 because the engine only scans every 2h,
# so a short window gave only 2-3 scans to catch a confirmation. Env: MIT_WINDOW.
MIT_WINDOW = int(os.environ.get('MIT_WINDOW', '40'))

# How deep into a 4H OB price must be before arming (fraction of zone depth).
# Because the free data feed differs from your broker/TradingView feed, arming
# on a mere edge-graze often looks like "not near the OB" on your chart.
# Requiring real penetration keeps armed pairs genuinely near the zone on your
# chart. 0.0 = old edge-touch behaviour; 0.25 = must be 25% into the zone.
# Raise for stricter/fewer arms, lower to keep more. Env: ARM_PENETRATION.
ARM_PENETRATION = float(os.environ.get('ARM_PENETRATION', '0.25'))

# ── Major 4H S/R confluence (LonesomeTheBlue SRchannel logic) ──
# Detected from the 4H candles we ALREADY fetch (no extra API credits, no
# caching needed). Used as a soft 7th confluence factor when a 4H OB OVERLAPS
# a strong S/R channel. Params match the original indicator's defaults.
SR_ENABLED      = os.environ.get('SR_ENABLED', '1').strip() not in ('0', 'false', 'False')
SR_PRD          = int(os.environ.get('SR_PRD', '10'))       # pivot period
SR_LOOPBACK     = int(os.environ.get('SR_LOOPBACK', '290')) # bars to scan
SR_CHANNEL_W    = int(os.environ.get('SR_CHANNEL_W', '5'))  # max channel width %
SR_MIN_STRENGTH = int(os.environ.get('SR_MIN_STRENGTH', '2'))
SR_MAX          = int(os.environ.get('SR_MAX', '6'))        # max channels

# Swing length for 4H OB detection. LuxAlgo's default of 50 is tuned for
# DISPLAYING swing points, but it's too large for OB generation on ~300 bars:
# pivots rarely form, so almost no OBs are detected and nothing ever arms.
# 25 still captures meaningful swings while actually producing OBs. Lower it
# for more (smaller) OBs, raise it for fewer (larger-swing) OBs.
SWING_LENGTH = int(os.environ.get('SWING_LENGTH', '25'))

engine = SMCEngine(swing_length=SWING_LENGTH, internal_length=5,
                   first_tap_only=FIRST_TAP_ONLY,
                   mitigation_window=MIT_WINDOW,
                   arm_penetration=ARM_PENETRATION)

# ── Shared state ──
signals = []          # current live signals shown on dashboard
armed = []            # price in 4H OB but 15m not yet aligned (awaiting confirm)
watchlist = []        # pairs approaching an OB (not yet inside)
history = []          # rolling history of 2+ confluence signals (max 50 kept)
tracked = []          # auto-tracker: signals with SL/TP, outcome checked each scan
tracker_stats = {'tp': 0, 'sl': 0, 'open': 0, 'total': 0}
scan_log = {          # diagnostics shown in dashboard footer
    'last_scan': None,
    'last_error': None,
    'pairs_scanned': 0,
    'credits_used_today': 0,
    'scanning': False,
    'progress': 0,
    'total_pairs': 0,
    'current_pair': None,
    'dropped_pairs': [],
}
_lock = threading.Lock()

# Free Twelve Data tier daily limit
DAILY_CREDIT_LIMIT = int(os.environ.get('DAILY_CREDIT_LIMIT', '800'))

# ── Persistent credit counter (survives restarts, resets each UTC day) ──
# Railway's filesystem is ephemeral across redeploys but stable across
# in-process restarts; we persist to /tmp so a worker restart doesn't lose
# the running daily total. Format: {"date": "YYYY-MM-DD", "credits": N}
CREDIT_FILE = os.environ.get('CREDIT_FILE', '/tmp/smc_credits.json')

def _load_credits():
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with open(CREDIT_FILE) as f:
            data = json.load(f)
        if data.get('date') == today:
            return data.get('credits', 0)
    except Exception:
        pass
    return 0   # new day or no file

def _save_credits(n):
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with open(CREDIT_FILE, 'w') as f:
            json.dump({'date': today, 'credits': n}, f)
    except Exception:
        pass

def add_credits(n):
    """Add to today's credit count, persist, and update scan_log."""
    cur = _load_credits() + n
    _save_credits(cur)
    scan_log['credits_used_today'] = cur
    return cur

# initialise from disk on boot
scan_log['credits_used_today'] = _load_credits()


# ── Fetch candles from Twelve Data ──
def fetch_candles(symbol, interval, outputsize):
    """
    Returns list[Candle] oldest-first, or None on error.
    interval: '4h' or '15min'
    Retries once on HTTP 429 (rate limit) after a 60s wait.
    """
    params = urllib.parse.urlencode({
        'symbol': symbol,
        'interval': interval,
        'outputsize': outputsize,
        'apikey': API_KEY,
        'timezone': CANDLE_TZ,
        'order': 'ASC',
    })
    url = f'https://api.twelvedata.com/time_series?{params}'

    data = None
    max_attempts = 3                       # initial try + 2 retries
    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                # rate limited — wait for the per-minute window to reset
                scan_log['last_error'] = f'{symbol}: rate limited, retry {attempt+1}'
                time.sleep(60)
                continue
            scan_log['last_error'] = f'{symbol} fetch error: {e}'
            return None
        except Exception as e:
            if attempt < max_attempts - 1:
                # transient network/timeout — brief wait then retry
                time.sleep(5)
                continue
            scan_log['last_error'] = f'{symbol} fetch error: {e}'
            return None

    if data is None:
        return None

    # Twelve Data also signals rate limits in the JSON body sometimes
    if data.get('status') == 'error':
        msg = data.get('message', 'api error')
        scan_log['last_error'] = f"{symbol}: {msg}"
        return None

    values = data.get('values')
    if not values:
        return None

    candles = []
    for v in values:
        try:
            candles.append(Candle(
                time=int(datetime.fromisoformat(v['datetime'].replace(' ', 'T')).timestamp()),
                open=float(v['open']),
                high=float(v['high']),
                low=float(v['low']),
                close=float(v['close']),
            ))
        except (KeyError, ValueError):
            continue
    return candles


# ── Scan all pairs once ──
def scan_once():
    new_signals = []
    new_armed = []
    latest_prices = {}    # pair -> latest price, for the auto-tracker
    latest_hl = {}        # pair -> (high, low) of last 15m candle
    watch = []
    scanned = 0

    scan_log['scanning'] = True
    scan_log['progress'] = 0
    scan_log['total_pairs'] = len(PAIRS)
    scan_log['dropped_pairs'] = []

    # Twelve Data FREE tier allows 8 API calls/minute. Each pair = 2 calls
    # (4h + 15min). To stay safely under the limit we space calls ~8s apart:
    # 8 calls/min = 1 call per 7.5s. We use 8s to be safe. This makes a full
    # 28-pair scan take ~7-8 minutes, which is fine for a 2-hour scan cycle.
    THROTTLE = float(os.environ.get('API_THROTTLE_SEC', '8'))

    # XAUUSD (gold) is the least reliable symbol on the free Twelve Data tier
    # and tends to drop out when fetched late in the scan (after rate-limit
    # pressure builds). Fetch priority symbols FIRST so they get the cleanest
    # shot at the API. Configurable via PRIORITY_PAIRS (comma-separated).
    priority = [p.strip() for p in os.environ.get(
        'PRIORITY_PAIRS', 'XAU/USD').split(',') if p.strip()]
    ordered = [p for p in priority if p in PAIRS] + \
              [p for p in PAIRS if p not in priority]

    dropped = []
    candle_cache = {}   # pair(clean) -> (c4, c15) for post-scan AI analysis
    for idx, symbol in enumerate(ordered):
        scan_log['current_pair'] = symbol
        c4 = fetch_candles(symbol, HTF_TF, HTF_BARS)
        time.sleep(THROTTLE)
        c15 = fetch_candles(symbol, LTF_TF, LTF_BARS)
        time.sleep(THROTTLE)
        add_credits(2)
        scan_log['progress'] = idx + 1

        if not c4 or not c15:
            dropped.append(symbol.replace('/', ''))
            scan_log['dropped_pairs'] = dropped
            continue
        scanned += 1
        candle_cache[symbol.replace('/', '')] = (c4, c15)

        # major S/R channels from the 4H candles we already have (no extra API)
        sr_channels = []
        if SR_ENABLED:
            try:
                sr_channels = engine.detect_sr_channels(
                    c4, prd=SR_PRD, loopback=SR_LOOPBACK,
                    channel_w_pct=SR_CHANNEL_W, min_strength=SR_MIN_STRENGTH,
                    max_sr=SR_MAX)
            except Exception:
                sr_channels = []

        res = engine.analyze(symbol, c4, c15, sr_channels=sr_channels)
        if res is None:
            continue

        # capture latest price for the auto-tracker (uses 15m close as "now")
        latest_prices[symbol.replace('/', '')] = round(res.price, 5)
        # also track the high/low of the most recent 15m candle so we can detect
        # whether price WICKED through SL/TP, not just closed through
        if c15:
            last15 = c15[-1]
            latest_hl[symbol.replace('/', '')] = (last15.high, last15.low)

        # If NOT in an OB, add to the watchlist if there's a nearby OB
        if not res.in_ob:
            if res.near_distance_pips is not None:
                watch.append({
                    'pair':     symbol.replace('/', ''),
                    'price':    round(res.price, 5),
                    'bias':     'bull' if res.near_ob_bias == BULLISH else 'bear',
                    'obType':   res.near_ob_type,
                    'obHigh':   round(res.near_ob_high, 5),
                    'obLow':    round(res.near_ob_low, 5),
                    'distancePips': res.near_distance_pips,
                })
            continue

        # Confirm 15m structure aligns with OB bias
        ob_bull = (res.ob_bias == BULLISH)
        struct_aligned = res.struct_aligned

        entry = {
            'pair':       symbol.replace('/', ''),
            'price':      round(res.price, 5),
            'bias':       'bull' if ob_bull else 'bear',
            'obType':     res.ob_type,
            'obHigh':     round(res.ob_high, 5),
            'obLow':      round(res.ob_low, 5),
            'm15struct':  res.last_structure if struct_aligned else None,
            'fvg':        res.fvg,
            'eqhl':       res.eqhl,
            'sweep':      res.liquidity_sweep,
            'nearSr':     res.near_sr,
            'srLevel':    res.sr_level,
            'srHi':       res.sr_hi,
            'srLo':       res.sr_lo,
            'confluence': res.confluence,
            'factors':    res.factors,
            'slPrice':    res.sl_price,
            'tpPrice':    res.tp_price,
            'slPips':     res.sl_pips,
            'tpPips':     res.tp_pips,
            'rr':         res.rr,
            'alert':      ('Bullish' if ob_bull else 'Bearish') + res.ob_type + 'OB',
            'timeframe':  '4H',
            'aligned':    struct_aligned,
            'session':    res.session,
            'mitigated':  res.mitigated,
            'currentlyIn': res.currently_in_ob,
            'barsSinceMit': res.bars_since_mit,
            'brState':    res.br_state,
            'ltfObHigh':  res.ltf_ob_high,
            'ltfObLow':   res.ltf_ob_low,
            'receivedAt': datetime.now(timezone.utc).isoformat(),
        }

        if struct_aligned:
            # full confluence signal — price in OB AND 15m confirmed
            new_signals.append(entry)
        else:
            # ARMED: price is in the 4H OB but 15m hasn't flipped to confirm.
            # Surface it so the user can watch for the 15m CHoCH instead of
            # missing the setup entirely.
            entry['m15needed'] = 'bearish CHoCH' if not ob_bull else 'bullish CHoCH'
            new_armed.append(entry)

    # sort watchlist by closest first, keep top 12
    watch.sort(key=lambda w: w['distancePips'])
    watch_top = watch[:12]

    # ── AI SETUP-QUALITY ANALYSIS (honest risk-flagger) ──
    # Runs only on ARMED setups + signals (the few that matter), not all pairs.
    # Each call is an Anthropic API request; skipped gracefully if no API key.
    # Purpose: describe structure quality + risks to help SKIP weak setups.
    # It does NOT predict, recommend, or score probability of success.
    if ai_analysis is not None and getattr(ai_analysis, 'AI_ENABLED', False):
        for entry in (new_signals + new_armed):
            cc = candle_cache.get(entry.get('pair'))
            if not cc:
                continue
            c4c, c15c = cc
            try:
                result = ai_analysis.analyze_setup(entry, c4c, c15c)
                if result.get('ok'):
                    entry['ai'] = {
                        'label': result.get('label', ''),
                        'note':  result.get('note', ''),
                        'flags': result.get('flags', []),
                    }
            except Exception:
                pass   # never let AI break the scan

    with _lock:
        signals.clear()
        signals.extend(new_signals)
        armed.clear()
        armed.extend(new_armed)
        watchlist.clear()
        watchlist.extend(watch_top)

        # ── record 2+ confluence signals into rolling history ──
        for s in new_signals:
            if s['confluence'] >= 2:
                # dedupe: skip if same pair+bias+zone+score already the most
                # recent history entry for that pair. Use .get() so a missing
                # key never crashes the scan loop.
                dup = next((h for h in history
                            if h.get('pair') == s.get('pair')
                            and h.get('bias') == s.get('bias')
                            and h.get('obLow') == s.get('obLow')
                            and h.get('confluence') == s.get('confluence')), None)
                if not dup:
                    history.insert(0, {
                        'pair':       s.get('pair'),
                        'bias':       s.get('bias'),
                        'obType':     s.get('obType'),
                        'obLow':      s.get('obLow'),
                        'obHigh':     s.get('obHigh'),
                        'confluence': s.get('confluence'),
                        'factors':    s.get('factors'),
                        'price':      s.get('price'),
                        'time':       s.get('receivedAt'),
                    })
        # keep history bounded
        while len(history) > 50:
            history.pop()

        # ── AUTO TRACKER: register new signals with their SL/TP ──
        for s in new_signals:
            sig_id = f"{s.get('pair')}|{s.get('bias')}|{s.get('obLow')}|{s.get('receivedAt')}"
            exists = any(tr['id'] == sig_id for tr in tracked)
            if not exists and s.get('slPrice') and s.get('tpPrice'):
                tracked.insert(0, {
                    'id': sig_id,
                    'pair': s.get('pair'),
                    'bias': s.get('bias'),
                    'entry': s.get('price'),
                    'sl': s.get('slPrice'),
                    'tp': s.get('tpPrice'),
                    'confluence': s.get('confluence'),
                    'factors': s.get('factors'),
                    'openedAt': s.get('receivedAt'),
                    'outcome': 'open',
                    'closedAt': None,
                })
        while len(tracked) > 100:
            tracked.pop()

        # ── AUTO TRACKER: check open positions against latest price ──
        # For a LONG: TP hit if price high >= tp; SL hit if price low <= sl.
        # For a SHORT: TP hit if price low <= tp; SL hit if price high >= sl.
        # If both appear hit in the same candle we can't know order, so we count
        # it conservatively as SL (assume the stop was tagged first).
        for tr in tracked:
            if tr['outcome'] != 'open':
                continue
            hl = latest_hl.get(tr['pair'])
            # store the latest price on the position so the dashboard can show
            # how close it is to TP/SL (best-effort; may be missing if a pair
            # dropped this scan)
            lp = latest_prices.get(tr['pair'])
            if lp is not None:
                tr['current'] = lp
            if not hl:
                continue
            hi, lo = hl
            is_long = (tr['bias'] == 'bull')
            tp, sl = tr['tp'], tr['sl']
            hit_tp = (hi >= tp) if is_long else (lo <= tp)
            hit_sl = (lo <= sl) if is_long else (hi >= sl)
            if hit_sl and hit_tp:
                tr['outcome'] = 'sl'   # conservative: assume stop first
                tr['closedAt'] = datetime.now(timezone.utc).isoformat()
            elif hit_tp:
                tr['outcome'] = 'tp'
                tr['closedAt'] = datetime.now(timezone.utc).isoformat()
            elif hit_sl:
                tr['outcome'] = 'sl'
                tr['closedAt'] = datetime.now(timezone.utc).isoformat()

        # recompute scoreboard
        tp_n = sum(1 for tr in tracked if tr['outcome'] == 'tp')
        sl_n = sum(1 for tr in tracked if tr['outcome'] == 'sl')
        open_n = sum(1 for tr in tracked if tr['outcome'] == 'open')
        tracker_stats['tp'] = tp_n
        tracker_stats['sl'] = sl_n
        tracker_stats['open'] = open_n
        tracker_stats['total'] = len(tracked)
        closed = tp_n + sl_n
        tracker_stats['win_rate'] = round(100 * tp_n / closed) if closed else None

        scan_log['last_scan'] = datetime.now(timezone.utc).isoformat()
        scan_log['pairs_scanned'] = scanned
        scan_log['scanning'] = False
        scan_log['current_pair'] = None


# ── Background scan loop ──
def scan_loop():
    # daily credit reset is handled by the persistent store (resets when the
    # UTC date changes); we just refresh the in-memory value each cycle.
    while True:
        scan_log['credits_used_today'] = _load_credits()
        try:
            scan_once()
        except Exception as e:
            scan_log['last_error'] = f'scan loop error: {e}'
        time.sleep(SCAN_INTERVAL)


# ── Routes ──
_dashboard_cache = {'html': None}

@app.route('/')
def dashboard():
    if _dashboard_cache['html'] is None:
        url = os.environ.get('DASHBOARD_URL', '')
        if url:
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    _dashboard_cache['html'] = r.read().decode()
            except Exception as e:
                return Response(f"Could not load dashboard from DASHBOARD_URL: {e}", mimetype='text/plain')
        else:
            return Response("Set DASHBOARD_URL env var to your raw GitHub dashboard.html link", mimetype='text/plain')
    return Response(_dashboard_cache['html'], mimetype='text/html')

@app.route('/signals')
def get_signals():
    with _lock:
        return jsonify(signals)

@app.route('/watchlist')
def get_watchlist():
    with _lock:
        return jsonify(watchlist)

@app.route('/armed')
def get_armed():
    with _lock:
        return jsonify(armed)

@app.route('/history')
def get_history():
    with _lock:
        return jsonify(history[:10])   # last 10 for the dashboard table

@app.route('/tracker')
def get_tracker():
    """Auto-tracker: outcomes of past signals + win/loss scoreboard."""
    with _lock:
        return jsonify({
            'stats': tracker_stats,
            'positions': tracked[:50],
        })

@app.route('/status')
def status():
    used = _load_credits()
    scan_log['credits_used_today'] = used
    return jsonify({
        'running': True,
        'pairs': PAIRS,
        'scan_interval': SCAN_INTERVAL,
        'log': scan_log,
        'signal_count': len(signals),
        'credits_used': used,
        'credit_limit': DAILY_CREDIT_LIMIT,
        'credits_remaining': max(0, DAILY_CREDIT_LIMIT - used),
        'htf': HTF_TF,
        'ltf': LTF_TF,
        'candle_tz': CANDLE_TZ,
        'tracker': tracker_stats,
    })

@app.route('/scan-now')
def scan_now():
    """Trigger an immediate scan (useful for testing)."""
    threading.Thread(target=scan_once, daemon=True).start()
    return jsonify({'status': 'scan triggered'})


@app.route('/settings')
def get_settings():
    """Return the current live arming settings (for the dashboard controls)."""
    return jsonify({
        'arm_penetration': engine.arm_penetration,
        'first_tap_only': engine.first_tap_only,
    })


@app.route('/set-settings')
def set_settings():
    """
    Update arming settings LIVE without a redeploy. Query params (all optional):
      penetration=0.0..0.9   how deep into the OB before arming
      first_tap=1|0          arm only on first tap, or every tap
    Changes take effect on the NEXT scan. Optionally pass rescan=1 to trigger
    an immediate scan so the change shows right away.
    """
    changed = {}
    pen = request.args.get('penetration')
    if pen is not None:
        try:
            v = max(0.0, min(0.9, float(pen)))
            engine.arm_penetration = v
            changed['arm_penetration'] = v
        except ValueError:
            return jsonify({'error': 'penetration must be a number 0..0.9'}), 400
    ft = request.args.get('first_tap')
    if ft is not None:
        v = ft.strip() not in ('0', 'false', 'False')
        engine.first_tap_only = v
        changed['first_tap_only'] = v
    # optional immediate rescan so the user sees the effect without waiting
    if request.args.get('rescan') in ('1', 'true', 'True'):
        threading.Thread(target=scan_once, daemon=True).start()
        changed['rescan'] = True
    return jsonify({'status': 'ok', 'changed': changed,
                    'arm_penetration': engine.arm_penetration,
                    'first_tap_only': engine.first_tap_only})


@app.route('/backtest')
def backtest():
    """
    Replay the past N days bar-by-bar and list every point where a pair
    reached >= min confluence. Use ?days=7&min=2  (defaults shown).
    Returns JSON; also renders a simple HTML table if &html=1.

    NOTE: this is heavier on the API (one 4H + one 15m fetch per pair, with
    larger outputsize). It does NOT loop the API per bar — it fetches once
    per pair then replays locally, so it stays cheap on credits.
    """
    from flask import request, Response
    days = int(request.args.get('days', 7))
    min_conf = int(request.args.get('min', 2))
    want_html = request.args.get('html', '0') == '1'

    # bars-per-day depends on the timeframe interval
    def bars_per_day(tf):
        tf = tf.lower().strip()
        mins = {'1min':1,'5min':5,'15min':15,'30min':30,'45min':45,
                '1h':60,'2h':120,'4h':240,'1day':1440,'1week':10080}.get(tf, 15)
        return max(1, int(24*60 / mins))

    ltf_pd = bars_per_day(LTF_TF)
    htf_pd = bars_per_day(HTF_TF)
    ltf_needed = days * ltf_pd + 220
    htf_needed = days * htf_pd + 320     # HTF bars over N days + warmup

    events = []
    errors = []

    throttle = float(os.environ.get('API_THROTTLE_SEC', '8'))
    for symbol in PAIRS:
        c4_all  = fetch_candles(symbol, HTF_TF, min(htf_needed, 5000))
        time.sleep(throttle)
        c15_all = fetch_candles(symbol, LTF_TF, min(ltf_needed, 5000))
        time.sleep(throttle)
        add_credits(2)
        if not c4_all or not c15_all:
            errors.append(symbol)
            continue

        # warmup: don't start replaying until we have enough history
        warm15 = engine.internal_length + 5
        start_i = max(warm15, len(c15_all) - days * ltf_pd)  # only last N days
        last_key = None

        for i in range(start_i, len(c15_all)):
            c15_slice = c15_all[:i+1]
            cutoff = c15_slice[-1].time
            # 4H candles available "as of" this 15m bar
            c4_slice = [c for c in c4_all if c.time <= cutoff]
            if len(c4_slice) < engine.swing_length + 5:
                continue

            res = engine.analyze(symbol, c4_slice, c15_slice)
            if res is None or not res.in_ob:
                continue
            if res.confluence < min_conf:
                continue

            # dedupe: one event per (pair, bias, zone, score) state
            key = f"{symbol}|{res.ob_bias}|{round(res.ob_low,5)}|{res.confluence}"
            if key == last_key:
                continue
            last_key = key

            events.append({
                'pair':       symbol.replace('/', ''),
                'time':       datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(),
                'bias':       'bull' if res.ob_bias == BULLISH else 'bear',
                'obType':     res.ob_type,
                'price':      round(res.price, 5),
                'obHigh':     round(res.ob_high, 5),
                'obLow':      round(res.ob_low, 5),
                'confluence': res.confluence,
                'factors':    res.factors,
                'fvg':        res.fvg,
                'eqhl':       res.eqhl,
                'sweep':      res.liquidity_sweep,
            })

    # newest first
    events.sort(key=lambda e: e['time'], reverse=True)

    if not want_html:
        return jsonify({
            'days': days, 'min_confluence': min_conf,
            'pairs': len(PAIRS), 'errors': errors,
            'event_count': len(events), 'events': events,
        })

    # simple HTML table for easy reading on phone
    rows = ''.join(
        f"<tr><td>{e['time'][5:16].replace('T',' ')}</td>"
        f"<td><b>{e['pair']}</b></td>"
        f"<td style='color:{'#00d97e' if e['bias']=='bull' else '#ff4466'}'>"
        f"{'LONG' if e['bias']=='bull' else 'SHORT'}</td>"
        f"<td>{e['obType']}</td>"
        f"<td style='text-align:center'>{e['confluence']}/5</td>"
        f"<td style='font-size:11px;color:#7a8999'>{'+'.join(e['factors'] or [])}</td></tr>"
        for e in events
    )
    html = f"""<!DOCTYPE html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>
    <style>body{{background:#0a0c0f;color:#c8d3e0;font-family:monospace;font-size:13px;padding:12px}}
    h2{{color:#fff}} table{{width:100%;border-collapse:collapse}} 
    td,th{{padding:6px 8px;border-bottom:1px solid #1e2530;text-align:left}}
    th{{color:#7a8999;font-size:10px;letter-spacing:0.1em}}</style></head><body>
    <h2>Backtest — last {days} days, confluence ≥ {min_conf}</h2>
    <p style='color:#7a8999'>{len(events)} events across {len(PAIRS)} pairs.
    {('Errors: '+', '.join(errors)) if errors else ''}</p>
    <table><tr><th>Time UTC</th><th>Pair</th><th>Dir</th><th>OB</th><th>Conf</th><th>Factors</th></tr>
    {rows}</table></body></html>"""
    return Response(html, mimetype='text/html')


# ── Start background scanner when app boots ──
if API_KEY:
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()
else:
    scan_log['last_error'] = 'No TWELVE_DATA_KEY set — add it in Railway Variables'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
