//+------------------------------------------------------------------+
//| ExportCandles.mq4 — Export 15m candles for all tickers to CSV    |
//| Drop on any chart, it exports ALL Pepperstone tickers            |
//+------------------------------------------------------------------+
#property strict

string TICKERS[] = {
   "XAUUSD.a","XAGUSD.a","XPTUSD.a","XPDUSD.a",
   "SpotCrude.a","SpotBrent.a","NatGas.a",
   "EURUSD.a","GBPUSD.a","USDJPY.a","AUDUSD.a","NZDUSD.a","USDCAD.a",
   "USDCHF.a","EURGBP.a","GBPNZD.a","GBPJPY.a",
   "AUDCAD.a","AUDCHF.a","AUDNZD.a","AUDJPY.a",
   "CADCHF.a","CADJPY.a","CHFJPY.a",
   "EURAUD.a","EURCAD.a","EURCHF.a","GBPAUD.a","GBPCHF.a","NZDCAD.a",
   "NAS100.a","US30.a","US500.a","US2000.a",
   "GER40.a","UK100.a","JPN225.a","HK50.a",
   "Copper.a","Corn.a","Wheat.a","Soybeans.a","Coffee.a","Sugar.a",
   "VIX.a"
};

int OnInit()
{
   string folder = "CandleExport";
   
   for(int t=0; t<ArraySize(TICKERS); t++)
   {
      string sym = TICKERS[t];
      string fname = folder + "/" + sym + "_15M.csv";
      
      int handle = FileOpen(fname, FILE_WRITE|FILE_CSV, ',');
      if(handle < 0) { Print("Cannot open ", fname); continue; }
      
      // Header
      FileWrite(handle, "timestamp","open","high","low","close","volume");
      
      // Get 30 days of 15m candles
      int bars = iBars(sym, PERIOD_M15);
      int maxBars = MathMin(bars, 2000); // ~30 days of 15m
      
      for(int i=maxBars-1; i>=0; i--)
      {
         datetime dt = iTime(sym, PERIOD_M15, i);
         double o  = iOpen(sym, PERIOD_M15, i);
         double h  = iHigh(sym, PERIOD_M15, i);
         double l  = iLow(sym, PERIOD_M15, i);
         double c  = iClose(sym, PERIOD_M15, i);
         long   v  = iVolume(sym, PERIOD_M15, i);
         
         if(o > 0) // valid bar
            FileWrite(handle, (long)dt, o, h, l, c, v);
      }
      
      FileClose(handle);
      Print("Exported ", sym, " (", maxBars, " bars) → ", fname);
   }
   
   Print("=== EXPORT COMPLETE: ", ArraySize(TICKERS), " tickers ===");
   Print("Files in: MQL4/Files/CandleExport/");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) {}
void OnTick() {}
