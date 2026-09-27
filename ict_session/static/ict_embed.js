/* ICT Session Liquidity - embeddable dashboard (v1.1.0)
 *
 * Add ONE line to your SMC dashboard page, just before </body>:
 *     <script src="/ict/static/ict_embed.js" defer></script>
 *
 * It adds a view switch (SMC | SMC + ICT | ICT) at the top of the page.
 * Your existing page content is left as-is (only wrapped so it can be shown/hidden),
 * and the ICT panel lives in a shadow DOM so neither page's styles leak into the other.
 */
(function () {
  "use strict";
  if (window.__ictEmbedLoaded) return;
  window.__ictEmbedLoaded = true;
  var API = "/ict/";
  var standalone = !!document.getElementById("ict-standalone");

  var ICT_CSS = "\n:host([hidden]){display:none!important}\n:host{display:block;\n  --bg:#0d1016; --panel:#141821; --card:#1a1f29; --line:#2b3240; --grid:#232a36;\n  --text:#dee2ea; --muted:#808999; --faint:#5a6272;\n  --green:#26a65b; --red:#d64541; --amber:#e6a23c; --blue:#4084e6; --idle:#343a46;\n  --bsl:#ff7a5c; --ssl:#3d9bff;\n  --asia:rgba(64,110,200,.13); --london:rgba(150,90,200,.13); --ny:rgba(38,166,91,.12);\n  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;\n}\n*{box-sizing:border-box}\n.ict-root{background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-font-smoothing:antialiased;text-align:left}\n[hidden]{display:none!important}\nbutton{font:inherit;color:inherit}\n.wrap{max-width:1320px;margin:0 auto;padding:12px 16px 40px}\nheader{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:6px 0 12px}\nh1{font-size:16px;font-weight:650;margin:0;letter-spacing:.02em}\n.badge{font:600 11px var(--mono);padding:2px 7px;border-radius:5px;background:var(--idle);color:var(--muted)}\n.badge.warn{background:#4a3614;color:var(--amber)}\n.hsp{flex:1}\n.clock{font:600 13px var(--mono)}\n.credits{font:12px var(--mono);color:var(--muted)}\n.btn{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;font-weight:600;font-size:12px}\n.btn:hover{border-color:#3d4658}\n.btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}\n.btn:disabled{opacity:.5;cursor:default}\n\n/* 24h session strip */\n.strip{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px 8px;margin-bottom:12px}\n.strip-bar{position:relative;height:22px;border-radius:5px;background:#10141b;overflow:hidden}\n.seg{position:absolute;top:0;bottom:0;display:flex;align-items:center;justify-content:center;font:600 10px/1 system-ui;letter-spacing:.06em;color:#cfd6e4}\n.seg.asia{background:rgba(64,110,200,.35)} .seg.london{background:rgba(150,90,200,.35)} .seg.ny{background:rgba(38,166,91,.35)}\n.now{position:absolute;top:-2px;bottom:-2px;width:2px;background:#fff;box-shadow:0 0 0 2px var(--bg)}\n.strip-ticks{display:flex;justify-content:space-between;font:10px var(--mono);color:var(--faint);margin-top:4px}\n.strip-head{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px}\n.strip-head b{color:var(--text);font-weight:600}\n\n/* controls */\n.controls{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;margin-bottom:14px}\n@media(max-width:620px){.controls{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:0 -16px 14px;padding:0 16px}.controls::-webkit-scrollbar{display:none}.ctl{flex:none}}\n@media(max-width:620px){header .credits{display:none}}\n.ctl{display:flex;align-items:center;gap:6px}\n.ctl>span{font:600 10px system-ui;letter-spacing:.08em;color:var(--faint);text-transform:uppercase}\n.seg-group{display:inline-flex;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:2px}\n.seg-group button{border:0;background:transparent;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;min-height:28px}\n.seg-group button.on{background:var(--blue);color:#fff}\n\n/* cards */\n.grid{display:grid;grid-template-columns:1fr;gap:14px}\n@media(min-width:760px){.grid{grid-template-columns:repeat(2,1fr)}}\n@media(min-width:1180px){.grid{grid-template-columns:repeat(3,1fr)}}\n.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0}\n.card-h{display:flex;align-items:center;gap:10px;margin-bottom:10px}\n.sym{font-size:16px;font-weight:700}\n.price{font:600 13px var(--mono);color:var(--muted)}\n.bias{margin-left:auto;font:700 11px system-ui;letter-spacing:.06em;padding:5px 9px;border-radius:6px;background:var(--idle);color:#c9d0dc;display:flex;gap:5px;align-items:center}\n.bias.buy{background:var(--green);color:#fff} .bias.sell{background:var(--red);color:#fff}\n.next{font:12px var(--mono);color:var(--muted);display:flex;justify-content:space-between;margin:-2px 0 10px}\n.pipe{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:8px}\n.st{font:700 9px system-ui;letter-spacing:.05em;text-align:center;padding:5px 0;border-radius:4px;background:var(--idle);color:var(--faint);position:relative}\n.st.s1{background:var(--amber);color:#2a1d05} .st.s2{background:var(--green);color:#fff} .st.s3{background:var(--red);color:#fff} .st.s4{background:var(--blue);color:#fff}\n.st.s1::after{content:\"\";position:absolute;inset:-2px;border:1px solid var(--amber);border-radius:5px;animation:pulse 1.6s infinite}\n@keyframes pulse{0%{opacity:.9}100%{opacity:0;transform:scale(1.08)}}\n@media (prefers-reduced-motion:reduce){.st.s1::after{animation:none;opacity:0}}\n.note{font-size:13px;margin-bottom:10px;min-height:18px}\n.chart{position:relative;background:#10141b;border:1px solid var(--grid);border-radius:8px;margin-bottom:10px;touch-action:pan-y}\n.chart svg{display:block;width:100%;height:200px}\n.tip{position:absolute;pointer-events:none;background:#0b0e13f0;border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:11px/1.45 var(--mono);white-space:nowrap;display:none;z-index:2}\n.legend{display:flex;flex-wrap:wrap;gap:4px 10px;font-size:11px;color:var(--muted);padding:0 2px 8px}\n.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}\n.cols{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px}\n.box{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}\n.box h4{margin:0 0 6px;font:700 10px system-ui;letter-spacing:.08em;color:var(--faint);display:flex;justify-content:space-between;align-items:center}\n.kv{display:flex;justify-content:space-between;font-size:12px;line-height:1.75;gap:6px}\n.kv span:first-child{color:var(--muted)} .kv span:last-child{font-family:var(--mono);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.pill{font:700 10px system-ui;padding:2px 6px;border-radius:4px;color:#fff}\n.pill.buy{background:var(--green)} .pill.sell{background:var(--red)}\n.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:4px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 4px;margin-bottom:10px}\n.stat{text-align:center;min-width:0}\n.stat b{display:block;font:600 14px var(--mono)}\n.stat span{font-size:10px;color:var(--faint);letter-spacing:.04em}\n.spark{height:34px;margin-bottom:10px}\n.spark svg{width:100%;height:34px;display:block}\n.recent-h{font:700 10px system-ui;letter-spacing:.08em;color:var(--faint);margin-bottom:5px;display:flex;justify-content:space-between}\n.recent{display:grid;grid-template-columns:repeat(12,1fr);gap:3px}\n.chip{font:600 9px var(--mono);text-align:center;padding:5px 0;border-radius:4px;background:var(--idle);color:var(--faint);cursor:default;overflow:hidden}\n.chip.w{background:var(--green);color:#fff} .chip.l{background:var(--red);color:#fff} .chip.live{background:var(--amber);color:#2a1d05} .chip.u{background:#474e5c;color:var(--text)}\n.pos{color:var(--green)} .neg{color:var(--red)} .mut{color:var(--muted)} .amb{color:var(--amber)}\n\ndetails{background:var(--panel);border:1px solid var(--line);border-radius:12px;margin-top:14px}\nsummary{cursor:pointer;padding:12px 14px;font-weight:650;list-style:none;display:flex;justify-content:space-between}\nsummary::after{content:\"+\";color:var(--muted)} details[open] summary::after{content:\"\u2013\"}\n.dbody{padding:0 14px 14px;overflow-x:auto}\ntable{border-collapse:collapse;width:100%;font-size:12px;min-width:640px}\nth{font:700 10px system-ui;letter-spacing:.06em;color:var(--faint);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap}\ntd{padding:6px 8px;border-bottom:1px solid var(--grid);font-family:var(--mono);white-space:nowrap}\n.guide ol{margin:0;padding-left:18px} .guide li{margin-bottom:8px;color:#c4cad6} .guide b{color:var(--text)}\n.empty{padding:30px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:12px}\n.foot{margin-top:14px;font-size:11px;color:var(--faint)}\n.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#222938;border:1px solid var(--line);padding:8px 14px;border-radius:8px;font-size:12px;display:none;z-index:5}\n";
  var MARKUP = "\n<div class=\"wrap\">\n  <header>\n    <h1>ICT Session Liquidity</h1>\n    <span class=\"badge\" id=\"ver\">v1.1.0</span>\n    <span class=\"badge warn\" id=\"demo\" hidden>DEMO DATA</span>\n    <span class=\"hsp\"></span>\n    <span class=\"clock\" id=\"clock\">--:--:--</span>\n    <span class=\"credits\" id=\"credits\" title=\"Twelve Data credits used by this module today / budget\">credits \u2013</span>\n    <button class=\"btn primary\" id=\"scan\" title=\"Fetch fresh bars (1 credit per symbol)\">Scan now</button>\n  </header>\n\n  <div class=\"strip\">\n    <div class=\"strip-head\"><span>New York time \u00b7 <b id=\"phase\">\u2013</b></span><span id=\"nextEvt\">\u2013</span></div>\n    <div class=\"strip-bar\" id=\"stripBar\"></div>\n    <div class=\"strip-ticks\"><span>18:00</span><span>22:00</span><span>02:00</span><span>06:00</span><span>10:00</span><span>14:00</span><span>18:00</span></div>\n  </div>\n\n  <div class=\"controls\" id=\"controls\">\n    <div class=\"ctl\"><span>Entry</span><div class=\"seg-group\" data-k=\"entry\"><button data-v=\"ce\">50% gap</button><button data-v=\"edge\">Edge</button></div></div>\n    <div class=\"ctl\"><span>Asia filter</span><div class=\"seg-group\" data-k=\"filter\"><button data-v=\"0\">Off</button><button data-v=\"1.5\">1.5\u00d7</button><button data-v=\"2\">2\u00d7</button></div></div>\n    <div class=\"ctl\"><span>Min R:R</span><div class=\"seg-group\" data-k=\"min_rr\"><button data-v=\"1\">1.0</button><button data-v=\"1.5\">1.5</button><button data-v=\"2\">2.0</button></div></div>\n    <div class=\"ctl\"><span>Sweep</span><div class=\"seg-group\" data-k=\"sweep\"><button data-v=\"london\">London</button><button data-v=\"ny\">Until NY</button></div></div>\n    <div class=\"ctl\"><span>OB fallback</span><div class=\"seg-group\" data-k=\"ob\"><button data-v=\"0\">Off</button><button data-v=\"1\">On</button></div></div>\n  </div>\n\n  <div class=\"grid\" id=\"grid\"><div class=\"empty\">Loading\u2026</div></div>\n\n  <details id=\"histBox\">\n    <summary><span>Setup history <span class=\"mut\" id=\"histCount\"></span></span></summary>\n    <div class=\"dbody\"><table id=\"hist\"><thead><tr>\n      <th data-s=\"date\">Date</th><th data-s=\"symbol\">Symbol</th><th data-s=\"dir\">Side</th><th data-s=\"sweep\">Swept</th>\n      <th data-s=\"zone\">Zone</th><th data-s=\"entry\">Entry</th><th data-s=\"sl\">SL</th><th data-s=\"tp\">TP</th>\n      <th data-s=\"rr\">R:R</th><th data-s=\"status\">Status</th><th data-s=\"r\">Result</th></tr></thead><tbody></tbody></table></div>\n  </details>\n\n  <details class=\"guide\">\n    <summary><span>How the model works</span></summary>\n    <div class=\"dbody\"><ol>\n      <li><b>Asian range (20:00\u201300:00 NY).</b> Its high is buy-side liquidity, its low sell-side. Days where the range is wider than the filter \u00d7 its 10-day average are skipped \u2014 the model wants a tight consolidation.</li>\n      <li><b>London sweep (02:00\u201305:00 NY).</b> London must take one side and then close back inside the range (the Judas swing). High swept \u2192 sell bias; low swept \u2192 buy bias. Both sides taken = no trade.</li>\n      <li><b>NY market-structure shift (08:00\u201311:00 NY).</b> A close through the most recent swing in the bias direction.</li>\n      <li><b>Entry.</b> Limit at 50% (or edge) of the fair value gap left by the displacement. Stop beyond the London sweep extreme; target the opposite side of the Asian range. Skipped if R:R is under the minimum.</li>\n      <li><b>Management.</b> Unfilled orders cancel at NY close or if price reaches the target first; open trades are flattened at 16:00 NY.</li>\n    </ol>\n    <p class=\"mut\" style=\"font-size:12px\">Stats are bar-based estimates on M5 (a bar touching both entry and stop counts as a loss) and exclude spread. The MT5 EA uses the same rules.</p></div>\n  </details>\n  <div class=\"foot\" id=\"foot\"></div>\n</div>\n<div class=\"toast\" id=\"toast\"></div>\n\n";

  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  function mountICT(host) {
    var root = host.attachShadow({ mode: "open" });
    root.innerHTML = "<style>" + ICT_CSS + "</style><div class=\"ict-root\">" + MARKUP + "</div>";
    var visible = function () { return !host.hidden && host.offsetParent !== null || standalone; };
    return (function (root, API, visible) {

      const $ = s => root.querySelector(s);
      const STAGE_TIPS = ["Asian range built","Range tight enough vs average","London swept Asia high/low","Closed back inside Asia","NY market-structure shift","Limit filled in FVG/OB","Trade result"];
      const STATUS = {0:"pending",1:"open",2:"TP hit",3:"SL hit",4:"time exit",5:"not filled",6:"target before fill"};
      const DEF = {entry:"ce", filter:"1.5", min_rr:"1.5", sweep:"london", ob:"0"};
      let prefs = {...DEF};
      try { prefs = {...DEF, ...JSON.parse(localStorage.getItem("ict_prefs") || "{}")}; } catch (e) {}
      let state = null, clockOffset = 0, histSort = {k:"date", dir:-1};

      // ---------- utils
      const pad = n => String(n).padStart(2, "0");
      const wall = ts => { const d = new Date(ts * 1000); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; };
      const wallDate = ts => { const d = new Date(ts * 1000); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth()+1)}-${pad(d.getUTCDate())} ${wall(ts)}`; };
      const nowNY = () => Math.floor(Date.now() / 1000) + clockOffset;
      const dur = s => { s = Math.max(0, s|0); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), sec = s%60;
        return h ? `${h}h ${pad(m)}m` : `${m}m ${pad(sec)}s`; };
      const fx = (v, d) => v == null ? "–" : Number(v).toFixed(d);
      const sgn = (v, d=2) => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d);
      const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
      function toast(msg){ const t = $("#toast"); t.textContent = msg; t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", 3500); }

      // ---------- controls
      function paintControls(){
        root.querySelectorAll(".seg-group").forEach(g => {
          g.querySelectorAll("button").forEach(b => b.classList.toggle("on", String(prefs[g.dataset.k]) === b.dataset.v));
        });
      }
      $("#controls").addEventListener("click", e => {
        const b = e.target.closest("button"); if (!b) return;
        const g = b.closest(".seg-group"); prefs[g.dataset.k] = b.dataset.v;
        try { localStorage.setItem("ict_prefs", JSON.stringify(prefs)); } catch (e) {}
        paintControls(); load();                      // reclassify: zero API credits
      });
      const qs = () => new URLSearchParams(prefs).toString();

      // ---------- data
      async function load(){
        if (!visible()) return;
        try {
          const r = await fetch(`${API}api/state?${qs()}`); state = await r.json();
          clockOffset = state.now - Math.floor(Date.now() / 1000);
          render();
        } catch (e) { toast("Could not load state: " + e.message); }
      }
      $("#scan").addEventListener("click", async () => {
        const b = $("#scan"); b.disabled = true; b.textContent = "Scanning…";
        try {
          const r = await fetch(`${API}api/scan?${qs()}`, {method:"POST"}); state = await r.json();
          clockOffset = state.now - Math.floor(Date.now() / 1000);
          const bad = Object.entries(state.scan || {}).filter(([, m]) => !["ok","fresh","demo"].includes(m));
          toast(bad.length ? bad.map(([s, m]) => `${s}: ${m}`).join(" · ") : "Scan complete");
          render();
        } catch (e) { toast("Scan failed: " + e.message); }
        b.disabled = false; b.textContent = "Scan now";
      });

      // ---------- session strip (18:00 -> 18:00 NY)
      function renderStrip(){
        const bar = $("#stripBar"); const pos = m => ((m - 18*60 + 1440) % 1440) / 1440 * 100;
        const segs = [["asia","ASIA",20*60,24*60],["london","LONDON",2*60,5*60],["ny","NY",8*60,11*60]];
        bar.innerHTML = segs.map(([c,l,s,e]) => `<div class="seg ${c}" style="left:${pos(s)}%;width:${(e-s)/1440*100}%">${l}</div>`).join("") + `<div class="now" id="nowMark"></div>`;
      }
      function tickStrip(){
        const t = nowNY(), m = Math.floor((t % 86400) / 60);
        const mk = $("#nowMark"); if (mk) mk.style.left = `calc(${((m - 18*60 + 1440) % 1440) / 1440 * 100}% - 1px)`;
        const ph = m >= 1200 ? "Asia range forming" : m < 120 ? "Asia range set" : m < 300 ? "London sweep window" : m < 480 ? "Between London and NY" : m < 660 ? "NY entry window" : m < 960 ? "NY afternoon · managing trades" : "Off-session";
        $("#phase").textContent = ph;
        const evts = [[0,"Asia closes"],[120,"London opens"],[300,"London closes"],[480,"NY opens"],[660,"NY closes"],[960,"Flat time"],[1200,"Asia opens"]];
        let best = null; for (const [em, name] of evts){ const d = ((em - m + 1440) % 1440) || 1440; if (!best || d < best[0]) best = [d, name]; }
        const secs = best[0]*60 - (t % 60);
        $("#nextEvt").textContent = `${best[1]} in ${dur(secs)}`;
        $("#clock").textContent = new Date(t*1000).toISOString().substr(11, 8) + " NY";
        root.querySelectorAll("[data-next]").forEach(el => {
          const c = JSON.parse(el.dataset.next);
          const ev = [[c.asia_end,"Asia closes"],[c.london_start,"London opens"],[c.london_end,"London closes"],[c.ny_start,"NY opens"],[c.ny_end,"NY closes"],[c.flat_at,"Flat"]].find(([x]) => x && x > t);
          el.textContent = ev ? `${ev[1]} in ${dur(ev[0] - t)}` : "Cycle complete";
        });
      }

      // ---------- mini chart
      function chartSVG(s, W, H){
        const ch = s.chart, cy = s.cycle, tr = s.trade, n = ch.t.length;
        if (n < 2) return {svg:"", map:null};
        const padL = 4, padR = 58, padT = 8, padB = 16;
        const live = tr && tr.status === 1;
        const t0 = ch.t[0], t1 = Math.max(live ? (cy.flat_at || cy.ny_end) : cy.ny_end + 3600, ch.t[n-1] + 300);
        let lo = Math.min(...ch.l), hi = Math.max(...ch.h);
        const extra = [cy.asia_hi, cy.asia_lo]; if (tr) extra.push(tr.sl, tr.tp, tr.entry);
        extra.filter(v => v != null).forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); });
        const pr = (hi - lo) || 1; lo -= pr * .05; hi += pr * .05;
        const X = ts => padL + (ts - t0) / (t1 - t0) * (W - padL - padR);
        const Y = p => padT + (hi - p) / (hi - lo) * (H - padT - padB);
        const bw = Math.max(1, (X(t0 + 300) - X(t0)) * .7);
        let g = "";
        // session bands
        [[cy.asia_start, cy.asia_end, "var(--asia)"], [cy.london_start, cy.london_end, "var(--london)"], [cy.ny_start, cy.ny_end, "var(--ny)"]]
          .forEach(([a, b, f]) => { g += `<rect x="${X(a)}" y="${padT}" width="${Math.max(0, X(b) - X(a))}" height="${H-padT-padB}" fill="${f}"/>`; });
        // time ticks (session opens)
        [[cy.asia_start,"Asia"],[cy.london_start,"LDN"],[cy.ny_start,"NY"]].forEach(([a, l]) => {
          g += `<text x="${X(a)+3}" y="${H-4}" fill="#5a6272" font-size="9" font-family="system-ui">${l} ${wall(a)}</text>`; });
        // FVG zone
        if (tr){
          const z1 = tr.fill_time || tr.exit_time || t1;
          g += `<rect x="${X(tr.zone_from)}" y="${Y(tr.zone_top)}" width="${Math.max(2, X(z1) - X(tr.zone_from))}" height="${Math.max(1, Y(tr.zone_bot) - Y(tr.zone_top))}" fill="${tr.dir > 0 ? "rgba(38,166,91,.28)" : "rgba(214,69,65,.28)"}" stroke="${tr.dir > 0 ? "#26a65b" : "#d64541"}" stroke-width="1" stroke-dasharray="2 2"/>`;
        }
        // Asia levels
        const lvl = (p, col, lab, dash) => { if (p == null) return;
          g += `<line x1="${X(cy.asia_end)}" x2="${W - padR}" y1="${Y(p)}" y2="${Y(p)}" stroke="${col}" stroke-width="1" ${dash ? 'stroke-dasharray="3 3"' : ""}/>`;
          g += `<text x="${W - padR + 4}" y="${Y(p) + 3}" fill="${col}" font-size="9" font-family="ui-monospace,monospace">${lab}</text>`; };
        const tpOn = v => tr && tr.tp_asia && Math.abs(tr.tp - v) < 1e-9;
        if (cy.asia_final){ lvl(cy.asia_hi, "var(--bsl)", tpOn(cy.asia_hi) ? "" : "BSL", true); lvl(cy.asia_lo, "var(--ssl)", tpOn(cy.asia_lo) ? "" : "SSL", true); }
        // candles
        for (let i = 0; i < n; i++){
          const up = ch.c[i] >= ch.o[i], col = up ? "#26a65b" : "#d64541", x = X(ch.t[i] + 150);
          g += `<line x1="${x}" x2="${x}" y1="${Y(ch.h[i])}" y2="${Y(ch.l[i])}" stroke="${col}" stroke-width="1"/>`;
          const y1 = Y(Math.max(ch.o[i], ch.c[i])), y2 = Y(Math.min(ch.o[i], ch.c[i]));
          g += `<rect x="${x - bw/2}" y="${y1}" width="${bw}" height="${Math.max(1, y2 - y1)}" fill="${col}"/>`;
        }
        // sweep marker
        if (cy.sweep){
          const p = cy.sweep > 0 ? cy.asia_hi : cy.asia_lo, x = X(cy.sweep_time + 150), y = Y(p);
          const col = cy.sweep > 0 ? "var(--bsl)" : "var(--ssl)";
          g += `<circle cx="${x}" cy="${y}" r="4.5" fill="none" stroke="${col}" stroke-width="2"/>`;
          g += `<text x="${x}" y="${cy.sweep > 0 ? y - 8 : y + 15}" fill="${col}" font-size="9" font-weight="700" text-anchor="middle" font-family="system-ui">SWEEP</text>`;
        }
        // MSS + trade levels
        if (tr){
          g += `<line x1="${X(tr.swing_time)}" x2="${X(tr.mss_time + 300)}" y1="${Y(tr.swing_level)}" y2="${Y(tr.swing_level)}" stroke="#c0c6d2" stroke-width="1" stroke-dasharray="4 2"/>`;
          g += `<text x="${X(tr.swing_time)}" y="${Y(tr.swing_level) + (tr.dir < 0 ? 11 : -4)}" fill="#c0c6d2" font-size="9" font-family="system-ui" font-weight="700">MSS</text>`;
          const x0 = X(tr.mss_time), x1 = X(tr.exit_time || t1);
          [[tr.entry, "#e8ecf2", "ENT", ""], [tr.sl, "#d64541", "SL", "4 3"], [tr.tp, "#26a65b", tr.tp_asia ? (tr.dir > 0 ? "TP·BSL" : "TP·SSL") : "TP", "4 3"]].forEach(([p, c, l, d]) => {
            g += `<line x1="${x0}" x2="${x1}" y1="${Y(p)}" y2="${Y(p)}" stroke="${c}" stroke-width="1.5" ${d ? `stroke-dasharray="${d}"` : ""}/>`;
            g += `<text x="${W - padR + 4}" y="${Y(p) + 3}" fill="${c}" font-size="9" font-family="ui-monospace,monospace">${l}</text>`;
          });
          if (tr.fill_time) g += `<circle cx="${X(tr.fill_time + 150)}" cy="${Y(tr.entry)}" r="4" fill="${tr.dir > 0 ? "#26a65b" : "#d64541"}" stroke="#10141b" stroke-width="2"/>`;
        }
        // last price
        const lp = ch.c[n-1];
        const tags = [cy.asia_final ? cy.asia_hi : null, cy.asia_final ? cy.asia_lo : null].concat(tr ? [tr.entry, tr.sl, tr.tp] : []).filter(v => v != null);
        const clash = tags.some(v => Math.abs(Y(v) - Y(lp)) < 11);
        g += `<line x1="${padL}" x2="${W - padR}" y1="${Y(lp)}" y2="${Y(lp)}" stroke="#808999" stroke-width=".6" stroke-dasharray="1 3"/>`;
        if (!clash) g += `<rect x="${W - padR + 1}" y="${Y(lp) - 7}" width="${padR - 2}" height="14" rx="3" fill="#2b3240"/><text x="${W - padR + 4}" y="${Y(lp) + 3}" fill="#dee2ea" font-size="9" font-family="ui-monospace,monospace">${fx(lp, s.digits)}</text>`;
        g += `<line class="xh" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="#808999" stroke-width="1" visibility="hidden"/>`;
        return {svg: g, map: {X, t0, t1, padL, padR, W}};
      }

      function drawChart(el, s){
        const svg = el.querySelector("svg"), W = Math.max(280, Math.round(el.clientWidth));
        svg.setAttribute("viewBox", `0 0 ${W} 200`); svg.setAttribute("width", W);
        const {svg: inner, map} = chartSVG(s, W, 200);
        svg.innerHTML = inner;
        if (map && !el._hover){ el._hover = true; attachHover(el, s, map); } else if (map) el._map = map;
        el._map = map; el._s = s;
      }
      let _rz; window.addEventListener("resize", () => { clearTimeout(_rz); _rz = setTimeout(() => {
        root.querySelectorAll(".card .chart").forEach(el => el._s && drawChart(el, el._s)); }, 150); });

      function attachHover(el, s0, map0){
        const svg = el.querySelector("svg"), tip = el.querySelector(".tip");
        const move = ev => {
          const map = el._map, s = el._s, ch = s.chart, xh = svg.querySelector(".xh"); if (!map || !xh) return;
          const rect = svg.getBoundingClientRect(), px = (ev.clientX - rect.left) / rect.width * map.W;
          const ts = map.t0 + (px - map.padL) / (map.W - map.padL - map.padR) * (map.t1 - map.t0);
          let a = 0, b = ch.t.length - 1;                       // nearest bar (handles weekend gaps)
          while (b - a > 1){ const m = (a + b) >> 1; if (ch.t[m] + 150 < ts) a = m; else b = m; }
          const k = Math.abs(ch.t[a] + 150 - ts) <= Math.abs(ch.t[b] + 150 - ts) ? a : b;
          const x = map.X(ch.t[k] + 150); xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
          const d = s.digits, up = ch.c[k] >= ch.o[k];
          tip.innerHTML = `<b>${wallDate(ch.t[k])}</b> NY<br>O ${fx(ch.o[k], d)} &nbsp;H ${fx(ch.h[k], d)}<br>L ${fx(ch.l[k], d)} &nbsp;C <span class="${up ? "pos" : "neg"}">${fx(ch.c[k], d)}</span>`;
          tip.style.display = "block";
          const left = (x / map.W) * rect.width; tip.style.left = (left > rect.width / 2 ? left - tip.offsetWidth - 10 : left + 10) + "px"; tip.style.top = "8px";
        };
        const out = () => { tip.style.display = "none"; const xh = svg.querySelector(".xh"); if (xh) xh.setAttribute("visibility", "hidden"); };
        svg.addEventListener("pointermove", move); svg.addEventListener("pointerleave", out);
      }

      function spark(eq){
        if (!eq || eq.length < 2) return `<div class="mut" style="font-size:11px;padding-top:10px">Equity curve appears after 2 closed trades</div>`;
        const W = 300, H = 34, v = [0, ...eq], lo = Math.min(0, ...v), hi = Math.max(0, ...v), r = (hi - lo) || 1;
        const X = i => i / (v.length - 1) * W, Y = y => 3 + (hi - y) / r * (H - 6);
        const pts = v.map((y, i) => `${X(i).toFixed(1)},${Y(y).toFixed(1)}`).join(" ");
        const col = v[v.length - 1] >= 0 ? "#26a65b" : "#d64541";
        return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Equity in R over lookback"><line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="#2b3240" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
      }

      // ---------- cards
      function card(s){
        const cy = s.cycle, tr = s.trade, d = s.digits, st = s.stats || {};
        let bias = `<span class="bias">WAITING</span>`;
        if (cy.fail >= 1 && cy.fail <= 4) bias = `<span class="bias" title="${esc(cy.note)}">NO TRADE</span>`;
        else if (cy.sweep > 0) bias = `<span class="bias sell">▼ SELL BIAS</span>`;
        else if (cy.sweep < 0) bias = `<span class="bias buy">▲ BUY BIAS</span>`;
        const pipe = state.stages.map((n, i) => `<div class="st s${cy.stages[i]}" title="${STAGE_TIPS[i]}">${n}</div>`).join("");
        const rm = cy.range_mult, rmCls = rm == null ? "mut" : (+prefs.filter > 0 && rm > +prefs.filter ? "neg" : rm <= 1 ? "pos" : "amb");
        const rangePts = cy.asia_hi != null ? fx(cy.asia_hi - cy.asia_lo, d) : "–";
        let swept = "";
        if (cy.sweep) swept = `<div class="kv"><span>Swept</span><span style="color:${cy.sweep > 0 ? "var(--bsl)" : "var(--ssl)"}">${cy.sweep > 0 ? "High" : "Low"} ${wall(cy.sweep_time)}</span></div>` +
          (cy.reject_time ? `<div class="kv"><span>Rejected</span><span>${wall(cy.reject_time)}</span></div>` : "");
        let setup = `<div class="mut" style="font-size:12px;padding:6px 0">Needs sweep → rejection → NY MSS</div>`;
        if (tr){
          let res = STATUS[tr.status], cls = "amb";
          if (tr.status >= 2 && tr.status <= 4){ res += " " + sgn(tr.r) + "R"; cls = tr.r >= 0 ? "pos" : "neg"; }
          if (tr.status >= 5) cls = "mut";
          setup = `<div class="kv"><span>Entry</span><span>${fx(tr.entry, d)}</span></div>
            <div class="kv"><span>Stop</span><span class="neg">${fx(tr.sl, d)}</span></div>
            <div class="kv"><span>Target</span><span class="pos">${fx(tr.tp, d)}</span></div>
            <div class="kv"><span>R:R · ${tr.fvg ? "FVG" : "OB"}</span><span>1:${fx(tr.rr, 2)}</span></div>
            <div class="kv"><span>Status</span><span class="${cls}">${res}</span></div>`;
        }
        const chips = s.recent.map(r => {
          let c = "", l = "·";
          if (r.status != null){
            if (r.status >= 2 && r.status <= 4){ c = r.r > 0 ? "w" : "l"; l = sgn(r.r, 1); }
            else if (r.status <= 1){ c = "live"; l = "live"; } else { c = "u"; l = "u"; }
          }
          return `<div class="chip ${c}" title="${esc(r.date + ": " + r.note)}">${l}</div>`;
        }).join("");
        const netCls = st.net_r == null ? "" : st.net_r >= 0 ? "pos" : "neg";
        return `<div class="card" data-sym="${esc(s.symbol)}">
          <div class="card-h"><span class="sym">${esc(s.symbol)}</span><span class="price">${fx(s.price, d)}</span>${bias}</div>
          <div class="next"><span data-next='${JSON.stringify({asia_end:cy.asia_end, london_start:cy.london_start, london_end:cy.london_end, ny_start:cy.ny_start, ny_end:cy.ny_end, flat_at:cy.flat_at})}'></span><span>bar ${esc((s.last_bar||"").slice(11))}</span></div>
          <div class="pipe">${pipe}</div>
          <div class="note">${esc(cy.note)}</div>
          <div class="chart"><svg height="200" role="img" aria-label="${esc(s.symbol)} current cycle, M5"></svg><div class="tip"></div></div>
          <div class="legend"><span><i style="background:rgba(64,110,200,.45)"></i>Asia</span><span><i style="background:rgba(150,90,200,.45)"></i>London</span><span><i style="background:rgba(38,166,91,.4)"></i>NY</span><span><i style="background:var(--bsl)"></i>Buy-side liq.</span><span><i style="background:var(--ssl)"></i>Sell-side liq.</span></div>
          <div class="cols">
            <div class="box"><h4>ASIAN RANGE</h4>
              <div class="kv"><span>High</span><span style="color:var(--bsl)">${fx(cy.asia_hi, d)}</span></div>
              <div class="kv"><span>Low</span><span style="color:var(--ssl)">${fx(cy.asia_lo, d)}</span></div>
              <div class="kv"><span>Range</span><span>${rangePts}</span></div>
              <div class="kv"><span>vs 10d avg</span><span class="${rmCls}">${rm == null ? (cy.asia_final ? "–" : "building") : fx(rm, 2) + "×"}</span></div>
              ${swept}
            </div>
            <div class="box"><h4>NY SETUP ${tr ? `<span class="pill ${tr.dir > 0 ? "buy" : "sell"}">${tr.dir > 0 ? "BUY" : "SELL"}</span>` : ""}</h4>${setup}</div>
          </div>
          <div class="stats" title="Last ${state.lookback_days} days · bar-based estimate, no spread">
            <div class="stat"><b>${st.trades ?? 0}</b><span>TRADES</span></div>
            <div class="stat"><b>${st.win == null ? "–" : Math.round(st.win) + "%"}</b><span>WIN</span></div>
            <div class="stat"><b class="${netCls}">${st.net_r == null ? "–" : sgn(st.net_r, 1)}</b><span>NET R</span></div>
            <div class="stat"><b>${st.pf == null ? (st.trades ? "∞" : "–") : fx(st.pf, 2)}</b><span>PF</span></div>
            <div class="stat"><b>${st.max_dd_r == null ? "–" : fx(st.max_dd_r, 1)}</b><span>MAX DD</span></div>
          </div>
          <div class="spark">${spark(st.equity)}</div>
          <div class="recent-h"><span>RECENT DAYS</span><span>${st.unfilled ? st.unfilled + " unfilled" : ""}</span></div>
          <div class="recent">${chips}</div>
        </div>`;
      }

      function render(){
        $("#demo").hidden = !state.demo;
        $("#credits").textContent = `credits ${state.credits.used}/${state.credits.budget}`;
        const grid = $("#grid");
        if (!state.symbols.length){
          grid.innerHTML = `<div class="empty">No bars cached yet${state.missing.length ? " for " + state.missing.join(", ") : ""}. Press <b>Scan now</b> to load ~35 days of M5 (2 credits per symbol the first time).</div>`;
        } else {
          grid.innerHTML = state.symbols.map(card).join("");
          state.symbols.forEach(s => {
            const el = grid.querySelector(`.card[data-sym="${CSS.escape(s.symbol)}"] .chart`);
            drawChart(el, s);
          });
        }
        renderHist();
        const ls = Object.entries(state.last_scan).filter(([, v]) => v).map(([k, v]) => `${k} ${new Date(v * 1000).toLocaleTimeString()}`);
        $("#foot").textContent = `Sessions (NY): Asia ${state.sessions.asia} · London ${state.sessions.london} · NY ${state.sessions.ny}. ` +
          (ls.length ? "Last scan: " + ls.join(" · ") + ". " : "") + "Toggles recompute from cached bars — no API credits.";
        tickStrip();
      }

      function renderHist(){
        const rows = [...state.history].sort((a, b) => {
          const k = histSort.k, x = a[k], y = b[k];
          return (x > y ? 1 : x < y ? -1 : 0) * histSort.dir;
        });
        $("#histCount").textContent = `(${rows.length})`;
        $("#hist tbody").innerHTML = rows.map(h => {
          let res = "–", cls = "mut";
          if (h.status >= 2 && h.status <= 4){ res = sgn(h.r) + "R"; cls = h.r >= 0 ? "pos" : "neg"; }
          else if (h.status <= 1) { res = STATUS[h.status]; cls = "amb"; }
          return `<tr><td>${h.date}</td><td>${esc(h.symbol)}</td><td class="${h.dir > 0 ? "pos" : "neg"}">${h.dir > 0 ? "BUY" : "SELL"}</td><td>${h.sweep}</td><td>${h.zone}</td>
            <td>${h.entry}</td><td>${h.sl}</td><td>${h.tp}</td><td>1:${fx(h.rr, 2)}</td><td>${STATUS[h.status]}</td><td class="${cls}">${res}</td></tr>`;
        }).join("") || `<tr><td colspan="11" class="mut">No setups in the lookback window.</td></tr>`;
      }
      $("#hist thead").addEventListener("click", e => {
        const th = e.target.closest("th"); if (!th) return;
        histSort = {k: th.dataset.s, dir: histSort.k === th.dataset.s ? -histSort.dir : -1}; renderHist();
      });

      paintControls(); renderStrip();
      setInterval(load, 30000);            // polls cached state only (no API credits)
      load();
      return { refresh(){ if (state) render(); load(); } };
      setInterval(() => visible() && tickStrip(), 1000);


    })(root, API, visible);
  }

  function init() {
    if (standalone) {
      var h = document.getElementById("ict-standalone");
      mountICT(h);
      return;
    }
    // 1. wrap the existing SMC page content (elements keep their ids/listeners)
    var smc = document.createElement("div");
    smc.id = "smc-view";
    var kids = Array.prototype.slice.call(document.body.childNodes).filter(function (n) {
      return !(n.nodeType === 1 && (n.tagName === "SCRIPT" || n.tagName === "STYLE" || n.tagName === "LINK"));
    });
    document.body.insertBefore(smc, document.body.firstChild);
    kids.forEach(function (n) { smc.appendChild(n); });

    // 2. view switch
    var bar = document.createElement("div");
    bar.id = "ict-view-switch";
    var bs = bar.attachShadow({ mode: "open" });
    bs.innerHTML =
      "<style>:host{display:block}.b{display:flex;justify-content:center;gap:0;padding:8px 12px;background:#0b0e13;border-bottom:1px solid #2b3240;font:600 12px system-ui,-apple-system,Segoe UI,Roboto,sans-serif}" +
      ".g{display:inline-flex;background:#141821;border:1px solid #2b3240;border-radius:9px;padding:2px}" +
      "button{border:0;background:transparent;color:#808999;font:inherit;padding:7px 14px;border-radius:7px;cursor:pointer;min-height:32px}" +
      "button.on{background:#4084e6;color:#fff}.l{color:#5a6272;align-self:center;margin-right:10px;letter-spacing:.08em;font-size:10px}</style>" +
      "<div class=\"b\"><span class=\"l\">VIEW</span><div class=\"g\">" +
      "<button data-m=\"smc\">SMC</button><button data-m=\"both\">SMC + ICT</button><button data-m=\"ict\">ICT</button></div></div>";
    document.body.insertBefore(bar, smc);

    // 3. ICT panel (above SMC in the combined view)
    var ictHost = document.createElement("div");
    ictHost.id = "ict-view";
    document.body.insertBefore(ictHost, smc);
    var app = mountICT(ictHost);

    function setMode(m) {
      if (["smc", "both", "ict"].indexOf(m) < 0) m = "both";
      store("ict_view_mode", m);
      smc.hidden = (m === "ict");
      ictHost.hidden = (m === "smc");
      Array.prototype.forEach.call(bs.querySelectorAll("button"), function (b) { b.classList.toggle("on", b.getAttribute("data-m") === m); });
      if (m !== "smc") setTimeout(function () { app.refresh(); }, 0);
      window.dispatchEvent(new Event("resize"));   // lets SMC charts re-fit after being shown
    }
    bs.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (b) setMode(b.getAttribute("data-m"));
    });
    var q = new URLSearchParams(location.search).get("view");
    setMode(q || store("ict_view_mode") || "both");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
