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

# htf_trend_filter: only arm OBs aligned with the 4H swing trend (bearish trend
# => only short/supply OBs; bullish => only long/demand OBs). OFF by default so
# the engine arms EVERY pair sitting in a 4H OB (both directions); toggle it on
# from the dashboard TREND button to hide counter-trend arms. Set
# HTF_TREND_FILTER=1 to have it on at boot.
HTF_TREND_FILTER = os.environ.get('HTF_TREND_FILTER', '0').strip() not in ('0', 'false', 'False')

# ── Area of Interest (AOI) scanner — a SEPARATE strategy from order blocks ──
# Horizontal S/R zones on Daily & Weekly that price has reacted off repeatedly.
# These zones barely move, so we refresh them slowly (default once a day) and
# cache them — toggling Daily/Weekly and checking "is price in a zone" are free.
AOI_PAIRS = [p.strip() for p in os.environ.get('AOI_PAIRS', '').split(',') if p.strip()]
AOI_REFRESH_HOURS = float(os.environ.get('AOI_REFRESH_HOURS', '24'))
AOI_DAILY_BARS   = int(os.environ.get('AOI_DAILY_BARS', '520'))    # ~2yr of daily bars
AOI_WEEKLY_BARS  = int(os.environ.get('AOI_WEEKLY_BARS', '300'))   # ~5.7yr of weekly bars
AOI_DAILY_LIFE   = int(os.environ.get('AOI_DAILY_LIFE', '504'))    # daily lifespan ~2yr
AOI_WEEKLY_LIFE  = int(os.environ.get('AOI_WEEKLY_LIFE', '260'))   # weekly lifespan ~5yr
AOI_MIN_TOUCHES  = int(os.environ.get('AOI_MIN_TOUCHES', '3'))
AOI_MIN_PIPS     = float(os.environ.get('AOI_MIN_PIPS', '5'))
AOI_MAX_PIPS     = float(os.environ.get('AOI_MAX_PIPS', '60'))
AOI_ENABLED      = os.environ.get('AOI_ENABLED', '1').strip() not in ('0', 'false', 'False')

# ── Daily S/R channels (broader than AOI) — computed from the SAME daily
# candles the AOI pass already fetches, so no extra credits. Uses the engine's
# SRchannel algorithm (the one behind the 'Major S/R' chip), run on Daily.
DAILY_SR_MAX      = int(os.environ.get('DAILY_SR_MAX', '6'))
DAILY_SR_MINSTR   = int(os.environ.get('DAILY_SR_MIN_STRENGTH', '2'))
DAILY_SR_WPCT     = float(os.environ.get('DAILY_SR_WIDTH_PCT', '4'))

engine = SMCEngine(swing_length=SWING_LENGTH, internal_length=5,
                   first_tap_only=FIRST_TAP_ONLY,
                   mitigation_window=MIT_WINDOW,
                   arm_penetration=ARM_PENETRATION,
                   htf_trend_filter=HTF_TREND_FILTER)

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

# Candles from the most recent successful scan, cached so ARM DEPTH / FIRST TAP
# changes can re-classify WITHOUT re-fetching from Twelve Data (0 credits).
_last_scan_candles = {}          # symbol(with slash) -> (c4, c15, sr_channels)
_last_scan_lock = threading.Lock()

# ── AOI (Area of Interest) shared state ──
# Which pairs the AOI scanner covers (defaults to the OB scanner's pairs).
AOI_SCAN_PAIRS = AOI_PAIRS if AOI_PAIRS else list(PAIRS)
aoi_zones = {}          # clean pair -> {'daily':[zone...], 'weekly':[zone...], 'price':float}
aoi_log = {
    'last_refresh': None, 'refreshing': False, 'progress': 0,
    'total': 0, 'current': None, 'errors': [], 'last_error': None,
}
_aoi_lock = threading.Lock()
# latest price per clean pair, updated by the OB scanner each cycle; used by the
# AOI panel to place price against zones live (free — no extra fetch).
last_prices_global = {}


def _nearest_daily_sr(clean, price):
    """Nearest daily support (band below price), resistance (band above), and
    the band price is currently inside, from the cached daily S/R channels.
    Read-only, no API calls — used to annotate signal cards."""
    with _aoi_lock:
        data = aoi_zones.get(clean)
        bands = list(data.get('sr_daily', [])) if data else []
    if not bands or price is None:
        return {}
    pip = engine._pip_size(clean) or 0.0001
    sup = res = inband = None
    for b in bands:
        if b['lo'] <= price <= b['hi']:
            inband = b
        elif b['hi'] < price:
            if sup is None or b['hi'] > sup['hi']:
                sup = b
        elif b['lo'] > price:
            if res is None or b['lo'] < res['lo']:
                res = b
    out = {}
    if inband:
        out['dailyAtLo'] = inband['lo']; out['dailyAtHi'] = inband['hi']
    if sup:
        out['dailySupLo'] = sup['lo']; out['dailySupHi'] = sup['hi']
        out['dailySupPips'] = round((price - sup['hi']) / pip, 1)
    if res:
        out['dailyResLo'] = res['lo']; out['dailyResHi'] = res['hi']
        out['dailyResPips'] = round((res['lo'] - price) / pip, 1)
    return out


def _sr_confluence(entry):
    """
    PRIME-LOCATION check: does the 4H order block overlap a DAILY or WEEKLY
    S/R band, and is price itself in there too?

    An OB is just a zone where orders were left behind. An OB that sits on a
    level the market has respected for months is a far better location than one
    floating in open space. When price is ALSO inside that band, the 4H setup
    and the higher-timeframe level line up — the highest-quality version of
    this setup. That state is tagged 'prime' and surfaced on the card.

    Adds to the entry (in place):
      srDLo/srDHi/srDStrength  daily band the OB overlaps
      srWLo/srWHi/srWStrength  weekly band the OB overlaps
      srPrime                  True when price is inside an overlapped band
    and appends 'Daily S/R' / 'Weekly S/R' factors, bumping the score.
    """
    pair = entry.get('pair')
    lo, hi = entry.get('obLow'), entry.get('obHigh')
    price = entry.get('price')
    if not pair or lo is None or hi is None:
        return
    with _aoi_lock:
        data = aoi_zones.get(pair)
        d_bands = list(data.get('sr_daily', [])) if data else []
        w_bands = list(data.get('sr_weekly', [])) if data else []

    def overlap(bands):
        best = None
        for b in bands:
            if lo <= b['hi'] and hi >= b['lo']:          # zone overlap
                if best is None or b.get('strength', 0) > best.get('strength', 0):
                    best = b
        return best

    d = overlap(d_bands)
    w = overlap(w_bands)
    factors = entry.get('factors') or []
    score = entry.get('confluence') or 0
    prime = False

    if d:
        entry['srDLo'], entry['srDHi'] = d['lo'], d['hi']
        entry['srDStrength'] = d.get('strength')
        if 'Daily S/R' not in factors:
            factors.append('Daily S/R'); score += 1
        if price is not None and d['lo'] <= price <= d['hi']:
            prime = True
    if w:
        entry['srWLo'], entry['srWHi'] = w['lo'], w['hi']
        entry['srWStrength'] = w.get('strength')
        if 'Weekly S/R' not in factors:
            factors.append('Weekly S/R'); score += 1
        if price is not None and w['lo'] <= price <= w['hi']:
            prime = True

    entry['srPrime'] = prime
    entry['factors'] = factors
    entry['confluence'] = score


def _attach_daily_sr(entry):
    """Augment a signal/armed entry with nearest daily S/R levels (free)."""
    try:
        entry.update(_nearest_daily_sr(entry.get('pair'), entry.get('price')))
    except Exception:
        pass


# ── TRADE JOURNAL: log REAL trades + analyse what actually works ──
# The auto-tracker is feed-based and crude. This is your actual fills, with the
# signal context attached, so win-rate stats mean something. Stored as JSON.
# NOTE: Railway's disk is ephemeral across redeploys — use /journal-export to
# back up, /journal-import to restore.
JOURNAL_FILE = os.environ.get('JOURNAL_FILE', '/tmp/smc_journal.json')
journal = []
_journal_lock = threading.Lock()


def _load_journal():
    global journal
    try:
        with open(JOURNAL_FILE) as f:
            data = json.load(f)
        journal = data if isinstance(data, list) else []
    except Exception:
        journal = []


def _save_journal():
    try:
        with open(JOURNAL_FILE, 'w') as f:
            json.dump(journal, f)
    except Exception:
        pass


def _r_multiple(t):
    """Realised R multiple: profit distance / original risk distance."""
    try:
        e, sl, x = float(t['entry']), float(t['sl']), float(t['exit'])
    except (TypeError, ValueError, KeyError):
        return None
    risk = abs(e - sl)
    if risk <= 0:
        return None
    move = (x - e) if t.get('dir') == 'long' else (e - x)
    return round(move / risk, 2)


def _mfe_r(t):
    """Max favorable excursion expressed in R — how far the trade ran in your
    favour, as a multiple of the initial risk. The key diagnostic:
      MFE < 1R  -> the trade never worked; the entry or the stop was the problem
      MFE > 2R but small realised R -> you gave profit back; a trailing/partial
                                       exit problem, not an entry problem."""
    try:
        e, sl = float(t['entry']), float(t['sl'])
        mfe = float(t['mfePips'])
    except (TypeError, ValueError, KeyError):
        return None
    pu = str(t.get('pair', '')).upper()
    pip = 0.01 if 'JPY' in pu else (0.10 if 'XAU' in pu else 0.0001)
    risk_pips = abs(e - sl) / pip
    if risk_pips <= 0 or mfe < 0:
        return None
    return round(mfe / risk_pips, 2)


def _giveback_diagnosis(closed):
    """Split closed trades into the two failure modes, using MFE vs realised R.
    Returns a dict the dashboard renders as a plain-English verdict."""
    never, gave, clean = [], [], []
    for t in closed:
        m, r = t.get('mfeR'), t.get('r')
        if m is None or r is None:
            continue
        if m < 1.0:
            never.append(t)             # never reached +1R -> entry/stop problem
        elif m >= 1.5 and r < m * 0.6:
            gave.append(t)              # ran far, kept little -> exit problem
        else:
            clean.append(t)
    n = len(never) + len(gave) + len(clean)
    if n == 0:
        return {'n': 0}
    left = sum((t['mfeR'] - t['r']) for t in gave) if gave else 0.0
    mfes = [t['mfeR'] for t in closed if t.get('mfeR') is not None]
    return {
        'n': n,
        'neverWorked': len(never),
        'gaveBack': len(gave),
        'cleanCapture': len(clean),
        'rLeftOnTable': round(left, 2),
        'avgMfeR': round(sum(mfes) / len(mfes), 2) if mfes else None,
    }


def _bucket_stats(trades, keyfn):
    """Group trades by keyfn and compute win rate / avg R / total R."""
    groups = {}
    for t in trades:
        k = keyfn(t)
        if k is None or k == '':
            continue
        groups.setdefault(str(k), []).append(t)
    out = []
    for k, ts in groups.items():
        rs = [t['r'] for t in ts if t.get('r') is not None]
        wins = sum(1 for t in ts if (t.get('r') or 0) > 0.05)
        losses = sum(1 for t in ts if (t.get('r') or 0) < -0.05)
        scratch = len(ts) - wins - losses
        out.append({
            'key': k, 'n': len(ts), 'wins': wins, 'losses': losses,
            'scratch': scratch,
            'winRate': round(100 * wins / (wins + losses)) if (wins + losses) else None,
            'avgR': round(sum(rs) / len(rs), 2) if rs else None,
            'totalR': round(sum(rs), 2) if rs else None,
        })
    out.sort(key=lambda g: (g['totalR'] if g['totalR'] is not None else -9e9), reverse=True)
    return out


def _journal_stats():
    with _journal_lock:
        ts = [dict(t) for t in journal]
    closed = []
    for t in ts:
        if t.get('exit') in (None, ''):
            continue
        t['r'] = _r_multiple(t)
        t['mfeR'] = _mfe_r(t)
        closed.append(t)
    if not closed:
        return {'n': 0, 'closed': 0, 'note': 'no closed trades logged yet'}

    rs = [t['r'] for t in closed if t.get('r') is not None]
    wins = [t for t in closed if (t.get('r') or 0) > 0.05]
    losses = [t for t in closed if (t.get('r') or 0) < -0.05]
    scratch = len(closed) - len(wins) - len(losses)
    wr = round(100 * len(wins) / (len(wins) + len(losses))) if (wins or losses) else None

    def stop_bucket(t):
        """How wide was the stop vs what the engine suggested?"""
        try:
            used = abs(float(t['entry']) - float(t['sl']))
            sug = t.get('sugSlPips')
            pip = 0.01 if 'JPY' in str(t.get('pair', '')).upper() else 0.0001
            used_p = used / pip
            if not sug:
                return None
            ratio = used_p / float(sug)
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            return None
        if ratio < 0.5:
            return 'much tighter than suggested (<50%)'
        if ratio < 0.9:
            return 'tighter than suggested (50-90%)'
        if ratio <= 1.15:
            return 'as suggested (~structural)'
        return 'wider than suggested (>115%)'

    def hold_bucket(t):
        try:
            a = datetime.fromisoformat(t['openedAt'].replace('Z', '+00:00'))
            b = datetime.fromisoformat(t['closedAt'].replace('Z', '+00:00'))
        except Exception:
            return None
        h = (b - a).total_seconds() / 3600.0
        if h < 4:
            return '< 4 hours'
        if h < 24:
            return '4-24 hours'
        if h < 72:
            return '1-3 days'
        return '> 3 days'

    def conf_bucket(t):
        c = t.get('confluence')
        if c is None:
            return None
        c = int(c)
        return '5+ confluence' if c >= 5 else ('4 confluence' if c == 4 else '<=3 confluence')

    factor_stats = {}
    for t in closed:
        for f in (t.get('factors') or []):
            factor_stats.setdefault(f, []).append(t)
    by_factor = []
    for f, fts in factor_stats.items():
        frs = [x['r'] for x in fts if x.get('r') is not None]
        w = sum(1 for x in fts if (x.get('r') or 0) > 0.05)
        l = sum(1 for x in fts if (x.get('r') or 0) < -0.05)
        by_factor.append({'key': f, 'n': len(fts), 'wins': w, 'losses': l,
                          'winRate': round(100 * w / (w + l)) if (w + l) else None,
                          'avgR': round(sum(frs) / len(frs), 2) if frs else None,
                          'totalR': round(sum(frs), 2) if frs else None})
    by_factor.sort(key=lambda g: (g['totalR'] if g['totalR'] is not None else -9e9), reverse=True)

    def mfe_bucket(t):
        m = t.get('mfeR')
        if m is None:
            return None
        if m < 0.5:
            return 'never ran (<0.5R) — entry/stop issue'
        if m < 1.0:
            return 'ran 0.5-1R — stop likely too tight'
        if m < 2.0:
            return 'ran 1-2R'
        return 'ran 2R+ — was there to be taken'

    return {
        'n': len(ts), 'closed': len(closed),
        'wins': len(wins), 'losses': len(losses), 'scratch': scratch,
        'winRate': wr,
        'avgR': round(sum(rs) / len(rs), 2) if rs else None,
        'totalR': round(sum(rs), 2) if rs else None,
        'bestR': round(max(rs), 2) if rs else None,
        'worstR': round(min(rs), 2) if rs else None,
        'diagnosis': _giveback_diagnosis(closed),
        'byMfe': _bucket_stats(closed, mfe_bucket),
        'bySession': _bucket_stats(closed, lambda t: t.get('session')),
        'byConfluence': _bucket_stats(closed, conf_bucket),
        'byDirection': _bucket_stats(closed, lambda t: t.get('dir')),
        'byPair': _bucket_stats(closed, lambda t: t.get('pair')),
        'byStopWidth': _bucket_stats(closed, stop_bucket),
        'byHoldTime': _bucket_stats(closed, hold_bucket),
        'byFactor': by_factor,
        'sampleWarning': len(closed) < 30,
    }


_load_journal()

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
    scan_candles = {}   # symbol(with slash) -> (c4, c15, sr_channels) for reanalyze
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

        scan_candles[symbol] = (c4, c15, sr_channels)
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
            'sweepLevel': res.sweep_level,
            'fvgLo':      res.fvg_lo,
            'fvgHi':      res.fvg_hi,
            'eqhlLevel':  res.eqhl_level,
            'eqhlKind':   res.eqhl_kind,
            'dblA':       res.dbl_a,
            'dblB':       res.dbl_b,
            'dblRef':     res.dbl_ref,
            'dblKind':    res.dbl_kind,
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
            'htfTrend':   'bull' if res.swing_trend == BULLISH else ('bear' if res.swing_trend == BEARISH else None),
            'barsSinceMit': res.bars_since_mit,
            'brState':    res.br_state,
            'ltfObHigh':  res.ltf_ob_high,
            'ltfObLow':   res.ltf_ob_low,
            'receivedAt': datetime.now(timezone.utc).isoformat(),
        }

        _attach_daily_sr(entry)
        _sr_confluence(entry)
        if struct_aligned:
            # full confluence signal — price in OB AND 15m confirmed
            new_signals.append(entry)
        else:
            # ARMED: price is in the 4H OB but 15m hasn't flipped to confirm.
            # Surface it so the user can watch for the 15m CHoCH instead of
            # missing the setup entirely.
            entry['m15needed'] = 'bearish CHoCH' if not ob_bull else 'bullish CHoCH'
            new_armed.append(entry)

    # cache this scan's candles so ARM DEPTH / FIRST TAP toggles can
    # re-classify instantly without spending API credits
    with _last_scan_lock:
        _last_scan_candles.clear()
        _last_scan_candles.update(scan_candles)

    # publish latest prices so the AOI panel can place price against its zones
    # live, without any extra API calls
    last_prices_global.update(latest_prices)

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
            # Identity is the SETUP (pair + direction + OB zone), NOT the scan
            # time. Including receivedAt made every scan re-register the same
            # ongoing signal as a brand-new position (the duplicate EURCHF /
            # USDJPY rows). We only add a position if there isn't already an
            # OPEN one for this exact setup; once it closes, the same zone can
            # register again later.
            sig_id = f"{s.get('pair')}|{s.get('bias')}|{s.get('obLow')}"
            exists = any(tr['id'] == sig_id and tr['outcome'] == 'open' for tr in tracked)
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


def reanalyze_from_cache():
    """
    Rebuild signals / armed / watchlist from the LAST scan's cached candles,
    using the engine's CURRENT arm_penetration / first_tap_only settings.
    Makes ZERO Twelve Data calls — safe to run on every filter toggle.
    Returns True if it ran, False if no scan has been cached yet.
    """
    with _last_scan_lock:
        cache = list(_last_scan_candles.items())
    if not cache:
        return False

    new_signals, new_armed, watch = [], [], []

    for symbol, (c4, c15, sr_channels) in cache:
        try:
            res = engine.analyze(symbol, c4, c15, sr_channels=sr_channels)
        except Exception:
            continue
        if res is None:
            continue

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
            'sweepLevel': res.sweep_level,
            'fvgLo':      res.fvg_lo,
            'fvgHi':      res.fvg_hi,
            'eqhlLevel':  res.eqhl_level,
            'eqhlKind':   res.eqhl_kind,
            'dblA':       res.dbl_a,
            'dblB':       res.dbl_b,
            'dblRef':     res.dbl_ref,
            'dblKind':    res.dbl_kind,
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
            'htfTrend':   'bull' if res.swing_trend == BULLISH else ('bear' if res.swing_trend == BEARISH else None),
            'barsSinceMit': res.bars_since_mit,
            'brState':    res.br_state,
            'ltfObHigh':  res.ltf_ob_high,
            'ltfObLow':   res.ltf_ob_low,
            'receivedAt': datetime.now(timezone.utc).isoformat(),
        }
        _attach_daily_sr(entry)
        _sr_confluence(entry)
        if struct_aligned:
            new_signals.append(entry)
        else:
            entry['m15needed'] = 'bearish CHoCH' if not ob_bull else 'bullish CHoCH'
            new_armed.append(entry)

    # re-attach any AI read already computed for these setups this scan, WITHOUT
    # making new API calls (reclassify/filter toggles must stay free).
    if ai_analysis is not None and getattr(ai_analysis, 'AI_ENABLED', False):
        for entry in (new_signals + new_armed):
            try:
                cached = ai_analysis.get_cached(entry)
                if cached.get('ok'):
                    entry['ai'] = {
                        'label': cached.get('label', ''),
                        'note':  cached.get('note', ''),
                        'flags': cached.get('flags', []),
                    }
            except Exception:
                pass

    watch.sort(key=lambda w: w['distancePips'])
    watch_top = watch[:12]

    with _lock:
        signals.clear();   signals.extend(new_signals)
        armed.clear();     armed.extend(new_armed)
        watchlist.clear(); watchlist.extend(watch_top)
    return True


# ── AOI scanner: compute Daily & Weekly S/R zones, refresh slowly ──
def scan_aoi_once():
    """
    Fetch Daily + Weekly candles for each AOI pair and compute Area-of-Interest
    zones (horizontal S/R touched 3+ times, 5-60 pips thick, within lifespan).
    Runs slowly (default once a day) since these zones barely move. Costs 2
    Twelve Data credits per pair per refresh.
    """
    aoi_log['refreshing'] = True
    aoi_log['progress'] = 0
    aoi_log['total'] = len(AOI_SCAN_PAIRS)
    aoi_log['errors'] = []
    THROTTLE = float(os.environ.get('API_THROTTLE_SEC', '8'))

    result = {}
    for idx, symbol in enumerate(AOI_SCAN_PAIRS):
        aoi_log['current'] = symbol
        cd = fetch_candles(symbol, '1day', AOI_DAILY_BARS)
        time.sleep(THROTTLE)
        cw = fetch_candles(symbol, '1week', AOI_WEEKLY_BARS)
        time.sleep(THROTTLE)
        add_credits(2)
        aoi_log['progress'] = idx + 1

        clean = symbol.replace('/', '')
        if not cd and not cw:
            aoi_log['errors'].append(clean)
            continue

        pip = engine._pip_size(symbol)
        entry = {'daily': [], 'weekly': [], 'sr_daily': [], 'sr_weekly': [], 'price': None}
        try:
            if cd:
                entry['daily'] = engine.detect_aoi(
                    cd, pip, min_touches=AOI_MIN_TOUCHES,
                    min_w_pips=AOI_MIN_PIPS, max_w_pips=AOI_MAX_PIPS,
                    lookback_bars=AOI_DAILY_LIFE, pivot_lr=3)
                entry['price'] = round(cd[-1].close, 5)
                # broader daily S/R channels from the same daily candles
                try:
                    entry['sr_daily'] = engine.detect_sr_channels(
                        cd, prd=10, loopback=min(len(cd), 365),
                        channel_w_pct=DAILY_SR_WPCT, min_strength=DAILY_SR_MINSTR,
                        max_sr=DAILY_SR_MAX)
                except Exception:
                    entry['sr_daily'] = []
            if cw:
                entry['weekly'] = engine.detect_aoi(
                    cw, pip, min_touches=AOI_MIN_TOUCHES,
                    min_w_pips=AOI_MIN_PIPS, max_w_pips=AOI_MAX_PIPS,
                    lookback_bars=AOI_WEEKLY_LIFE, pivot_lr=2)
                # WEEKLY S/R channels from the same weekly candles (no extra
                # credits). A 4H OB sitting on a weekly level is the strongest
                # location confluence available.
                try:
                    entry['sr_weekly'] = engine.detect_sr_channels(
                        cw, prd=6, loopback=min(len(cw), 260),
                        channel_w_pct=DAILY_SR_WPCT, min_strength=DAILY_SR_MINSTR,
                        max_sr=DAILY_SR_MAX)
                except Exception:
                    entry['sr_weekly'] = []
        except Exception as e:
            aoi_log['last_error'] = f'{clean} aoi error: {e}'
        result[clean] = entry

    with _aoi_lock:
        aoi_zones.clear()
        aoi_zones.update(result)
    aoi_log['last_refresh'] = datetime.now(timezone.utc).isoformat()
    aoi_log['refreshing'] = False
    aoi_log['current'] = None


def aoi_loop():
    # small initial delay so the first AOI burst doesn't collide with the first
    # OB scan burst on boot
    time.sleep(45)
    while True:
        try:
            scan_aoi_once()
        except Exception as e:
            aoi_log['last_error'] = f'aoi loop error: {e}'
        time.sleep(max(0.25, AOI_REFRESH_HOURS) * 3600)


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


@app.route('/aoi')
def get_aoi():
    """
    ONE row per pair: the single most relevant AOI zone for that pair —
    the one price is inside, else the nearest one.

    v2: the old version returned every zone for every pair (200+ rows, nearly
    all tagged IN ZONE because the bands are wide). That was noise. Now each
    pair contributes at most one zone, anything beyond `max_dist` pips is
    dropped as un-actionable, and the strongest/nearest wins.

    Query params:  tf=daily|weekly, max_dist=<pips> (default 120), all=1
    """
    tf = request.args.get('tf', 'daily')
    if tf not in ('daily', 'weekly'):
        tf = 'daily'
    show_all = request.args.get('all') in ('1', 'true', 'True')
    try:
        max_dist = float(request.args.get('max_dist', 120))
    except ValueError:
        max_dist = 120.0

    rows, hidden = [], 0
    with _aoi_lock:
        items = list(aoi_zones.items())

    for clean, data in items:
        price = last_prices_global.get(clean, data.get('price'))
        pip = engine._pip_size(clean)
        best = None
        for z in data.get(tf, []):
            if price is None:
                dist, status = None, 'unknown'
            elif z['lo'] <= price <= z['hi']:
                dist, status = 0.0, 'in'
            elif price < z['lo']:
                dist, status = round((z['lo'] - price) / pip, 1), 'above'
            else:
                dist, status = round((price - z['hi']) / pip, 1), 'below'
            cand = {
                'pair': clean, 'tf': tf,
                'hi': z['hi'], 'lo': z['lo'], 'mid': z['mid'],
                'touches': z['touches'], 'widthPips': z['width_pips'],
                'ageBars': z['age_bars'], 'price': price,
                'distancePips': dist, 'status': status,
            }
            # keep the closest zone; tie-break on more touches
            if best is None:
                best = cand
            else:
                d0 = best['distancePips'] if best['distancePips'] is not None else 9e9
                d1 = dist if dist is not None else 9e9
                if d1 < d0 or (d1 == d0 and cand['touches'] > best['touches']):
                    best = cand
        if best is None:
            continue
        d = best['distancePips']
        if not show_all and d is not None and d > max_dist:
            hidden += 1
            continue
        rows.append(best)

    # in-zone first, then nearest
    rows.sort(key=lambda a: (a['distancePips'] if a['distancePips'] is not None else 9e9))
    return jsonify({'tf': tf, 'count': len(rows), 'hidden': hidden,
                    'maxDist': max_dist, 'zones': rows, 'log': aoi_log})


@app.route('/aoi-refresh')
def aoi_refresh():
    """Manually trigger an AOI recompute (costs credits: 2 per AOI pair)."""
    if aoi_log.get('refreshing'):
        return jsonify({'status': 'already refreshing'})
    threading.Thread(target=scan_aoi_once, daemon=True).start()
    return jsonify({'status': 'aoi refresh triggered', 'pairs': len(AOI_SCAN_PAIRS)})


@app.route('/aoi-status')
def aoi_status():
    return jsonify({
        'enabled': AOI_ENABLED,
        'pairs': AOI_SCAN_PAIRS,
        'refresh_hours': AOI_REFRESH_HOURS,
        'rules': {'min_touches': AOI_MIN_TOUCHES,
                  'min_pips': AOI_MIN_PIPS, 'max_pips': AOI_MAX_PIPS},
        'log': aoi_log,
    })


@app.route('/journal-parse', methods=['POST'])
def journal_parse():
    """
    Parse a broker-history screenshot into trade fields for review.
    POST JSON: {"image": "<base64>", "media_type": "image/jpeg"}
    Returns {'ok':True,'trades':[...]} — does NOT save anything. The dashboard
    shows these for confirmation, then calls /journal-add.
    """
    if ai_analysis is None or not getattr(ai_analysis, 'AI_ENABLED', False):
        return jsonify({'ok': False,
                        'error': 'AI not enabled — set ANTHROPIC_API_KEY in Railway'}), 400
    body = request.get_json(silent=True) or {}
    b64 = body.get('image')
    if not b64:
        return jsonify({'ok': False, 'error': 'no image supplied'}), 400
    if len(b64) > 8_000_000:
        return jsonify({'ok': False, 'error': 'image too large (max ~6MB)'}), 400

    res = ai_analysis.parse_trade_screenshot(b64, body.get('media_type', 'image/jpeg'))
    if not res.get('ok'):
        return jsonify(res), 200

    # enrich: derive the session from the OPEN time so stats stay consistent
    for t in res['trades']:
        try:
            if t.get('openedAt'):
                dt = datetime.fromisoformat(t['openedAt'].replace(' ', 'T'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                t['session'] = engine._session_for(int(dt.timestamp()))
        except Exception:
            t['session'] = None
    return jsonify(res)


@app.route('/journal')
def journal_list():
    with _journal_lock:
        ts = [dict(t) for t in journal]
    for t in ts:
        t['r'] = _r_multiple(t) if t.get('exit') not in (None, '') else None
    ts.sort(key=lambda t: str(t.get('openedAt') or ''), reverse=True)
    return jsonify({'trades': ts, 'count': len(ts)})


@app.route('/journal-add')
def journal_add():
    """Log a real trade. Query params:
       pair, dir(long|short), entry, sl, tp, exit, openedAt, closedAt,
       session, confluence, factors(comma), sugSlPips, notes, outcome
    """
    a = request.args
    pair = (a.get('pair') or '').strip().upper().replace('/', '')
    if not pair:
        return jsonify({'error': 'pair required'}), 400

    def num(k):
        v = a.get(k)
        try:
            return float(v) if v not in (None, '') else None
        except ValueError:
            return None

    now = datetime.now(timezone.utc).isoformat()
    t = {
        'id': f"{pair}-{int(time.time()*1000)}",
        'pair': pair,
        'dir': 'long' if (a.get('dir') or 'long').lower().startswith('l') else 'short',
        'entry': num('entry'), 'sl': num('sl'), 'tp': num('tp'), 'exit': num('exit'),
        'openedAt': a.get('openedAt') or now,
        'closedAt': a.get('closedAt') or (now if num('exit') is not None else None),
        'session': (a.get('session') or '').strip() or None,
        'confluence': int(a['confluence']) if (a.get('confluence') or '').isdigit() else None,
        'factors': [f.strip() for f in (a.get('factors') or '').split(',') if f.strip()],
        'sugSlPips': num('sugSlPips'),
        'mfePips': num('mfePips'),
        'outcome': (a.get('outcome') or '').strip() or None,
        'notes': (a.get('notes') or '').strip() or None,
        'loggedAt': now,
    }
    with _journal_lock:
        journal.insert(0, t)
        while len(journal) > 1000:
            journal.pop()
        _save_journal()
    return jsonify({'status': 'ok', 'trade': t})


@app.route('/journal-delete')
def journal_delete():
    tid = request.args.get('id')
    if not tid:
        return jsonify({'error': 'id required'}), 400
    with _journal_lock:
        before = len(journal)
        journal[:] = [t for t in journal if t.get('id') != tid]
        _save_journal()
        removed = before - len(journal)
    return jsonify({'status': 'ok', 'removed': removed})


@app.route('/journal-stats')
def journal_stats_route():
    return jsonify(_journal_stats())


@app.route('/journal-export')
def journal_export():
    with _journal_lock:
        return jsonify({'trades': journal, 'count': len(journal)})


@app.route('/journal-import', methods=['GET', 'POST'])
def journal_import():
    """Restore a journal backup. POST a JSON body {"trades":[...]} or pass
    ?data=<urlencoded json>. Replaces the current journal."""
    payload = None
    if request.method == 'POST':
        payload = request.get_json(silent=True)
    if payload is None and request.args.get('data'):
        try:
            payload = json.loads(request.args['data'])
        except Exception:
            return jsonify({'error': 'bad json in data param'}), 400
    if not payload:
        return jsonify({'error': 'no data'}), 400
    trades = payload.get('trades') if isinstance(payload, dict) else payload
    if not isinstance(trades, list):
        return jsonify({'error': 'expected a list of trades'}), 400
    with _journal_lock:
        journal[:] = trades
        _save_journal()
    return jsonify({'status': 'ok', 'count': len(trades)})


@app.route('/daily-sr')
def get_daily_sr():
    """
    ONE row per pair: nearest daily resistance ABOVE price and nearest support
    BELOW price (plus the band price sits inside, if any).

    v2: the old version returned every band for every pair — 150+ rows,
    including supports 800 pips away that no 4H setup will ever reach. A level
    only matters if your trade can actually reach it, so anything beyond
    `max_dist` pips is dropped.

    Query params:  max_dist=<pips> (default 200), all=1
    """
    show_all = request.args.get('all') in ('1', 'true', 'True')
    try:
        max_dist = float(request.args.get('max_dist', 200))
    except ValueError:
        max_dist = 200.0

    rows, hidden = [], 0
    with _aoi_lock:
        items = list(aoi_zones.items())

    for clean, data in items:
        bands = data.get('sr_daily', [])
        if not bands:
            continue
        price = last_prices_global.get(clean, data.get('price'))
        if price is None:
            continue
        pip = engine._pip_size(clean)
        res = sup = inband = None
        for b in bands:
            if b['lo'] <= price <= b['hi']:
                if inband is None or b.get('strength', 0) > inband.get('strength', 0):
                    inband = b
            elif b['lo'] > price:                       # above price -> resistance
                if res is None or b['lo'] < res['lo']:
                    res = b
            else:                                        # below price -> support
                if sup is None or b['hi'] > sup['hi']:
                    sup = b

        resPips = round((res['lo'] - price) / pip, 1) if res else None
        supPips = round((price - sup['hi']) / pip, 1) if sup else None

        # drop levels that are too far to matter for a 4H setup
        if not show_all:
            if resPips is not None and resPips > max_dist:
                res, resPips, hidden = None, None, hidden + 1
            if supPips is not None and supPips > max_dist:
                sup, supPips, hidden = None, None, hidden + 1
        if res is None and sup is None and inband is None:
            continue

        # how much room before the trade hits a wall, in each direction
        rows.append({
            'pair': clean, 'price': price,
            'inLo': inband['lo'] if inband else None,
            'inHi': inband['hi'] if inband else None,
            'resLo': res['lo'] if res else None,
            'resHi': res['hi'] if res else None,
            'resPips': resPips,
            'resStrength': res.get('strength') if res else None,
            'supLo': sup['lo'] if sup else None,
            'supHi': sup['hi'] if sup else None,
            'supPips': supPips,
            'supStrength': sup.get('strength') if sup else None,
        })

    # tightest room first — those are the pairs about to hit something
    def room(r):
        vals = [v for v in (r['resPips'], r['supPips']) if v is not None]
        return min(vals) if vals else 9e9
    rows.sort(key=room)
    return jsonify({'levels': rows, 'count': len(rows), 'hidden': hidden,
                    'maxDist': max_dist, 'log': aoi_log})


@app.route('/settings')
def get_settings():
    """Return the current live arming settings (for the dashboard controls)."""
    return jsonify({
        'arm_penetration': engine.arm_penetration,
        'first_tap_only': engine.first_tap_only,
        'htf_trend_filter': engine.htf_trend_filter,
        'swing_length': engine.swing_length,
    })


@app.route('/set-settings')
def set_settings():
    """
    Update arming settings LIVE without a redeploy. Query params (all optional):
      penetration=0.0..0.9   how deep into the OB before arming
      first_tap=1|0          arm only on first tap, or every tap
    Changes take effect on the NEXT scan. Pass reclassify=1 to instantly
    rebuild armed/signals/watchlist from the LAST scan's cached candles with
    ZERO API credits (this is what the dashboard filter buttons use). Pass
    rescan=1 only if you want a full fresh fetch (costs credits).
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
    tf = request.args.get('trend_filter')
    if tf is not None:
        v = tf.strip() not in ('0', 'false', 'False')
        engine.htf_trend_filter = v
        changed['htf_trend_filter'] = v
    sl = request.args.get('swing_length')
    if sl is not None:
        try:
            v = max(5, min(100, int(sl)))
            engine.swing_length = v
            changed['swing_length'] = v
        except ValueError:
            return jsonify({'error': 'swing_length must be an integer 5..100'}), 400
    # instant reclassify from cached candles — 0 API credits. This is what the
    # ARM DEPTH / FIRST TAP buttons use so toggling never re-fetches data.
    if request.args.get('reclassify') in ('1', 'true', 'True'):
        changed['reclassified'] = reanalyze_from_cache()
    # optional immediate rescan so the user sees the effect without waiting
    if request.args.get('rescan') in ('1', 'true', 'True'):
        threading.Thread(target=scan_once, daemon=True).start()
        changed['rescan'] = True
    return jsonify({'status': 'ok', 'changed': changed,
                    'arm_penetration': engine.arm_penetration,
                    'first_tap_only': engine.first_tap_only,
                    'htf_trend_filter': engine.htf_trend_filter,
                    'swing_length': engine.swing_length})


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
    if AOI_ENABLED:
        t_aoi = threading.Thread(target=aoi_loop, daemon=True)
        t_aoi.start()
else:
    scan_log['last_error'] = 'No TWELVE_DATA_KEY set — add it in Railway Variables'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
