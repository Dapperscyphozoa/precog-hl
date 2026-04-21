//+------------------------------------------------------------------+
//| Portfolio_Manager.mq4 v5.0 — COMPOUNDING + PER-SIGNAL GATES      |
//| - Equity-scaled lot sizing (risk % per trade)                    |
//| - Per-signal trail/SL params from precog v4.8 queue              |
//| - Trail logic in ManagePositions (peak + retrace)                |
//| - Broker-side SL on entry                                        |
//| - GetLot SKIPs instead of rounding up to minlot                  |
//| - Per-ticket state in GlobalVariables                            |
//| - Time-cut exit per signal                                       |
//+------------------------------------------------------------------+
#property copyright "CPM"
#property version   "5.12"
#property strict

// ===== INPUTS =====
extern bool   UseEquityScaling = true;   // v5: scale lot to account
extern double RiskPctPerTrade  = 1.0;    // v5: 1% of equity per trade (at SL_Pct loss)
extern double LotSize          = 0.01;   // fallback if scaling off or minlot forces
extern double MaxLotCap        = 5.0;    // hard cap on any single trade lot size
extern int    EMAFast          = 9;
extern int    EMASlow          = 21;
extern int    Timeframe        = PERIOD_H1;
extern int    MaxPos           = 10;
extern int    MagicNum         = 20260416;
extern int    Slippage         = 30;
extern string SymFilter        = ".a";
extern double TP_Pct           = 0.0;    // set 0 to disable (use trail instead)
extern double SL_Pct_Default   = 1.4;    // fallback if signal has no sl_pct
extern bool   UseTrailingStop  = true;
extern double Trail_Activate_Default = 0.4;
extern double Trail_Distance_Default = 0.2;
extern bool   EMA_Exit         = false;
extern string SignalURL        = "https://precog-hl-web.onrender.com/mt4/signals";
extern int    PollSec          = 2;
extern bool   UseLocalEMAFilter = false; // v5.12: gates handle trend, local EMA was overfiltering
extern bool   UseLimitOrders   = true;
extern int    LimitExpiryMin   = 15;
extern double LimitOffsetPips  = 2;
extern bool   FlattenOnInit    = false;
extern string FlattenURL       = "https://precog-hl-web.onrender.com/mt4/flatten/check";
extern string FlattenAckURL    = "https://precog-hl-web.onrender.com/mt4/flatten/ack";
extern string TradeClosedURL   = "https://precog-hl-web.onrender.com/mt4/trade-closed";
extern string TradeOpenedURL   = "https://precog-hl-web.onrender.com/mt4/trade-opened";
extern double MaxSpreadPctDefault = 0.15;    // v5.12: widened for metals/oil/indices. Server overrides per-ticker.

// ===== STATE =====
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

//+------------------------------------------------------------------+
// v5: Equity-scaled lot sizing.
// Risk = RiskPctPerTrade% of equity at SL_Pct loss.
// lot = (equity * risk%) / (entry_px * sl% * contract_size)
// Simpler approach: scale linearly from 0.01 at $1400 -> larger at higher equity
//+------------------------------------------------------------------+
double GetLot(string sym, double sl_pct) {
   double mn = MarketInfo(sym, MODE_MINLOT);
   double mx = MarketInfo(sym, MODE_MAXLOT);
   double st = MarketInfo(sym, MODE_LOTSTEP);
   double lot = LotSize;

   if (UseEquityScaling && sl_pct > 0) {
      double eq = AccountEquity();
      double risk_usd = eq * (RiskPctPerTrade / 100.0);
      double tick_val = MarketInfo(sym, MODE_TICKVALUE);
      double tick_sz  = MarketInfo(sym, MODE_TICKSIZE);
      double px = MarketInfo(sym, MODE_ASK);
      if (tick_val > 0 && tick_sz > 0 && px > 0) {
         // Dollar-per-1lot-per-1% price move
         double pct_move = sl_pct / 100.0;
         double price_delta = px * pct_move;
         double ticks_to_sl = price_delta / tick_sz;
         double usd_per_lot_at_sl = ticks_to_sl * tick_val;
         if (usd_per_lot_at_sl > 0) {
            lot = risk_usd / usd_per_lot_at_sl;
         }
      }
   }

   // Cap
   if (lot > MaxLotCap) lot = MaxLotCap;
   if (lot > mx) lot = mx;

   // v5.12: accept minlot up to 1.5x sized_lot (effective risk <=1.5%). SKIP only if way too big.
   if (mn > lot * 1.5 && mn > lot + 0.00001) {
      Print("GetLot SKIP ", sym, ": minlot=", mn, " > 1.5x sized_lot=", DoubleToStr(lot, 4));
      return 0;
   }
   if (mn > lot + 0.00001) {
      Print("GetLot BUMP ", sym, ": sized_lot=", DoubleToStr(lot, 4), " -> minlot=", mn, " (eff risk ", DoubleToStr(mn/lot*100,0), "% of target)");
      lot = mn;
   }

   // Step-align
   if (st > 0) lot = MathFloor(lot / st) * st;
   if (lot < mn) lot = mn;

   return NormalizeDouble(lot, 2);
}

//+------------------------------------------------------------------+
// Per-ticket state: store trail/SL/peak/entry_time in GlobalVariables
// Key format: "PM_<ticket>_<field>"
//+------------------------------------------------------------------+
void SetTicketParam(int ticket, string field, double val) {
   GlobalVariableSet("PM_" + IntegerToString(ticket) + "_" + field, val);
}
double GetTicketParam(int ticket, string field, double def_val) {
   string key = "PM_" + IntegerToString(ticket) + "_" + field;
   if (GlobalVariableCheck(key)) return GlobalVariableGet(key);
   return def_val;
}
void DeleteTicketParams(int ticket) {
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_trail_act");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_trail_dist");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_sl_pct");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_peak_pct");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_active_trail");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_entry_time");
   GlobalVariableDel("PM_" + IntegerToString(ticket) + "_time_cut_h");
}

//+------------------------------------------------------------------+
int flattenClosed = 0, flattenDeleted = 0;
void FlattenAll(string reason) {
   flattenClosed = 0; flattenDeleted = 0;
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;
      int typ = OrderType();
      int tk = OrderTicket();
      string sym = OrderSymbol();
      if (typ == OP_BUY) {
         double bid = MarketInfo(sym, MODE_BID);
         if (OrderClose(tk, OrderLots(), bid, Slippage*5, clrYellow)) { flattenClosed++; DeleteTicketParams(tk); }
         else Print("FLATTEN close BUY err ", sym, " ", GetLastError());
      } else if (typ == OP_SELL) {
         double ask = MarketInfo(sym, MODE_ASK);
         if (OrderClose(tk, OrderLots(), ask, Slippage*5, clrYellow)) { flattenClosed++; DeleteTicketParams(tk); }
         else Print("FLATTEN close SELL err ", sym, " ", GetLastError());
      } else {
         if (OrderDelete(tk)) flattenDeleted++;
         else Print("FLATTEN delete err ", sym, " ", GetLastError());
      }
   }
   Print("FLATTEN DONE reason=", reason, " closed=", flattenClosed, " deleted=", flattenDeleted);
   string cookie = "", headers = "";
   char post[], result[];
   string body = "{\"closed\":" + IntegerToString(flattenClosed) + ",\"deleted\":" + IntegerToString(flattenDeleted) + "}";
   StringToCharArray(body, post, 0, StringLen(body));
   WebRequest("POST", FlattenAckURL, cookie, NULL, 3000, post, StringLen(body), result, headers);
}

void PollFlatten() {
   string cookie = "", headers = "";
   char post[], result[];
   int res = WebRequest("GET", FlattenURL, cookie, NULL, 3000, post, 0, result, headers);
   if (res < 0) return;
   string body = CharArrayToString(result);
   if (StringFind(body, "\"pending\":true") >= 0) {
      Print("FLATTEN SIGNAL received");
      FlattenAll("webhook");
   }
}

//+------------------------------------------------------------------+
// JSON helpers
//+------------------------------------------------------------------+
string ExtractJSON(string json, string key) {
   string needle = "\"" + key + "\":\"";
   int p = StringFind(json, needle);
   if (p < 0) return "";
   int start = p + StringLen(needle);
   int end = StringFind(json, "\"", start);
   if (end < 0) return "";
   return StringSubstr(json, start, end - start);
}

double ExtractJSONNum(string json, string key) {
   string needle = "\"" + key + "\":";
   int p = StringFind(json, needle);
   if (p < 0) return 0;
   int start = p + StringLen(needle);
   int end = start;
   while (end < StringLen(json)) {
      ushort ch = StringGetCharacter(json, end);
      if ((ch >= '0' && ch <= '9') || ch == '.' || ch == '-' || ch == '+') end++;
      else break;
   }
   if (end == start) return 0;
   return StrToDouble(StringSubstr(json, start, end - start));
}

//+------------------------------------------------------------------+
// v5: Poll /mt4/signals — reads per-signal trail/SL params
//+------------------------------------------------------------------+
void PollDynaPro() {
   if (TimeCurrent() - lastPoll < PollSec) return;
   lastPoll = TimeCurrent();

   string cookie = "", headers = "";
   char   post[], result[];
   int res = WebRequest("GET", SignalURL, cookie, NULL, 3000, post, 0, result, headers);
   if (res < 0) {
      int err = GetLastError();
      if (err != 4060) Print("DynaPro poll err=", err);
      return;
   }

   string body = CharArrayToString(result);
   if (StringLen(body) < 5) return;

   string sym = ExtractJSON(body, "symbol");
   string dir = ExtractJSON(body, "direction");
   if (StringLen(sym) < 2 || StringLen(dir) < 2) return;

   double wh_price    = ExtractJSONNum(body, "price");
   double sig_trail_a = ExtractJSONNum(body, "trail_activate");
   double sig_trail_d = ExtractJSONNum(body, "trail_distance");
   double sig_sl      = ExtractJSONNum(body, "sl_pct");
   double sig_mult    = ExtractJSONNum(body, "size_mult");
   double sig_tcut    = ExtractJSONNum(body, "time_cut_hours");
   double sig_maxslip = ExtractJSONNum(body, "max_slip_pct");
   if (sig_maxslip <= 0) sig_maxslip = 0.3;
   double sig_maxspread = ExtractJSONNum(body, "max_spread_pct");
   if (sig_maxspread <= 0) sig_maxspread = MaxSpreadPctDefault;

   // v5.1: spread gate -- reject if broker spread too wide
   if (!SpreadOK(sym, sig_maxspread)) return;

   // Apply defaults where signal didn't specify
   if (sig_trail_a <= 0) sig_trail_a = Trail_Activate_Default;
   if (sig_trail_d <= 0) sig_trail_d = Trail_Distance_Default;
   if (sig_sl <= 0)      sig_sl      = SL_Pct_Default;
   if (sig_mult <= 0)    sig_mult    = 1.0;

   Print("DYNAPRO SIGNAL: ", dir, " ", sym, " wh=", wh_price,
         " trail=", sig_trail_a, "/", sig_trail_d, " sl=", sig_sl,
         " mult=", sig_mult, " tcut=", sig_tcut);

   if (!MarketInfo(sym, MODE_TRADEALLOWED)) { Print("DynaPro: ", sym, " not tradeable"); return; }
   if (MarketInfo(sym, MODE_BID) <= 0)       { Print("DynaPro: ", sym, " no price"); return; }

   double lot = GetLot(sym, sig_sl);
   if (lot <= 0) { Print("DynaPro: ", sym, " skip - minlot too large"); return; }
   lot = NormalizeDouble(lot * sig_mult, 2);  // apply VIX overlay multiplier
   double mn = MarketInfo(sym, MODE_MINLOT);
   if (lot < mn) {
      Print("DynaPro: ", sym, " sized-lot ", lot, " < minlot ", mn, " — skip");
      return;
   }

   double margin_needed = MarketInfo(sym, MODE_MARGINREQUIRED) * lot;
   if (margin_needed > AccountFreeMargin() * 0.15) {
      Print("DynaPro: ", sym, " margin too high (", DoubleToStr(margin_needed,2), " vs ", DoubleToStr(AccountFreeMargin()*0.15,2), ")");
      return;
   }

   if (CountMyPositions() >= MaxPos) { Print("DynaPro: max positions reached"); return; }

   double point = MarketInfo(sym, MODE_POINT);
   double offset = LimitOffsetPips * point * 10;
   int digits = (int)MarketInfo(sym, MODE_DIGITS);

   int ticket = -1;

   if (dir == "BUY" || dir == "buy") {
      if (UseLocalEMAFilter) {
         double fEMA = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 0);
         double sEMA = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 0);
         if (fEMA < sEMA) { Print("DYNAPRO BUY ", sym, " SKIP EMA bearish"); return; }
      }
      CloseBySymbol(sym, OP_SELL);
      if (!HasPosition(sym, OP_BUY)) {
         double ask = MarketInfo(sym, MODE_ASK);
         double sl_px = NormalizeDouble(ask * (1.0 - sig_sl/100.0), digits);
         if (UseLimitOrders && wh_price > 0) {
            double limit_px = MathMin(wh_price, ask - offset);
            if (limit_px >= ask) limit_px = ask - offset;
            double sl_limit = NormalizeDouble(limit_px * (1.0 - sig_sl/100.0), digits);
            datetime expiry = TimeCurrent() + LimitExpiryMin * 60;
            ticket = OrderSend(sym, OP_BUYLIMIT, lot, NormalizeDouble(limit_px, digits),
                          Slippage, sl_limit, 0, "DYNAPRO_LIMIT", MagicNum, expiry, clrLime);
            if (ticket < 0) {
               int err_buy = GetLastError();
               double slip = (wh_price > 0) ? MathAbs(ask - wh_price) / wh_price * 100.0 : 0;
               if (slip > sig_maxslip) {
                  Print("DYNAPRO BUY ", sym, " SKIP — slip ", DoubleToStr(slip,3), "% > max ", DoubleToStr(sig_maxslip,3), "% (limit err=", err_buy, ")");
                  return;
               }
               Print("DYNAPRO BUYLIMIT ", sym, " err=", err_buy, " slip=", DoubleToStr(slip,3), "% → market");
               ticket = OrderSend(sym, OP_BUY, lot, ask, Slippage, sl_px, 0, "DYNAPRO", MagicNum, 0, clrLime);
               if (ticket > 0) ReportOpen(ticket, sym, "BUY", ask, lot);
            } else {
               Print("DYNAPRO BUYLIMIT ", sym, " @", limit_px, " sl=", sl_limit, " lot=", lot, " ticket=", ticket);
               if (ticket > 0) ReportOpen(ticket, sym, "BUY", limit_px, lot);
            }
         } else {
            double slip_mkt = (wh_price > 0) ? MathAbs(ask - wh_price) / wh_price * 100.0 : 0;
            if (slip_mkt > sig_maxslip) {
               Print("DYNAPRO BUY ", sym, " SKIP — market slip ", DoubleToStr(slip_mkt,3), "% > max ", DoubleToStr(sig_maxslip,3), "%");
               return;
            }
            ticket = OrderSend(sym, OP_BUY, lot, ask, Slippage, sl_px, 0, "DYNAPRO", MagicNum, 0, clrLime);
            if (ticket > 0) { Print("DYNAPRO BUY ", sym, " @", ask, " sl=", sl_px, " lot=", lot, " ticket=", ticket); ReportOpen(ticket, sym, "BUY", ask, lot); }
            else            Print("DYNAPRO BUY ", sym, " err=", GetLastError());
         }
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
         double sl_px = NormalizeDouble(bid * (1.0 + sig_sl/100.0), digits);
         if (UseLimitOrders && wh_price > 0) {
            double limit_px = MathMax(wh_price, bid + offset);
            if (limit_px <= bid) limit_px = bid + offset;
            double sl_limit = NormalizeDouble(limit_px * (1.0 + sig_sl/100.0), digits);
            datetime expiry = TimeCurrent() + LimitExpiryMin * 60;
            ticket = OrderSend(sym, OP_SELLLIMIT, lot, NormalizeDouble(limit_px, digits),
                          Slippage, sl_limit, 0, "DYNAPRO_LIMIT", MagicNum, expiry, clrRed);
            if (ticket > 0) ReportOpen(ticket, sym, "SELL", limit_px, lot);
            if (ticket < 0) {
               int err_sell = GetLastError();
               double slip = (wh_price > 0) ? MathAbs(bid - wh_price) / wh_price * 100.0 : 0;
               if (slip > sig_maxslip) {
                  Print("DYNAPRO SELL ", sym, " SKIP — slip ", DoubleToStr(slip,3), "% > max ", DoubleToStr(sig_maxslip,3), "% (limit err=", err_sell, ")");
                  return;
               }
               Print("DYNAPRO SELLLIMIT ", sym, " err=", err_sell, " slip=", DoubleToStr(slip,3), "% → market");
               ticket = OrderSend(sym, OP_SELL, lot, bid, Slippage, sl_px, 0, "DYNAPRO", MagicNum, 0, clrRed);
            } else {
               Print("DYNAPRO SELLLIMIT ", sym, " @", limit_px, " sl=", sl_limit, " lot=", lot, " ticket=", ticket);
            }
         } else {
            double slip_mkt = (wh_price > 0) ? MathAbs(bid - wh_price) / wh_price * 100.0 : 0;
            if (slip_mkt > sig_maxslip) {
               Print("DYNAPRO SELL ", sym, " SKIP — market slip ", DoubleToStr(slip_mkt,3), "% > max ", DoubleToStr(sig_maxslip,3), "%");
               return;
            }
            ticket = OrderSend(sym, OP_SELL, lot, bid, Slippage, sl_px, 0, "DYNAPRO", MagicNum, 0, clrRed);
            if (ticket > 0) Print("DYNAPRO SELL ", sym, " @", bid, " sl=", sl_px, " lot=", lot, " ticket=", ticket);
            if (ticket > 0) ReportOpen(ticket, sym, "SELL", bid, lot);
            else            Print("DYNAPRO SELL ", sym, " err=", GetLastError());
         }
      }
   }

   // Store per-ticket params for trail logic
   if (ticket > 0) {
      SetTicketParam(ticket, "trail_act", sig_trail_a);
      SetTicketParam(ticket, "trail_dist", sig_trail_d);
      SetTicketParam(ticket, "sl_pct", sig_sl);
      SetTicketParam(ticket, "peak_pct", 0);
      SetTicketParam(ticket, "active_trail", -999);
      SetTicketParam(ticket, "entry_time", TimeCurrent());
      SetTicketParam(ticket, "time_cut_h", sig_tcut);
   }
}

//+------------------------------------------------------------------+
// Report successful OrderSend to server -- stashes side per ticket
//+------------------------------------------------------------------+
void ReportOpen(int ticket, string sym, string side, double entry, double lots) {
   // v5.11: route through /mt4/trade-closed with exit_type="OPEN" sentinel
   // This avoids needing a separate WebRequest whitelist entry for trade-opened
   string cookie = "", headers = "";
   char post[], result[];
   string body = "{\"ticket\":" + IntegerToString(ticket) +
                 ",\"symbol\":\"" + sym + "\",\"side\":\"" + side +
                 "\",\"entry\":" + DoubleToStr(entry, 5) +
                 ",\"lots\":" + DoubleToStr(lots, 2) +
                 ",\"exit_type\":\"OPEN\",\"peak_pct\":0,\"exit_pct\":0}";
   StringToCharArray(body, post, 0, StringLen(body));
   WebRequest("POST", TradeClosedURL, cookie, NULL, 3000, post, StringLen(body), result, headers);
}

//+------------------------------------------------------------------+
// Spread filter -- returns true if current spread is acceptable
//+------------------------------------------------------------------+
bool SpreadOK(string sym, double max_spread_pct) {
   double ask = MarketInfo(sym, MODE_ASK);
   double bid = MarketInfo(sym, MODE_BID);
   if (ask <= 0 || bid <= 0) return true;
   double mid = (ask + bid) / 2.0;
   if (mid <= 0) return true;
   double spread_pct = (ask - bid) / mid * 100.0;
   if (spread_pct > max_spread_pct) {
      Print("SPREAD REJECT ", sym, " spread=", DoubleToStr(spread_pct,3), "% > max ", DoubleToStr(max_spread_pct,3), "%");
      return false;
   }
   return true;
}

//+------------------------------------------------------------------+
// Report trail/SL exit to server for retest re-entry
//+------------------------------------------------------------------+
void ReportExit(string sym, string exit_type, double entry, double peak_pct, double exit_pct, int ticket) {
   string cookie = "", headers = "";
   char post[], result[];
   string body = "{\"symbol\":\"" + sym + "\",\"exit_type\":\"" + exit_type +
                 "\",\"entry\":" + DoubleToStr(entry, 5) +
                 ",\"peak_pct\":" + DoubleToStr(peak_pct, 2) +
                 ",\"exit_pct\":" + DoubleToStr(exit_pct, 2) +
                 ",\"ticket\":" + IntegerToString(ticket) + "}";
   StringToCharArray(body, post, 0, StringLen(body));
   WebRequest("POST", TradeClosedURL, cookie, NULL, 3000, post, StringLen(body), result, headers);
}

//+------------------------------------------------------------------+
// Cancel stale limit orders
//+------------------------------------------------------------------+
void CancelStaleLimits() {
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;
      int typ = OrderType();
      if (typ != OP_BUYLIMIT && typ != OP_SELLLIMIT) continue;
      datetime expiry = OrderExpiration();
      if (expiry > 0 && TimeCurrent() > expiry) {
         int tk = OrderTicket();
         if (OrderDelete(tk)) DeleteTicketParams(tk);
      }
   }
}

//+------------------------------------------------------------------+
// v5 ManagePositions: per-ticket trail, time-cut, optional TP
//+------------------------------------------------------------------+

//+------------------------------------------------------------------+
// Detect broker-side SL/external closes via history scan
//+------------------------------------------------------------------+
int g_last_history_total = 0;
void CheckClosedOrders() {
   int total = OrdersHistoryTotal();
   if (g_last_history_total == 0) { g_last_history_total = total; return; }
   if (total <= g_last_history_total) { g_last_history_total = total; return; }

   for (int i = g_last_history_total; i < total; i++) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY)) continue;
      if (OrderMagicNumber() != MagicNum) continue;
      int type = OrderType();
      if (type != OP_BUY && type != OP_SELL) continue;

      int tk = OrderTicket();
      double entry = OrderOpenPrice();
      double exit_px = OrderClosePrice();
      double pnl_pct = 0;
      if (type == OP_BUY)  pnl_pct = (exit_px - entry) / entry * 100.0;
      if (type == OP_SELL) pnl_pct = (entry - exit_px) / entry * 100.0;

      double peak = GetTicketParam(tk, "peak_pct", -999);
      if (peak < -900) continue;

      string sym = OrderSymbol();
      string exit_type = (pnl_pct < 0) ? "SL" : "MANUAL";
      Print(exit_type, " EXIT (history) ", sym, " pnl=", DoubleToStr(pnl_pct,2), "% ticket=", tk);
      ReportExit(sym, exit_type, entry, peak, pnl_pct, tk);
      DeleteTicketParams(tk);
   }
   g_last_history_total = total;
}

void ManagePositions() {
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      if (!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
      if (OrderMagicNumber() != MagicNum) continue;

      string sym = OrderSymbol();
      int    type = OrderType();
      if (type != OP_BUY && type != OP_SELL) continue;

      int    tk    = OrderTicket();
      double entry = OrderOpenPrice();
      double px = (type == OP_BUY) ? MarketInfo(sym, MODE_BID) : MarketInfo(sym, MODE_ASK);
      if (px <= 0) continue;

      double pnl_pct = 0;
      if (type == OP_BUY)  pnl_pct = (px - entry) / entry * 100.0;
      if (type == OP_SELL) pnl_pct = (entry - px) / entry * 100.0;

      // Load per-ticket params
      double trail_act  = GetTicketParam(tk, "trail_act", Trail_Activate_Default);
      double trail_dist = GetTicketParam(tk, "trail_dist", Trail_Distance_Default);
      double peak_pct   = GetTicketParam(tk, "peak_pct", 0);
      double active_t   = GetTicketParam(tk, "active_trail", -999);
      double t_cut_h    = GetTicketParam(tk, "time_cut_h", 0);
      double entry_time = GetTicketParam(tk, "entry_time", TimeCurrent());

      // Update peak
      if (pnl_pct > peak_pct) {
         peak_pct = pnl_pct;
         SetTicketParam(tk, "peak_pct", peak_pct);
         if (UseTrailingStop && peak_pct >= trail_act) {
            active_t = peak_pct - trail_dist;
            SetTicketParam(tk, "active_trail", active_t);
         }
      }

      // Trail exit
      if (UseTrailingStop && active_t > -999 && pnl_pct <= active_t) {
         bool ok = OrderClose(tk, OrderLots(), px, Slippage, clrGold);
         if (ok) {
            Print("TRAIL EXIT ", sym, " peak=+", DoubleToStr(peak_pct,2), "% exit=+", DoubleToStr(pnl_pct,2), "% ticket=", tk);
            ReportExit(sym, "TRAIL", entry, peak_pct, pnl_pct, tk);
            DeleteTicketParams(tk);
         } else {
            Print("TRAIL close fail ", sym, " err=", GetLastError());
         }
         continue;
      }

      // Time-cut exit
      if (t_cut_h > 0 && (TimeCurrent() - entry_time) >= (int)(t_cut_h * 3600)) {
         if (pnl_pct > 0) {
            bool ok = OrderClose(tk, OrderLots(), px, Slippage, clrBlue);
            if (ok) {
               Print("TIME_CUT EXIT ", sym, " +", DoubleToStr(pnl_pct,2), "% after ", DoubleToStr(t_cut_h,1), "h ticket=", tk);
               ReportExit(sym, "TIME_CUT", entry, peak_pct, pnl_pct, tk);
               DeleteTicketParams(tk);
               continue;
            }
         }
      }

      // Optional TP
      if (TP_Pct > 0 && pnl_pct >= TP_Pct) {
         bool ok = OrderClose(tk, OrderLots(), px, Slippage, clrGold);
         if (ok) {
            Print("TP EXIT ", sym, " +", DoubleToStr(pnl_pct,2), "% ticket=", tk);
            ReportExit(sym, "TP", entry, peak_pct, pnl_pct, tk);
            DeleteTicketParams(tk);
         }
         continue;
      }

      // EMA exit (legacy option)
      if (EMA_Exit) {
         double slowEMA = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 0);
         if (slowEMA > 0) {
            if ((type == OP_BUY && px < slowEMA) || (type == OP_SELL && px > slowEMA)) {
               bool ok = OrderClose(tk, OrderLots(), px, Slippage, clrOrange);
               if (ok) { Print("EMA EXIT ", sym, " ticket=", tk); DeleteTicketParams(tk); }
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
// EMA crossover on bar close (unchanged logic, just equity lot)
//+------------------------------------------------------------------+
void ProcessSymbol(string sym, int idx) {
   datetime barTime = iTime(sym, Timeframe, 0);
   if (barTime <= lastBarTime[idx]) return;
   lastBarTime[idx] = barTime;

   double fEMA = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 1);
   double sEMA = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 1);
   double fPrev = iMA(sym, Timeframe, EMAFast, 0, MODE_EMA, PRICE_CLOSE, 2);
   double sPrev = iMA(sym, Timeframe, EMASlow, 0, MODE_EMA, PRICE_CLOSE, 2);
   if (fEMA <= 0 || sEMA <= 0 || fPrev <= 0 || sPrev <= 0) return;

   bool goLong  = (fPrev <= sPrev && fEMA > sEMA);
   bool goShort = (fPrev >= sPrev && fEMA < sEMA);
   if (!goLong && !goShort) return;

   if (CountMyPositions() >= MaxPos) return;

   double lot = GetLot(sym, SL_Pct_Default);
   if (lot <= 0) return;
   double margin_needed = MarketInfo(sym, MODE_MARGINREQUIRED) * lot;
   if (margin_needed > AccountFreeMargin() * 0.15) return;

   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   int ticket = -1;

   if (goLong) {
      CloseBySymbol(sym, OP_SELL);
      if (!HasPosition(sym, OP_BUY)) {
         double ask = MarketInfo(sym, MODE_ASK);
         double sl_px = NormalizeDouble(ask * (1.0 - SL_Pct_Default/100.0), digits);
         ticket = OrderSend(sym, OP_BUY, lot, ask, Slippage, sl_px, 0, "EMA_X", MagicNum, 0, clrLime);
         if (ticket > 0) Print("BUY ", sym, " @", ask, " sl=", sl_px, " lot=", lot, " ticket=", ticket);
      }
   }
   if (goShort) {
      CloseBySymbol(sym, OP_BUY);
      if (!HasPosition(sym, OP_SELL)) {
         double bid = MarketInfo(sym, MODE_BID);
         double sl_px = NormalizeDouble(bid * (1.0 + SL_Pct_Default/100.0), digits);
         ticket = OrderSend(sym, OP_SELL, lot, bid, Slippage, sl_px, 0, "EMA_X", MagicNum, 0, clrRed);
         if (ticket > 0) Print("SELL ", sym, " @", bid, " sl=", sl_px, " lot=", lot, " ticket=", ticket);
      }
   }

   if (ticket > 0) {
      SetTicketParam(ticket, "trail_act", Trail_Activate_Default);
      SetTicketParam(ticket, "trail_dist", Trail_Distance_Default);
      SetTicketParam(ticket, "sl_pct", SL_Pct_Default);
      SetTicketParam(ticket, "peak_pct", 0);
      SetTicketParam(ticket, "active_trail", -999);
      SetTicketParam(ticket, "entry_time", TimeCurrent());
      SetTicketParam(ticket, "time_cut_h", 0);
   }
}

//+------------------------------------------------------------------+
int OnInit() {
   LoadSymbols();
   Print("Portfolio_Manager v5.1 COMPOUNDING+ZONES  EquityScale=", UseEquityScaling, " Risk%=", RiskPctPerTrade,
         " MaxLot=", MaxLotCap, " SL=", SL_Pct_Default, "% TrailDefault=", Trail_Activate_Default, "/", Trail_Distance_Default,
         " symbols=", symCount);

   // Probe WebRequest
   string cookie = "", headers = "";
   char post[], result[];
   int probe = WebRequest("GET", SignalURL, cookie, NULL, 3000, post, 0, result, headers);
   if (probe < 0) {
      Print("WebRequest probe FAIL err=", GetLastError(), " — check MT4 Tools → Options → Expert Advisors → Allow WebRequest for ", SignalURL);
      return INIT_FAILED;
   }
   Print("EA v5.12 live -- probe OK (", ArraySize(result), " bytes)");

   if (FlattenOnInit) {
      Print("FlattenOnInit=true — closing all magic-matched positions and pendings");
      FlattenAll("init");
   }
   return INIT_SUCCEEDED;
}

void OnTick() {
   CheckClosedOrders();
   ManagePositions();
   CancelStaleLimits();
   PollFlatten();
   PollDynaPro();
   for (int i = 0; i < symCount; i++) ProcessSymbol(syms[i], i);
}

void OnDeinit(const int r) { }
