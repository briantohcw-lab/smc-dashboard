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

from flask import Flask, jsonify, Response
from flask_cors import CORS
from datetime import datetime, timezone
import os, time, threading, urllib.request, urllib.parse, json

from smc_engine import SMCEngine, Candle, BULLISH, BEARISH




app = Flask(__name__)
CORS(app)

# ── Config from environment ──
API_KEY       = os.environ.get('TWELVE_DATA_KEY', '')
PAIRS         = [p.strip() for p in os.environ.get(
                    'PAIRS', 'GBP/JPY,EUR/USD,USD/JPY,XAU/USD,GBP/USD,AUD/USD'
                ).split(',') if p.strip()]
SCAN_INTERVAL = int(os.environ.get('SCAN_INTERVAL', '300'))
HTF_BARS      = int(os.environ.get('HTF_BARS', '300'))   # 4H candles to fetch
LTF_BARS      = int(os.environ.get('LTF_BARS', '120'))   # 15m candles to fetch

engine = SMCEngine(swing_length=50, internal_length=5)

# ── Shared state ──
signals = []          # current live signals shown on dashboard
scan_log = {          # diagnostics shown in dashboard footer
    'last_scan': None,
    'last_error': None,
    'pairs_scanned': 0,
    'credits_used_today': 0,
}
_lock = threading.Lock()


# ── Fetch candles from Twelve Data ──
def fetch_candles(symbol, interval, outputsize):
    """
    Returns list[Candle] oldest-first, or None on error.
    interval: '4h' or '15min'
    """
    params = urllib.parse.urlencode({
        'symbol': symbol,
        'interval': interval,
        'outputsize': outputsize,
        'apikey': API_KEY,
        'timezone': 'UTC',
        'order': 'ASC',
    })
    url = f'https://api.twelvedata.com/time_series?{params}'
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        scan_log['last_error'] = f'{symbol} fetch error: {e}'
        return None

    if data.get('status') == 'error':
        scan_log['last_error'] = f"{symbol}: {data.get('message','api error')}"
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
    scanned = 0

    for symbol in PAIRS:
        c4 = fetch_candles(symbol, '4h', HTF_BARS)
        time.sleep(1)  # gentle on rate limit
        c15 = fetch_candles(symbol, '15min', LTF_BARS)
        time.sleep(1)
        scan_log['credits_used_today'] += 2

        if not c4 or not c15:
            continue
        scanned += 1

        res = engine.analyze(symbol, c4, c15)
        if res is None:
            continue

        # Only emit a signal if price is inside a 4H OB
        if not res.in_ob:
            continue

        # Confirm 15m structure aligns with OB bias
        ob_bull = (res.ob_bias == BULLISH)
        struct_aligned = False
        if res.last_structure and res.last_structure_bias != 0:
            struct_aligned = (
                (ob_bull and res.last_structure_bias == BULLISH) or
                (not ob_bull and res.last_structure_bias == BEARISH)
            )

        sig = {
            'pair':       symbol.replace('/', ''),
            'price':      round(res.price, 5),
            'bias':       'bull' if ob_bull else 'bear',
            'obType':     res.ob_type,
            'obHigh':     round(res.ob_high, 5),
            'obLow':      round(res.ob_low, 5),
            'm15struct':  res.last_structure if struct_aligned else None,
            'alert':      ('Bullish' if ob_bull else 'Bearish') + res.ob_type + 'OB',
            'timeframe':  '4H',
            'aligned':    struct_aligned,
            'receivedAt': datetime.now(timezone.utc).isoformat(),
        }
        new_signals.append(sig)

    with _lock:
        signals.clear()
        signals.extend(new_signals)
        scan_log['last_scan'] = datetime.now(timezone.utc).isoformat()
        scan_log['pairs_scanned'] = scanned


# ── Background scan loop ──
def scan_loop():
    # reset daily credit counter at UTC midnight
    last_day = datetime.now(timezone.utc).date()
    while True:
        today = datetime.now(timezone.utc).date()
        if today != last_day:
            scan_log['credits_used_today'] = 0
            last_day = today
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

@app.route('/status')
def status():
    return jsonify({
        'running': True,
        'pairs': PAIRS,
        'scan_interval': SCAN_INTERVAL,
        'log': scan_log,
        'signal_count': len(signals),
    })

@app.route('/scan-now')
def scan_now():
    """Trigger an immediate scan (useful for testing)."""
    threading.Thread(target=scan_once, daemon=True).start()
    return jsonify({'status': 'scan triggered'})


# ── Start background scanner when app boots ──
if API_KEY:
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()
else:
    scan_log['last_error'] = 'No TWELVE_DATA_KEY set — add it in Railway Variables'


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
