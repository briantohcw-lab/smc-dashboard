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

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SMC OB Confluence Scanner</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap');

  :root {
    --bg:       #0a0c0f;
    --surface:  #10141a;
    --border:   #1e2530;
    --border2:  #2a3342;
    --bull:     #00d97e;
    --bear:     #ff4466;
    --warn:     #f5a623;
    --dim:      #4a5568;
    --text:     #c8d3e0;
    --text2:    #7a8999;
    --accent:   #3b82f6;
    --hit:      rgba(0,217,126,0.08);
    --hit-bear: rgba(255,68,102,0.08);
    --glow-b:   0 0 12px rgba(0,217,126,0.3);
    --glow-r:   0 0 12px rgba(255,68,102,0.3);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    min-height: 100vh;
  }

  body::before {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
    pointer-events: none; z-index: 999;
  }

  /* ── Header ── */
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky; top: 0; z-index: 50;
  }
  .logo { display: flex; align-items: center; gap: 10px; }
  .logo-icon {
    width: 28px; height: 28px;
    background: linear-gradient(135deg, var(--bull), var(--accent));
    clip-path: polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);
    flex-shrink: 0;
  }
  .logo-text { font-size: 14px; font-weight: 600; letter-spacing: 0.12em; color: #fff; }
  .logo-sub  { font-size: 10px; color: var(--text2); letter-spacing: 0.18em; margin-top: 2px; }

  .header-right { display: flex; align-items: center; gap: 14px; flex-shrink: 0; }

  .conn-badge {
    display: flex; align-items: center; gap: 6px;
    font-size: 10px; letter-spacing: 0.12em;
    padding: 4px 10px; border-radius: 2px; transition: all 0.3s;
  }
  .conn-badge.connected    { color: var(--bull); border: 1px solid rgba(0,217,126,0.3); }
  .conn-badge.disconnected { color: var(--bear); border: 1px solid rgba(255,68,102,0.3); }
  .conn-badge.connecting   { color: var(--warn); border: 1px solid rgba(245,166,35,0.3); }
  .conn-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .conn-badge.connected .conn-dot { animation: pulse 1.4s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.7)} }

  #clock { font-size: 11px; color: var(--text2); letter-spacing: 0.05em; }

  /* ── Session bar ── */
  .session-bar {
    display: flex; padding: 0 24px;
    border-bottom: 1px solid var(--border);
    font-size: 9px; letter-spacing: 0.12em; overflow-x: auto;
  }
  .session {
    padding: 6px 16px 6px 0; margin-right: 16px;
    color: var(--text2); border-right: 1px solid var(--border);
    display: flex; align-items: center; gap: 5px; white-space: nowrap;
  }
  .session:last-child { border: none; }
  .session.active { color: #fff; }
  .session-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--border2); flex-shrink: 0; }
  .session.active .session-dot { background: var(--bull); box-shadow: 0 0 5px var(--bull); }

  /* ── Controls ── */
  .controls {
    display: flex; align-items: center; gap: 10px;
    padding: 10px 24px; border-bottom: 1px solid var(--border);
    background: rgba(16,20,26,0.6); flex-wrap: wrap;
  }
  .ctrl-label { font-size: 10px; color: var(--text2); letter-spacing: 0.12em; white-space: nowrap; }
  .filter-group { display: flex; gap: 4px; }
  .filter-btn {
    padding: 4px 10px; border: 1px solid var(--border2);
    background: transparent; color: var(--text2);
    font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    letter-spacing: 0.08em; cursor: pointer; border-radius: 2px; transition: all 0.15s;
  }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active      { background: var(--accent); border-color: var(--accent); color: #fff; }
  .filter-btn.bull-active { background: var(--bull);   border-color: var(--bull);   color: #000; }
  .filter-btn.bear-active { background: var(--bear);   border-color: var(--bear);   color: #fff; }
  .sep { width: 1px; height: 20px; background: var(--border2); margin: 0 2px; flex-shrink: 0; }

  .right-controls { margin-left: auto; display: flex; align-items: center; gap: 8px; }
  #lastUpdateEl { font-size: 9px; color: var(--dim); white-space: nowrap; }
  .ctrl-btn {
    padding: 5px 12px; border: 1px solid var(--border2);
    background: transparent; color: var(--text2);
    font-family: 'IBM Plex Mono', monospace; font-size: 10px;
    cursor: pointer; border-radius: 2px; transition: all 0.2s; white-space: nowrap;
  }
  .ctrl-btn:hover { border-color: var(--accent); color: var(--text); }
  #clearBtn { border-color: rgba(255,68,102,0.3); color: var(--bear); }
  #clearBtn:hover { background: rgba(255,68,102,0.1); border-color: var(--bear); }

  /* ── Stats bar ── */
  .stats-bar {
    display: flex; padding: 0 24px;
    border-bottom: 1px solid var(--border);
    overflow-x: auto;
  }
  .stat {
    padding: 10px 24px 10px 0; margin-right: 24px;
    border-right: 1px solid var(--border); white-space: nowrap; flex-shrink: 0;
  }
  .stat:last-child { border: none; }
  .stat-val { font-size: 22px; font-weight: 600; line-height: 1; }
  .stat-lbl { font-size: 9px; color: var(--text2); letter-spacing: 0.12em; margin-top: 3px; }
  .stat-val.bull { color: var(--bull); }
  .stat-val.bear { color: var(--bear); }
  .stat-val.warn { color: var(--warn); }
  .stat-val.acc  { color: var(--accent); }

  /* ── Legend ── */
  .legend {
    padding: 7px 24px; display: flex; gap: 16px; flex-wrap: wrap;
    border-bottom: 1px solid var(--border);
    font-size: 9px; color: var(--text2); letter-spacing: 0.08em;
  }
  .legend-item { display: flex; align-items: center; gap: 5px; }
  .legend-dot  { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }

  /* ── Main grid ── */
  main {
    padding: 20px 24px;
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px;
  }
  @media (max-width: 820px) { main { grid-template-columns: 1fr; } }

  .section-label {
    font-size: 10px; letter-spacing: 0.2em; color: var(--text2);
    padding-bottom: 10px; border-bottom: 1px solid var(--border);
    margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
  }
  .section-label::before { content:''; display:inline-block; width:3px; height:12px; }
  .bull-col .section-label::before { background: var(--bull); box-shadow: var(--glow-b); }
  .bear-col .section-label::before { background: var(--bear); box-shadow: var(--glow-r); }
  .count-pill {
    margin-left: auto; background: var(--border);
    color: var(--text2); font-size: 9px; padding: 2px 8px; border-radius: 10px;
  }

  /* ── Signal cards ── */
  .card {
    border: 1px solid var(--border2); border-radius: 3px;
    margin-bottom: 8px; overflow: hidden;
    transition: border-color 0.2s, box-shadow 0.2s, transform 0.15s;
    cursor: default; position: relative;
  }
  .card:hover { transform: translateY(-1px); }
  .card.bull  { border-left: 3px solid var(--bull); background: var(--hit); }
  .card.bull:hover { border-color: var(--bull); box-shadow: var(--glow-b); }
  .card.bear  { border-left: 3px solid var(--bear); background: var(--hit-bear); }
  .card.bear:hover { border-color: var(--bear); box-shadow: var(--glow-r); }
  .card.fresh { animation: slideIn 0.5s ease; }
  @keyframes slideIn { from{opacity:0;transform:translateX(-10px)} to{opacity:1;transform:translateX(0)} }

  .new-badge {
    position: absolute; top: 8px; right: 8px;
    font-size: 8px; padding: 2px 6px; letter-spacing: 0.1em;
    background: rgba(245,166,35,0.2); color: var(--warn);
    border: 1px solid rgba(245,166,35,0.4); border-radius: 1px;
    animation: blink 0.8s step-end 8;
  }
  @keyframes blink { 50%{opacity:0} }

  .card-top {
    display: flex; align-items: center; gap: 8px; padding: 10px 12px 6px;
  }
  .pair-name { font-size: 16px; font-weight: 600; color: #fff; letter-spacing: 0.04em; }
  .dir-tag {
    font-size: 9px; font-weight: 600; letter-spacing: 0.12em;
    padding: 2px 8px; border-radius: 1px;
  }
  .bull .dir-tag { background: rgba(0,217,126,0.18); color: var(--bull); border: 1px solid rgba(0,217,126,0.3); }
  .bear .dir-tag { background: rgba(255,68,102,0.18); color: var(--bear); border: 1px solid rgba(255,68,102,0.3); }
  .ob-tag {
    font-size: 9px; color: var(--warn); letter-spacing: 0.1em;
    border: 1px solid rgba(245,166,35,0.3); padding: 2px 7px;
  }
  .tf-tag {
    font-size: 9px; color: var(--accent); letter-spacing: 0.1em;
    border: 1px solid rgba(59,130,246,0.3); padding: 2px 7px;
  }

  .conf-track { height: 2px; background: var(--border); margin: 0 12px 8px; border-radius: 1px; overflow:hidden; }
  .conf-fill  { height: 100%; border-radius: 1px; transition: width 0.5s ease; }
  .bull .conf-fill { background: linear-gradient(90deg, var(--bull), #00ffb3); }
  .bear .conf-fill { background: linear-gradient(90deg, var(--bear), #ff8fa3); }

  .card-grid {
    padding: 0 12px 10px;
    display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px;
  }
  .m-lbl { font-size: 9px; color: var(--text2); letter-spacing: 0.08em; margin-bottom: 3px; }
  .m-val { font-size: 12px; font-weight: 500; line-height: 1.3; }

  .dep-badge { font-size: 9px; padding: 2px 6px; border-radius: 1px; display: inline-block; }
  .dep-shallow { background:rgba(245,166,35,0.15); color:var(--warn);   border:1px solid rgba(245,166,35,0.3); }
  .dep-mid     { background:rgba(59,130,246,0.15);  color:var(--accent); border:1px solid rgba(59,130,246,0.3); }
  .dep-deep    { background:rgba(0,217,126,0.15);   color:var(--bull);   border:1px solid rgba(0,217,126,0.3); }
  .bear .dep-deep { background:rgba(255,68,102,0.15); color:var(--bear); border-color:rgba(255,68,102,0.3); }

  .card-foot {
    padding: 6px 12px; border-top: 1px solid var(--border);
    display: flex; align-items: center;
    font-size: 9px; color: var(--text2);
  }
  .dots { display: flex; gap: 3px; margin-left: auto; }
  .dot  { width: 7px; height: 7px; border-radius: 50%; background: var(--border2); }
  .dot.on.bull { background: var(--bull); box-shadow: 0 0 4px var(--bull); }
  .dot.on.bear { background: var(--bear); box-shadow: 0 0 4px var(--bear); }

  /* ── Empty / waiting ── */
  .empty-col {
    padding: 28px 16px; text-align: center;
    color: var(--dim); font-size: 10px; letter-spacing: 0.15em;
    border: 1px dashed var(--border); border-radius: 3px;
  }
  .pulse-text { margin-top: 8px; font-size: 9px; animation: fade 1.5s ease-in-out infinite; }
  @keyframes fade { 0%,100%{opacity:0.3} 50%{opacity:1} }

  /* ── Tooltip ── */
  #tip {
    position: fixed; background: #0d1117;
    border: 1px solid var(--border2); padding: 14px 16px;
    font-size: 11px; max-width: 270px; z-index: 200;
    pointer-events: none; opacity: 0; transition: opacity 0.15s;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6); border-radius: 3px;
  }
  #tip.on { opacity: 1; }
  .tr { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 6px; }
  .tl { color: var(--text2); }
  .tv { color: var(--text); font-weight: 500; text-align: right; }
  .tdiv { border-top: 1px solid var(--border); margin: 8px 0; }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 20px; right: 20px;
    background: var(--surface); border: 1px solid var(--border2);
    padding: 10px 16px; font-size: 11px; border-radius: 3px;
    transform: translateY(70px); transition: transform 0.3s ease;
    z-index: 300; box-shadow: 0 4px 20px rgba(0,0,0,0.5);
    max-width: 300px;
  }
  #toast.on { transform: translateY(0); }

  /* ── Mobile tweaks ── */
  @media (max-width: 600px) {
    header { padding: 10px 14px; }
    .logo-text { font-size: 12px; }
    .logo-sub  { display: none; }
    main { padding: 12px 14px; gap: 12px; }
    .controls { padding: 8px 14px; }
    .stats-bar { padding: 0 14px; }
    .session-bar { padding: 0 14px; }
    .legend { padding: 6px 14px; }
  }

  /* scrollbar */
  ::-webkit-scrollbar { width: 3px; height: 3px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 2px; }
</style>
</head>
<body>

<!-- ── HEADER ─────────────────────────────────────── -->
<header>
  <div class="logo">
    <div class="logo-icon"></div>
    <div>
      <div class="logo-text">SMC OB CONFLUENCE</div>
      <div class="logo-sub">4H ORDER BLOCK × 15M ALIGNMENT — AUTO ENGINE</div>
    </div>
  </div>
  <div class="header-right">
    <div class="conn-badge connected" id="connBadge">
      <div class="conn-dot"></div>
      <span id="connText">LIVE</span>
    </div>
    <div id="clock">--:--:-- UTC</div>
  </div>
</header>

<!-- ── SESSIONS ───────────────────────────────────── -->
<div class="session-bar">
  <div class="session" id="ss-sydney"> <div class="session-dot"></div>SYDNEY</div>
  <div class="session" id="ss-tokyo">  <div class="session-dot"></div>TOKYO</div>
  <div class="session" id="ss-london"> <div class="session-dot"></div>LONDON</div>
  <div class="session" id="ss-newyork"><div class="session-dot"></div>NEW YORK</div>
  <div class="session" style="margin-left:auto;border:none" id="pollStatus">SCANNING...</div>
</div>

<!-- ── CONTROLS ───────────────────────────────────── -->
<div class="controls">
  <span class="ctrl-label">DIRECTION</span>
  <div class="filter-group">
    <button class="filter-btn active"  id="f-all"  onclick="setDir('all')">ALL</button>
    <button class="filter-btn"         id="f-bull" onclick="setDir('bull')">BULLISH</button>
    <button class="filter-btn"         id="f-bear" onclick="setDir('bear')">BEARISH</button>
  </div>
  <div class="sep"></div>
  <span class="ctrl-label">OB TYPE</span>
  <div class="filter-group">
    <button class="filter-btn active" id="ob-both"     onclick="setOB('both')">BOTH</button>
    <button class="filter-btn"        id="ob-swing"    onclick="setOB('swing')">SWING</button>
    <button class="filter-btn"        id="ob-internal" onclick="setOB('internal')">INTERNAL</button>
  </div>
  <div class="sep"></div>
  <span class="ctrl-label">CONFLUENCE</span>
  <div class="filter-group">
    <button class="filter-btn active" id="cf-0" onclick="setConf(0)">ANY</button>
    <button class="filter-btn"        id="cf-2" onclick="setConf(2)">2+</button>
    <button class="filter-btn"        id="cf-3" onclick="setConf(3)">3+</button>
  </div>
  <div class="right-controls">
    <span id="lastUpdateEl">—</span>
    <button class="ctrl-btn" onclick="doFetch()">⟳ REFRESH</button>
    <button class="ctrl-btn" id="scanBtn" onclick="doScanNow()">⚡ SCAN NOW</button>
  </div>
</div>

<!-- ── STATS ──────────────────────────────────────── -->
<div class="stats-bar">
  <div class="stat"><div class="stat-val"     id="s-tot">0</div><div class="stat-lbl">TOTAL SIGNALS</div></div>
  <div class="stat"><div class="stat-val bull" id="s-bul">0</div><div class="stat-lbl">BULLISH HITS</div></div>
  <div class="stat"><div class="stat-val bear" id="s-ber">0</div><div class="stat-lbl">BEARISH HITS</div></div>
  <div class="stat"><div class="stat-val warn" id="s-hcf">0</div><div class="stat-lbl">HIGH CONFLUENCE</div></div>
  <div class="stat"><div class="stat-val acc"  id="s-prs">0</div><div class="stat-lbl">PAIRS</div></div>
</div>

<!-- ── LEGEND ─────────────────────────────────────── -->
<div class="legend">
  <div class="legend-item"><div class="legend-dot" style="background:var(--bull)"></div>4H Bullish OB + 15m align ↑</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--bear)"></div>4H Bearish OB + 15m align ↓</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--warn)"></div>High confluence 3+</div>
  <div class="legend-item"><div class="legend-dot" style="background:var(--dim)"></div>Dots = confluence score /4</div>
</div>

<!-- ── MAIN GRID ───────────────────────────────────── -->
<main>
  <div class="bull-col">
    <div class="section-label">▲ BULLISH SETUPS <span class="count-pill" id="bc">0</span></div>
    <div id="bull-col"></div>
  </div>
  <div class="bear-col">
    <div class="section-label">▼ BEARISH SETUPS <span class="count-pill" id="rc">0</span></div>
    <div id="bear-col"></div>
  </div>
</main>

<div id="tip"></div>
<div id="toast"></div>

<script>
// ════════════════════════════════════════════════════
// Since this page IS served from the Railway server,
// all API calls use relative paths — no URL config needed
// ════════════════════════════════════════════════════
const API = '';   // same origin — /signals, /webhook, etc.

let signals    = [];
let seenIds    = new Set();
let dirF       = 'all';
let obF        = 'both';
let cfF        = 0;
let toastTimer = null;

// ── Fetch signals from server ────────────────────────
async function doFetch() {
  try {
    const r = await fetch(API + '/signals');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();

    // Detect genuinely new signals
    const fresh = data.filter(s => {
      const id = (s.pair || '') + '_' + (s.receivedAt || '');
      return !seenIds.has(id);
    });
    data.forEach(s => seenIds.add((s.pair||'') + '_' + (s.receivedAt||'')));

    if (fresh.length && signals.length > 0) {
      fresh.forEach(s =>
        toast(`⚡ ${s.pair} — ${s.bias==='bull'?'▲ BULLISH':'▼ BEARISH'} ${s.obType||''} OB`)
      );
    }

    signals = data;
    const now = new Date();
    document.getElementById('lastUpdateEl').textContent =
      pad(now.getUTCHours())+':'+pad(now.getUTCMinutes())+':'+pad(now.getUTCSeconds())+' UTC';
    document.getElementById('pollStatus').textContent = 'LAST POLL: ' + pad(now.getUTCSeconds()) + 's';

    setConn('connected','LIVE');
    render();
  } catch(e) {
    setConn('disconnected','SERVER ERROR');
    console.error(e);
  }
}

// ── Trigger immediate scan ──────────────────────────
async function doScanNow() {
  toast('⚡ Scan triggered — results in ~30s');
  try { await fetch(API + '/scan-now'); } catch(e) {}
}

// ── Helpers ─────────────────────────────────────────
function pad(n) { return String(n).padStart(2,'0'); }

function fmt(pair, val) {
  if (val == null || val === '' || val === undefined) return '—';
  const v = parseFloat(val);
  if (isNaN(v)) return String(val);
  if ((pair||'').includes('JPY')) return v.toFixed(3);
  if ((pair||'').includes('XAU')) return v.toFixed(2);
  if ((pair||'').includes('XAG')) return v.toFixed(3);
  return v.toFixed(5);
}

function depth(s) {
  const hi = parseFloat(s.obHigh), lo = parseFloat(s.obLow), pr = parseFloat(s.price);
  if (!hi || !lo || !pr || hi===lo) return {pct:50,lbl:'MID',cls:'dep-mid'};
  const r = hi - lo;
  const d = s.bias==='bull' ? (pr-lo)/r : (hi-pr)/r;
  const p = Math.round(d*100);
  if (p < 30) return {pct:p,lbl:'SHALLOW',cls:'dep-shallow'};
  if (p < 70) return {pct:p,lbl:'MID',    cls:'dep-mid'};
  return           {pct:p,lbl:'DEEP',   cls:'dep-deep'};
}

function conf(s) {
  let sc = 1;
  if (s.m15struct) sc++;
  if (s.fvg)  sc++;
  if (s.eqhl) sc++;
  return Math.min(sc, 4);
}

function ago(iso) {
  if (!iso) return '—';
  const d = Math.floor((Date.now()-new Date(iso))/1000);
  if (d < 60)   return d+'s ago';
  if (d < 3600) return Math.floor(d/60)+'m ago';
  return Math.floor(d/3600)+'h ago';
}

function isFresh(iso) {
  if (!iso) return false;
  return (Date.now()-new Date(iso)) < 120000;
}

// ── Render ──────────────────────────────────────────
function filtered() {
  return signals.filter(s => {
    if (dirF !== 'all' && s.bias !== dirF) return false;
    if (obF  !== 'both') {
      const t = (s.obType||'').toLowerCase();
      if (t !== obF) return false;
    }
    if (conf(s) < cfF) return false;
    return true;
  });
}

function render() {
  const all   = filtered();
  const bulls = all.filter(s => s.bias==='bull');
  const bears = all.filter(s => s.bias==='bear');
  const hcf   = all.filter(s => conf(s)>=3);
  const pairs = new Set(signals.map(s=>s.pair)).size;

  document.getElementById('s-tot').textContent = all.length;
  document.getElementById('s-bul').textContent = bulls.length;
  document.getElementById('s-ber').textContent = bears.length;
  document.getElementById('s-hcf').textContent = hcf.length;
  document.getElementById('s-prs').textContent = pairs;
  document.getElementById('bc').textContent    = bulls.length;
  document.getElementById('rc').textContent    = bears.length;

  document.getElementById('bull-col').innerHTML =
    bulls.length ? bulls.map(card).join('') : emptyCol('BULLISH');
  document.getElementById('bear-col').innerHTML =
    bears.length ? bears.map(card).join('') : emptyCol('BEARISH');
}

function emptyCol(dir) {
  return `<div class="empty-col">
    NO ${dir} SIGNALS
    <div class="pulse-text">WAITING FOR TRADINGVIEW ALERTS...</div>
  </div>`;
}

function card(s) {
  const c   = conf(s);
  const dep = depth(s);
  const pct = Math.round((c/4)*100);
  const fr  = isFresh(s.receivedAt);
  const mc  = s.bias==='bull' ? 'var(--bull)' : 'var(--bear)';

  const dots = [1,2,3,4].map(i =>
    `<div class="dot ${i<=c?'on '+s.bias:''}"></div>`
  ).join('');

  // Safely encode data for tooltip
  const enc = encodeURIComponent(JSON.stringify(s));

  return `<div class="card ${s.bias} ${fr?'fresh':''}"
    onmouseenter="showTip(event,'${enc}')" onmouseleave="hideTip()">
    ${fr?'<div class="new-badge">NEW</div>':''}
    <div class="card-top">
      <span class="pair-name">${s.pair||'—'}</span>
      <span class="dir-tag">${s.bias==='bull'?'▲ LONG':'▼ SHORT'}</span>
      <span class="ob-tag">${(s.obType||'OB').toUpperCase()}</span>
      <span class="tf-tag">${s.timeframe||'4H'}</span>
    </div>
    <div class="conf-track"><div class="conf-fill" style="width:${pct}%"></div></div>
    <div class="card-grid">
      <div>
        <div class="m-lbl">PRICE</div>
        <div class="m-val" style="color:#fff">${fmt(s.pair,s.price)}</div>
      </div>
      <div>
        <div class="m-lbl">OB ZONE</div>
        <div class="m-val" style="color:var(--text2);font-size:10px;line-height:1.7">
          ${fmt(s.pair,s.obHigh)}<br>${fmt(s.pair,s.obLow)}
        </div>
      </div>
      <div>
        <div class="m-lbl">15M STRUCT</div>
        <div class="m-val" style="color:${mc}">${s.m15struct||'—'}</div>
      </div>
      <div>
        <div class="m-lbl">DEPTH</div>
        <div class="m-val"><span class="dep-badge ${dep.cls}">${dep.lbl} ${dep.pct}%</span></div>
      </div>
      <div>
        <div class="m-lbl">ALERT</div>
        <div class="m-val" style="font-size:10px;color:var(--text2)">${s.alert||'—'}</div>
      </div>
      <div>
        <div class="m-lbl">RECEIVED</div>
        <div class="m-val" style="font-size:10px;color:var(--text2)">${ago(s.receivedAt)}</div>
      </div>
    </div>
    <div class="card-foot">
      <span style="color:${c>=3?'var(--warn)':'var(--dim)'}">CONF ${c}/4</span>
      <div class="dots">${dots}</div>
    </div>
  </div>`;
}

// ── Tooltip ─────────────────────────────────────────
function showTip(e, enc) {
  const s  = JSON.parse(decodeURIComponent(enc));
  const c  = conf(s);
  const dep = depth(s);
  const mc = s.bias==='bull'?'var(--bull)':'var(--bear)';

  document.getElementById('tip').innerHTML = `
    <div style="font-size:13px;font-weight:600;color:#fff;margin-bottom:10px;
                border-bottom:1px solid var(--border);padding-bottom:8px">
      ${s.pair} &mdash; ${s.bias==='bull'?'▲ BULLISH SETUP':'▼ BEARISH SETUP'}
    </div>
    <div class="tr"><span class="tl">Alert</span>    <span class="tv">${s.alert||'—'}</span></div>
    <div class="tr"><span class="tl">OB Type</span>  <span class="tv">${s.obType||'—'}</span></div>
    <div class="tr"><span class="tl">Timeframe</span><span class="tv">${s.timeframe||'4H'}</span></div>
    <div class="tdiv"></div>
    <div class="tr"><span class="tl">OB High</span>  <span class="tv">${fmt(s.pair,s.obHigh)}</span></div>
    <div class="tr"><span class="tl">OB Low</span>   <span class="tv">${fmt(s.pair,s.obLow)}</span></div>
    <div class="tr"><span class="tl">Entry Price</span><span class="tv" style="color:#fff">${fmt(s.pair,s.price)}</span></div>
    <div class="tr"><span class="tl">OB Depth</span> <span class="tv">${dep.lbl} (${dep.pct}%)</span></div>
    <div class="tdiv"></div>
    <div class="tr"><span class="tl">15m Structure</span><span class="tv" style="color:${mc}">${s.m15struct||'—'}</span></div>
    <div class="tr"><span class="tl">FVG Aligned</span>
      <span class="tv" style="color:${s.fvg?'var(--bull)':'var(--dim)'}">${s.fvg?'YES':'NO'}</span></div>
    <div class="tr"><span class="tl">EQH/EQL Near</span>
      <span class="tv" style="color:${s.eqhl?'var(--bull)':'var(--dim)'}">${s.eqhl?'YES':'NO'}</span></div>
    <div class="tdiv"></div>
    <div class="tr"><span class="tl">Confluence</span>
      <span class="tv" style="color:${c>=3?'var(--warn)':'var(--text)'}">${c}/4</span></div>
    <div class="tr"><span class="tl">Received</span> <span class="tv">${ago(s.receivedAt)}</span></div>
  `;

  const tip = document.getElementById('tip');
  const x   = Math.min(e.clientX+14, window.innerWidth-285);
  const y   = Math.min(e.clientY-10, window.innerHeight-360);
  tip.style.left = x+'px'; tip.style.top = y+'px';
  tip.classList.add('on');
}
function hideTip() { document.getElementById('tip').classList.remove('on'); }

// ── Filters ─────────────────────────────────────────
function setDir(v) {
  dirF = v;
  ['all','bull','bear'].forEach(x => document.getElementById('f-'+x).className='filter-btn');
  const cls={all:'active',bull:'bull-active',bear:'bear-active'};
  document.getElementById('f-'+v).classList.add(cls[v]);
  render();
}
function setOB(v) {
  obF = v;
  ['both','swing','internal'].forEach(x => document.getElementById('ob-'+x).className='filter-btn');
  document.getElementById('ob-'+v).classList.add('active');
  render();
}
function setConf(n) {
  cfF = n;
  [0,2,3].forEach(x => document.getElementById('cf-'+x).className='filter-btn');
  document.getElementById('cf-'+n).classList.add('active');
  render();
}

// ── Connection badge ─────────────────────────────────
function setConn(state, txt) {
  const b = document.getElementById('connBadge');
  b.className = 'conn-badge ' + state;
  document.getElementById('connText').textContent = txt;
}

// ── Toast ────────────────────────────────────────────
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('on'), 3500);
}

// ── Clock & sessions ─────────────────────────────────
function tick() {
  const n = new Date();
  document.getElementById('clock').textContent =
    pad(n.getUTCHours())+':'+pad(n.getUTCMinutes())+':'+pad(n.getUTCSeconds())+' UTC';

  const h = n.getUTCHours();
  const sess = {sydney:[21,6],tokyo:[0,9],london:[7,16],newyork:[12,21]};
  Object.entries(sess).forEach(([id,[st,en]])=>{
    const on = st<en ? (h>=st&&h<en) : (h>=st||h<en);
    document.getElementById('ss-'+id).classList.toggle('active',on);
  });
}

// ── Start ────────────────────────────────────────────
tick();
setInterval(tick, 1000);
doFetch();
setInterval(doFetch, 5000);   // poll signals every 5 seconds
pollStatus();
setInterval(pollStatus, 15000); // poll engine status every 15s
render();                      // initial empty render

// ── Engine status (shows scan diagnostics) ──────────
async function pollStatus() {
  try {
    const r = await fetch(API + '/status');
    const s = await r.json();
    const log = s.log || {};
    let txt = `SCAN: ${s.pairs ? s.pairs.length : 0} pairs`;
    if (log.last_scan) {
      const ago = Math.floor((Date.now() - new Date(log.last_scan))/1000);
      txt += ` | ${ago}s ago | ${log.credits_used_today||0} credits`;
    }
    if (log.last_error) txt += ` | ⚠ ${log.last_error.slice(0,40)}`;
    document.getElementById('pollStatus').textContent = txt;
  } catch(e) { /* ignore */ }
}
</script>
</body>
</html>
"""


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
@app.route('/')
def dashboard():
    return Response(DASHBOARD_HTML, mimetype='text/html')

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
