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
watchlist = []        # pairs approaching an OB (not yet inside)
history = []          # rolling history of 2+ confluence signals (max 50 kept)
scan_log = {          # diagnostics shown in dashboard footer
    'last_scan': None,
    'last_error': None,
    'pairs_scanned': 0,
    'credits_used_today': 0,
    'scanning': False,
    'progress': 0,
    'total_pairs': 0,
    'current_pair': None,
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
    watch = []
    scanned = 0

    scan_log['scanning'] = True
    scan_log['progress'] = 0
    scan_log['total_pairs'] = len(PAIRS)

    for idx, symbol in enumerate(PAIRS):
        scan_log['current_pair'] = symbol
        c4 = fetch_candles(symbol, '4h', HTF_BARS)
        time.sleep(1)  # gentle on rate limit
        c15 = fetch_candles(symbol, '15min', LTF_BARS)
        time.sleep(1)
        scan_log['credits_used_today'] += 2
        scan_log['progress'] = idx + 1

        if not c4 or not c15:
            continue
        scanned += 1

        res = engine.analyze(symbol, c4, c15)
        if res is None:
            continue

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
            'fvg':        res.fvg,
            'eqhl':       res.eqhl,
            'sweep':      res.liquidity_sweep,
            'confluence': res.confluence,
            'factors':    res.factors,
            'alert':      ('Bullish' if ob_bull else 'Bearish') + res.ob_type + 'OB',
            'timeframe':  '4H',
            'aligned':    struct_aligned,
            'receivedAt': datetime.now(timezone.utc).isoformat(),
        }
        new_signals.append(sig)

    # sort watchlist by closest first, keep top 12
    watch.sort(key=lambda w: w['distancePips'])
    watch_top = watch[:12]

    with _lock:
        signals.clear()
        signals.extend(new_signals)
        watchlist.clear()
        watchlist.extend(watch_top)

        # ── record 2+ confluence signals into rolling history ──
        for s in new_signals:
            if s['confluence'] >= 2:
                # dedupe: skip if same pair+bias+zone+score already the most
                # recent history entry for that pair
                dup = next((h for h in history
                            if h['pair'] == s['pair']
                            and h['bias'] == s['bias']
                            and h['obLow'] == s['obLow']
                            and h['confluence'] == s['confluence']), None)
                if not dup:
                    history.insert(0, {
                        'pair':       s['pair'],
                        'bias':       s['bias'],
                        'obType':     s['obType'],
                        'confluence': s['confluence'],
                        'factors':    s['factors'],
                        'price':      s['price'],
                        'time':       s['receivedAt'],
                    })
        # keep history bounded
        while len(history) > 50:
            history.pop()

        scan_log['last_scan'] = datetime.now(timezone.utc).isoformat()
        scan_log['pairs_scanned'] = scanned
        scan_log['scanning'] = False
        scan_log['current_pair'] = None


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

@app.route('/watchlist')
def get_watchlist():
    with _lock:
        return jsonify(watchlist)

@app.route('/history')
def get_history():
    with _lock:
        return jsonify(history[:10])   # last 10 for the dashboard table

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

    # bars needed: 15m bars over N days = N*24*4 ; plus warmup for swing_length
    ltf_needed = days * 96 + 220
    htf_needed = days * 6 + 320     # 4H bars over N days + warmup

    events = []
    errors = []

    for symbol in PAIRS:
        c4_all  = fetch_candles(symbol, '4h', min(htf_needed, 5000))
        time.sleep(1)
        c15_all = fetch_candles(symbol, '15min', min(ltf_needed, 5000))
        time.sleep(1)
        if not c4_all or not c15_all:
            errors.append(symbol)
            continue

        # warmup: don't start replaying until we have enough history
        warm15 = engine.internal_length + 5
        start_i = max(warm15, len(c15_all) - days * 96)  # only last N days
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
