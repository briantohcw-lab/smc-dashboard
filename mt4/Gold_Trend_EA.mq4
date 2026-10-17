//+------------------------------------------------------------------+
//|                                               Gold_Trend_EA.mq4  |
//|  Trend-following on XAUUSD, 1H, as tested (Sep 2025 - Aug 2026):  |
//|   ENTRY  SMA(9) crosses above EMA(30) on a closed 1H candle and    |
//|          that candle closes above both averages                    |
//|   FILTER only while gold's daily close is above its 200-day SMA    |
//|          (the "off switch" for when gold stops trending up)        |
//|   STOP   below the last confirmed 1H swing low (3 bars each side)  |
//|          minus 0.1 x ATR(14), never closer than 0.5 x ATR          |
//|   EXIT   SMA(9) closes back below EMA(30), or the stop, or 5 days  |
//|  Longs only by default. One position at a time.                    |
//|  Attach to ONE XAUUSD chart (any timeframe; it reads 1H and D1).   |
//|  Works in the MT4 Strategy Tester for long-history backtests.      |
//+------------------------------------------------------------------+
#property copyright "Brian Toh / built with Claude"
#property version   "1.10"
#property strict

enum ENUM_SL_MODE
  {
   SL_PIVOT = 0,    // Below the last 1H swing low (- 0.1 ATR)
   SL_ATR   = 1     // N x ATR(14) from entry
  };
enum ENUM_TRAIL
  {
   TR_NONE = 0,     // No trailing stop
   TR_BE   = 1,     // Move stop to breakeven at +X R
   TR_STEP = 2,     // Step trail: lock in every +X R (stop = gained - 1R)
   TR_ATR  = 3      // ATR trail: N x ATR below the highest high since entry
  };

enum ENUM_LOT_MODE
  {
   LOT_RISK  = 0,   // Risk % of equity (lot size from the stop distance)
   LOT_FIXED = 1    // Fixed lot size
  };

//---- strategy
input int    InpFastSMA      = 9;       // Fast SMA period (1H)
input int    InpSlowEMA      = 30;      // Slow EMA period (1H)
input int    InpPivotBars    = 3;       // Swing-low pivot: bars on each side
input bool   InpRegimeFilter = true;    // Only trade with the daily trend (daily close vs SMA below)
input int    InpRegimeSMA    = 200;     // Daily SMA period for the trend filter
input bool   InpAllowShorts  = false;   // Also take shorts (tested flat-to-losing; off by default)
input int    InpMaxHoldHours = 120;     // Close after N hours (backtest: 5 days)
input double InpMaxSpreadR   = 0.15;    // Skip if spread > this fraction of the stop distance
//---- stop loss / take profit / trailing
input ENUM_SL_MODE InpSLMode = SL_PIVOT;  // Stop-loss mode
input double InpSL_ATR       = 1.5;     // SL = N x ATR (SL mode = ATR)
input double InpTP_R         = 0;       // Take profit in R (0 = none)
input bool   InpExitOnCross  = true;    // Exit when SMA crosses back below EMA (1H close)
input ENUM_TRAIL InpTrailMode = TR_NONE; // Trailing stop mode
input double InpBE_R         = 1.0;     // Breakeven trigger in R (trail = Breakeven)
input double InpStep_R       = 0.5;     // Step size in R (trail = Step)
input double InpTrail_ATR    = 3.0;     // N x ATR below the high (trail = ATR)
//---- size
input ENUM_LOT_MODE InpLotMode = LOT_RISK; // Lot size mode
input double InpRiskPercent  = 0.5;     // Risk % per trade (lot mode = Risk %)
input double InpFixedLots    = 0.01;    // Fixed lots per trade (lot mode = Fixed)
//---- account guards
input bool   InpPropGuards   = false;   // Prop-firm guards (daily stop / max drawdown / profit target)
input double InpAccountSize  = 100000;  // Starting balance for the guards
input double InpDailyStopPct = 3.0;     // Stop for the day at -N% (0 = off)
input double InpMaxDDStopPct = 8.0;     // Stop for good at -N% from the starting balance (0 = off)
input double InpTargetPct    = 0;       // Stop opening trades at +N% (0 = off)
//---- misc
input int    InpMagic        = 26201;   // Magic number
input int    InpSlippage     = 3;       // Max slippage in pips
input bool   InpPush         = true;    // Push notification to the MT4 phone app
input bool   InpPopup        = false;   // Pop-up alert on the terminal

datetime g_lastBar = 0;
double   g_dayStartEq = 0;
int      g_dayKey = -1;
bool     g_haltDay = false, g_haltAll = false, g_targetHit = false;
string   g_last = "waiting for the next 1H close";

//+------------------------------------------------------------------+
int OnInit()
  {
   if(StringFind(Symbol(), "XAU") < 0) Print("Gold_Trend_EA: this chart is ", Symbol(), " - the rules were tested on XAUUSD only.");
   Guard();
   Panel();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason) { Comment(""); ObjectDelete(0, "GT_stop"); }

void OnTick()
  {
   Guard();
   ManageTrail();
   datetime t = iTime(NULL, PERIOD_H1, 0);
   if(t != g_lastBar && t > 0)
     {
      g_lastBar = t;
      OnNewBar();
     }
   Panel();
  }

//+------------------------------------------------------------------+
double Fast(int s) { return iMA(NULL, PERIOD_H1, InpFastSMA, 0, MODE_SMA, PRICE_CLOSE, s); }
double Slow(int s) { return iMA(NULL, PERIOD_H1, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE, s); }

// daily trend: +1 up, -1 down, 0 unknown (filter off -> both allowed)
int Regime()
  {
   if(!InpRegimeFilter) return(2);
   double c = iClose(NULL, PERIOD_D1, 1);
   double m = iMA(NULL, PERIOD_D1, InpRegimeSMA, 0, MODE_SMA, PRICE_CLOSE, 1);
   if(c <= 0 || m <= 0) return(0);
   return(c > m ? 1 : -1);
  }

// most recent confirmed 1H swing low below 'below' (swing high above 'above' for shorts)
double Pivot(int d, double ref)
  {
   int k = InpPivotBars;
   for(int j = k + 1; j < 400; j++)
     {
      double pj = (d > 0) ? iLow(NULL, PERIOD_H1, j) : iHigh(NULL, PERIOD_H1, j);
      if(pj <= 0) return(0);
      bool ok = true;
      for(int m = j - k; m <= j + k; m++)
        {
         if(m == j) continue;
         double pm = (d > 0) ? iLow(NULL, PERIOD_H1, m) : iHigh(NULL, PERIOD_H1, m);
         if((d > 0 && pm <= pj) || (d < 0 && pm >= pj)) { ok = false; break; }
        }
      if(ok && ((d > 0 && pj < ref) || (d < 0 && pj > ref))) return(pj);
     }
   return(0);
  }

int MyPosition()
  {
   for(int i = OrdersTotal() - 1; i >= 0; i--)
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && OrderMagicNumber() == InpMagic && OrderSymbol() == Symbol() && OrderType() <= OP_SELL)
         return(OrderTicket());
   return(-1);
  }

void Notify(string msg)
  {
   if(InpPopup) Alert(msg);
   if(InpPush) SendNotification(msg);
   Print(msg);
  }

bool CloseTicket(int tk, string why)
  {
   if(!OrderSelect(tk, SELECT_BY_TICKET)) return(false);
   RefreshRates();
   double px = (OrderType() == OP_BUY) ? Bid : Ask;
   bool ok = OrderClose(tk, OrderLots(), px, InpSlippage * 10, clrOrange);
   if(ok) Notify(StringFormat("GOLD TREND: closed %s %.2f lots (%s), P/L %.2f", OrderType() == OP_BUY ? "BUY" : "SELL", OrderLots(), why, OrderProfit() + OrderSwap() + OrderCommission()));
   else Print("Gold_Trend_EA close failed ", GetLastError());
   return(ok);
  }

//+------------------------------------------------------------------+
void OnNewBar()
  {
   double f1 = Fast(1), f2 = Fast(2), s1 = Slow(1), s2 = Slow(2), c1 = iClose(NULL, PERIOD_H1, 1);
   double atr = iATR(NULL, PERIOD_H1, 14, 1);
   if(f1 <= 0 || s1 <= 0 || atr <= 0) return;

   // ---- manage the open position: trend exit, time exit
   int tk = MyPosition();
   if(tk >= 0 && OrderSelect(tk, SELECT_BY_TICKET))
     {
      bool isBuy = (OrderType() == OP_BUY);
      if(InpExitOnCross && ((isBuy && f1 < s1) || (!isBuy && f1 > s1))) { CloseTicket(tk, "SMA crossed back - trend exit"); g_last = "trend exit"; }
      else if(TimeCurrent() - OrderOpenTime() >= InpMaxHoldHours * 3600) { CloseTicket(tk, "max hold time"); g_last = "time exit"; }
      return;     // one position at a time
     }
   if(g_haltDay || g_haltAll || g_targetHit) { g_last = "guards: no new trades"; return; }

   // ---- entries
   int rg = Regime();
   int d = 0;
   if(f2 <= s2 && f1 > s1 && c1 > MathMax(f1, s1) && (rg == 1 || rg == 2)) d = 1;
   if(InpAllowShorts && f2 >= s2 && f1 < s1 && c1 < MathMin(f1, s1) && (rg == -1 || rg == 2)) d = -1;
   if(d == 0)
     {
      if(f2 <= s2 && f1 > s1 && rg == -1) g_last = "bull cross skipped: daily trend is down";
      return;
     }
   RefreshRates();
   double px = (d > 0) ? Ask : Bid;
   double stop;
   if(InpSLMode == SL_ATR) stop = px - d * InpSL_ATR * atr;
   else
     {
      double piv = Pivot(d, px);
      if(piv <= 0) { g_last = "cross, but no swing level found for the stop"; return; }
      stop = piv - d * 0.1 * atr;
      if((px - stop) * d < 0.5 * atr) stop = px - d * 0.5 * atr;
     }
   double risk = (px - stop) * d;
   if(Ask - Bid > InpMaxSpreadR * risk) { g_last = "cross skipped: spread too wide vs stop"; return; }

   double tv = MarketInfo(Symbol(), MODE_TICKVALUE), ts = MarketInfo(Symbol(), MODE_TICKSIZE);
   double step = MarketInfo(Symbol(), MODE_LOTSTEP), mn = MarketInfo(Symbol(), MODE_MINLOT), mx = MarketInfo(Symbol(), MODE_MAXLOT);
   if(tv <= 0 || ts <= 0 || step <= 0) return;
   double lots;
   if(InpLotMode == LOT_FIXED) lots = MathMax(mn, MathMin(mx, MathFloor(InpFixedLots / step + 1e-9) * step));
   else
     {
      lots = MathFloor(AccountEquity() * InpRiskPercent / 100.0 / (risk / ts * tv) / step) * step;
      if(lots < mn) { g_last = "cross skipped: lot size below broker minimum"; return; }
      lots = MathMin(mx, lots);
     }
   double sl = NormalizeDouble(stop, Digits);
   double tp = (InpTP_R > 0) ? NormalizeDouble(px + d * InpTP_R * risk, Digits) : 0;
   int type = (d > 0) ? OP_BUY : OP_SELL;
   int t = OrderSend(Symbol(), type, lots, px, InpSlippage * 10, sl, tp, "GoldTrend", InpMagic, 0, d > 0 ? clrDodgerBlue : clrTomato);
   if(t < 0)
     {
      int err = GetLastError();
      if(err == 130)
        {
         RefreshRates(); px = (d > 0) ? Ask : Bid;
         t = OrderSend(Symbol(), type, lots, px, InpSlippage * 10, 0, 0, "GoldTrend", InpMagic, 0, d > 0 ? clrDodgerBlue : clrTomato);
         if(t >= 0 && OrderSelect(t, SELECT_BY_TICKET) && !OrderModify(t, OrderOpenPrice(), sl, tp, 0)) { Print("Gold_Trend_EA: could not set stop - closing"); CloseTicket(t, "no stop"); return; }
        }
      if(t < 0) { Print("Gold_Trend_EA order failed ", err); g_last = "order failed, error " + IntegerToString(err); return; }
     }
   GlobalVariableSet("GT_R_" + IntegerToString(t), risk);   // the initial 1R, for breakeven / step trailing
   g_last = StringFormat("%s %.2f lots @ %s, stop %s, TP %s", d > 0 ? "BUY" : "SELL", lots, DoubleToString(px, Digits), DoubleToString(sl, Digits), tp > 0 ? DoubleToString(tp, Digits) : "none");
   Notify(StringFormat("GOLD TREND: %s %.2f lots @ %s, stop %s (%.1f risk), exit when SMA%d crosses back below EMA%d", d > 0 ? "BUY" : "SELL", lots,
                       DoubleToString(px, Digits), DoubleToString(sl, Digits), risk, InpFastSMA, InpSlowEMA));
  }


//+------------------------------------------------------------------+
//| trailing stop: breakeven / step / ATR                            |
//+------------------------------------------------------------------+
void ManageTrail()
  {
   if(InpTrailMode == TR_NONE) return;
   int tk = MyPosition();
   if(tk < 0 || !OrderSelect(tk, SELECT_BY_TICKET)) return;
   int d = (OrderType() == OP_BUY) ? 1 : -1;
   double open = OrderOpenPrice(), cur = OrderStopLoss();
   string gv = "GT_R_" + IntegerToString(tk);
   double R = GlobalVariableCheck(gv) ? GlobalVariableGet(gv) : MathAbs(open - cur);
   if(R <= 0) return;
   // best price since entry, from 1H highs/lows plus the current price
   int sh = iBarShift(NULL, PERIOD_H1, OrderOpenTime());
   double best;
   if(d > 0) { best = iHigh(NULL, PERIOD_H1, iHighest(NULL, PERIOD_H1, MODE_HIGH, sh + 1, 0)); best = MathMax(best, Bid); }
   else      { best = iLow(NULL, PERIOD_H1, iLowest(NULL, PERIOD_H1, MODE_LOW, sh + 1, 0));   best = MathMin(best, Ask); }
   double mfeR = (best - open) * d / R;
   double ns = cur;
   if(InpTrailMode == TR_BE && mfeR >= InpBE_R) ns = open;
   if(InpTrailMode == TR_STEP && InpStep_R > 0)
     {
      double gained = MathFloor(mfeR / InpStep_R) * InpStep_R;
      if(gained >= 1.0) ns = open + d * (gained - 1.0) * R;
     }
   if(InpTrailMode == TR_ATR)
     {
      double atr = iATR(NULL, PERIOD_H1, 14, 1);
      if(atr > 0) ns = best - d * InpTrail_ATR * atr;
     }
   ns = NormalizeDouble(ns, Digits);
   if((ns - cur) * d <= Point) return;                         // only ever tighten
   double px = (d > 0) ? Bid : Ask;
   double minDist = MarketInfo(Symbol(), MODE_STOPLEVEL) * Point;
   if((px - ns) * d <= minDist) return;                        // too close to price for the broker
   if(OrderModify(tk, open, ns, OrderTakeProfit(), 0, clrGold))
      g_last = StringFormat("trailing stop moved to %s (+%.1fR best)", DoubleToString(ns, Digits), mfeR);
  }

//+------------------------------------------------------------------+
void Guard()
  {
   int key = TimeDayOfYear(TimeCurrent()) + 1000 * TimeYear(TimeCurrent());
   if(key != g_dayKey)
     {
      g_dayKey = key;
      string gv = "GT_DAY_" + IntegerToString(InpMagic) + "_" + IntegerToString(key);
      if(GlobalVariableCheck(gv)) g_dayStartEq = GlobalVariableGet(gv);
      else { g_dayStartEq = MathMax(AccountBalance(), AccountEquity()); GlobalVariableSet(gv, g_dayStartEq); }
      g_haltDay = false;
     }
   if(!InpPropGuards) return;
   double eq = AccountEquity();
   int tk = MyPosition();
   if(InpDailyStopPct > 0 && !g_haltDay && eq <= g_dayStartEq * (1 - InpDailyStopPct / 100.0))
     { g_haltDay = true; if(tk >= 0) CloseTicket(tk, "daily stop"); Notify("GOLD TREND: daily stop hit - no more trades today"); }
   if(InpMaxDDStopPct > 0 && !g_haltAll && eq <= InpAccountSize * (1 - InpMaxDDStopPct / 100.0))
     { g_haltAll = true; if(tk >= 0) CloseTicket(tk, "max drawdown stop"); Notify("GOLD TREND: max drawdown stop hit - trading stopped"); }
   if(InpTargetPct > 0 && !g_targetHit && eq >= InpAccountSize * (1 + InpTargetPct / 100.0))
     { g_targetHit = true; Notify("GOLD TREND: profit target reached - no new trades"); }
  }

void Panel()
  {
   double f1 = Fast(1), s1 = Slow(1);
   int rg = Regime();
   double dc = iClose(NULL, PERIOD_D1, 1), dm = iMA(NULL, PERIOD_D1, InpRegimeSMA, 0, MODE_SMA, PRICE_CLOSE, 1);
   string reg = (rg == 2) ? "filter OFF" : (rg == 1) ? StringFormat("UP (daily %s > SMA%d %s) - longs allowed", DoubleToString(dc, Digits), InpRegimeSMA, DoubleToString(dm, Digits))
                : (rg == -1) ? StringFormat("DOWN (daily %s < SMA%d %s) - standing aside", DoubleToString(dc, Digits), InpRegimeSMA, DoubleToString(dm, Digits)) : "loading D1 history...";
   string pos = "flat";
   int tk = MyPosition();
   if(tk >= 0 && OrderSelect(tk, SELECT_BY_TICKET))
     {
      pos = StringFormat("%s %.2f @ %s, stop %s, P/L %.2f, open %dh", OrderType() == OP_BUY ? "LONG" : "SHORT", OrderLots(), DoubleToString(OrderOpenPrice(), Digits),
                         DoubleToString(OrderStopLoss(), Digits), OrderProfit() + OrderSwap() + OrderCommission(), (int)((TimeCurrent() - OrderOpenTime()) / 3600));
      if(ObjectFind(0, "GT_stop") < 0) ObjectCreate(0, "GT_stop", OBJ_HLINE, 0, 0, OrderStopLoss());
      ObjectMove(0, "GT_stop", 0, 0, OrderStopLoss());
      ObjectSetInteger(0, "GT_stop", OBJPROP_COLOR, clrTomato);
      ObjectSetInteger(0, "GT_stop", OBJPROP_STYLE, STYLE_DASH);
     }
   else ObjectDelete(0, "GT_stop");
   string next;
   if(tk >= 0) next = InpExitOnCross ? StringFormat("exit when a 1H candle closes with SMA%d below EMA%d (or stop/TP)", InpFastSMA, InpSlowEMA) : "exit at stop / TP / trailing stop / time limit";
   else if(rg == -1) next = "stand aside until the daily close is back above the SMA";
   else next = (f1 > s1) ? StringFormat("SMA%d already above EMA%d - wait for the next fresh cross up", InpFastSMA, InpSlowEMA)
                         : StringFormat("wait for SMA%d to cross above EMA%d on a 1H close", InpFastSMA, InpSlowEMA);
   string lot = (InpLotMode == LOT_FIXED) ? StringFormat("fixed %.2f lots", InpFixedLots) : StringFormat("risk %.2f%%", InpRiskPercent);
   string tn[] = {"none", "breakeven", "step", "ATR"};
   lot += StringFormat("  |  SL %s  TP %s  trail %s%s", InpSLMode == SL_ATR ? DoubleToString(InpSL_ATR, 1) + "xATR" : "swing low",
                       InpTP_R > 0 ? DoubleToString(InpTP_R, 1) + "R" : "none", tn[(int)InpTrailMode], InpExitOnCross ? " + MA exit" : "");
   Comment(StringFormat("GOLD TREND EA  |  %s  |  %s%s\nDaily trend: %s\n1H: SMA%d %s  EMA%d %s  (%s)\nPosition: %s\nNext: %s\nLast: %s",
                        Symbol(), lot, InpPropGuards ? "  |  prop guards ON" : "", reg,
                        InpFastSMA, DoubleToString(f1, Digits), InpSlowEMA, DoubleToString(s1, Digits), f1 > s1 ? "bullish" : "bearish",
                        pos, next, g_last));
  }
//+------------------------------------------------------------------+
