//+------------------------------------------------------------------+
//| NY_Open_Straddle_EA.mq5                                          |
//| NY-open straddle for XAUUSD: every new M5 (or M1) candle inside  |
//| the New York window, a buy stop goes above the previous candle   |
//| high and a sell stop below its low. When one fills the other is  |
//| deleted (OCO). Stop = other side of the candle. Trailing stop.   |
//| After a trade closes, the next candle places a new straddle.     |
//| Test with "Every tick based on real ticks" in the MT5 tester.    |
//+------------------------------------------------------------------+
#property version   "1.00"
#property description "NY-open M5 straddle with OCO stops, trailing stop and lot-size options."
#include <Trade/Trade.mqh>

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
input ulong           InpMagic            = 26103001;    // Magic number
input int             InpSlippagePts      = 30;          // Max slippage (points)
input string          InpComment          = "NYO";       // Order comment

CTrade   trade;
int      g_atr = INVALID_HANDLE;
datetime g_lastBar = 0;
bool     g_dayStopped = false;
int      g_stopDay = -1;

//+------------------------------------------------------------------+
datetime NYTime()            { return TimeCurrent() - InpServerMinusNY * 3600; }
int      NYMinuteOfDay()     { MqlDateTime d; TimeToStruct(NYTime(), d); return d.hour * 60 + d.min; }
int      NYDayOfYear()       { MqlDateTime d; TimeToStruct(NYTime(), d); return d.day_of_year; }
datetime NYDayStartServer()  { MqlDateTime d; TimeToStruct(NYTime(), d); return TimeCurrent() - (d.hour * 3600 + d.min * 60 + d.sec); }
int      StartMin()          { return InpStartHourNY * 60 + InpStartMinNY; }

bool TradingDay()
{
   MqlDateTime d; TimeToStruct(NYTime(), d);
   if(d.day_of_week == 0 || d.day_of_week == 6) return false;
   if(d.day_of_week == 5 && !InpTradeFriday) return false;
   return true;
}
bool InWindow()   { int m = NYMinuteOfDay(); return TradingDay() && m >= StartMin() && m < StartMin() + InpWindowMin; }
bool PastHold()   { int m = NYMinuteOfDay(); return m >= StartMin() + InpWindowMin + InpHoldAfterMin; }

double ATRv(int shift)
{
   double b[1];
   if(g_atr == INVALID_HANDLE || CopyBuffer(g_atr, 0, shift, 1, b) != 1) return 0;
   return b[0];
}
double Bid() { return SymbolInfoDouble(_Symbol, SYMBOL_BID); }
double Ask() { return SymbolInfoDouble(_Symbol, SYMBOL_ASK); }
double StopLvl()
{
   long a = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL), b = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   return (double)MathMax(a, b) * _Point;
}

//+------------------------------------------------------------------+
double NormLots(double l)
{
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), mx = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0) step = 0.01;
   l = MathFloor(l / step + 1e-9) * step;
   l = MathMin(l, MathMin(mx, InpMaxLots));
   if(l < mn) l = mn;
   return NormalizeDouble(l, 2);
}

double CalcLots(double riskDist)
{
   double lots = InpFixedLots, bal = AccountInfoDouble(ACCOUNT_BALANCE);
   if(InpLotMode == LOT_RISK_PCT)
   {
      double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS), ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(tv <= 0) tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      if(tv > 0 && ts > 0 && riskDist > 0)
         lots = (bal * InpRiskPct / 100.0) / (riskDist / ts * tv);
   }
   else if(InpLotMode == LOT_PER_BALANCE)
   {
      if(InpBalancePer001 > 0) lots = 0.01 * MathFloor(bal / InpBalancePer001);
   }
   return NormLots(lots);
}

//+------------------------------------------------------------------+
int CountPositions()
{
   int c = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t > 0 && PositionGetString(POSITION_SYMBOL) == _Symbol && (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic) c++;
   }
   return c;
}

void DeletePendings()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol || (ulong)OrderGetInteger(ORDER_MAGIC) != InpMagic) continue;
      ENUM_ORDER_TYPE ty = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(ty == ORDER_TYPE_BUY_STOP || ty == ORDER_TYPE_SELL_STOP)
         if(!trade.OrderDelete(t)) Print("Delete pending failed ", trade.ResultRetcode());
   }
}

void CloseAll()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      if(!trade.PositionClose(t)) Print("Close failed ", trade.ResultRetcode());
   }
   DeletePendings();
}

// trades filled today and today's P&L (closed + open), counted from the NY day start
void DayStats(int &trades, double &pnl)
{
   trades = 0; pnl = 0;
   datetime d0 = NYDayStartServer();
   if(HistorySelect(d0, TimeCurrent() + 60))
   {
      for(int i = HistoryDealsTotal() - 1; i >= 0; i--)
      {
         ulong t = HistoryDealGetTicket(i);
         if(t == 0) continue;
         if(HistoryDealGetString(t, DEAL_SYMBOL) != _Symbol || (ulong)HistoryDealGetInteger(t, DEAL_MAGIC) != InpMagic) continue;
         long entry = HistoryDealGetInteger(t, DEAL_ENTRY);
         if(entry == DEAL_ENTRY_IN) trades++;
         pnl += HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_SWAP) + HistoryDealGetDouble(t, DEAL_COMMISSION);
      }
   }
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      pnl += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
}

//+------------------------------------------------------------------+
void PlaceStraddle()
{
   double hi = iHigh(_Symbol, InpTF, 1), lo = iLow(_Symbol, InpTF, 1), atr = ATRv(1);
   if(atr <= 0 || hi <= lo) return;
   if(hi - lo < InpMinRangeATR * atr) return;
   double ask = Ask(), bid = Bid();
   if((ask - bid) / _Point > InpMaxSpreadPts) return;

   double buf = InpBufferATR * atr;
   double bs = NormalizeDouble(hi + buf, _Digits), ss = NormalizeDouble(lo - buf, _Digits);
   double risk = bs - ss, lvl = StopLvl();
   double lots = CalcLots(risk);

   if(bs > ask + lvl && risk > lvl)
   {
      double tp = InpTP_R > 0 ? NormalizeDouble(bs + InpTP_R * risk, _Digits) : 0;
      if(!trade.BuyStop(lots, bs, _Symbol, ss, tp, ORDER_TIME_GTC, 0, InpComment)) Print("BuyStop failed ", trade.ResultRetcode());
   }
   if(ss < bid - lvl && risk > lvl)
   {
      double tp = InpTP_R > 0 ? NormalizeDouble(ss - InpTP_R * risk, _Digits) : 0;
      if(!trade.SellStop(lots, ss, _Symbol, bs, tp, ORDER_TIME_GTC, 0, InpComment)) Print("SellStop failed ", trade.ResultRetcode());
   }
}

//+------------------------------------------------------------------+
void ManageTrail()
{
   if(InpTrailMode == TRAIL_NONE) return;
   double lvl = StopLvl(), atr = ATRv(1), buf = InpBufferATR * atr;
   if(atr <= 0) return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol || (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      double open = PositionGetDouble(POSITION_PRICE_OPEN), sl = PositionGetDouble(POSITION_SL), tp = PositionGetDouble(POSITION_TP);
      string gv = "NYO_R_" + IntegerToString((long)PositionGetInteger(POSITION_IDENTIFIER));
      if(!GlobalVariableCheck(gv)) GlobalVariableSet(gv, MathAbs(open - sl));
      double R = GlobalVariableGet(gv); if(R <= 0) continue;
      int sh = iBarShift(_Symbol, InpTF, (datetime)PositionGetInteger(POSITION_TIME));
      if(sh < 0) sh = 0;
      double newSL;
      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
      {
         double best = MathMax(iHigh(_Symbol, InpTF, iHighest(_Symbol, InpTF, MODE_HIGH, sh + 1, 0)), Bid());
         if(best - open < InpTrailStartR * R) continue;
         newSL = InpTrailMode == TRAIL_CANDLE ? iLow(_Symbol, InpTF, 1) - buf : best - InpTrailATR * atr;
         newSL = NormalizeDouble(newSL, _Digits);
         if(newSL > sl + _Point && newSL < Bid() - lvl)
            if(!trade.PositionModify(t, newSL, tp)) Print("Trail modify failed ", trade.ResultRetcode());
      }
      else
      {
         double best = MathMin(iLow(_Symbol, InpTF, iLowest(_Symbol, InpTF, MODE_LOW, sh + 1, 0)), Ask());
         if(open - best < InpTrailStartR * R) continue;
         newSL = InpTrailMode == TRAIL_CANDLE ? iHigh(_Symbol, InpTF, 1) + buf : best + InpTrailATR * atr;
         newSL = NormalizeDouble(newSL, _Digits);
         if((sl == 0 || newSL < sl - _Point) && newSL > Ask() + lvl)
            if(!trade.PositionModify(t, newSL, tp)) Print("Trail modify failed ", trade.ResultRetcode());
      }
   }
}

//+------------------------------------------------------------------+
int OnInit()
{
   if(InpWindowMin <= 0) { Print("Window must be > 0 minutes"); return INIT_PARAMETERS_INCORRECT; }
   g_atr = iATR(_Symbol, InpTF, InpATRPeriod);
   if(g_atr == INVALID_HANDLE) { Print("ATR handle failed"); return INIT_FAILED; }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippagePts);
   trade.SetTypeFillingBySymbol(_Symbol);
   g_lastBar = iTime(_Symbol, InpTF, 0);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_atr != INVALID_HANDLE) IndicatorRelease(g_atr);
   Comment("");
}

//+------------------------------------------------------------------+
void OnTick()
{
   int trades; double pnl;
   DayStats(trades, pnl);
   int today = NYDayOfYear();
   if(g_stopDay != today) { g_dayStopped = false; g_stopDay = today; }
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);

   if(!g_dayStopped && InpMaxDailyLossPct > 0 && pnl <= -bal * InpMaxDailyLossPct / 100.0)
   {
      g_dayStopped = true; CloseAll();
      Print("Daily loss limit hit: ", DoubleToString(pnl, 2));
   }

   if(CountPositions() > 0) DeletePendings();   // OCO: one side filled -> remove the other
   ManageTrail();

   if(PastHold()) CloseAll();
   if(!InWindow()) DeletePendings();

   datetime bt = iTime(_Symbol, InpTF, 0);
   if(bt != g_lastBar)
   {
      g_lastBar = bt;
      DeletePendings();                        // straddle is only valid for one candle
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
           InpTP_R > 0 ? DoubleToString(InpTP_R, 1) + "R" : "none", trades, pnl, AccountInfoString(ACCOUNT_CURRENCY)));
}
//+------------------------------------------------------------------+
