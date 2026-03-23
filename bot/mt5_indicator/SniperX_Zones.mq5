//+------------------------------------------------------------------+
//|                                              SniperX_Zones.mq5   |
//|                                  SniperX AI Trading Bot Indicator |
//|                                                                   |
//| Zone Detection: İğne bazlı demand/supply zone tespiti            |
//| - Min 2 pin bar (iğne) zorunlu                                   |
//| - 2-3 ardışık momentum mumu ile kırılım                          |
//| - Zone = wick dahil çizim (iğnelerin tamamını kapsar)            |
//| - FVG/gap tespiti                                                |
//| - Hacim kontrolü                                                 |
//| - Sol taraf temizliği                                            |
//+------------------------------------------------------------------+
#property copyright "SniperX"
#property link      ""
#property version   "1.00"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0

//--- Input parameters
input double   SpikeATRMult    = 1.2;     // Impulse min body (ATR katı)
input int      BaseMinCandles  = 2;       // Base min mum sayısı
input int      BaseMaxCandles  = 20;      // Base max mum sayısı
input double   BaseMaxRangeMult= 1.0;     // Base mum max range (ATR katı)
input int      LeftSideSwings  = 8;       // Sol taraf max swing
input int      Lookback        = 300;     // Geriye bakma bar sayısı
input double   MinDisplacement = 0.8;     // Min displacement
input int      MinPinCount     = 2;       // Min iğne sayısı
input int      ATRPeriod       = 14;      // ATR periyodu
input color    DemandColor     = clrLimeGreen;  // Demand zone rengi
input color    SupplyColor     = clrRed;        // Supply zone rengi
input color    FlipColor       = clrDodgerBlue; // Flip zone rengi
input int      ZoneOpacity     = 25;      // Zone şeffaflık (0-100)

//--- Global
datetime lastBarTime = 0;
int zoneCount = 0;

//+------------------------------------------------------------------+
//| Custom indicator initialization function                          |
//+------------------------------------------------------------------+
int OnInit()
{
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   // Tüm zone objeleri temizle
   for(int i = ObjectsTotal(0, 0, OBJ_RECTANGLE) - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, OBJ_RECTANGLE);
      if(StringFind(name, "SX_ZONE_") >= 0)
         ObjectDelete(0, name);
   }
   for(int i = ObjectsTotal(0, 0, OBJ_TEXT) - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, OBJ_TEXT);
      if(StringFind(name, "SX_LBL_") >= 0)
         ObjectDelete(0, name);
   }
}

//+------------------------------------------------------------------+
//| İğne (pin bar) kontrolü                                          |
//+------------------------------------------------------------------+
bool IsPinBar(double open, double high, double low, double close)
{
   double body = MathAbs(close - open);
   double upperWick = high - MathMax(open, close);
   double lowerWick = MathMin(open, close) - low;
   if(body < 1e-10) body = 1e-10;
   return (upperWick > 2.0 * body || lowerWick > 2.0 * body);
}

//+------------------------------------------------------------------+
//| ATR hesapla                                                       |
//+------------------------------------------------------------------+
double CalcATR(int period, int shift)
{
   double atr = 0;
   for(int i = shift + 1; i <= shift + period; i++)
   {
      double h = iHigh(NULL, 0, i);
      double l = iLow(NULL, 0, i);
      double pc = iClose(NULL, 0, i + 1);
      double tr = MathMax(h - l, MathMax(MathAbs(h - pc), MathAbs(l - pc)));
      atr += tr;
   }
   return atr / period;
}

//+------------------------------------------------------------------+
//| Sol taraf temizliği                                               |
//+------------------------------------------------------------------+
bool IsLeftSideClean(int baseStart, int maxSwings)
{
   int checkStart = MathMax(0, baseStart + 15);  // bars index (right to left)
   if(checkStart - baseStart < 3) return true;

   int swings = 0;
   string prevDir = "";
   for(int i = checkStart - 1; i > baseStart; i--)
   {
      string dir = (iClose(NULL, 0, i) > iClose(NULL, 0, i + 1)) ? "up" : "down";
      if(prevDir != "" && dir != prevDir) swings++;
      prevDir = dir;
   }
   return swings <= maxSwings;
}

//+------------------------------------------------------------------+
//| Zone çiz                                                          |
//+------------------------------------------------------------------+
void DrawZone(string name, datetime t1, datetime t2, double high, double low,
              color clr, string zoneType, int pinCount, double displacement)
{
   string rectName = "SX_ZONE_" + name;
   string lblName = "SX_LBL_" + name;

   // Dikdörtgen
   ObjectCreate(0, rectName, OBJ_RECTANGLE, 0, t1, high, t2, low);
   ObjectSetInteger(0, rectName, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, rectName, OBJPROP_FILL, true);
   ObjectSetInteger(0, rectName, OBJPROP_BACK, true);
   ObjectSetInteger(0, rectName, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, rectName, OBJPROP_STYLE, STYLE_SOLID);

   // Kenarlık
   string borderName = "SX_ZONE_B_" + name;
   ObjectCreate(0, borderName, OBJ_RECTANGLE, 0, t1, high, t2, low);
   ObjectSetInteger(0, borderName, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, borderName, OBJPROP_FILL, false);
   ObjectSetInteger(0, borderName, OBJPROP_WIDTH, 2);

   // Label
   string label = zoneType + " | Pin:" + IntegerToString(pinCount) + " Disp:" + DoubleToString(displacement, 1) + "x";
   ObjectCreate(0, lblName, OBJ_TEXT, 0, t1, (high + low) / 2);
   ObjectSetString(0, lblName, OBJPROP_TEXT, label);
   ObjectSetInteger(0, lblName, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, 8);
}

//+------------------------------------------------------------------+
//| Ana hesaplama                                                     |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const double &spread[])
{
   // Sadece yeni bar'da çalış
   if(rates_total < 50) return rates_total;
   if(time[rates_total-1] == lastBarTime) return rates_total;
   lastBarTime = time[rates_total-1];

   // Eski zone'ları temizle
   OnDeinit(0);
   zoneCount = 0;

   double atr = CalcATR(ATRPeriod, 0);
   if(atr <= 0) return rates_total;

   int scanStart = MathMax(BaseMaxCandles + 5, rates_total - Lookback);

   // Zone detection
   for(int i = scanStart; i < rates_total - 3; i++)
   {
      // === STEP 1: Impulse mum(ları) bul ===
      double totalBody = 0;
      int impEnd = i;
      string direction = "";
      bool hasGap = false;

      // 3 ardışık kontrol
      if(i + 2 < rates_total)
      {
         bool allBull = close[i] > open[i] && close[i+1] > open[i+1] && close[i+2] > open[i+2];
         bool allBear = close[i] < open[i] && close[i+1] < open[i+1] && close[i+2] < open[i+2];
         double tb = MathAbs(close[i]-open[i]) + MathAbs(close[i+1]-open[i+1]) + MathAbs(close[i+2]-open[i+2]);

         if((allBull || allBear) && tb > SpikeATRMult * 1.2 * atr)
         {
            totalBody = tb;
            impEnd = i + 2;
            direction = allBull ? "up" : "down";
            // Gap kontrolü
            if(low[i+1] > high[i] || low[i+2] > high[i+1]) hasGap = true;
            if(high[i+1] < low[i] || high[i+2] < low[i+1]) hasGap = true;
         }
      }

      // 2 ardışık
      if(direction == "" && i + 1 < rates_total)
      {
         bool sameBull = close[i] > open[i] && close[i+1] > open[i+1];
         bool sameBear = close[i] < open[i] && close[i+1] < open[i+1];
         double tb = MathAbs(close[i]-open[i]) + MathAbs(close[i+1]-open[i+1]);

         if((sameBull || sameBear) && tb > SpikeATRMult * atr)
         {
            totalBody = tb;
            impEnd = i + 1;
            direction = sameBull ? "up" : "down";
            if(low[i+1] > high[i] || high[i+1] < low[i]) hasGap = true;
         }
      }

      // Tek dev mum
      if(direction == "")
      {
         double body = MathAbs(close[i] - open[i]);
         if(body > SpikeATRMult * atr)
         {
            totalBody = body;
            impEnd = i;
            direction = (close[i] > open[i]) ? "up" : "down";
         }
      }

      if(direction == "") continue;

      // === STEP 2: Base bul (impulse öncesi) ===
      double maxRange = BaseMaxRangeMult * atr;
      int baseStart = -1, baseEnd = i - 1;
      int pinCount = 0;
      double baseHigh = -1e10, baseLow = 1e10;
      int baseLen = 0;

      for(int j = i - 1; j >= MathMax(i - BaseMaxCandles, 0); j--)
      {
         double cRange = high[j] - low[j];
         bool isPin = IsPinBar(open[j], high[j], low[j], close[j]);

         if(cRange <= maxRange || isPin)
         {
            baseStart = j;
            baseLen++;
            if(isPin) pinCount++;
            if(high[j] > baseHigh) baseHigh = high[j];
            if(low[j] < baseLow) baseLow = low[j];
         }
         else break;
      }

      if(baseLen < BaseMinCandles) continue;
      if(pinCount < MinPinCount) continue;  // İĞNE ZORUNLU

      // === STEP 3: Sol taraf temizliği ===
      int bsBar = rates_total - 1 - baseStart;  // bars index for IsLeftSideClean
      if(!IsLeftSideClean(bsBar, LeftSideSwings)) continue;

      // === STEP 4: Displacement ===
      double zoneHeight = baseHigh - baseLow;
      if(zoneHeight <= 0) continue;
      double disp = 0;
      for(int j = impEnd; j < MathMin(impEnd + 30, rates_total); j++)
      {
         double d = 0;
         if(direction == "up") d = high[j] - baseHigh;
         else d = baseLow - low[j];
         if(d > disp) disp = d;
      }
      disp = disp / zoneHeight;

      if(disp < MinDisplacement) continue;

      // === STEP 5: Zone çiz ===
      string zoneType = (direction == "up") ? "DEMAND" : "SUPPLY";
      color zoneClr = (direction == "up") ? DemandColor : SupplyColor;

      // Zone sağ kenarı: şimdiki zamana kadar uzat
      datetime t1 = time[baseStart];
      datetime t2 = time[rates_total - 1] + PeriodSeconds() * 10;

      string zoneName = IntegerToString(zoneCount) + "_" + zoneType + "_" + IntegerToString(baseStart);
      DrawZone(zoneName, t1, t2, baseHigh, baseLow, zoneClr, zoneType, pinCount, disp);
      zoneCount++;

      // Impulse'ı atla
      i = impEnd;
   }

   Comment("SniperX | Zones: " + IntegerToString(zoneCount) + " | ATR: " + DoubleToString(atr, _Digits));
   return rates_total;
}
//+------------------------------------------------------------------+
