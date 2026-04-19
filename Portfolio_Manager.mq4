//+------------------------------------------------------------------+
//| Portfolio_Manager.mq4 — EMA 9/21 Cross + DynaPro Webhook Signals |
//| v4.0 — Polls precog-hl-web for DynaPro signals + own EMA logic   |
//+------------------------------------------------------------------+
#property copyright "CPM"
#property version   "4.10"
#property strict

extern double LotSize    = 0.01;
extern int    EMAFast    = 9;
extern int    EMASlow    = 21;
extern int    Timeframe  = PERIOD_H1;
extern int    MaxPos     = 10;
extern int    MagicNum   = 20260416;
extern int    Slippage   = 30;
extern string SymFilter  = ".a";
extern double TP_Pct     = 0.8;
extern bool   EMA_Exit   = false;
extern string SignalURL  = "https://precog-hl-web.onrender.com/mt4/signals";
extern int    PollSec    = 10;
extern bool   UseLocalEMAFilter = true;  // 2nd-gate webhook signals with local EMA (was always-on in v4.0)

datetime lastBarTime[256];
string   syms[256];
int      symCount = 0;
datetime lastPoll = 0;

//+------------------------------------------------------------------+
void LoadSymbols() {
   symCount = 0;
   int total = SymbolsTotal(true);
   int flen = StringLen(SymFilter);
   for (int i = 0; i < total && symCount < 256; i++) {
      string s = SymbolName(i, true);
      if (flen > 0) {
         if (StringLen(s) < flen) continue;
         if (StringSubstr(s, StringLen(s) - flen) != SymFilter) continue;
      }
      syms[symCount] = s;
      lastBarTime[symCount] = 0;
      symCount++;
   }
}

int CountMyPositions() {
   int cnt = 0;
   for (int i = 0; i < OrdersTotal(); i++) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() == MagicNum) cnt++;
   }
   return cnt;
}

bool HasPosition(string sym, int type) {
   for (int i = 0; i < OrdersTotal(); i++) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;
      if (OrderSymbol() == sym && OrderType() == type) return true;
   }
   return false;
}

void CloseBySymbol(string sym, int type) {
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;
      if (OrderSymbol() != sym || OrderType() != type) continue;
      double px = (type == OP_BUY) ? MarketInfo(sym, MODE_BID)
                                   : MarketInfo(sym, MODE_ASK);
      bool ok = OrderClose(OrderTicket(), OrderLots(), px, Slippage, clrYellow);
      if (!ok) Print("Close fail ", sym, " err=", GetLastError());
   }
}

double GetLot(string sym) {
   double mn = MarketInfo(sym, MODE_MINLOT);
   double mx = MarketInfo(sym, MODE_MAXLOT);
   double st = MarketInfo(sym, MODE_LOTSTEP);
   double lot = LotSize;
   if (lot < mn) lot = mn;
   if (lot > mx) lot = mx;
   if (st > 0)   lot = MathFloor(lot / st) * st;
   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
// v4: Poll /mt4/signals for DynaPro webhook signals
//+------------------------------------------------------------------+
void PollDynaPro() {
   if (TimeCurrent() - lastPoll < PollSec) return;
   lastPoll = TimeCurrent();

   string cookie = "", headers = "";
   char   post[], result[];
   string url = SignalURL;

   int timeout = 3000;
   int res = WebRequest("GET", url, cookie, NULL, timeout, post, 0, result, headers);
   if (res < 0) {
      int err = GetLastError();
      if (err != 4060) Print("DynaPro poll err=", err);
      return;
   }

   string body = CharArrayToString(result);
   if (StringLen(body) < 5) return;

   // Parse JSON: {"symbol":"XAUUSD.a","direction":"BUY","price":4800}
   string sym = ExtractJSON(body, "symbol");
   string dir = ExtractJSON(body, "direction");

   if (StringLen(sym) < 2 || StringLen(dir) < 2) return;

   Print("DYNAPRO SIGNAL: ", dir, " ", sym);

   // Check if symbol is tradeable
   if (!MarketInfo(sym, MODE_TRADEALLOWED)) {
      Print("DynaPro: ", sym, " not tradeable"); return;
   }
   if (MarketInfo(sym, MODE_BID) <= 0) {
      Print("DynaPro: ", sym, " no price"); return;
   }

   // Margin check
   double lot = GetLot(sym);
   double margin_needed = MarketInfo(sym, MODE_MARGINREQUIRED) * lot;
   if (margin_needed > AccountFreeMargin() * 0.15) {
      Print("DynaPro: ", sym, " margin too high"); return;
   }

   if (CountMyPositions() >= MaxPos) {
      Print("DynaPro: max positions reached"); return;
   }

   if (dir == "BUY" || dir == "buy") {
      if (UseLocalEMAFilter) {
         double fEMA = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 0);
         double sEMA = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 0);
         if (fEMA < sEMA) { Print("DYNAPRO BUY ", sym, " SKIP EMA bearish"); return; }
      }
      CloseBySymbol(sym, OP_SELL);
      if (!HasPosition(sym, OP_BUY)) {
         double ask = MarketInfo(sym, MODE_ASK);
         int t = OrderSend(sym, OP_BUY, lot, ask, Slippage, 0, 0,
                           "DYNAPRO", MagicNum, 0, clrLime);
         if (t < 0) Print("DYNAPRO BUY ", sym, " err=", GetLastError());
         else Print("DYNAPRO BUY ", sym, " @", ask, " ticket=", t);
      }
   }
   else if (dir == "SELL" || dir == "sell") {
      if (UseLocalEMAFilter) {
         double fEMA2 = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 0);
         double sEMA2 = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 0);
         if (fEMA2 > sEMA2) { Print("DYNAPRO SELL ", sym, " SKIP EMA bullish"); return; }
      }
      CloseBySymbol(sym, OP_BUY);
      if (!HasPosition(sym, OP_SELL)) {
         double bid = MarketInfo(sym, MODE_BID);
         int t = OrderSend(sym, OP_SELL, lot, bid, Slippage, 0, 0,
                           "DYNAPRO", MagicNum, 0, clrRed);
         if (t < 0) Print("DYNAPRO SELL ", sym, " err=", GetLastError());
         else Print("DYNAPRO SELL ", sym, " @", bid, " ticket=", t);
      }
   }
}

string ExtractJSON(string json, string key) {
   string search = "\"" + key + "\":\"";
   int start = StringFind(json, search);
   if (start < 0) return "";
   start += StringLen(search);
   int end = StringFind(json, "\"", start);
   if (end < 0) return "";
   return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
// v3: TP exit — runs every tick on all open positions
//+------------------------------------------------------------------+
void ManagePositions() {
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;

      string sym = OrderSymbol();
      int    type = OrderType();
      double entry = OrderOpenPrice();
      double px = (type == OP_BUY) ? MarketInfo(sym, MODE_BID) : MarketInfo(sym, MODE_ASK);
      if (px <= 0) continue;

      if (TP_Pct > 0) {
         double pnl_pct = 0;
         if (type == OP_BUY)  pnl_pct = (px - entry) / entry * 100.0;
         if (type == OP_SELL) pnl_pct = (entry - px) / entry * 100.0;
         if (pnl_pct >= TP_Pct) {
            bool ok = OrderClose(OrderTicket(), OrderLots(), px, Slippage, clrGold);
            if (ok) Print("TP EXIT ", sym, " +", DoubleToStr(pnl_pct, 2), "% ticket=", OrderTicket());
            else    Print("TP close fail ", sym, " err=", GetLastError());
            continue;
         }
      }

      if (EMA_Exit) {
         double slowEMA = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 0);
         if (slowEMA > 0) {
            if (type == OP_BUY && px < slowEMA) {
               bool ok = OrderClose(OrderTicket(), OrderLots(), px, Slippage, clrOrange);
               if (ok) Print("EMA EXIT ", sym, " ticket=", OrderTicket());
               continue;
            }
            if (type == OP_SELL && px > slowEMA) {
               bool ok = OrderClose(OrderTicket(), OrderLots(), px, Slippage, clrOrange);
               if (ok) Print("EMA EXIT ", sym, " ticket=", OrderTicket());
               continue;
            }
         }
      }
   }
}

void ProcessSymbol(string sym, int idx) {
   if (!MarketInfo(sym, MODE_TRADEALLOWED)) return;
   if (MarketInfo(sym, MODE_BID) <= 0) return;

   datetime bt = iTime(sym, Timeframe, 0);
   if (bt == 0) return;
   if (bt == lastBarTime[idx]) return;
   lastBarTime[idx] = bt;

   double f1 = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 1);
   double s1 = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 1);
   double f2 = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 2);
   double s2 = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 2);

   bool crossUp   = (f2 <= s2 && f1 >  s1);
   bool crossDown = (f2 >= s2 && f1 <  s1);

   if (crossUp)   CloseBySymbol(sym, OP_SELL);
   if (crossDown) CloseBySymbol(sym, OP_BUY);

   if (CountMyPositions() >= MaxPos) return;

   double lot = GetLot(sym);
   double margin_needed = MarketInfo(sym, MODE_MARGINREQUIRED) * lot;
   if (margin_needed > AccountFreeMargin() * 0.15) return;

   if (crossUp && !HasPosition(sym, OP_BUY)) {
      double ask = MarketInfo(sym, MODE_ASK);
      int t = OrderSend(sym, OP_BUY, lot, ask, Slippage, 0, 0,
                        "CPM_EMA", MagicNum, 0, clrLime);
      if (t < 0) Print("BUY ", sym, " err=", GetLastError());
      else       Print("BUY ", sym, " @", ask, " ticket=", t);
   }
   if (crossDown && !HasPosition(sym, OP_SELL)) {
      double bid = MarketInfo(sym, MODE_BID);
      int t = OrderSend(sym, OP_SELL, lot, bid, Slippage, 0, 0,
                        "CPM_EMA", MagicNum, 0, clrRed);
      if (t < 0) Print("SELL ", sym, " err=", GetLastError());
      else       Print("SELL ", sym, " @", bid, " ticket=", t);
   }
}

//+------------------------------------------------------------------+
int OnInit() {
   LoadSymbols();
   Print("Portfolio_Manager v4.1 EMA ", EMAFast, "/", EMASlow,
         " TF=", Timeframe, " TP=", TP_Pct, "% EMA_Exit=", EMA_Exit,
         " LocalEMAFilter=", UseLocalEMAFilter,
         " DynaPro=", SignalURL,
         " filter='", SymFilter, "' symbols=", symCount);
   for (int i = 0; i < symCount; i++) Print("  [", i, "] ", syms[i]);

   // --- FAIL-FAST: probe WebRequest once before entering OnTick loop ---
   string _cookie = "", _headers = "";
   char   _post[], _result[];
   int _probe = WebRequest("GET", SignalURL, _cookie, NULL, 3000, _post, 0, _result, _headers);
   if (_probe < 0) {
      int _err = GetLastError();
      string _msg = "";
      if (_err == 4060) _msg = "URL NOT WHITELISTED — add '" + SignalURL + "' in Tools>Options>Expert Advisors>WebRequest";
      else if (_err == 4014) _msg = "WebRequest function not allowed — check Tools>Options>Expert Advisors";
      else if (_err == 5203) _msg = "HTTP request failed — no internet or server down";
      else _msg = "WebRequest probe failed err=" + IntegerToString(_err);
      Print("FATAL: ", _msg);
      Alert("Portfolio_Manager: ", _msg);
      Comment("ABORT: ", _msg);
      return INIT_FAILED;
   }
   Print("WebRequest probe OK (", _probe, " bytes) — EA live");
   Comment("Portfolio_Manager v4.1 — EA live, polling every ", PollSec, "s");
   return INIT_SUCCEEDED;
}

void OnTick() {
   ManagePositions();
   PollDynaPro();

   static int ticks = 0;
   if (++ticks % 500 == 0) LoadSymbols();
   for (int i = 0; i < symCount; i++) ProcessSymbol(syms[i], i);
}

void OnDeinit(const int r) { }
//+------------------------------------------------------------------+
