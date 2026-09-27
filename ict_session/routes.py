"""Flask blueprint for the ICT session-liquidity dashboard.

Endpoints (all under /ict):
  GET  /                 dashboard page
  GET  /api/state        recompute from cached bars with the toggles in the query string - ZERO API credits
  POST /api/scan         fetch fresh bars from Twelve Data (1 credit per symbol, budget-guarded)
  GET  /api/version      module version + config

Environment:
  TWELVE_DATA_API_KEY (or TWELVEDATA_API_KEY / TD_API_KEY)
  ICT_SYMBOLS            default "XAU/USD,EUR/USD,GBP/USD"
  ICT_SCAN_MINUTES       default 10  (scan cadence inside the active windows)
  ICT_DAILY_CREDITS      default 250 (hard cap for this module; Twelve Data resets at 00:00 UTC)
  ICT_LOOKBACK_DAYS      default 20  (stats window)
  ICT_CACHE_DIR          default /tmp/ict_cache (bars survive restarts, not redeploys)
  ICT_DEMO=1             synthetic bars, no API calls - for previewing the UI
"""
from __future__ import annotations

import calendar
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import requests
from flask import Blueprint, jsonify, render_template, request

from . import engine
from .engine import Params, STAGES

VERSION = "1.1.0"
NY = ZoneInfo("America/New_York")
BAR = 300

ict_bp = Blueprint("ict", __name__, url_prefix="/ict", template_folder="templates",
                   static_folder="static", static_url_path="/static")

API_KEY = (os.environ.get("TWELVE_DATA_API_KEY") or os.environ.get("TWELVEDATA_API_KEY")
           or os.environ.get("TD_API_KEY") or "")
SYMBOLS = [s.strip() for s in os.environ.get("ICT_SYMBOLS", "XAU/USD,EUR/USD,GBP/USD").split(",") if s.strip()]
SCAN_MIN = int(os.environ.get("ICT_SCAN_MINUTES", "10"))
DAILY_CREDITS = int(os.environ.get("ICT_DAILY_CREDITS", "250"))
LOOKBACK_DAYS = int(os.environ.get("ICT_LOOKBACK_DAYS", "20"))
CACHE_DIR = os.environ.get("ICT_CACHE_DIR", "/tmp/ict_cache")
DEMO = os.environ.get("ICT_DEMO", "") == "1"

_lock = threading.RLock()
_bars: dict[str, dict] = {}          # symbol -> {"t": [...], "o": [...], ...}
_last_scan: dict[str, float] = {}
_credits = {"day": "", "used": 0}
_memo: dict = {}                      # (symbol, last_t, params) -> computed payload


# ---------------------------------------------------------------- time helpers
def ny_wall(dt: datetime | None = None) -> int:
    """Current NY wall-clock as naive epoch seconds (the engine's time base)."""
    dt = (dt or datetime.now(timezone.utc)).astimezone(NY)
    return calendar.timegm(dt.replace(tzinfo=None).timetuple())


def fmt(ts: int | None) -> str | None:
    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


# ---------------------------------------------------------------- cache
def _cache_path(sym):
    return os.path.join(CACHE_DIR, sym.replace("/", "_") + ".json")


def _load_cache():
    os.makedirs(CACHE_DIR, exist_ok=True)
    for sym in SYMBOLS:
        try:
            with open(_cache_path(sym)) as f:
                _bars[sym] = json.load(f)
        except (OSError, ValueError):
            pass
    try:
        with open(os.path.join(CACHE_DIR, "_credits.json")) as f:
            _credits.update(json.load(f))
    except (OSError, ValueError):
        pass


def _save(sym):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(sym), "w") as f:
            json.dump(_bars[sym], f)
        with open(os.path.join(CACHE_DIR, "_credits.json"), "w") as f:
            json.dump(_credits, f)
    except OSError:
        pass


def _merge(sym, new):
    old = _bars.get(sym)
    if not old:
        _bars[sym] = new
    else:
        m = {t: i for i, t in enumerate(old["t"])}
        for i, t in enumerate(new["t"]):
            row = [new[k][i] for k in "tohlc"]
            if t in m:
                j = m[t]
                for k, v in zip("tohlc", row):
                    old[k][j] = v
            else:
                for k, v in zip("tohlc", row):
                    old[k].append(v)
        order = np.argsort(old["t"], kind="stable")
        for k in "tohlc":
            old[k] = [old[k][i] for i in order]
    keep = 45 * 288                                   # ~45 days of M5
    for k in "tohlc":
        _bars[sym][k] = _bars[sym][k][-keep:]


# ---------------------------------------------------------------- Twelve Data
def _credit_ok(n=1):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _credits["day"] != today:
        _credits.update(day=today, used=0)
    return _credits["used"] + n <= DAILY_CREDITS


def _td_fetch(sym, outputsize=5000, end_date=None):
    params = dict(symbol=sym, interval="5min", outputsize=outputsize, timezone="America/New_York",
                  order="ASC", apikey=API_KEY)
    if end_date:
        params["end_date"] = end_date
    r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=20)
    _credits["used"] += 1
    js = r.json()
    if js.get("status") != "ok":
        raise RuntimeError(js.get("message", "Twelve Data error"))
    out = {k: [] for k in "tohlc"}
    for v in js.get("values", []):
        ts = calendar.timegm(datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").timetuple())
        out["t"].append(ts)
        out["o"].append(float(v["open"])); out["h"].append(float(v["high"]))
        out["l"].append(float(v["low"])); out["c"].append(float(v["close"]))
    return out


def scan_symbol(sym, force=False):
    """Fetch new bars for one symbol. Returns (ok, message)."""
    if DEMO:
        return True, "demo"
    if not API_KEY:
        return False, "No Twelve Data API key set"
    with _lock:
        have = _bars.get(sym)
        need_backfill = not have or len(have["t"]) < 6000
        cost = 2 if need_backfill else 1
        if not _credit_ok(cost):
            return False, f"Daily ICT credit budget reached ({DAILY_CREDITS})"
        if not force and time.time() - _last_scan.get(sym, 0) < 60:
            return True, "fresh"
    try:
        if need_backfill:                                  # two pages ~ 35 days (MSS needs M5)
            recent = _td_fetch(sym, 5000)
            end = datetime.utcfromtimestamp(recent["t"][0]).strftime("%Y-%m-%d %H:%M:%S")
            older = _td_fetch(sym, 5000, end_date=end)
            with _lock:
                _merge(sym, older)
                _merge(sym, recent)
        else:
            new = _td_fetch(sym, 60)
            with _lock:
                _merge(sym, new)
        with _lock:
            _last_scan[sym] = time.time()
            _save(sym)
        return True, "ok"
    except Exception as exc:  # network / API message
        return False, str(exc)


# ---------------------------------------------------------------- demo data
def _demo_bars(sym):
    seed = sum(map(ord, sym))
    rng = np.random.default_rng(seed)
    base = {"XAU/USD": 2650.0, "EUR/USD": 1.085, "GBP/USD": 1.29}.get(sym, 100.0)
    vol = base * 0.00045
    end = ny_wall() - ny_wall() % BAR
    t = np.arange(end - 35 * 86400, end, BAR)
    dt = [datetime.utcfromtimestamp(x) for x in t]
    keep = np.array([not (d.weekday() == 5 or (d.weekday() == 4 and d.hour >= 17) or
                          (d.weekday() == 6 and d.hour < 17)) for d in dt])
    t = t[keep]
    hrs = np.array([datetime.utcfromtimestamp(x).hour for x in t])
    sess = np.where((hrs >= 20) | (hrs < 1), 0.45, np.where((hrs >= 2) & (hrs < 5), 1.3,
                    np.where((hrs >= 8) & (hrs < 11), 1.6, 0.8)))
    r = rng.standard_t(4, len(t)) * vol * sess
    c = base + np.cumsum(r)
    o = np.r_[c[0], c[:-1]]
    wick = np.abs(rng.normal(0, vol * 0.6, len(t))) * sess
    h = np.maximum(o, c) + wick
    l = np.minimum(o, c) - np.abs(rng.normal(0, vol * 0.6, len(t))) * sess
    return dict(t=t.tolist(), o=o.tolist(), h=h.tolist(), l=l.tolist(), c=c.tolist())


# ---------------------------------------------------------------- compute
def _digits(sym, price):
    if "JPY" in sym:
        return 3
    return 2 if price > 50 else 5


def _closed(b):
    """Drop the forming bar (Twelve Data returns it)."""
    now = ny_wall()
    n = len(b["t"])
    while n and b["t"][n - 1] + BAR > now:
        n -= 1
    return n


def _refresh_from_disk(sym):
    """Pick up bars saved by the scheduler in another worker process."""
    try:
        mt = os.path.getmtime(_cache_path(sym))
    except OSError:
        return
    if _disk_seen.get(sym, 0) < mt:
        try:
            with open(_cache_path(sym)) as f:
                _bars[sym] = json.load(f)
            _disk_seen[sym] = mt
        except (OSError, ValueError):
            pass


_disk_seen: dict[str, float] = {}


def compute(sym, P: Params, pkey):
    if not DEMO:
        with _lock:
            _refresh_from_disk(sym)
    with _lock:
        b = _bars.get(sym)
        if not b or len(b["t"]) < 500:
            return None
        n = _closed(b)
        key = (sym, b["t"][n - 1], pkey)
        if key in _memo:
            return _memo[key]
        t, o, h, l, c = (np.asarray(b[k][:n]) for k in "tohlc")
        last_price = b["c"][-1]
    cycles, trades = engine.run(t, o, h, l, c, P)
    if not cycles:
        return None
    cutoff = int(t[-1]) - LOOKBACK_DAYS * 86400
    win_trades = [x for x in trades if x["mss_time"] >= cutoff]
    cur = cycles[-1]
    ctr = trades[cur["trade"]] if cur["trade"] is not None else None
    dg = _digits(sym, last_price)

    recent = []
    for cy in [x for x in cycles if x["aFinal"]][-12:]:
        tr = trades[cy["trade"]] if cy["trade"] is not None else None
        recent.append(dict(date=fmt(cy["asiaStart"] + 86400)[:10], note=cy["note"],
                           status=tr["status"] if tr else None, r=round(tr["r"], 2) if tr else None,
                           dir=tr["dir"] if tr else 0))
    # chart bars: from 1h before Asia open to now (current cycle)
    i0 = int(np.searchsorted(t, cur["asiaStart"] - 3600))
    chart = dict(t=t[i0:].tolist(), o=o[i0:].round(dg).tolist(), h=h[i0:].round(dg).tolist(),
                 l=l[i0:].round(dg).tolist(), c=c[i0:].round(dg).tolist())
    history = []
    for x in win_trades:
        cyx = cycles[x["cycle"]]
        history.append(dict(symbol=sym, date=fmt(cyx["asiaStart"] + 86400)[:10], dir=x["dir"],
                            entry=round(x["entry"], dg), sl=round(x["sl"], dg), tp=round(x["tp"], dg),
                            rr=round(x["rr"], 2), status=x["status"], r=round(x["r"], 2),
                            fill=fmt(x["fill_time"]), exit=fmt(x["exit_time"]), zone="FVG" if x["fvg"] else "OB",
                            sweep="Asia high" if cyx["sweep"] > 0 else "Asia low"))
    out = dict(symbol=sym, digits=dg, price=round(last_price, dg), last_bar=fmt(int(t[-1])),
               cycle=dict(asia_start=cur["asiaStart"], asia_end=cur["aEnd"], london_start=cur["lStart"],
                          london_end=cur["lEnd"], ny_start=cur["nStart"], ny_end=cur["nEnd"],
                          flat_at=cur["flatAt"], asia_hi=cur["aHi"], asia_lo=cur["aLo"],
                          asia_final=cur["aFinal"], range=cur["range"], avg=cur["avg"],
                          range_mult=(cur["range"] / cur["avg"]) if cur["avg"] and cur["aFinal"] else None,
                          sweep=cur["sweep"], sweep_time=cur["sweepTime"], ext=cur["ext"],
                          reject_time=cur["rejectTime"], note=cur["note"], fail=cur["fail"],
                          stages=engine.stage_states(cur, trades)),
               trade=ctr, stats=engine.summarize(win_trades), recent=recent, chart=chart, history=history)
    with _lock:
        if len(_memo) > 200:
            _memo.clear()
        _memo[key] = out
    return out


def _state_payload():
    P = Params.from_query(request.args)
    pkey = tuple(sorted(request.args.items()))
    if DEMO:
        with _lock:
            for s in SYMBOLS:
                if s not in _bars:
                    _bars[s] = _demo_bars(s)
    syms = [dict(x) for x in (compute(s, P, pkey) for s in SYMBOLS) if x]   # copies: memo stays intact
    history = sorted((h for s in syms for h in s.pop("history")), key=lambda h: h["date"], reverse=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return dict(version=VERSION, now=ny_wall(), stages=STAGES, symbols=syms, history=history[:60],
                missing=[s for s in SYMBOLS if s not in {x["symbol"] for x in syms}],
                params=dict(entry="ce" if P.entry_ce else "edge", filter=P.max_asia_mult, min_rr=P.min_rr,
                            sweep="ny" if P.sweep_until_ny else "london", ob=P.ob_fallback),
                credits=dict(used=_credits["used"] if _credits["day"] == today else 0, budget=DAILY_CREDITS),
                last_scan={s: _last_scan.get(s) for s in SYMBOLS}, demo=DEMO, lookback_days=LOOKBACK_DAYS,
                sessions=dict(asia="20:00-00:00", london="02:00-05:00", ny="08:00-11:00", tz="New York"))


# ---------------------------------------------------------------- routes
@ict_bp.route("/")
def dashboard():
    return render_template("ict_dashboard.html", version=VERSION)


@ict_bp.route("/api/state")
def api_state():
    return jsonify(_state_payload())


@ict_bp.route("/api/scan", methods=["POST"])
def api_scan():
    sym = request.args.get("symbol")
    msgs = {}
    for s in ([sym] if sym else SYMBOLS):
        ok, msg = scan_symbol(s, force=True)
        msgs[s] = msg
        if not DEMO and not sym:
            time.sleep(1)
    out = _state_payload()
    out["scan"] = msgs
    return jsonify(out)


@ict_bp.route("/api/version")
def api_version():
    return jsonify(version=VERSION, symbols=SYMBOLS, scan_minutes=SCAN_MIN, daily_credits=DAILY_CREDITS,
                   demo=DEMO, has_key=bool(API_KEY))


# ---------------------------------------------------------------- scheduler
def _due(now_ny: datetime, sym: str) -> bool:
    """Scan only while something can change: Asia close, London, NY (+ afternoon if a trade is live)."""
    wd, m = now_ny.weekday(), now_ny.hour * 60 + now_ny.minute
    if wd == 5 or (wd == 6 and m < 20 * 60) or (wd == 4 and m >= 17 * 60):
        return False
    age = (time.time() - _last_scan.get(sym, 0)) / 60
    if 0 <= m < 15 or 2 * 60 <= m < 5 * 60 + 15 or 7 * 60 + 55 <= m < 11 * 60 + 15:
        return age >= SCAN_MIN
    if 11 * 60 + 15 <= m < 16 * 60 + 5:                 # follow an open/pending trade to the flat time
        live = any((x.get("trade") or {}).get("status") in (0, 1)
                   for x in [v for k, v in _memo.items() if k[0] == sym][-1:])
        return live and age >= 30
    return False


def _single_instance_lock():
    """Only one scheduler per server even if gunicorn runs several workers (avoids double credit use)."""
    try:
        import fcntl
        os.makedirs(CACHE_DIR, exist_ok=True)
        fh = open(os.path.join(CACHE_DIR, "_scheduler.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh                       # keep the handle open for the life of the process
    except (OSError, ImportError):
        return None


_sched_started = False


def start_ict_scheduler(app=None):
    global _sched_started
    _load_cache()
    if DEMO or not API_KEY or _sched_started:
        return None
    lock = _single_instance_lock()
    if lock is None:
        return None                     # another worker already scans
    _sched_started = True

    def loop():
        while True:
            try:
                now = datetime.now(timezone.utc).astimezone(NY)
                for s in SYMBOLS:
                    if _due(now, s):
                        scan_symbol(s)
                        time.sleep(8)                     # stay under the 8 req/min free-tier limit
            except Exception as exc:  # keep the thread alive
                print("ICT scheduler:", exc)
            time.sleep(60)

    loop._lock = lock
    th = threading.Thread(target=loop, name="ict-scheduler", daemon=True)
    th.start()
    return th


_load_cache()
