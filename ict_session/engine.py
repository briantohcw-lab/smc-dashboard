"""Session-liquidity engine: Asian range -> London sweep + rejection -> NY MSS -> FVG limit.

Bar-for-bar port of ICT_AsiaLondonNY.mq5 (ict_core.mqh v2.10), so the web dashboard,
the MT5 panel and the EA agree on every day.

Timestamps are *wall-clock* seconds (naive local time encoded as if UTC). Feed bars in
the timezone your session times are written in: America/New_York for the defaults below
(the MT5 files use broker time = NY + 7h, same sessions).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np

STATUS_TEXT = {0: "pending", 1: "open", 2: "TP hit", 3: "SL hit", 4: "time exit",
               5: "not filled", 6: "target hit before fill"}
STAGES = ["ASIA", "RANGE", "SWEEP", "REJECT", "MSS", "ENTRY", "EXIT"]


def hm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


@dataclass
class Params:
    asia: tuple = (hm("20:00"), hm("00:00"))     # NY time
    london: tuple = (hm("02:00"), hm("05:00"))
    ny: tuple = (hm("08:00"), hm("11:00"))
    flat: Optional[int] = hm("16:00")
    sweep_until_ny: bool = False
    max_asia_mult: float = 1.5                   # 0 = off
    asia_avg: int = 10
    swing: int = 3
    min_disp_atr: float = 0.0
    fvg_lookback: int = 10
    ob_fallback: bool = False
    entry_ce: bool = True                        # False = gap edge
    atr_period: int = 14
    sl_buf_atr: float = 0.1
    tp_asia: bool = True
    rr: float = 2.0
    min_rr: float = 1.5
    close_at_ny_end: bool = False

    @classmethod
    def from_query(cls, q) -> "Params":
        p = cls()
        if "entry" in q:
            p.entry_ce = q.get("entry", "ce") != "edge"
        if "filter" in q:
            p.max_asia_mult = float(q.get("filter") or 0)
        if "min_rr" in q:
            p.min_rr = float(q.get("min_rr"))
        if "sweep" in q:
            p.sweep_until_ny = q.get("sweep") == "ny"
        if "ob" in q:
            p.ob_fallback = q.get("ob") in ("1", "true", "on")
        if "swing" in q:
            p.swing = max(1, int(q.get("swing")))
        return p


def _dur(s, e):
    return (e - s) % 1440


def _sess_start(ts, s, e):
    m = (ts % 86400) // 60
    d = ts - ts % 86400
    if s < e:
        return d + s * 60 if s <= m < e else 0
    if m >= s:
        return d + s * 60
    if m < e:
        return d - 86400 + s * 60
    return 0


def atr_sma(h, l, c, n):
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:], c[:-1]) - np.minimum(l[1:], c[:-1])
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        cs = np.cumsum(np.r_[0.0, tr])
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def run(t, o, h, l, c, P: Params = None, period_sec: int = 300):
    """Process closed bars [0..n-1]. Returns (cycles, trades) as lists of dicts."""
    P = P or Params()
    t = np.asarray(t, dtype=np.int64)
    o, h, l, c = (np.asarray(x, dtype=float) for x in (o, h, l, c))
    n = len(t)
    atr = atr_sma(h, l, c, P.atr_period)
    As, Ae = P.asia
    Ls, Le = P.london
    Ns, Ne = P.ny
    off = lambda m: ((m - As) % 1440) * 60
    L = P.swing
    sw = dict(H=0.0, L=0.0, Ht=0, Lt=0, Hok=False, Lok=False)
    cycles, trades = [], []
    cur = None

    def avg_range():
        rs = [cy["range"] for cy in reversed(cycles) if cy["aFinal"] and cy["range"] > 0][:P.asia_avg]
        return sum(rs) / len(rs) if len(rs) >= 3 else 0.0

    def exit_(tr, st, tm, px):
        tr.update(status=st, exit_time=int(tm), exit_price=float(px))
        risk = abs(tr["entry"] - tr["sl"])
        tr["r"] = (px - tr["entry"]) * tr["dir"] / risk if risk > 0 else 0.0

    def done(cy, stage, note):
        cy["state"] = 6
        if stage is not None:
            cy["fail"] = stage
        cy["note"] = note

    def force_close(cy):
        k = cy["trade"]
        if k is None:
            return
        tr = trades[k]
        if tr["status"] == 0:
            tr["status"] = 5
            tr["exit_time"] = cy["last_t"]
        elif tr["status"] == 1:
            exit_(tr, 4, cy["last_t"], cy["last_c"])
        if cy["state"] in (4, 5):
            cy["state"] = 6
            cy["note"] = STATUS_TEXT[tr["status"]]

    def check_mss(cy, i):
        d = -cy["sweep"]
        if d < 0:
            if not sw["Lok"] or sw["Lt"] < cy["aEnd"] or c[i] >= sw["L"]:
                return
            lvl, lt = sw["L"], sw["Lt"]
        else:
            if not sw["Hok"] or sw["Ht"] < cy["aEnd"] or c[i] <= sw["H"]:
                return
            lvl, lt = sw["H"], sw["Ht"]
        a = atr[i]
        if not (a > 0):
            return
        if P.min_disp_atr > 0 and abs(c[i] - o[i]) < P.min_disp_atr * a:
            cy["note"] = "MSS candle too weak - waiting"
            return
        found = isfvg = False
        top = bot = 0.0
        zf = 0
        lim = max(cy["extBar"] + 2, i - P.fvg_lookback, 2)
        for j in range(i, lim - 1, -1):
            if d < 0 and h[j] < l[j - 2]:
                zt, zb = l[j - 2], h[j]
                if not any(h[q] >= zt for q in range(j + 1, i + 1)):
                    top, bot, zf, found, isfvg = zt, zb, t[j - 2], True, True
                    break
            if d > 0 and l[j] > h[j - 2]:
                zb, zt = h[j - 2], l[j]
                if not any(l[q] <= zb for q in range(j + 1, i + 1)):
                    top, bot, zf, found, isfvg = zt, zb, t[j - 2], True, True
                    break
        if not found and P.ob_fallback:
            for q in range(i, max(cy["extBar"], 0) - 1, -1):
                if (c[q] > o[q]) if d < 0 else (c[q] < o[q]):
                    top, bot, zf, found = h[q], l[q], t[q], True
                    break
        if not found:
            cy["note"] = "MSS without FVG - waiting for another"
            return
        entry = (top + bot) / 2 if P.entry_ce else (bot if d < 0 else top)
        if (entry - c[i]) * d >= 0:
            return
        sl = cy["ext"] - d * P.sl_buf_atr * a
        risk = abs(entry - sl)
        if risk <= 0 or (sl - entry) * d >= 0:
            return
        tp, tpA = entry + d * P.rr * risk, False
        if P.tp_asia:
            t2 = cy["aLo"] if d < 0 else cy["aHi"]
            if (t2 - entry) * d > 0:
                tp, tpA = t2, True
        rr = (tp - entry) * d / risk
        if rr < P.min_rr:
            cy["note"] = f"MSS found but R:R {rr:.2f} < min - waiting"
            return
        trades.append(dict(dir=d, mss_time=int(t[i]), swing_level=float(lvl), swing_time=int(lt),
                           zone_top=float(top), zone_bot=float(bot), zone_from=int(zf), fvg=isfvg,
                           entry=float(entry), sl=float(sl), tp=float(tp), tp_asia=tpA, rr=float(rr),
                           risk=float(risk), status=0, fill_time=0, exit_time=0, exit_price=0.0, r=0.0,
                           cycle=len(cycles) - 1))
        cy["trade"] = len(trades) - 1
        cy["state"] = 4
        cy["note"] = f"{'BUY' if d > 0 else 'SELL'} limit set in {'FVG' if isfvg else 'OB'}"

    def process(cy, i):
        ti = int(t[i])
        cy["last_t"], cy["last_c"] = ti, float(c[i])
        inA = ti < cy["aEnd"]
        inL = cy["lStart"] <= ti < cy["lEnd"]
        inN = cy["nStart"] <= ti < cy["nEnd"]
        if inA:
            if not cy["aHas"]:
                cy["aHas"], cy["aFrom"] = True, ti
            cy["aHi"] = max(cy["aHi"], h[i])
            cy["aLo"] = min(cy["aLo"], l[i])
            return
        if cy["aHas"] and not cy["aFinal"]:
            cy["aFinal"] = True
            cy["range"] = cy["aHi"] - cy["aLo"]
            m, av = P.max_asia_mult, cy["avg"]
            if m <= 0 or av <= 0 or cy["range"] <= m * av:
                cy["state"], cy["note"] = 1, "Waiting for London sweep"
            else:
                done(cy, 1, f"Asia range too wide ({cy['range'] / av:.1f}x avg) - no trade")
        if not cy["aFinal"]:
            return
        if inL:
            cy["lHas"] = True
            cy["lHi"] = max(cy["lHi"], h[i]); cy["lLo"] = min(cy["lLo"], l[i])
        if inN:
            cy["nHas"] = True
            cy["nHi"] = max(cy["nHi"], h[i]); cy["nLo"] = min(cy["nLo"], l[i])
        d = -cy["sweep"]
        adv = l[i] if d > 0 else h[i]
        fav = h[i] if d > 0 else l[i]
        st = cy["state"]
        if st == 5:
            tr = trades[cy["trade"]]
            up = (cy["flatAt"] and ti >= cy["flatAt"]) or (P.close_at_ny_end and ti >= cy["nEnd"])
            if up:
                exit_(tr, 4, ti, o[i])
            elif (adv - tr["sl"]) * d <= 0:
                exit_(tr, 3, ti, tr["sl"])
            elif (fav - tr["tp"]) * d >= 0:
                exit_(tr, 2, ti, tr["tp"])
            if tr["status"] >= 2:
                cy["state"] = 6
                cy["note"] = f"{STATUS_TEXT[tr['status']]} ({tr['r']:+.2f}R)"
            return
        if st == 4:
            tr = trades[cy["trade"]]
            if ti >= cy["nEnd"]:
                tr["status"], tr["exit_time"] = 5, ti
                done(cy, 5, "Entry not filled before NY close")
            elif (adv - tr["entry"]) * d <= 0:
                tr["status"], tr["fill_time"] = 1, ti
                cy["state"], cy["note"] = 5, "In trade"
                if (adv - tr["sl"]) * d <= 0:
                    exit_(tr, 3, ti, tr["sl"])
                    cy["state"], cy["note"] = 6, "SL hit (-1.00R)"
            elif (fav - tr["tp"]) * d >= 0:
                tr["status"], tr["exit_time"] = 6, ti
                done(cy, 5, "Target reached before entry filled")
            return
        swF = cy["aEnd"] if P.sweep_until_ny else cy["lStart"]
        swT = cy["nStart"] if P.sweep_until_ny else cy["lEnd"]
        if st == 1:
            if swF <= ti < swT:
                hi, lo = h[i] > cy["aHi"], l[i] < cy["aLo"]
                if hi and lo:
                    return done(cy, 2, "Both Asia sides swept in one bar - no bias")
                if hi or lo:
                    cy.update(sweep=1 if hi else -1, sweepTime=ti, ext=float(h[i] if hi else l[i]),
                              extBar=i, state=2, note=f"Asia {'HIGH' if hi else 'LOW'} swept - waiting for rejection")
            elif ti >= swT:
                return done(cy, 2, "No London sweep of the Asian range")
        if cy["state"] in (2, 3):
            if cy["sweep"] > 0 and h[i] > cy["ext"]:
                cy["ext"], cy["extBar"] = float(h[i]), i
            if cy["sweep"] < 0 and l[i] < cy["ext"]:
                cy["ext"], cy["extBar"] = float(l[i]), i
            opp = l[i] < cy["aLo"] if cy["sweep"] > 0 else h[i] > cy["aHi"]
            if opp:
                return done(cy, 3 if cy["state"] == 2 else 4, "Both sides of Asia taken - bias invalid")
        if cy["state"] == 2:
            rej = (c[i] < cy["aHi"]) if cy["sweep"] > 0 else (c[i] > cy["aLo"])
            if rej:
                cy["rejectTime"] = ti
                cy["state"] = 3
                cy["note"] = f"{'SELL' if cy['sweep'] > 0 else 'BUY'} bias - waiting for NY MSS"
            elif ti >= cy["nEnd"]:
                return done(cy, 3, "Sweep never rejected")
        if cy["state"] == 3:
            if ti >= cy["nEnd"]:
                return done(cy, 4, "No MSS during NY window")
            if inN:
                check_mss(cy, i)

    start = P.atr_period
    for i in range(start, n):
        k = i - L
        if k - L >= start:
            if all(h[k] > h[k - dd] and h[k] >= h[k + dd] for dd in range(1, L + 1)):
                sw["H"], sw["Ht"], sw["Hok"] = float(h[k]), int(t[k]), True
            if all(l[k] < l[k - dd] and l[k] <= l[k + dd] for dd in range(1, L + 1)):
                sw["L"], sw["Lt"], sw["Lok"] = float(l[k]), int(t[k]), True
        aS = _sess_start(int(t[i]), As, Ae)
        if aS and (cur is None or cur["asiaStart"] != aS):
            if cur is not None:
                force_close(cur)
            nS = aS + off(Ns)
            nE = nS + _dur(Ns, Ne) * 60
            fl = max(aS + off(P.flat), nE) if P.flat is not None else 0
            lS = aS + off(Ls)
            cur = dict(asiaStart=aS, aEnd=aS + _dur(As, Ae) * 60, lStart=lS, lEnd=lS + _dur(Ls, Le) * 60,
                       nStart=nS, nEnd=nE, flatAt=fl, aHas=False, aFinal=False, aFrom=0,
                       aHi=-math.inf, aLo=math.inf, lHas=False, lHi=-math.inf, lLo=math.inf,
                       nHas=False, nHi=-math.inf, nLo=math.inf, range=0.0, avg=avg_range(),
                       sweep=0, sweepTime=0, rejectTime=0, ext=0.0, extBar=-1, state=0, fail=-1,
                       note="Building Asian range", trade=None, last_t=aS, last_c=0.0)
            cycles.append(cur)
        if cur is not None:
            process(cur, i)
        if sw["Lok"] and c[i] < sw["L"]:
            sw["Lok"] = False
        if sw["Hok"] and c[i] > sw["H"]:
            sw["Hok"] = False
    for cy in cycles:
        for key in ("aHi", "aLo", "lHi", "lLo", "nHi", "nLo"):
            if not math.isfinite(cy[key]):
                cy[key] = None
        cy.pop("extBar", None)
    return cycles, trades


def stage_states(cy, trades):
    """0 idle, 1 active, 2 done, 3 failed, 4 done-neutral (time exit). Mirrors DStage()."""
    st = trades[cy["trade"]]["status"] if cy["trade"] is not None else -1
    f, s = cy["fail"], cy["state"]
    out = [2 if cy["aFinal"] else 1,
           (3 if f == 1 else 2) if cy["aFinal"] else 0,
           2 if cy["sweep"] else (3 if f == 2 else (1 if s == 1 else 0)),
           2 if cy["rejectTime"] else (3 if f == 3 else (1 if s == 2 else 0)),
           2 if cy["trade"] is not None else (3 if f == 4 else (1 if s == 3 else 0))]
    out.append(0 if st < 0 else 1 if st == 0 else 3 if st in (5, 6) else 2)
    out.append({1: 1, 2: 2, 3: 3, 4: 4}.get(st, 0))
    return out


def summarize(trades, spread=0.0):
    """spread in price units, charted as a cost in R on every filled trade."""
    closed = [x for x in trades if 2 <= x["status"] <= 4]
    rs = np.array([x["r"] - (spread / x["risk"] if x["risk"] > 0 else 0) for x in closed])
    out = dict(setups=len(trades), trades=len(closed),
               unfilled=sum(1 for x in trades if x["status"] in (5, 6)))
    if len(rs):
        eq = np.r_[0.0, np.cumsum(rs)]
        gw, gl = rs[rs > 0].sum(), -rs[rs <= 0].sum()
        out.update(win=float(100 * (rs > 0).mean()), net_r=float(rs.sum()), avg_r=float(rs.mean()),
                   pf=float(gw / gl) if gl > 0 else None, max_dd_r=float((np.maximum.accumulate(eq) - eq).max()),
                   equity=[round(float(v), 3) for v in eq[1:]])
    return out
