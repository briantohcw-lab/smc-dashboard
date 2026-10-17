//+------------------------------------------------------------------+
//|                                          SMC_AutoTrader_EA.mq4   |
//|  Brian's 4H OB -> 15m CHoCH -> 15m OB retest -> confirmation      |
//|  TRADES the CONFIRMED setups (A-grade by default) with prop-firm  |
//|  guards: risk per trade, daily-loss stop, max-drawdown stop,      |
//|  profit-target lock, max open trades, one trade per currency.     |
//|  Run it on a DEMO / free trial first.                             |
//|                                                                  |
//|  Rules (backtest on 30 pairs, May-Oct 2026):                      |
//|   - 4H order block (LuxAlgo-style structure, internal 5 + swing 25)|
//|   - 15m CHoCH in the OB direction after price entered the OB      |
//|   - wait for price to come back into the 15m OB that the CHoCH    |
//|     left, then an engulfing / rejection candle that CLOSES back   |
//|     out of it = CONFIRMED (entry = that close)                    |
//|   - stop beyond the 4H OB (+0.25 x 15m ATR)                       |
//|   - grade = daily candle: A agrees, B neutral, C against (= SKIP) |
//|   - OB respect = earlier visits the 4H OB already held            |
//|   - target = nearest liquidity >= 1R (prev-day, Asian, London     |
//|     extreme, or next opposing 4H OB within 3 x 4H ATR)            |
//+------------------------------------------------------------------+
#property copyright "Brian Toh / built with Claude"
#property version   "1.10"
#property strict

//---- inputs
input string InpSymbols      = "AUDCAD,AUDCHF,AUDJPY,AUDNZD,AUDUSD,CADCHF,CADJPY,CHFJPY,EURAUD,EURCAD,EURCHF,EURGBP,EURJPY,EURNZD,EURUSD,GBPAUD,GBPCAD,GBPCHF,GBPJPY,GBPNZD,GBPUSD,NZDCAD,NZDCHF,NZDJPY,NZDUSD,USDCAD,USDCHF,USDJPY,XAUUSD"; // Symbols (broker suffix is found automatically)
input int    InpScanSeconds  = 60;      // Rescan every N seconds
input int    InpGMTOffset    = -99;     // Broker GMT offset in hours (-99 = auto)
input double InpRiskPercent  = 0.5;     // Risk % per trade for the lot-size column
input bool   InpAlertsA      = true;    // Alert on A-grade setups
input bool   InpAlertsB      = false;   // Alert on B-grade setups
input bool   InpPush         = true;    // Send push notification to the MT4 phone app
input bool   InpPopup        = true;    // Pop-up alert on the terminal
input bool   InpShowDead     = false;   // Also list skipped / missed / invalid rows
input int    InpPanelX       = 10;      // Panel X offset
input int    InpPanelY       = 20;      // Panel Y offset
input int    InpFontSize     = 8;       // Panel font size
input bool   InpDrawLevels   = true;    // Draw OB boxes and entry/stop/target on this chart

//---- trading
input bool   InpAutoTrade     = true;    // Place trades on CONFIRMED setups
input bool   InpTradeB        = false;   // Also trade B-grade (daily neutral); backtest edge is in A
input int    InpMinRespect    = 0;       // Only trade if the 4H OB held at least N times before (0 = any)
input int    InpMagic         = 26101;   // Magic number
input int    InpMaxOpen       = 2;       // Max open SMC trades at once
input bool   InpOnePerCcy     = true;    // Never two open trades sharing a currency
input double InpMaxSpreadR    = 0.15;    // Skip if spread > this fraction of the stop distance
input double InpMaxSlipR      = 0.25;    // Skip if price moved more than this many R past the confirmation close
input int    InpMaxHoldHours  = 120;     // Close after N hours (backtest: 5 days)
input double InpAccountSize   = 100000;  // Challenge starting balance
input double InpDailyStopPct  = 3.0;     // Stop for the day at -N% from the day's start (FTMO limit is 5)
input double InpMaxDDStopPct  = 8.0;     // Stop for good at -N% from the starting balance (FTMO limit is 10)
input double InpTargetPct     = 10.0;    // Stop opening trades once equity is +N% (Phase 1 = 10, Phase 2 = 5)
input int    InpSlippage      = 3;       // Max slippage in pips

//---- status codes (lower = more actionable)
#define ST_CONF    0
#define ST_ZONE    1
#define ST_CHOCH   2
#define ST_WAIT    3
#define ST_NEAR    4
#define ST_SKIP    5
#define ST_RAN     6
#define ST_FAILED  7
#define ST_FLIPPED 8
#define ST_INVALID 9
#define ST_NONE    10
#define ST_LOADING 11

struct Ev
  {
   int      i;
   datetime t;
   int      d;
   bool     choch;
   double   lvl;
   int      from;
   double   obHi;
   double   obLo;
   datetime obT;
  };

struct Zone
  {
   int      d;
   double   hi;
   double   lo;
   int      born;
   bool     swing;
   bool     alive;
  };

struct Setup
  {
   string   sym;
   int      status;
   int      d;
   string   st4;
   int      depth;
   int      held;
   bool     swing;
   double   zHi;
   double   zLo;
   datetime zT;
   string   daily;      // agrees / neutral / AGAINST / ?
   string   grade;
   double   chLvl;
   datetime chT;
   bool     hasOb15;
   double   obHi;
   double   obLo;
   datetime obT;
   double   entry;
   double   stop;
   double   stop15;
   double   target;
   string   tgtSrc;
   double   rr;
   double   riskPips;
   double   lots;
   double   price;
   string   next;
   string   confKind;
   datetime confT;
   int      dp;
  };

string   g_syms[];
Setup    g_res[];
int      g_gmt = 0;
datetime g_lastScan = 0;
string   PFX = "SMC_";
string   g_rowSym[];

//+------------------------------------------------------------------+
int OnInit()
  {
   string parts[];
   int n = StringSplit(InpSymbols, ',', parts);
   ArrayResize(g_syms, 0);
   for(int k = 0; k < n; k++)
     {
      string b = parts[k];
      b = StringTrimLeft(StringTrimRight(b));
      if(b == "") continue;
      string s = ResolveSymbol(b);
      if(s == "") { Print("SMC: symbol not found at this broker: ", b); continue; }
      int m = ArraySize(g_syms);
      ArrayResize(g_syms, m + 1);
      g_syms[m] = s;
     }
   EventSetTimer((int)MathMax(10, InpScanSeconds));
   GuardEquity();
   ScanAll();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PFX);
   Comment("");
  }

void OnTimer() { ScanAll(); ManageTrades(); }
void OnTick()  { GuardEquity(); }

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id != CHARTEVENT_OBJECT_CLICK) return;
   if(StringFind(sparam, PFX + "R") != 0) return;
   // name = SMC_R<row>_<col>
   int us = StringFind(sparam, "_", StringLen(PFX) + 1);
   if(us < 0) return;
   int row = (int)StringToInteger(StringSubstr(sparam, StringLen(PFX) + 1, us - StringLen(PFX) - 1));
   if(row < 0 || row >= ArraySize(g_rowSym)) return;
   string sym = g_rowSym[row];
   if(sym != "" && sym != Symbol()) ChartSetSymbolPeriod(0, sym, PERIOD_M15);
  }

//+------------------------------------------------------------------+
//| symbols                                                          |
//+------------------------------------------------------------------+
string ResolveSymbol(string base)
  {
   if(SymbolSelect(base, true) && MarketInfo(base, MODE_DIGITS) > 0) return(base);
   int tot = SymbolsTotal(false);
   for(int i = 0; i < tot; i++)
     {
      string nm = SymbolName(i, false);
      if(StringFind(nm, base) == 0 || (StringFind(nm, base) > 0 && StringLen(nm) <= StringLen(base) + 4))
        {
         if(SymbolSelect(nm, true)) return(nm);
        }
     }
   return("");
  }

double PipOf(string sym)
  {
   if(StringFind(sym, "XAU") >= 0) return(0.1);
   if(StringFind(sym, "BTC") >= 0) return(1.0);
   int dg = (int)MarketInfo(sym, MODE_DIGITS);
   double pt = MarketInfo(sym, MODE_POINT);
   if(dg == 3 || dg == 5) return(pt * 10.0);
   return(pt);
  }

//+------------------------------------------------------------------+
//| bars (oldest first)                                              |
//+------------------------------------------------------------------+
int LoadBars(string sym, int tf, int start, int count, double &O[], double &H[], double &L[], double &C[], datetime &T[])
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(sym, tf, start, count, r);
   if(got <= 0) return(0);
   ArrayResize(O, got); ArrayResize(H, got); ArrayResize(L, got); ArrayResize(C, got); ArrayResize(T, got);
   for(int i = 0; i < got; i++)
     {
      O[i] = r[i].open; H[i] = r[i].high; L[i] = r[i].low; C[i] = r[i].close; T[i] = r[i].time;
     }
   return(got);
  }

//+------------------------------------------------------------------+
//| structure: LuxAlgo-style swing legs, BOS / CHoCH, order blocks    |
//+------------------------------------------------------------------+
void Structure(const double &H[], const double &L[], const double &C[], const datetime &T[], int n, int len,
               Ev &ev[], Zone &obs[], double &openHi, double &openLo, int &biasOut)
  {
   ArrayResize(ev, 0);
   ArrayResize(obs, 0);
   double pH[], pL[];
   ArrayResize(pH, n); ArrayResize(pL, n);
   double atr = 0, sum = 0;
   for(int i = 0; i < n; i++)
     {
      double tr = (i > 0) ? MathMax(H[i] - L[i], MathMax(MathAbs(H[i] - C[i - 1]), MathAbs(L[i] - C[i - 1]))) : H[i] - L[i];
      if(i < 200) { sum += tr; atr = sum / (i + 1); }
      else atr = (atr * 199 + tr) / 200;
      bool hv = (H[i] - L[i]) >= 2 * atr;
      pH[i] = hv ? L[i] : H[i];
      pL[i] = hv ? H[i] : L[i];
     }
   int leg = 0, bias = 0;
   bool shSet = false, shX = true, slSet = false, slX = true;
   double shL = 0, slL = 0;
   int shI = 0, slI = 0;
   for(int i = 0; i < n; i++)
     {
      if(i >= len)
        {
         double hh = -1e18, ll = 1e18;
         for(int k = i - len + 1; k <= i; k++) { if(H[k] > hh) hh = H[k]; if(L[k] < ll) ll = L[k]; }
         int p = i - len;
         int nl = leg;
         if(H[p] > hh) nl = 0;
         else if(L[p] < ll) nl = 1;
         if(nl != leg)
           {
            if(nl == 1) { slL = L[p]; slX = false; slI = p; slSet = true; }
            else        { shL = H[p]; shX = false; shI = p; shSet = true; }
            leg = nl;
           }
        }
      if(shSet && !shX && C[i] > shL)
        {
         int m = shI;
         for(int k = shI; k < i; k++) if(pL[k] < pL[m]) m = k;
         int e = ArraySize(ev); ArrayResize(ev, e + 1);
         ev[e].i = i; ev[e].t = T[i]; ev[e].d = 1; ev[e].choch = (bias == -1); ev[e].lvl = shL; ev[e].from = shI;
         ev[e].obHi = MathMax(pH[m], pL[m]); ev[e].obLo = MathMin(pH[m], pL[m]); ev[e].obT = T[m];
         shX = true; bias = 1;
         int z = ArraySize(obs); ArrayResize(obs, z + 1);
         obs[z].d = 1; obs[z].hi = pH[m]; obs[z].lo = pL[m]; obs[z].born = i; obs[z].alive = true; obs[z].swing = (len >= 25);
        }
      if(slSet && !slX && C[i] < slL)
        {
         int m = slI;
         for(int k = slI; k < i; k++) if(pH[k] > pH[m]) m = k;
         int e = ArraySize(ev); ArrayResize(ev, e + 1);
         ev[e].i = i; ev[e].t = T[i]; ev[e].d = -1; ev[e].choch = (bias == 1); ev[e].lvl = slL; ev[e].from = slI;
         ev[e].obHi = MathMax(pH[m], pL[m]); ev[e].obLo = MathMin(pH[m], pL[m]); ev[e].obT = T[m];
         slX = true; bias = -1;
         int z = ArraySize(obs); ArrayResize(obs, z + 1);
         obs[z].d = -1; obs[z].hi = pH[m]; obs[z].lo = pL[m]; obs[z].born = i; obs[z].alive = true; obs[z].swing = (len >= 25);
        }
      for(int z = 0; z < ArraySize(obs); z++)
        {
         if(!obs[z].alive || obs[z].born == i) continue;
         if(obs[z].d == 1 && L[i] < obs[z].lo) obs[z].alive = false;
         if(obs[z].d == -1 && H[i] > obs[z].hi) obs[z].alive = false;
        }
     }
   openHi = (shSet && !shX) ? shL : 0;
   openLo = (slSet && !slX) ? slL : 0;
   biasOut = bias;
  }

//+------------------------------------------------------------------+
//| daily bias from the last two closed D1 candles                   |
//+------------------------------------------------------------------+
int DailyBias(string sym, double &pdH, double &pdL)
  {
   double o1 = iOpen(sym, PERIOD_D1, 1), h1 = iHigh(sym, PERIOD_D1, 1), l1 = iLow(sym, PERIOD_D1, 1), c1 = iClose(sym, PERIOD_D1, 1);
   double o2 = iOpen(sym, PERIOD_D1, 2), h2 = iHigh(sym, PERIOD_D1, 2), l2 = iLow(sym, PERIOD_D1, 2), c2 = iClose(sym, PERIOD_D1, 2);
   pdH = h1; pdL = l1;
   if(h1 <= 0 || h2 <= 0) return(99);
   int eng = 0;
   if(c1 > o1 && c2 < o2 && c1 >= o2 && o1 <= c2) eng = 1;
   if(c1 < o1 && c2 > o2 && c1 <= o2 && o1 >= c2) eng = -1;
   if(c1 > h2) return(1);
   if(c1 < l2) return(-1);
   if(h1 > h2 && c1 < h2) return(-1);
   if(l1 < l2 && c1 > l2) return(1);
   return(eng);
  }

//+------------------------------------------------------------------+
//| range of bars between two UTC times                              |
//+------------------------------------------------------------------+
bool RangeUTC(const double &H[], const double &L[], const datetime &T[], int n, datetime t0utc, datetime t1utc, double &lo, double &hi)
  {
   lo = 1e18; hi = -1e18;
   bool any = false;
   for(int i = n - 1; i >= 0; i--)
     {
      datetime u = T[i] - g_gmt * 3600;
      if(u < t0utc) break;
      if(u >= t1utc) continue;
      lo = MathMin(lo, L[i]); hi = MathMax(hi, H[i]); any = true;
     }
   return(any);
  }

//+------------------------------------------------------------------+
//| scan                                                             |
//+------------------------------------------------------------------+
void ScanAll()
  {
   if(InpGMTOffset != -99) g_gmt = InpGMTOffset;
   else
     {
      int off = (int)MathRound((double)(TimeCurrent() - TimeGMT()) / 3600.0);
      if(off >= -12 && off <= 14) g_gmt = off;
     }
   ArrayResize(g_res, 0);
   for(int s = 0; s < ArraySize(g_syms); s++) ScanSymbol(g_syms[s]);
   SortResults();
   DrawPanel();
   if(InpDrawLevels) DrawLevels();
   g_lastScan = TimeLocal();
  }

void AddResult(Setup &x)
  {
   int k = ArraySize(g_res);
   ArrayResize(g_res, k + 1);
   g_res[k] = x;
  }

int Rank(const Setup &x)
  {
   int g = (x.grade == "A") ? 0 : (x.grade == "B") ? 1 : 2;
   return(x.status * 100 + g * 10 - MathMin(x.held, 5));
  }

void SortResults()
  {
   int n = ArraySize(g_res);
   for(int i = 1; i < n; i++)
     {
      Setup key = g_res[i];
      int kr = Rank(key);
      int j = i - 1;
      while(j >= 0 && Rank(g_res[j]) > kr) { g_res[j + 1] = g_res[j]; j--; }
      g_res[j + 1] = key;
     }
  }

void ScanSymbol(string sym)
  {
   Setup best;
   best.sym = sym; best.status = ST_NONE; best.grade = ""; best.held = 0; best.next = ""; best.dp = (int)MarketInfo(sym, MODE_DIGITS);
   best.d = 0; best.st4 = ""; best.depth = 0; best.daily = ""; best.hasOb15 = false; best.entry = 0; best.stop = 0; best.stop15 = 0;
   best.target = 0; best.tgtSrc = ""; best.rr = 0; best.riskPips = 0; best.lots = 0; best.price = 0; best.confKind = ""; best.chLvl = 0;
   best.zHi = 0; best.zLo = 0; best.swing = false; best.obHi = 0; best.obLo = 0; best.zT = 0; best.chT = 0; best.obT = 0; best.confT = 0;

   double O4[], H4[], L4[], C4[]; datetime T4[];
   double O1[], H1[], L1[], C1[]; datetime T1[];
   int n4 = LoadBars(sym, PERIOD_H4, 0, 600, O4, H4, L4, C4, T4);
   int n1 = LoadBars(sym, PERIOD_M15, 1, 900, O1, H1, L1, C1, T1);   // closed 15m candles only
   if(n4 < 150 || n1 < 250) { best.status = ST_LOADING; best.next = "loading history..."; AddResult(best); return; }

   double pip = PipOf(sym);
   double bid = MarketInfo(sym, MODE_BID);
   double cl = bid > 0 ? bid : C4[n4 - 1];
   double atr4 = 0; for(int k = n4 - 14; k < n4; k++) atr4 += H4[k] - L4[k]; atr4 /= 14.0;
   double atr15 = 0; for(int k = n1 - 14; k < n1; k++) atr15 += H1[k] - L1[k]; atr15 /= 14.0;

   // ---- 4H zones (internal 5 + swing 25), alive and de-duplicated
   Zone zs[]; ArrayResize(zs, 0);
   for(int pass = 0; pass < 2; pass++)
     {
      int len = (pass == 0) ? 5 : 25;
      Ev e4[]; Zone o4[]; double oh, ol; int b4;
      Structure(H4, L4, C4, T4, n4, len, e4, o4, oh, ol, b4);
      for(int z = 0; z < ArraySize(o4); z++)
        {
         if(!o4[z].alive) continue;
         bool dup = false;
         for(int q = 0; q < ArraySize(zs); q++) if(zs[q].hi == o4[z].hi && zs[q].lo == o4[z].lo) dup = true;
         if(dup) continue;
         int m = ArraySize(zs); ArrayResize(zs, m + 1); zs[m] = o4[z];
        }
     }

   // ---- 15m structure
   Ev e1[]; Zone o1[]; double openHi, openLo; int bias15;
   Structure(H1, L1, C1, T1, n1, 5, e1, o1, openHi, openLo, bias15);

   // ---- daily + liquidity
   double pdH, pdL;
   int dBias = DailyBias(sym, pdH, pdL);
   datetime nowU = TimeCurrent() - g_gmt * 3600;
   datetime d0 = nowU - (nowU % 86400);
   double aLo, aHi, lLo, lHi;
   bool hasA = RangeUTC(H1, L1, T1, n1, d0, d0 + 7 * 3600, aLo, aHi);
   bool hasL = RangeUTC(H1, L1, T1, n1, d0 + 7 * 3600, d0 + 12 * 3600, lLo, lHi);

   bool any = false;
   for(int z = 0; z < ArraySize(zs); z++)
     {
      double hi = zs[z].hi, lo = zs[z].lo;
      int d = zs[z].d;
      if(hi - lo <= 0) continue;
      int lastTouch = -1, eps = 0; bool away = true;
      for(int j = zs[z].born + 1; j < n4; j++)
        {
         bool t = (d == 1) ? (L4[j] <= hi) : (H4[j] >= lo);
         if(t) { lastTouch = j; if(away) { eps++; away = false; } }
         else
           {
            double dist = (d == 1) ? (L4[j] - hi) : (lo - H4[j]);
            if(dist >= 0.5 * atr4) away = true;
           }
        }
      bool inside = (cl <= hi && cl >= lo);
      string st = "";
      if(inside && lastTouch >= n4 - 6) st = "IN";
      else if(lastTouch >= n4 - 6) st = "TAP";
      else
        {
         double dist2 = (d == 1) ? (cl - hi) : (lo - cl);
         if(dist2 > 0 && dist2 <= 0.5 * atr4) st = "NEAR";
        }
      if(st == "") continue;

      Setup x = best;
      x.d = d; x.zHi = hi; x.zLo = lo; x.zT = T4[zs[z].born]; x.st4 = st; x.swing = zs[z].swing; x.price = cl;
      x.held = (st == "NEAR") ? eps : MathMax(0, eps - 1);
      x.daily = (dBias == 99) ? "?" : (dBias == d) ? "agrees" : (dBias == -d) ? "AGAINST" : "neutral";
      x.grade = (dBias == d) ? "A" : (dBias == -d) ? "C" : "B";
      datetime entT = 0;
      if(st != "NEAR")
        {
         int e0 = lastTouch;
         while(e0 - 1 > zs[z].born && ((d == 1) ? (L4[e0 - 1] <= hi) : (H4[e0 - 1] >= lo))) e0--;
         double ext = (d == 1) ? 1e18 : -1e18;
         for(int j = e0; j < n4; j++) ext = (d == 1) ? MathMin(ext, L4[j]) : MathMax(ext, H4[j]);
         x.depth = (int)MathRound(100.0 * ((d == 1) ? (hi - ext) : (ext - lo)) / (hi - lo));
         entT = T4[e0];
        }
      EvaluateLTF(x, entT, e1, H1, L1, C1, T1, O1, n1, atr15, atr4, pip, pdH, pdL, hasA, aLo, aHi, hasL, lLo, lHi, zs, openHi, openLo);
      if(!any || Rank(x) < Rank(best)) { best = x; any = true; }
     }
   if(!any) { best.status = ST_NONE; best.next = "no 4H OB in play"; }
   if(best.status != ST_NONE) { CheckAlert(best); AddResult(best); }
   else if(InpShowDead) AddResult(best);
  }

//+------------------------------------------------------------------+
//| 15m: CHoCH, 15m OB, retest, confirmation, levels, status         |
//+------------------------------------------------------------------+
void EvaluateLTF(Setup &x, datetime entT, Ev &e1[], const double &H[], const double &L[], const double &C[], const datetime &T[], const double &O[], int n,
                 double atr15, double atr4, double pip, double pdH, double pdL, bool hasA, double aLo, double aHi, bool hasL, double lLo, double lHi,
                 Zone &zs[], double openHi, double openLo)
  {
   int d = x.d;
   if(x.st4 == "NEAR")
     {
      x.status = ST_NEAR;
      x.next = StringFormat("Approaching 4H %s OB %s-%s", d > 0 ? "bull" : "bear", DoubleToString(x.zLo, x.dp), DoubleToString(x.zHi, x.dp));
      return;
     }
   // last CHoCH in the OB direction since price entered the OB (else the last BOS)
   int ci = -1, bi = -1;
   for(int k = 0; k < ArraySize(e1); k++)
     {
      if(e1[k].d != d || e1[k].t < entT) continue;
      if(e1[k].choch) ci = k; else bi = k;
     }
   int c = (ci >= 0) ? ci : bi;
   if(c < 0)
     {
      x.status = ST_WAIT;
      // the level whose 15m close would be the CHoCH: the open (unbroken) 15m swing on the other side
      double lvl = (d > 0) ? openHi : openLo;
      if(lvl > 0) x.next = "In 4H OB: wait for a 15m close " + (d > 0 ? "above " : "below ") + DoubleToString(lvl, x.dp) + " (CHoCH)";
      else        x.next = "In 4H OB: wait for a new 15m swing, then a CHoCH " + (d > 0 ? "up" : "down");
      return;
     }
   Ev ch = e1[c];
   x.chLvl = ch.lvl; x.chT = ch.t;
   bool flipped = false;
   for(int k = c + 1; k < ArraySize(e1); k++) if(e1[k].d != d) flipped = true;
   double legExt = (d > 0) ? 1e18 : -1e18;
   for(int k = ch.from; k <= ch.i; k++) legExt = (d > 0) ? MathMin(legExt, L[k]) : MathMax(legExt, H[k]);
   bool invalid = false;
   for(int k = ch.i; k < n; k++) if((d > 0 && C[k] < legExt) || (d < 0 && C[k] > legExt)) invalid = true;

   int status = ST_CHOCH;
   x.hasOb15 = (ch.obHi > ch.obLo);
   double obHi = ch.obHi, obLo = ch.obLo;
   if(x.hasOb15)
     {
      x.obHi = obHi; x.obLo = obLo; x.obT = ch.obT;
      int tk = -1;
      for(int k = ch.i + 1; k < n; k++) if((d > 0) ? (L[k] <= obHi) : (H[k] >= obLo)) { tk = k; break; }
      bool broke = false;
      if(tk >= 0) for(int k = tk; k < n; k++) if((d > 0) ? (C[k] < obLo) : (C[k] > obHi)) { broke = true; break; }
      int cf = -1; string kind = "";
      if(tk >= 0)
        {
         for(int k = MathMax(tk, 1); k < n; k++)
           {
            if((d > 0) ? (C[k] < obLo) : (C[k] > obHi)) break;
            double body = MathAbs(C[k] - O[k]);
            double wick = (d < 0) ? H[k] - MathMax(O[k], C[k]) : MathMin(O[k], C[k]) - L[k];
            bool outside = (d < 0) ? (C[k] < obLo) : (C[k] > obHi);
            bool touched = (d < 0) ? (H[k] >= obLo) : (L[k] <= obHi);
            bool prevTouched = (d < 0) ? (H[k - 1] >= obLo) : (L[k - 1] <= obHi);
            bool eng = (d < 0) ? (C[k] < O[k] && C[k - 1] > O[k - 1] && C[k] <= O[k - 1] && O[k] >= C[k - 1])
                               : (C[k] > O[k] && C[k - 1] < O[k - 1] && C[k] >= O[k - 1] && O[k] <= C[k - 1]);
            bool rej = touched && wick >= 2 * body && wick >= 0.5 * (H[k] - L[k]);
            if(outside && (eng || rej) && (touched || prevTouched)) { cf = k; kind = eng ? "engulfing" : "rejection"; break; }
           }
        }
      if(broke) status = ST_FAILED;
      else if(cf >= 0) { status = ST_CONF; x.confKind = kind; x.confT = T[cf]; x.entry = C[cf]; }
      else if(tk >= 0) status = ST_ZONE;
      else status = ST_CHOCH;
      if(cf < 0) x.entry = (d > 0) ? obHi : obLo;
      x.stop15 = (d > 0) ? MathMin(legExt, obLo) : MathMax(legExt, obHi);
     }
   else
     {
      x.entry = ch.lvl;
      x.stop15 = legExt;
     }
   if(invalid) status = ST_INVALID;
   else if(flipped) status = ST_FLIPPED;

   // stop beyond the 4H OB, never tighter than the 15m structure, never inside 0.5 x 15m ATR
   x.stop = (d > 0) ? MathMin(x.zLo - 0.25 * atr15, x.stop15) : MathMax(x.zHi + 0.25 * atr15, x.stop15);
   if(MathAbs(x.entry - x.stop) < 0.5 * atr15) x.stop = x.entry - d * 0.5 * atr15;
   double risk = MathAbs(x.entry - x.stop);
   x.riskPips = risk / pip;

   // target: nearest liquidity >= 1R
   double bestV = 0; string bestS = ""; bool found = false;
   double cand[5]; string src[5]; int nc = 0;
   if(d > 0) { cand[nc] = pdH; src[nc++] = "prev-day high"; if(hasA) { cand[nc] = aHi; src[nc++] = "Asian high"; } if(hasL) { cand[nc] = lHi; src[nc++] = "London high"; } }
   else      { cand[nc] = pdL; src[nc++] = "prev-day low";  if(hasA) { cand[nc] = aLo; src[nc++] = "Asian low"; }  if(hasL) { cand[nc] = lLo; src[nc++] = "London low"; } }
   double opp = 0; bool hasOpp = false;
   for(int z = 0; z < ArraySize(zs); z++)
     {
      if(zs[z].d != -d || zs[z].hi <= zs[z].lo) continue;
      double v = (d > 0) ? zs[z].lo : zs[z].hi;
      if((v - x.price) * d <= 0) continue;
      if(!hasOpp || (v - opp) * d < 0) { opp = v; hasOpp = true; }
     }
   if(hasOpp && MathAbs(opp - x.entry) <= 3 * atr4) { cand[nc] = opp; src[nc++] = "next 4H OB"; }
   for(int k = 0; k < nc; k++)
     {
      if(cand[k] <= 0) continue;
      if((cand[k] - x.entry) * d < risk) continue;
      if(!found || (cand[k] - bestV) * d < 0) { bestV = cand[k]; bestS = src[k]; found = true; }
     }
   if(found) { x.target = bestV; x.tgtSrc = bestS; x.rr = MathAbs(bestV - x.entry) / risk; }

   // price already beyond the stop / past the target / ran away
   if((x.price - x.stop) * d <= 0 && status < ST_SKIP) status = ST_INVALID;
   bool pastT = found && (x.price - x.target) * d >= 0;
   double runR = risk > 0 ? (x.price - x.entry) * d / risk : 0;
   if(status == ST_CONF && (runR > 1.5 || pastT)) status = ST_RAN;
   if((status == ST_CHOCH || status == ST_ZONE) && pastT) status = ST_RAN;
   if(!ch.choch && status <= ST_CHOCH) status = ST_RAN;      // BOS = continuation, the move already happened
   if(x.grade == "C" && status <= ST_CHOCH) status = ST_SKIP;  // daily against
   x.status = status;

   // lot size for InpRiskPercent
   double tv = MarketInfo(x.sym, MODE_TICKVALUE), ts = MarketInfo(x.sym, MODE_TICKSIZE);
   double step = MarketInfo(x.sym, MODE_LOTSTEP), mn = MarketInfo(x.sym, MODE_MINLOT), mx = MarketInfo(x.sym, MODE_MAXLOT);
   if(tv > 0 && ts > 0 && risk > 0 && step > 0)
     {
      double money = AccountEquity() * InpRiskPercent / 100.0;
      double lots = money / (risk / ts * tv);
      lots = MathFloor(lots / step) * step;
      x.lots = MathMax(mn, MathMin(mx, lots));
     }

   string zone = DoubleToString(MathMin(obLo, obHi), x.dp) + "-" + DoubleToString(MathMax(obLo, obHi), x.dp);
   if(status == ST_CONF)       x.next = StringFormat("ENTRY VALID: %s %s close %s. Stop %s, target %s", x.confKind, d > 0 ? "BUY" : "SELL", DoubleToString(x.entry, x.dp), DoubleToString(x.stop, x.dp), found ? DoubleToString(x.target, x.dp) : "none 1R+");
   else if(status == ST_ZONE)  x.next = "In 15m OB " + zone + ": wait for engulfing/rejection close " + (d > 0 ? "above " + DoubleToString(obHi, x.dp) : "below " + DoubleToString(obLo, x.dp));
   else if(status == ST_CHOCH) x.next = "CHoCH done: wait for pullback into 15m OB " + zone;
   else if(status == ST_SKIP)  x.next = "Daily candle against - skip";
   else if(status == ST_RAN)   x.next = ch.choch ? "Ran without us - do not chase" : "BOS continuation, not a CHoCH - skip";
   else if(status == ST_FAILED) x.next = "15m candle closed through the 15m OB - setup off";
   else if(status == ST_FLIPPED) x.next = "15m structure flipped - setup off";
   else if(status == ST_INVALID) x.next = "Invalidated (closed beyond leg / stop) - setup off";
  }

//+------------------------------------------------------------------+
//| alerts (once per setup + status), state survives chart changes   |
//+------------------------------------------------------------------+
void CheckAlert(Setup &x)
  {
   if(x.status != ST_CONF && x.status != ST_ZONE) return;
   if(!((x.grade == "A" && InpAlertsA) || (x.grade == "B" && InpAlertsB))) return;
   string key = PFX + "AL_" + x.sym + "_" + IntegerToString((int)x.chT) + "_" + IntegerToString(x.status);
   if(GlobalVariableCheck(key)) return;
   GlobalVariableSet(key, (double)TimeCurrent());
   string msg = StringFormat("SMC %s %s %s-grade: %s", x.sym, x.d > 0 ? "LONG" : "SHORT", x.grade, x.next);
   if(InpPopup) Alert(msg);
   if(InpPush) SendNotification(msg);
   PlaySound(x.status == ST_CONF ? "alert2.wav" : "alert.wav");
  }


//+------------------------------------------------------------------+
//| TRADING                                                          |
//+------------------------------------------------------------------+
double   g_dayStartEq = 0;
int      g_dayKey = -1;
bool     g_haltDay = false;
bool     g_haltAll = false;
bool     g_targetHit = false;
string   g_guardMsg = "";

bool IsOurs()
  {
   return(OrderMagicNumber() == InpMagic);
  }

int OpenCount()
  {
   int c = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && IsOurs() && OrderType() <= OP_SELL) c++;
   return(c);
  }

bool CcyInUse(string sym)
  {
   string a = StringSubstr(sym, 0, 3), b = StringSubstr(sym, 3, 3);
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || !IsOurs()) continue;
      string s2 = OrderSymbol();
      string c1 = StringSubstr(s2, 0, 3), c2 = StringSubstr(s2, 3, 3);
      if(c1 == a || c1 == b || c2 == a || c2 == b) return(true);
     }
   return(false);
  }

void CloseAll(string why)
  {
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || !IsOurs()) continue;
      RefreshRates();
      double px = (OrderType() == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID) : MarketInfo(OrderSymbol(), MODE_ASK);
      if(OrderType() <= OP_SELL)
        {
         if(!OrderClose(OrderTicket(), OrderLots(), px, InpSlippage * 10, clrOrange)) Print("SMC close failed ", GetLastError());
        }
      else if(!OrderDelete(OrderTicket())) Print("SMC delete failed ", GetLastError());
     }
   Print("SMC: closed all trades - ", why);
  }

// prop-firm guards, checked on every tick
void GuardEquity()
  {
   int key = TimeDayOfYear(TimeCurrent()) + 1000 * TimeYear(TimeCurrent());   // broker day (FTMO server = CE(S)T midnight)
   if(key != g_dayKey)
     {
      g_dayKey = key;
      // remember the day's starting equity across EA restarts, so a restart mid-day cannot reset the daily stop
      string gv = PFX + "DAY_" + IntegerToString(InpMagic) + "_" + IntegerToString(key);
      if(GlobalVariableCheck(gv)) g_dayStartEq = GlobalVariableGet(gv);
      else { g_dayStartEq = MathMax(AccountBalance(), AccountEquity()); GlobalVariableSet(gv, g_dayStartEq); }
      g_haltDay = false;
     }
   double eq = AccountEquity();
   if(!g_haltDay && g_dayStartEq > 0 && eq <= g_dayStartEq * (1 - InpDailyStopPct / 100.0))
     {
      g_haltDay = true;
      CloseAll(StringFormat("daily stop %.1f%% hit", InpDailyStopPct));
      Notify(StringFormat("SMC: daily stop -%.1f%% hit. No more trades today.", InpDailyStopPct));
     }
   if(!g_haltAll && eq <= InpAccountSize * (1 - InpMaxDDStopPct / 100.0))
     {
      g_haltAll = true;
      CloseAll(StringFormat("max drawdown stop %.1f%% hit", InpMaxDDStopPct));
      Notify(StringFormat("SMC: max drawdown stop -%.1f%% hit. Trading stopped. Review before restarting.", InpMaxDDStopPct));
     }
   if(!g_targetHit && eq >= InpAccountSize * (1 + InpTargetPct / 100.0))
     {
      g_targetHit = true;
      Notify(StringFormat("SMC: profit target +%.1f%% reached. No new trades; open trades run to their stop/target.", InpTargetPct));
     }
   g_guardMsg = StringFormat("day %.2f%%  total %.2f%%%s%s%s",
                             g_dayStartEq > 0 ? 100.0 * (eq / g_dayStartEq - 1) : 0,
                             100.0 * (eq / InpAccountSize - 1),
                             g_haltDay ? "  DAY STOP" : "", g_haltAll ? "  HALTED" : "", g_targetHit ? "  TARGET HIT" : "");
  }

void Notify(string msg)
  {
   if(InpPopup) Alert(msg);
   if(InpPush) SendNotification(msg);
   Print(msg);
  }

void ManageTrades()
  {
   GuardEquity();
   // time exit
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || !IsOurs() || OrderType() > OP_SELL) continue;
      if(TimeCurrent() - OrderOpenTime() >= InpMaxHoldHours * 3600)
        {
         RefreshRates();
         double px = (OrderType() == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID) : MarketInfo(OrderSymbol(), MODE_ASK);
         if(OrderClose(OrderTicket(), OrderLots(), px, InpSlippage * 10, clrOrange)) Notify("SMC: closed " + OrderSymbol() + " after max hold time");
        }
     }
   if(!InpAutoTrade || g_haltDay || g_haltAll || g_targetHit) return;
   if(!IsTradeAllowed()) return;
   for(int k = 0; k < ArraySize(g_res); k++)
     {
      Setup x = g_res[k];
      if(x.status != ST_CONF) continue;
      if(!(x.grade == "A" || (x.grade == "B" && InpTradeB))) continue;
      if(x.held < InpMinRespect) continue;
      // only the candle that just closed: never chase an older confirmation
      if(x.confT != iTime(x.sym, PERIOD_M15, 1)) continue;
      string key = PFX + "TR_" + x.sym + "_" + IntegerToString((int)x.chT);
      if(GlobalVariableCheck(key)) continue;
      if(OpenCount() >= InpMaxOpen) { Print("SMC skip ", x.sym, ": max open trades"); continue; }
      if(InpOnePerCcy && CcyInUse(x.sym)) { Print("SMC skip ", x.sym, ": currency already in an open trade"); continue; }
      if(PlaceTrade(x)) GlobalVariableSet(key, (double)TimeCurrent());
     }
  }

bool PlaceTrade(Setup &x)
  {
   RefreshRates();
   int d = x.d;
   double ask = MarketInfo(x.sym, MODE_ASK), bid = MarketInfo(x.sym, MODE_BID);
   double px = (d > 0) ? ask : bid;
   double riskPlan = MathAbs(x.entry - x.stop);
   if(riskPlan <= 0) return(false);
   double spread = ask - bid;
   if(spread > InpMaxSpreadR * riskPlan) { Print("SMC skip ", x.sym, ": spread too wide vs stop"); return(false); }
   if((px - x.entry) * d > InpMaxSlipR * riskPlan) { Print("SMC skip ", x.sym, ": price already ran past the confirmation close"); return(false); }
   if((px - x.stop) * d <= 0) return(false);
   double risk = MathAbs(px - x.stop);
   double tv = MarketInfo(x.sym, MODE_TICKVALUE), ts = MarketInfo(x.sym, MODE_TICKSIZE);
   double step = MarketInfo(x.sym, MODE_LOTSTEP), mn = MarketInfo(x.sym, MODE_MINLOT), mx = MarketInfo(x.sym, MODE_MAXLOT);
   if(tv <= 0 || ts <= 0 || step <= 0) return(false);
   double money = AccountEquity() * InpRiskPercent / 100.0;
   double lots = MathFloor(money / (risk / ts * tv) / step) * step;
   if(lots < mn) { Print("SMC skip ", x.sym, ": lot size below broker minimum"); return(false); }
   lots = MathMin(mx, lots);
   int dg = (int)MarketInfo(x.sym, MODE_DIGITS);
   double sl = NormalizeDouble(x.stop, dg);
   double tp = (x.target > 0) ? NormalizeDouble(x.target, dg) : 0;
   double minDist = MarketInfo(x.sym, MODE_STOPLEVEL) * MarketInfo(x.sym, MODE_POINT);
   if(tp > 0 && MathAbs(tp - px) < minDist) tp = 0;
   string cmt = StringFormat("SMC %s %s", x.grade, x.held >= 2 ? "**" : "");
   int type = (d > 0) ? OP_BUY : OP_SELL;
   int tk = OrderSend(x.sym, type, lots, px, InpSlippage * 10, sl, tp, cmt, InpMagic, 0, d > 0 ? clrDodgerBlue : clrTomato);
   if(tk < 0)
     {
      // some brokers want stops added after the fill
      int err = GetLastError();
      if(err == 130 || err == 4107)
        {
         RefreshRates();
         px = (d > 0) ? MarketInfo(x.sym, MODE_ASK) : MarketInfo(x.sym, MODE_BID);
         tk = OrderSend(x.sym, type, lots, px, InpSlippage * 10, 0, 0, cmt, InpMagic, 0, d > 0 ? clrDodgerBlue : clrTomato);
         if(tk >= 0 && OrderSelect(tk, SELECT_BY_TICKET))
           {
            if(!OrderModify(tk, OrderOpenPrice(), sl, tp, 0))
              {
               Print("SMC: could not set the stop on ", x.sym, " - closing it");
               if(!OrderClose(tk, lots, (d > 0) ? MarketInfo(x.sym, MODE_BID) : MarketInfo(x.sym, MODE_ASK), InpSlippage * 10)) Print("SMC: close failed ", GetLastError());
               return(false);
              }
           }
        }
      if(tk < 0) { Print("SMC order failed on ", x.sym, " error ", err); return(false); }
     }
   Notify(StringFormat("SMC TRADE %s %s %.2f lots @ %s, stop %s, target %s (%s-grade, %s)", d > 0 ? "BUY" : "SELL", x.sym, lots,
                       DoubleToString(px, dg), DoubleToString(sl, dg), tp > 0 ? DoubleToString(tp, dg) : "none (5-day exit)", x.grade, x.confKind));
   return(true);
  }

//+------------------------------------------------------------------+
//| panel                                                            |
//+------------------------------------------------------------------+
string StatusText(int s)
  {
   switch(s)
     {
      case ST_CONF:    return("CONFIRMED");
      case ST_ZONE:    return("IN 15m OB");
      case ST_CHOCH:   return("WAIT 15m OB");
      case ST_WAIT:    return("WAIT CHoCH");
      case ST_NEAR:    return("NEAR 4H OB");
      case ST_SKIP:    return("SKIP");
      case ST_RAN:     return("RAN");
      case ST_FAILED:  return("FAILED");
      case ST_FLIPPED: return("FLIPPED");
      case ST_INVALID: return("INVALID");
      case ST_LOADING: return("loading");
     }
   return("-");
  }

color StatusColor(int s)
  {
   switch(s)
     {
      case ST_CONF:  return(clrLime);
      case ST_ZONE:  return(clrGold);
      case ST_CHOCH: return(C'180,140,255');
      case ST_WAIT:  return(clrSilver);
      case ST_NEAR:  return(clrGray);
      case ST_SKIP:  return(C'220,90,90');
     }
   return(C'110,110,110');
  }

void Label(string name, int x, int y, string text, color clr, int size, string tip = "")
  {
   if(ObjectFind(0, name) < 0)
     {
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
      ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
      ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
     }
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, tip);
  }

void DrawPanel()
  {
   ObjectsDeleteAll(0, PFX + "R");
   ObjectsDeleteAll(0, PFX + "H");
   int fs = InpFontSize, rowH = fs * 2 + 2;
   int cols[] = {0, 70, 112, 200, 222, 262, 330, 398, 466, 502, 548};
   int x0 = InpPanelX + 8, y = InpPanelY + 6;

   int shown = 0, nA = 0, nConf = 0, nZone = 0;
   for(int i = 0; i < ArraySize(g_res); i++)
     {
      if(g_res[i].status <= ST_CHOCH && g_res[i].grade == "A") nA++;
      if(g_res[i].status == ST_CONF) nConf++;
      if(g_res[i].status == ST_ZONE) nZone++;
      if(g_res[i].status <= ST_NEAR || g_res[i].status == ST_LOADING || InpShowDead) shown++;
     }
   int w = 900, h = (shown + 5) * rowH + 10;
   string bg = PFX + "HBG";
   ObjectCreate(0, bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, bg, OBJPROP_XDISTANCE, InpPanelX);
   ObjectSetInteger(0, bg, OBJPROP_YDISTANCE, InpPanelY);
   ObjectSetInteger(0, bg, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, bg, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, bg, OBJPROP_BGCOLOR, C'18,20,26');
   ObjectSetInteger(0, bg, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, bg, OBJPROP_COLOR, C'60,64,76');
   ObjectSetInteger(0, bg, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, bg, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, bg, OBJPROP_BACK, false);

   Label(PFX + "Htitle", x0, y, StringFormat("SMC 4H/15m  |  %d A-grade live  |  %d confirmed  |  %d in 15m OB  |  GMT%+d  |  %s", nA, nConf, nZone, g_gmt, TimeToString(TimeLocal(), TIME_MINUTES)), clrWhite, fs + 1);
   y += rowH;
   Label(PFX + "Hacct", x0, y, StringFormat("AUTO-TRADE %s  |  risk %.2f%%  |  open %d/%d  |  %s  |  stops: day -%.1f%%, total -%.1f%%, target +%.1f%%",
                                            InpAutoTrade ? "ON" : "OFF", InpRiskPercent, OpenCount(), InpMaxOpen, g_guardMsg, InpDailyStopPct, InpMaxDDStopPct, InpTargetPct),
         (g_haltDay || g_haltAll) ? clrTomato : g_targetHit ? clrLime : clrGold, fs);
   y += rowH + 4;
   string hd[] = {"PAIR", "DIR", "STATUS", "GR", "OB", "ENTRY", "STOP", "TARGET", "R:R", "PIPS", "NEXT MOVE"};
   for(int c = 0; c < 11; c++) Label(PFX + "Hc" + IntegerToString(c), x0 + cols[c], y, hd[c], C'140,145,160', fs);
   y += rowH;

   int row = 0;
   ArrayResize(g_rowSym, 0);
   for(int i = 0; i < ArraySize(g_res); i++)
     {
      Setup x = g_res[i];
      if(!(x.status <= ST_NEAR || x.status == ST_LOADING || InpShowDead)) continue;
      color sc = StatusColor(x.status);
      string pre = PFX + "R" + IntegerToString(row) + "_";
      ArrayResize(g_rowSym, row + 1); g_rowSym[row] = x.sym;
      string stars = (x.held >= 2) ? "**" : (x.held == 1) ? "*" : "-";
      bool lv = (x.status <= ST_CHOCH);
      Label(pre + "0", x0 + cols[0], y, x.sym, clrWhite, fs, x.sym);
      Label(pre + "1", x0 + cols[1], y, x.d > 0 ? "BUY" : x.d < 0 ? "SELL" : "", x.d > 0 ? clrDodgerBlue : C'255,110,110', fs, x.sym);
      Label(pre + "2", x0 + cols[2], y, StatusText(x.status), sc, fs, x.sym);
      Label(pre + "3", x0 + cols[3], y, x.grade, x.grade == "A" ? clrLime : x.grade == "B" ? clrGold : clrGray, fs, "daily " + x.daily);
      Label(pre + "4", x0 + cols[4], y, stars, x.held >= 2 ? clrLime : clrSilver, fs, "4H OB held " + IntegerToString(x.held) + "x before, depth " + IntegerToString(x.depth) + "%");
      Label(pre + "5", x0 + cols[5], y, lv ? DoubleToString(x.entry, x.dp) : "", clrWhite, fs, x.sym);
      Label(pre + "6", x0 + cols[6], y, lv ? DoubleToString(x.stop, x.dp) : "", C'255,110,110', fs, x.sym);
      Label(pre + "7", x0 + cols[7], y, lv && x.target > 0 ? DoubleToString(x.target, x.dp) : "", clrLime, fs, x.tgtSrc);
      Label(pre + "8", x0 + cols[8], y, lv && x.rr > 0 ? DoubleToString(x.rr, 1) : "", clrWhite, fs, x.sym);
      Label(pre + "9", x0 + cols[9], y, lv ? DoubleToString(x.riskPips, 0) : "", clrSilver, fs, lv ? StringFormat("%.2f lots at %.1f%% risk", x.lots, InpRiskPercent) : "");
      Label(pre + "10", x0 + cols[10], y, x.next, sc, fs, x.next);
      y += rowH;
      row++;
     }
   Label(PFX + "Hfoot", x0, y + 2, "Trades CONFIRMED A-grade setups on the candle that just closed. Click a pair to open its 15m chart. Test on DEMO first.", C'110,115,130', fs);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| levels on this chart                                             |
//+------------------------------------------------------------------+
void Box(string name, datetime t1, double p1, datetime t2, double p2, color clr)
  {
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2);
   ObjectMove(0, name, 0, t1, p1);
   ObjectMove(0, name, 1, t2, p2);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
  }

void HLine(string name, double p, color clr, int style, string txt)
  {
   if(ObjectFind(0, name) < 0) ObjectCreate(0, name, OBJ_HLINE, 0, 0, p);
   ObjectMove(0, name, 0, 0, p);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetString(0, name, OBJPROP_TEXT, txt);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, txt);
  }

void DrawLevels()
  {
   ObjectsDeleteAll(0, PFX + "L");
   for(int i = 0; i < ArraySize(g_res); i++)
     {
      Setup x = g_res[i];
      if(x.sym != Symbol() || x.d == 0) continue;
      datetime tEnd = TimeCurrent() + 6 * 3600;
      Box(PFX + "L4H", x.zT, x.zHi, tEnd, x.zLo, x.d > 0 ? C'20,60,110' : C'110,30,40');
      if(x.hasOb15) Box(PFX + "L15", x.obT, x.obHi, tEnd, x.obLo, C'80,50,130');
      if(x.status <= ST_CHOCH || x.status == ST_RAN)
        {
         HLine(PFX + "Lent", x.entry, clrDodgerBlue, STYLE_SOLID, "entry " + DoubleToString(x.entry, x.dp));
         HLine(PFX + "Lstp", x.stop, C'255,90,90', STYLE_DASH, "stop " + DoubleToString(x.stop, x.dp));
         if(x.target > 0) HLine(PFX + "Ltgt", x.target, clrLime, STYLE_DASH, "target " + DoubleToString(x.target, x.dp) + " (" + x.tgtSrc + ")");
        }
      break;
     }
   ChartRedraw(0);
  }
//+------------------------------------------------------------------+
