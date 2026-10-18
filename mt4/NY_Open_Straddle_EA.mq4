//+------------------------------------------------------------------+
//| NY_Open_Straddle_EA.mq4                                          |
//| NY-open straddle for XAUUSD: every new M5 (or M1) candle inside  |
//| the New York window, a buy stop goes above the previous candle   |
//| high and a sell stop below its low. When one fills the other is  |
//| deleted (OCO). Stop = other side of the candle. Trailing stop.   |
//| After a trade closes, the next candle places a new straddle.     |
//| Backtest (Sep 2025-Aug 2026, M5, 3 brokers): best default is     |
//| 08:30 NY + 90 min, trail behind previous candle after +0.5R.     |
//| Test in the Strategy Tester and on demo before real money.       |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "NY-open M5 straddle with OCO stops, trailing stop and lot-size options."

enum ENUM_LOTMODE   { LOT_FIXED = 0,       // Fixed lots
                      LOT_RISK_PCT = 1,    // % of balance risked per trade
                      LOT_PER_BALANCE = 2  // 0.01 lot per X balance
                    };
enum ENUM_TRAILMODE { TRAIL_CANDLE = 0,    // Behind previous candle low/high
                      TRAIL_ATR = 1,       // X * ATR behind best price
                      TRAIL_NONE = 2       // No trailing (SL/TP only)
                    };

input string          s1 = "===== Lot size =====";      // .
input ENUM_LOTMODE    InpLotMode          = LOT_FIXED;   // Lot mode
input double          InpFixedLots        = 0.01;        // Fixed lots (LOT_FIXED)
input double          InpRiskPct          = 0.5;         // Risk % of balance per trade (LOT_RISK_PCT)
input double          InpBalancePer001    = 1000;        // Balance per 0.01 lot (LOT_PER_BALANCE)
input double          InpMaxLots          = 1.0;         // Maximum lots per trade
input string          s2 = "===== Session (New York time) ====="; // .
input int             InpServerMinusNY    = 7;           // Server time minus New York time (hours). ICM/Tickmall/Eightcap = 7
input int             InpStartHourNY      = 8;           // Window start hour (NY)
input int             InpStartMinNY       = 30;          // Window start minute (NY)
input int             InpWindowMin        = 90;          // Minutes to keep placing new straddles
input int             InpHoldAfterMin     = 60;          // Close anything still open X minutes after the window
input bool            InpTradeFriday      = true;        // Trade on Fridays
input string          s3 = "===== Entry ====="; // .
input ENUM_TIMEFRAMES InpTF               = PERIOD_M5;   // Candle timeframe (M5 tested; M1 possible)
input double          InpBufferATR        = 0.05;        // Stop-order buffer beyond candle (x ATR)
input double          InpMinRangeATR      = 0.3;         // Skip candles smaller than X * ATR
input int             InpATRPeriod        = 14;          // ATR period
input int             InpMaxSpreadPts     = 60;          // Max spread in points (gold 2-digit: 60 = $0.60)
input int             InpMaxTradesPerDay  = 20;          // Max filled trades per day
input string          s4 = "===== Exit ====="; // .
input ENUM_TRAILMODE  InpTrailMode        = TRAIL_CANDLE;// Trailing mode
input double          InpTrailATR         = 1.0;         // Trail distance for TRAIL_ATR (x ATR)
input double          InpTrailStartR      = 0.5;         // Start trailing after profit of X R (0 = at once)
input double          InpTP_R             = 0;           // Take profit in R (0 = none)
input double          InpMaxDailyLossPct  = 3.0;         // Stop for the day after this loss % (0 = off)
input string          s5 = "===== Other ====="; // .
input int             InpMagic            = 26103001;    // Magic number
input int             InpSlippagePts      = 30;          // Max slippage (points)
input string          InpComment          = "NYO";       // Order comment

datetime g_lastBar = 0;
bool     g_dayStopped = false;
int      g_stopDay = -1;

//+------------------------------------------------------------------+
datetime NYTime()            { return TimeCurrent() - InpServerMinusNY * 3600; }
int      NYMinuteOfDay()     { datetime t = NYTime(); return TimeHour(t) * 60 + TimeMinute(t); }
int      NYDayOfYear()       { return TimeDayOfYear(NYTime()); }
datetime NYDayStartServer()  { datetime t = NYTime(); return TimeCurrent() - (TimeHour(t) * 3600 + TimeMinute(t) * 60 + TimeSeconds(t)); }
int      StartMin()          { return InpStartHourNY * 60 + InpStartMinNY; }

bool TradingDay()
{
   int dow = TimeDayOfWeek(NYTime());
   if(dow == 0 || dow == 6) return false;
   if(dow == 5 && !InpTradeFriday) return false;
   return true;
}
bool InWindow()   { int m = NYMinuteOfDay(); return TradingDay() && m >= StartMin() && m < StartMin() + InpWindowMin; }
bool PastHold()   { int m = NYMinuteOfDay(); return m >= StartMin() + InpWindowMin + InpHoldAfterMin; }

double ATRv(int shift) { return iATR(_Symbol, InpTF, InpATRPeriod, shift); }

//+------------------------------------------------------------------+
double NormLots(double l)
{
   double step = MarketInfo(_Symbol, MODE_LOTSTEP), mn = MarketInfo(_Symbol, MODE_MINLOT), mx = MarketInfo(_Symbol, MODE_MAXLOT);
   if(step <= 0) step = 0.01;
   l = MathFloor(l / step + 1e-9) * step;
   l = MathMin(l, MathMin(mx, InpMaxLots));
   if(l < mn) l = mn;
   return NormalizeDouble(l, 2);
}

double CalcLots(double riskDist)
{
   double lots = InpFixedLots;
   if(InpLotMode == LOT_RISK_PCT)
   {
      double tv = MarketInfo(_Symbol, MODE_TICKVALUE), ts = MarketInfo(_Symbol, MODE_TICKSIZE);
      if(tv > 0 && ts > 0 && riskDist > 0)
         lots = (AccountBalance() * InpRiskPct / 100.0) / (riskDist / ts * tv);
   }
   else if(InpLotMode == LOT_PER_BALANCE)
   {
      if(InpBalancePer001 > 0) lots = 0.01 * MathFloor(AccountBalance() / InpBalancePer001);
   }
   return NormLots(lots);
}

//+------------------------------------------------------------------+
int CountPositions()
{
   int c = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && OrderSymbol() == _Symbol && OrderMagicNumber() == InpMagic && OrderType() <= OP_SELL) c++;
   return c;
}

void DeletePendings()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && OrderSymbol() == _Symbol && OrderMagicNumber() == InpMagic && OrderType() > OP_SELL)
         if(!OrderDelete(OrderTicket())) Print("Delete pending failed ", GetLastError());
}

void CloseAll()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || OrderSymbol() != _Symbol || OrderMagicNumber() != InpMagic) continue;
      if(OrderType() == OP_BUY)       { if(!OrderClose(OrderTicket(), OrderLots(), Bid, InpSlippagePts, clrNONE)) Print("Close failed ", GetLastError()); }
      else if(OrderType() == OP_SELL) { if(!OrderClose(OrderTicket(), OrderLots(), Ask, InpSlippagePts, clrNONE)) Print("Close failed ", GetLastError()); }
      else if(!OrderDelete(OrderTicket())) Print("Delete failed ", GetLastError());
   }
}

// trades filled today and today's P&L (closed + open), counted from the NY day start
void DayStats(int &trades, double &pnl)
{
   trades = 0; pnl = 0;
   datetime d0 = NYDayStartServer();
   for(int i = OrdersHistoryTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY)) continue;
      if(OrderSymbol() != _Symbol || OrderMagicNumber() != InpMagic || OrderType() > OP_SELL) continue;
      if(OrderOpenTime() >= d0) trades++;
      if(OrderCloseTime() >= d0) pnl += OrderProfit() + OrderSwap() + OrderCommission();
   }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if(OrderSymbol() != _Symbol || OrderMagicNumber() != InpMagic || OrderType() > OP_SELL) continue;
      if(OrderOpenTime() >= d0) trades++;
      pnl += OrderProfit() + OrderSwap() + OrderCommission();
   }
}

//+------------------------------------------------------------------+
void PlaceStraddle()
{
   double hi = iHigh(_Symbol, InpTF, 1), lo = iLow(_Symbol, InpTF, 1), atr = ATRv(1);
   if(atr <= 0 || hi <= lo) return;
   if(hi - lo < InpMinRangeATR * atr) return;
   if((Ask - Bid) / _Point > InpMaxSpreadPts) return;

   double buf = InpBufferATR * atr;
   double bs = NormalizeDouble(hi + buf, _Digits), ss = NormalizeDouble(lo - buf, _Digits);
   double risk = bs - ss;
   double lvl = MathMax(MarketInfo(_Symbol, MODE_STOPLEVEL), MarketInfo(_Symbol, MODE_FREEZELEVEL)) * _Point;
   double lots = CalcLots(risk);

   // buy stop: SL at the sell-stop level
   if(bs > Ask + lvl && bs - ss > lvl)
   {
      double tp = InpTP_R > 0 ? NormalizeDouble(bs + InpTP_R * risk, _Digits) : 0;
      if(OrderSend(_Symbol, OP_BUYSTOP, lots, bs, InpSlippagePts, ss, tp, InpComment, InpMagic, 0, clrDodgerBlue) < 0)
         Print("BuyStop failed ", GetLastError());
   }
   if(ss < Bid - lvl && bs - ss > lvl)
   {
      double tp = InpTP_R > 0 ? NormalizeDouble(ss - InpTP_R * risk, _Digits) : 0;
      if(OrderSend(_Symbol, OP_SELLSTOP, lots, ss, InpSlippagePts, bs, tp, InpComment, InpMagic, 0, clrOrangeRed) < 0)
         Print("SellStop failed ", GetLastError());
   }
}

//+------------------------------------------------------------------+
void ManageTrail()
{
   if(InpTrailMode == TRAIL_NONE) return;
   double lvl = MathMax(MarketInfo(_Symbol, MODE_STOPLEVEL), MarketInfo(_Symbol, MODE_FREEZELEVEL)) * _Point;
   double atr = ATRv(1), buf = InpBufferATR * atr;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES) || OrderSymbol() != _Symbol || OrderMagicNumber() != InpMagic || OrderType() > OP_SELL) continue;
      string gv = "NYO_R_" + IntegerToString(OrderTicket());
      if(!GlobalVariableCheck(gv)) GlobalVariableSet(gv, MathAbs(OrderOpenPrice() - OrderStopLoss()));
      double R = GlobalVariableGet(gv); if(R <= 0) continue;
      int sh = iBarShift(_Symbol, InpTF, OrderOpenTime());
      if(sh < 0) sh = 0;
      double newSL = 0;
      if(OrderType() == OP_BUY)
      {
         double best = MathMax(iHigh(_Symbol, InpTF, iHighest(_Symbol, InpTF, MODE_HIGH, sh + 1, 0)), Bid);
         if(best - OrderOpenPrice() < InpTrailStartR * R) continue;
         newSL = InpTrailMode == TRAIL_CANDLE ? iLow(_Symbol, InpTF, 1) - buf : best - InpTrailATR * atr;
         newSL = NormalizeDouble(newSL, _Digits);
         if(newSL > OrderStopLoss() + _Point && newSL < Bid - lvl)
            if(!OrderModify(OrderTicket(), OrderOpenPrice(), newSL, OrderTakeProfit(), 0, clrNONE)) Print("Trail modify failed ", GetLastError());
      }
      else
      {
         double best = MathMin(iLow(_Symbol, InpTF, iLowest(_Symbol, InpTF, MODE_LOW, sh + 1, 0)), Ask);
         if(OrderOpenPrice() - best < InpTrailStartR * R) continue;
         newSL = InpTrailMode == TRAIL_CANDLE ? iHigh(_Symbol, InpTF, 1) + buf : best + InpTrailATR * atr;
         newSL = NormalizeDouble(newSL, _Digits);
         if((OrderStopLoss() == 0 || newSL < OrderStopLoss() - _Point) && newSL > Ask + lvl)
            if(!OrderModify(OrderTicket(), OrderOpenPrice(), newSL, OrderTakeProfit(), 0, clrNONE)) Print("Trail modify failed ", GetLastError());
      }
   }
}

//+------------------------------------------------------------------+
int OnInit()
{
   if(InpWindowMin <= 0) { Print("Window must be > 0 minutes"); return INIT_PARAMETERS_INCORRECT; }
   g_lastBar = iTime(_Symbol, InpTF, 0);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) { Comment(""); }

//+------------------------------------------------------------------+
void OnTick()
{
   int trades; double pnl;
   DayStats(trades, pnl);
   int today = NYDayOfYear();
   if(g_stopDay != today) { g_dayStopped = false; g_stopDay = today; }

   // daily loss stop
   if(!g_dayStopped && InpMaxDailyLossPct > 0 && pnl <= -AccountBalance() * InpMaxDailyLossPct / 100.0)
   {
      g_dayStopped = true; CloseAll();
      Print("Daily loss limit hit: ", DoubleToString(pnl, 2));
   }

   int pos = CountPositions();
   if(pos > 0) DeletePendings();            // OCO: one side filled -> remove the other
   ManageTrail();

   if(PastHold() && OrdersTotal() > 0) CloseAll();
   if(!InWindow()) DeletePendings();

   datetime bt = iTime(_Symbol, InpTF, 0);
   if(bt != g_lastBar)
   {
      g_lastBar = bt;
      DeletePendings();                     // straddle is only valid for one candle
      if(InWindow() && !g_dayStopped && CountPositions() == 0 && trades < InpMaxTradesPerDay)
         PlaceStraddle();
   }

   string mode = InpLotMode == LOT_FIXED ? "Fixed " + DoubleToString(InpFixedLots, 2) :
                 InpLotMode == LOT_RISK_PCT ? "Risk " + DoubleToString(InpRiskPct, 2) + "%" :
                 "0.01 per " + DoubleToString(InpBalancePer001, 0);
   Comment(StringFormat("NY Open Straddle EA\nNY time: %s | window %02d:%02d +%d min | %s\nLots: %s | Trail: %s | TP: %s\nToday: %d trades, P&L %.2f %s",
           TimeToString(NYTime(), TIME_MINUTES), InpStartHourNY, InpStartMinNY, InpWindowMin,
           g_dayStopped ? "STOPPED (daily loss)" : (InWindow() ? "ACTIVE" : "waiting"),
           mode, InpTrailMode == TRAIL_CANDLE ? "prev candle" : InpTrailMode == TRAIL_ATR ? DoubleToString(InpTrailATR, 1) + "x ATR" : "none",
           InpTP_R > 0 ? DoubleToString(InpTP_R, 1) + "R" : "none", trades, pnl, AccountCurrency()));
}
//+------------------------------------------------------------------+
