//+------------------------------------------------------------------+
//|                                       DayTrader_Assistant_EA.mq4 |
//|  You pick the trades. This EA enforces the things that decide     |
//|  whether a day trader survives:                                   |
//|   - one-click BUY/SELL sized to a fixed % risk from a stop line   |
//|   - every trade gets a stop (manual trades without one get one)   |
//|   - daily / weekly loss lockout, max trades per day               |
//|   - trading only inside your session windows (Singapore time)     |
//|   - optional breakeven at +1R                                     |
//|   - automatic trade journal (CSV) with setup tag, R result and    |
//|     rule breaks, plus live stats from your own trades             |
//|  It never opens a trade on its own.                               |
//+------------------------------------------------------------------+
#property copyright "Brian Toh / built with Claude"
#property version   "1.00"
#property strict

//---- risk
input double InpRiskPct      = 0.5;     // Risk % of equity per trade
input double InpDefaultRR    = 2.0;     // Default take profit in R (0 = no TP)
input double InpStopATR      = 1.0;     // Default stop distance = N x ATR(14) of the chart timeframe
input bool   InpBreakevenOn  = true;    // Move stop to breakeven at +BE R
input double InpBE_R         = 1.0;     // Breakeven trigger in R
//---- discipline
input double InpDailyLossPct = 2.0;     // Lock trading for the day at -N% (closed + open)
input double InpWeeklyLossPct= 4.0;     // Lock trading for the week at -N%
input int    InpMaxTradesDay = 3;       // Max new trades per day
input bool   InpEnforceLock  = true;    // While locked, close any trade opened by hand
input bool   InpForceStop    = true;    // Give any trade without a stop a stop at N x ATR
input string InpSession1     = "15:00-19:00";   // Session window 1 (Singapore time, empty = off)
input string InpSession2     = "20:30-23:30";   // Session window 2 (Singapore time, empty = off)
input bool   InpBlockOutside = true;    // Disable the BUY/SELL buttons outside the windows
//---- journal / panel
input string InpJournalFile  = "TradeJournal.csv"; // Journal file (MQL4/Files)
input int    InpPanelX       = 12;      // Panel X
input int    InpPanelY       = 24;      // Panel Y
input bool   InpPush         = true;    // Push notifications to the MT4 phone app
input int    InpMagic        = 26301;   // Magic number for trades placed from the panel

string P = "DTA_";
string g_tags[] = {"SMC-A", "SMC-B", "Other"};
int    g_tag = 0;
int    g_known[];             // open tickets we are tracking
double g_dayStart = 0, g_weekStart = 0;
int    g_dayKey = -1, g_weekKey = -1;
bool   g_lockDay = false, g_lockWeek = false;
int    g_tradesToday = 0;
string g_msg = "";

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(1);
   ResetPeriods();
   SyncKnown(false);
   BuildPanel();
   return(INIT_SUCCEEDED);
  }
void OnDeinit(const int reason) { EventKillTimer(); ObjectsDeleteAll(0, P); }
void OnTick()  { Tick(); }
void OnTimer() { Tick(); }

void Tick()
  {
   ResetPeriods();
   CheckLocks();
   ManageOpen();
   SyncKnown(true);
   UpdatePanel();
  }

//+------------------------------------------------------------------+
//| time helpers (Singapore = GMT+8)                                  |
//+------------------------------------------------------------------+
datetime SGT() { return(TimeGMT() + 8 * 3600); }
int MinOfDay(datetime t) { return(TimeHour(t) * 60 + TimeMinute(t)); }
bool InWindow(string w, int m)
  {
   if(StringLen(w) < 11) return(false);
   int a = (int)StringToInteger(StringSubstr(w, 0, 2)) * 60 + (int)StringToInteger(StringSubstr(w, 3, 2));
   int b = (int)StringToInteger(StringSubstr(w, 6, 2)) * 60 + (int)StringToInteger(StringSubstr(w, 9, 2));
   return(a <= b ? (m >= a && m < b) : (m >= a || m < b));
  }
bool InSession()
  {
   if(InpSession1 == "" && InpSession2 == "") return(true);
   int m = MinOfDay(SGT());
   return(InWindow(InpSession1, m) || InWindow(InpSession2, m));
  }

//+------------------------------------------------------------------+
//| day / week start equity, persisted across restarts               |
//+------------------------------------------------------------------+
double Persist(string name, double val)
  {
   if(GlobalVariableCheck(name)) return(GlobalVariableGet(name));
   GlobalVariableSet(name, val);
   return(val);
  }
void ResetPeriods()
  {
   datetime now = TimeCurrent();
   int dk = TimeYear(now) * 1000 + TimeDayOfYear(now);
   int wk = (int)((now - 3 * 86400) / (7 * 86400));          // broker weeks (Mon-based)
   double base = MathMax(AccountBalance(), AccountEquity());
   if(dk != g_dayKey)
     {
      g_dayKey = dk;
      g_dayStart = Persist(P + "D" + IntegerToString(dk), base);
      g_lockDay = GlobalVariableCheck(P + "LD" + IntegerToString(dk));
      g_tradesToday = (int)(GlobalVariableCheck(P + "N" + IntegerToString(dk)) ? GlobalVariableGet(P + "N" + IntegerToString(dk)) : 0);
     }
   if(wk != g_weekKey)
     {
      g_weekKey = wk;
      g_weekStart = Persist(P + "W" + IntegerToString(wk), base);
      g_lockWeek = GlobalVariableCheck(P + "LW" + IntegerToString(wk));
     }
  }

void CloseAll(string why)
  {
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || OrderType() > OP_SELL) continue;
      RefreshRates();
      double px = (OrderType() == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID) : MarketInfo(OrderSymbol(), MODE_ASK);
      if(!OrderClose(OrderTicket(), OrderLots(), px, 30, clrOrange)) Print("DTA close failed ", GetLastError());
     }
   Print("DTA: closed all - ", why);
  }

void Notify(string m) { Alert(m); if(InpPush) SendNotification(m); g_msg = m; }

void CheckLocks()
  {
   double eq = AccountEquity();
   if(!g_lockDay && InpDailyLossPct > 0 && g_dayStart > 0 && eq <= g_dayStart * (1 - InpDailyLossPct / 100.0))
     {
      g_lockDay = true; GlobalVariableSet(P + "LD" + IntegerToString(g_dayKey), 1);
      CloseAll("daily loss limit");
      Notify(StringFormat("Daily loss limit -%.1f%% hit. Trading locked until tomorrow. Step away from the screen.", InpDailyLossPct));
     }
   if(!g_lockWeek && InpWeeklyLossPct > 0 && g_weekStart > 0 && eq <= g_weekStart * (1 - InpWeeklyLossPct / 100.0))
     {
      g_lockWeek = true; GlobalVariableSet(P + "LW" + IntegerToString(g_weekKey), 1);
      CloseAll("weekly loss limit");
      Notify(StringFormat("Weekly loss limit -%.1f%% hit. Trading locked until next week. Review your journal.", InpWeeklyLossPct));
     }
  }

bool Locked(string &why)
  {
   if(g_lockWeek) { why = "LOCKED: weekly loss limit"; return(true); }
   if(g_lockDay)  { why = "LOCKED: daily loss limit"; return(true); }
   if(g_tradesToday >= InpMaxTradesDay) { why = StringFormat("LOCKED: %d trades today (max)", g_tradesToday); return(true); }
   if(InpBlockOutside && !InSession()) { why = "outside your session windows"; return(true); }
   why = "";
   return(false);
  }

//+------------------------------------------------------------------+
//| open-trade management: forced stop, breakeven                    |
//+------------------------------------------------------------------+
double ATRof(string sym) { return(iATR(sym, Period(), 14, 1)); }

void ManageOpen()
  {
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || OrderType() > OP_SELL) continue;
      string sym = OrderSymbol();
      int d = (OrderType() == OP_BUY) ? 1 : -1;
      int dg = (int)MarketInfo(sym, MODE_DIGITS);
      double open = OrderOpenPrice(), sl = OrderStopLoss();
      string rk = P + "R" + IntegerToString(OrderTicket());
      if(sl == 0 && InpForceStop)
        {
         double a = ATRof(sym);
         if(a > 0)
           {
            double ns = NormalizeDouble(open - d * InpStopATR * a, dg);
            if(OrderModify(OrderTicket(), open, ns, OrderTakeProfit(), 0, clrRed))
              { Notify(StringFormat("%s had no stop - stop set at %s (%.1f x ATR).", sym, DoubleToString(ns, dg), InpStopATR)); GlobalVariableSet(P + "NS" + IntegerToString(OrderTicket()), 1); }
           }
         continue;
        }
      if(sl != 0 && !GlobalVariableCheck(rk)) GlobalVariableSet(rk, MathAbs(open - sl));   // remember the initial 1R
      if(InpBreakevenOn && sl != 0 && GlobalVariableCheck(rk))
        {
         double R = GlobalVariableGet(rk);
         double px = (d > 0) ? MarketInfo(sym, MODE_BID) : MarketInfo(sym, MODE_ASK);
         if(R > 0 && (px - open) * d >= InpBE_R * R && (sl - open) * d < 0)
            if(OrderModify(OrderTicket(), open, NormalizeDouble(open, dg), OrderTakeProfit(), 0, clrGold))
               g_msg = sym + " stop moved to breakeven";
        }
     }
  }

//+------------------------------------------------------------------+
//| track opens/closes -> journal                                     |
//+------------------------------------------------------------------+
bool Known(int tk) { for(int i = 0; i < ArraySize(g_known); i++) if(g_known[i] == tk) return(true); return(false); }

void SyncKnown(bool act)
  {
   int cur[]; ArrayResize(cur, 0);
   for(int i = 0; i < OrdersTotal(); i++)
     {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || OrderType() > OP_SELL) continue;
      int tk = OrderTicket();
      int n = ArraySize(cur); ArrayResize(cur, n + 1); cur[n] = tk;
      if(act && !Known(tk))
        {
         // a new trade: count it, tag it, flag rule breaks
         g_tradesToday++;
         GlobalVariableSet(P + "N" + IntegerToString(g_dayKey), g_tradesToday);
         GlobalVariableSet(P + "TAG" + IntegerToString(tk), g_tag);
         int flags = 0;
         if(!InSession()) flags |= 1;
         if(OrderMagicNumber() != InpMagic) flags |= 2;           // placed by hand, not from the panel
         bool lockedNow = (g_lockDay || g_lockWeek || g_tradesToday > InpMaxTradesDay);
         if(lockedNow) flags |= 4;
         GlobalVariableSet(P + "FL" + IntegerToString(tk), flags);
         if(lockedNow && InpEnforceLock)
           {
            RefreshRates();
            double px = (OrderType() == OP_BUY) ? MarketInfo(OrderSymbol(), MODE_BID) : MarketInfo(OrderSymbol(), MODE_ASK);
            if(OrderClose(tk, OrderLots(), px, 30, clrOrange)) Notify("Trade closed: you are locked for today (" + (g_lockWeek ? "weekly loss" : g_lockDay ? "daily loss" : "max trades") + ").");
           }
        }
     }
   if(act)
      for(int k = 0; k < ArraySize(g_known); k++)
        {
         bool still = false;
         for(int j = 0; j < ArraySize(cur); j++) if(cur[j] == g_known[k]) still = true;
         if(!still) Journal(g_known[k]);
        }
   ArrayResize(g_known, ArraySize(cur));
   for(int q = 0; q < ArraySize(cur); q++) g_known[q] = cur[q];
  }

void Journal(int tk)
  {
   if(!OrderSelect(tk, SELECT_BY_TICKET, MODE_HISTORY)) return;
   int d = (OrderType() == OP_BUY) ? 1 : -1;
   string rk = P + "R" + IntegerToString(tk);
   double R = GlobalVariableCheck(rk) ? GlobalVariableGet(rk) : 0;
   double rRes = (R > 0) ? (OrderClosePrice() - OrderOpenPrice()) * d / R : 0;
   int tag = (int)(GlobalVariableCheck(P + "TAG" + IntegerToString(tk)) ? GlobalVariableGet(P + "TAG" + IntegerToString(tk)) : 2);
   int fl = (int)(GlobalVariableCheck(P + "FL" + IntegerToString(tk)) ? GlobalVariableGet(P + "FL" + IntegerToString(tk)) : 0);
   if(GlobalVariableCheck(P + "NS" + IntegerToString(tk))) fl |= 8;
   string breaks = "";
   if(fl & 1) breaks += "outside-session ";
   if(fl & 4) breaks += "while-locked ";
   if(fl & 8) breaks += "no-stop ";
   string note = ObjectGetString(0, P + "note", OBJPROP_TEXT);
   StringReplace(note, ",", ";");
   bool isNew = !FileIsExist(InpJournalFile);
   int h = FileOpen(InpJournalFile, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE) { Print("DTA journal open failed ", GetLastError()); return; }
   FileSeek(h, 0, SEEK_END);
   if(isNew) FileWrite(h, "ticket", "open_time", "close_time", "symbol", "dir", "lots", "entry", "initial_stop_dist", "exit", "R", "profit", "setup", "rule_breaks", "note");
   FileWrite(h, tk, TimeToString(OrderOpenTime()), TimeToString(OrderCloseTime()), OrderSymbol(), d > 0 ? "BUY" : "SELL", DoubleToString(OrderLots(), 2),
             DoubleToString(OrderOpenPrice(), (int)MarketInfo(OrderSymbol(), MODE_DIGITS)), DoubleToString(R, (int)MarketInfo(OrderSymbol(), MODE_DIGITS)),
             DoubleToString(OrderClosePrice(), (int)MarketInfo(OrderSymbol(), MODE_DIGITS)), DoubleToString(rRes, 2),
             DoubleToString(OrderProfit() + OrderSwap() + OrderCommission(), 2), g_tags[(int)MathMin(tag, 2)], breaks, note);
   FileClose(h);
   // running stats
   double n = GlobalVariableCheck(P + "SN") ? GlobalVariableGet(P + "SN") : 0, sr = GlobalVariableCheck(P + "SR") ? GlobalVariableGet(P + "SR") : 0, sw = GlobalVariableCheck(P + "SW") ? GlobalVariableGet(P + "SW") : 0;
   GlobalVariableSet(P + "SN", n + 1); GlobalVariableSet(P + "SR", sr + rRes); GlobalVariableSet(P + "SW", sw + (rRes > 0 ? 1 : 0));
   g_msg = StringFormat("Journal: %s %s %+.2fR (%s)%s", OrderSymbol(), d > 0 ? "BUY" : "SELL", rRes, g_tags[(int)MathMin(tag, 2)], breaks != "" ? "  RULE BREAK: " + breaks : "");
  }

//+------------------------------------------------------------------+
//| order entry from the stop line                                   |
//+------------------------------------------------------------------+
void PlaceLines()
  {
   double a = ATRof(Symbol());
   if(a <= 0) return;
   double sl = Bid - InpStopATR * a;
   if(ObjectFind(0, P + "SL") < 0) ObjectCreate(0, P + "SL", OBJ_HLINE, 0, 0, sl);
   ObjectMove(0, P + "SL", 0, 0, sl);
   ObjectSetInteger(0, P + "SL", OBJPROP_COLOR, clrTomato);
   ObjectSetInteger(0, P + "SL", OBJPROP_STYLE, STYLE_DASH);
   ObjectSetInteger(0, P + "SL", OBJPROP_SELECTABLE, true);
   ObjectSetInteger(0, P + "SL", OBJPROP_SELECTED, true);
   ObjectSetString(0, P + "SL", OBJPROP_TEXT, "STOP - drag me");
   g_msg = "Drag the red STOP line to your structural stop, then press BUY (stop below price) or SELL (stop above).";
  }

void Place(int d)
  {
   string why;
   if(Locked(why)) { Notify("Not placed - " + why); return; }
   if(ObjectFind(0, P + "SL") < 0) { g_msg = "Press SET STOP first and drag the line to your stop."; return; }
   RefreshRates();
   double sl = ObjectGetDouble(0, P + "SL", OBJPROP_PRICE, 0);
   double px = (d > 0) ? Ask : Bid;
   if((px - sl) * d <= 0) { g_msg = d > 0 ? "For a BUY the stop line must be below price." : "For a SELL the stop line must be above price."; return; }
   double risk = MathAbs(px - sl);
   double minDist = MarketInfo(Symbol(), MODE_STOPLEVEL) * Point;
   if(risk <= minDist) { g_msg = "Stop too close for your broker."; return; }
   double tv = MarketInfo(Symbol(), MODE_TICKVALUE), ts = MarketInfo(Symbol(), MODE_TICKSIZE), step = MarketInfo(Symbol(), MODE_LOTSTEP);
   double mn = MarketInfo(Symbol(), MODE_MINLOT), mx = MarketInfo(Symbol(), MODE_MAXLOT);
   if(tv <= 0 || ts <= 0 || step <= 0) return;
   double lots = MathFloor(AccountEquity() * InpRiskPct / 100.0 / (risk / ts * tv) / step) * step;
   if(lots < mn) { g_msg = "Stop too wide for your risk % - minimum lot would risk more."; return; }
   lots = MathMin(mx, lots);
   double tp = (InpDefaultRR > 0) ? NormalizeDouble(px + d * InpDefaultRR * risk, Digits) : 0;
   int tk = OrderSend(Symbol(), d > 0 ? OP_BUY : OP_SELL, lots, px, 30, NormalizeDouble(sl, Digits), tp, "DTA " + g_tags[g_tag], InpMagic, 0, d > 0 ? clrDodgerBlue : clrTomato);
   if(tk < 0) { g_msg = "Order failed, error " + IntegerToString(GetLastError()); return; }
   GlobalVariableSet(P + "R" + IntegerToString(tk), risk);
   ObjectDelete(0, P + "SL");
   g_msg = StringFormat("%s %.2f lots, risk %.2f%% (%s), stop %s, TP %s", d > 0 ? "BUY" : "SELL", lots, InpRiskPct, g_tags[g_tag], DoubleToString(sl, Digits), tp > 0 ? DoubleToString(tp, Digits) : "none");
  }

//+------------------------------------------------------------------+
//| panel                                                            |
//+------------------------------------------------------------------+
void Btn(string n, int x, int y, int w, int h, string t, color bg)
  {
   if(ObjectFind(0, P + n) < 0) ObjectCreate(0, P + n, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, P + n, OBJPROP_XDISTANCE, x); ObjectSetInteger(0, P + n, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, P + n, OBJPROP_XSIZE, w); ObjectSetInteger(0, P + n, OBJPROP_YSIZE, h);
   ObjectSetString(0, P + n, OBJPROP_TEXT, t); ObjectSetInteger(0, P + n, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, P + n, OBJPROP_COLOR, clrWhite); ObjectSetInteger(0, P + n, OBJPROP_FONTSIZE, 9);
   ObjectSetInteger(0, P + n, OBJPROP_STATE, false); ObjectSetInteger(0, P + n, OBJPROP_HIDDEN, true);
  }
void Lbl(string n, int x, int y, string t, color c, int fs = 9)
  {
   if(ObjectFind(0, P + n) < 0) { ObjectCreate(0, P + n, OBJ_LABEL, 0, 0, 0); ObjectSetInteger(0, P + n, OBJPROP_HIDDEN, true); ObjectSetInteger(0, P + n, OBJPROP_SELECTABLE, false); }
   ObjectSetInteger(0, P + n, OBJPROP_XDISTANCE, x); ObjectSetInteger(0, P + n, OBJPROP_YDISTANCE, y);
   ObjectSetString(0, P + n, OBJPROP_TEXT, t); ObjectSetString(0, P + n, OBJPROP_FONT, "Consolas");
   ObjectSetInteger(0, P + n, OBJPROP_FONTSIZE, fs); ObjectSetInteger(0, P + n, OBJPROP_COLOR, c);
  }

void BuildPanel()
  {
   int x = InpPanelX, y = InpPanelY;
   ObjectCreate(0, P + "bg", OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, P + "bg", OBJPROP_XDISTANCE, x - 6); ObjectSetInteger(0, P + "bg", OBJPROP_YDISTANCE, y - 6);
   ObjectSetInteger(0, P + "bg", OBJPROP_XSIZE, 430); ObjectSetInteger(0, P + "bg", OBJPROP_YSIZE, 262);
   ObjectSetInteger(0, P + "bg", OBJPROP_BGCOLOR, C'18,20,26'); ObjectSetInteger(0, P + "bg", OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, P + "bg", OBJPROP_HIDDEN, true); ObjectSetInteger(0, P + "bg", OBJPROP_SELECTABLE, false);
   Btn("set", x, y + 120, 100, 26, "SET STOP", C'60,64,80');
   Btn("buy", x + 106, y + 120, 100, 26, "BUY", C'20,90,160');
   Btn("sell", x + 212, y + 120, 100, 26, "SELL", C'160,40,50');
   Btn("tag", x + 318, y + 120, 100, 26, "setup: " + g_tags[g_tag], C'70,50,110');
   if(ObjectFind(0, P + "note") < 0) ObjectCreate(0, P + "note", OBJ_EDIT, 0, 0, 0);
   ObjectSetInteger(0, P + "note", OBJPROP_XDISTANCE, x); ObjectSetInteger(0, P + "note", OBJPROP_YDISTANCE, y + 152);
   ObjectSetInteger(0, P + "note", OBJPROP_XSIZE, 418); ObjectSetInteger(0, P + "note", OBJPROP_YSIZE, 22);
   ObjectSetString(0, P + "note", OBJPROP_TEXT, "why this trade? (saved to the journal)");
   ObjectSetInteger(0, P + "note", OBJPROP_BGCOLOR, C'34,36,44'); ObjectSetInteger(0, P + "note", OBJPROP_COLOR, clrSilver);
   ObjectSetInteger(0, P + "note", OBJPROP_HIDDEN, true);
  }

void UpdatePanel()
  {
   int x = InpPanelX, y = InpPanelY;
   double eq = AccountEquity();
   double dayPct = g_dayStart > 0 ? 100 * (eq / g_dayStart - 1) : 0, wkPct = g_weekStart > 0 ? 100 * (eq / g_weekStart - 1) : 0;
   string why; bool lk = Locked(why);
   Lbl("t0", x, y, "DAY TRADER ASSISTANT", clrWhite, 10);
   Lbl("t1", x, y + 20, StringFormat("Today %+.2f%%  (lock at -%.1f%%)   Week %+.2f%%  (lock at -%.1f%%)", dayPct, InpDailyLossPct, wkPct, InpWeeklyLossPct),
       dayPct < 0 ? C'255,120,120' : clrLime);
   Lbl("t2", x, y + 38, StringFormat("Trades today %d / %d    Risk per trade %.2f%%    SGT %s", g_tradesToday, InpMaxTradesDay, InpRiskPct, TimeToString(SGT(), TIME_MINUTES)), clrSilver);
   Lbl("t3", x, y + 56, lk ? why : (InSession() ? "IN SESSION - trading allowed" : "outside session"), lk ? clrTomato : clrLime);
   // live lot size for the stop line
   string sz = "Press SET STOP, drag the line to your structural stop.";
   if(ObjectFind(0, P + "SL") >= 0)
     {
      double sl = ObjectGetDouble(0, P + "SL", OBJPROP_PRICE, 0);
      double risk = MathAbs(Bid - sl), tv = MarketInfo(Symbol(), MODE_TICKVALUE), ts = MarketInfo(Symbol(), MODE_TICKSIZE);
      if(risk > 0 && tv > 0 && ts > 0)
        {
         double lots = AccountEquity() * InpRiskPct / 100.0 / (risk / ts * tv);
         sz = StringFormat("Stop %s = %.1f pts away -> %.2f lots, TP %s", DoubleToString(sl, Digits), risk / Point, lots, InpDefaultRR > 0 ? DoubleToString(InpDefaultRR, 1) + "R" : "none");
        }
     }
   Lbl("t4", x, y + 74, sz, clrGold);
   double n = GlobalVariableCheck(P + "SN") ? GlobalVariableGet(P + "SN") : 0;
   double sr = GlobalVariableCheck(P + "SR") ? GlobalVariableGet(P + "SR") : 0, sw = GlobalVariableCheck(P + "SW") ? GlobalVariableGet(P + "SW") : 0;
   Lbl("t5", x, y + 94, n > 0 ? StringFormat("Your journal: %d trades, win %.0f%%, avg %+.2fR, total %+.1fR", (int)n, 100 * sw / n, sr / n, sr) : "Your journal: no closed trades yet", clrSilver);
   Lbl("t6", x, y + 182, g_msg, C'180,200,255', 8);
   Lbl("t7", x, y + 202, "Rules: trade only A setups, in session, with a structural stop.", C'110,115,130', 8);
   Lbl("t8", x, y + 220, "Journal file: MQL4/Files/" + InpJournalFile + "  (send it to Claude monthly)", C'110,115,130', 8);
   ObjectSetInteger(0, P + "buy", OBJPROP_BGCOLOR, lk ? C'50,55,65' : C'20,90,160');
   ObjectSetInteger(0, P + "sell", OBJPROP_BGCOLOR, lk ? C'50,55,65' : C'160,40,50');
   ChartRedraw(0);
  }

void OnChartEvent(const int id, const long &lp, const double &dp, const string &sp)
  {
   if(id == CHARTEVENT_OBJECT_CLICK)
     {
      if(sp == P + "set")  PlaceLines();
      if(sp == P + "buy")  Place(1);
      if(sp == P + "sell") Place(-1);
      if(sp == P + "tag")  { g_tag = (g_tag + 1) % 3; ObjectSetString(0, P + "tag", OBJPROP_TEXT, "setup: " + g_tags[g_tag]); }
      ObjectSetInteger(0, sp, OBJPROP_STATE, false);
      UpdatePanel();
     }
   if(id == CHARTEVENT_OBJECT_DRAG && sp == P + "SL") UpdatePanel();
  }
//+------------------------------------------------------------------+
