        if not rows: return ''
        body = ''.join(
            f"<tr><td>{g['key']}</td><td>{g['n']}</td><td>{g['winRate']}%</td>"
            f"<td>{g['avgR']}R</td><td style='color:{'#3fd68b' if g['totalR']>0 else '#ff5d6c'}'>{g['totalR']}R</td></tr>"
            for g in rows)
        return (f"<h3>{title}</h3><table><tr><th>group</th><th>n</th><th>win%</th>"
                f"<th>avg R</th><th>total R</th></tr>{body}</table>")

    wr = summary['winRate']
    rows = ''.join(
        f"<tr><td>{t['openedAt'][:16].replace('T',' ')}</td><td><b>{t['pair']}</b></td>"
        f"<td style='color:{'#3fd68b' if t['dir']=='long' else '#ff5d6c'}'>{t['dir'].upper()}</td>"
        f"<td>{t.get('grade') or '-'}</td><td>{t['confluence']}</td>"
        f"<td>{t['slPips']}p</td><td>{t['outcome'].upper()}</td>"
        f"<td style='color:{'#3fd68b' if t['r']>0 else '#ff5d6c'}'>{t['r']}R</td>"
        f"<td>{t.get('mfeR')}R</td><td>{t['bars']}</td></tr>" for t in closed[:300])
    html = f"""<!DOCTYPE html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>
    <style>body{{background:#07090d;color:#c8d3e0;font-family:monospace;font-size:13px;padding:14px}}
    h2,h3{{color:#fff}} table{{width:100%;border-collapse:collapse;margin-bottom:18px}}
    td,th{{padding:6px 8px;border-bottom:1px solid #1e2530;text-align:left}}
    th{{color:#7a8999;font-size:10px;letter-spacing:0.1em}}
    .kpi{{display:inline-block;margin-right:22px}} .kpi b{{font-size:20px;display:block;color:#fff}}
    .kpi span{{font-size:9px;color:#5a6877;letter-spacing:0.1em}}</style></head><body>
    <h2>SMC Backtest — {days} days, confluence &ge; {min_conf}{', grade ' + grade_min + '+' if grade_min else ''}</h2>
    <p style='color:#7a8999'>entry mode: <b>{mode}</b> &middot; trailing: {'ON (activate ' + str(act_r) + 'R, trail ' + str(trail_r) + 'R)' if use_trail else 'OFF'}
     &middot; {settings['pairs']} pairs &middot; {HTF_TF}/{LTF_TF}
     {('&middot; no data: ' + ', '.join(errors)) if errors else ''}</p>
    <div>
      <div class=kpi><b style='color:{"#3fd68b" if (wr or 0)>=50 else "#ff5d6c"}'>{wr if wr is not None else '-'}%</b><span>WIN RATE</span></div>
      <div class=kpi><b style='color:{"#3fd68b" if (summary['totalR'] or 0)>0 else "#ff5d6c"}'>{summary['totalR']}R</b><span>TOTAL R</span></div>
      <div class=kpi><b>{summary['avgR']}R</b><span>AVG R</span></div>
      <div class=kpi><b>{summary['profitFactor']}</b><span>PROFIT FACTOR</span></div>
      <div class=kpi><b>{summary['trades']}</b><span>TRADES</span></div>
      <div class=kpi><b>{summary['avgMfeR']}R</b><span>AVG MFE</span></div>
      <div class=kpi><b>{summary['neverReached1R']}</b><span>NEVER HIT 1R</span></div>
    </div>
    <p style='color:#f0b429;font-size:11px'>Simulated on the Twelve Data feed. No spread, commission or slippage —
