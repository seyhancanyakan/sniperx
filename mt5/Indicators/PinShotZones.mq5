//+------------------------------------------------------------------+
//|                                              PinShotZones.mq5    |
//|                        SniperX PinShot Zone Indicator             |
//|                  Compact micro-origin zone detector               |
//+------------------------------------------------------------------+
#property copyright "SniperX"
#property version   "4.1"
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0
#property strict

//--- Input parameters
input int    InpLookback          = 200;    // Lookback bars
input double InpSpikeATRMult      = 1.0;    // Min impulse body (ATR multiple)
input int    InpMaxClusterCandles  = 6;      // Max cluster candles
input double InpMaxClusterHeightATR= 2.5;   // Max cluster height (ATR)
input int    InpMaxBarsToImpulse   = 2;      // Max bars cluster-to-impulse
input double InpMinDisplacement    = 1.0;    // Min displacement (zone height x)
input int    InpLeftSideMaxSwings  = 5;      // Max left-side swings
input int    InpATRPeriod          = 14;     // ATR period
input double InpShelfTolerance     = 0.10;   // Shelf tolerance (ATR fraction)
input bool   InpUseTrendFilter     = true;   // Trend extreme filter
input bool   InpShowStale          = false;  // Show stale zones
input color  InpDemandColor        = clrLime;
input color  InpSupplyColor        = clrRed;
input color  InpStaleColor         = clrGray;
input int    InpMaxZones           = 10;     // Max zones displayed

//--- Zone data arrays (parallel arrays instead of struct with strings)
double g_zHigh[];
double g_zLow[];
double g_wHigh[];
double g_wLow[];
double g_disp[];
double g_conf[];
double g_mag[];
int    g_startIdx[];
int    g_endIdx[];
int    g_impIdx[];
int    g_pins[];
int    g_gaps[];
int    g_cCount[];
int    g_touch[];
int    g_type[];     // 0=demand, 1=supply
int    g_status[];   // 0=fresh, 1=active, 2=stale
bool   g_valid[];
int    g_zoneCount = 0;
string g_prefix = "PSZ_";

//+------------------------------------------------------------------+
int OnInit()
{
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   ObjectsDeleteAll(0, g_prefix);
}

//+------------------------------------------------------------------+
double CalcATR(int period, int shift)
{
   double sum = 0;
   int bars = Bars(_Symbol, _Period);
   for(int i = shift + 1; i <= shift + period && i < bars; i++)
   {
      double h = iHigh(_Symbol, _Period, i);
      double l = iLow(_Symbol, _Period, i);
      double pc = iClose(_Symbol, _Period, i + 1);
      double tr = MathMax(h - l, MathMax(MathAbs(h - pc), MathAbs(l - pc)));
      sum += tr;
   }
   return (period > 0) ? sum / period : 0;
}

//+------------------------------------------------------------------+
double AvgVolume(int idx, int lookback = 20)
{
   double sum = 0;
   int cnt = 0;
   int bars = Bars(_Symbol, _Period);
   for(int i = idx + 1; i <= idx + lookback && i < bars; i++)
   {
      sum += (double)iVolume(_Symbol, _Period, i);
      cnt++;
   }
   return (cnt > 0) ? sum / cnt : 0;
}

//+------------------------------------------------------------------+
bool IsBuyPin(int idx)
{
   double o = iOpen(_Symbol, _Period, idx);
   double c = iClose(_Symbol, _Period, idx);
   double l = iLow(_Symbol, _Period, idx);
   double body = MathAbs(c - o);
   double lw = MathMin(o, c) - l;
   if(body < _Point) body = _Point;
   return (lw > body);
}

//+------------------------------------------------------------------+
bool IsSellPin(int idx)
{
   double o = iOpen(_Symbol, _Period, idx);
   double c = iClose(_Symbol, _Period, idx);
   double h = iHigh(_Symbol, _Period, idx);
   double body = MathAbs(c - o);
   double uw = h - MathMax(o, c);
   if(body < _Point) body = _Point;
   return (uw > body);
}

//+------------------------------------------------------------------+
bool CheckLeftSide(int baseStart, int maxSwings)
{
   int bars = Bars(_Symbol, _Period);
   int checkStart = MathMin(baseStart + 15, bars - 1);
   if(checkStart - baseStart < 3) return true;
   int swings = 0;
   int prevDir = 0;
   for(int i = checkStart - 1; i > baseStart; i--)
   {
      double curr = iClose(_Symbol, _Period, i);
      double prev = iClose(_Symbol, _Period, i + 1);
      int dir = (curr > prev) ? 1 : -1;
      if(prevDir != 0 && dir != prevDir) swings++;
      prevDir = dir;
   }
   return (swings <= maxSwings);
}

//+------------------------------------------------------------------+
bool CheckTrendExtreme(int baseStart, int dir) // dir: 0=up, 1=down
{
   int bars = Bars(_Symbol, _Period);
   int tb = MathMin(20, bars - baseStart - 1);
   if(tb < 5) return true;
   double ts = iClose(_Symbol, _Period, baseStart + tb);
   double te = iClose(_Symbol, _Period, baseStart);
   double move = te - ts;
   if(dir == 0 && move > 0) return false; // up impulse but uptrend = bad demand
   if(dir == 1 && move < 0) return false; // down impulse but downtrend = bad supply
   return true;
}

//+------------------------------------------------------------------+
bool DetectImpulse(int idx, double atr, int &dir, double &mag, int &endIdx, bool &hasGap)
{
   // dir: 0=up, 1=down
   if(atr <= 0 || idx < 0) return false;
   int bars = Bars(_Symbol, _Period);

   double o0 = iOpen(_Symbol, _Period, idx);
   double c0 = iClose(_Symbol, _Period, idx);
   double h0 = iHigh(_Symbol, _Period, idx);
   double l0 = iLow(_Symbol, _Period, idx);
   double b0 = MathAbs(c0 - o0);
   bool bull0 = (c0 > o0);
   hasGap = false;

   // 3 consecutive
   if(idx >= 2)
   {
      double o1 = iOpen(_Symbol, _Period, idx-1), c1 = iClose(_Symbol, _Period, idx-1);
      double o2 = iOpen(_Symbol, _Period, idx-2), c2 = iClose(_Symbol, _Period, idx-2);
      double b1 = MathAbs(c1 - o1), b2 = MathAbs(c2 - o2);
      bool bull1 = (c1 > o1), bull2 = (c2 > o2);
      bool allBull = bull0 && bull1 && bull2;
      bool allBear = !bull0 && !bull1 && !bull2;
      double total = b0 + b1 + b2;
      if((allBull || allBear) && total > InpSpikeATRMult * 1.2 * atr)
      {
         dir = allBull ? 0 : 1;
         mag = NormalizeDouble(total / atr, 1);
         endIdx = idx - 2;
         if(dir == 0 && iLow(_Symbol, _Period, idx-1) > iHigh(_Symbol, _Period, idx)) hasGap = true;
         if(dir == 1 && iHigh(_Symbol, _Period, idx-1) < iLow(_Symbol, _Period, idx)) hasGap = true;
         return true;
      }
   }

   // 2 consecutive
   if(idx >= 1)
   {
      double o1 = iOpen(_Symbol, _Period, idx-1), c1 = iClose(_Symbol, _Period, idx-1);
      double b1 = MathAbs(c1 - o1);
      bool bull1 = (c1 > o1);
      bool same = (bull0 && bull1) || (!bull0 && !bull1);
      if(same && (b0 + b1) > InpSpikeATRMult * atr)
      {
         dir = bull0 ? 0 : 1;
         mag = NormalizeDouble((b0 + b1) / atr, 1);
         endIdx = idx - 1;
         if(dir == 0 && iLow(_Symbol, _Period, idx-1) > iHigh(_Symbol, _Period, idx)) hasGap = true;
         if(dir == 1 && iHigh(_Symbol, _Period, idx-1) < iLow(_Symbol, _Period, idx)) hasGap = true;
         return true;
      }
   }

   // Single sword
   if(b0 > InpSpikeATRMult * atr)
   {
      dir = bull0 ? 0 : 1;
      mag = NormalizeDouble(b0 / atr, 1);
      endIdx = idx;
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
void ClusterShelves(int sIdx, int eIdx, double atr,
                    double &upperShelf, double &lowerShelf,
                    double &wHigh, double &wLow)
{
   double levels[];
   int cnt = 0;
   wHigh = 0;
   wLow = 999999;

   for(int i = sIdx; i >= eIdx; i--)
   {
      double o = iOpen(_Symbol, _Period, i);
      double c = iClose(_Symbol, _Period, i);
      double h = iHigh(_Symbol, _Period, i);
      double l = iLow(_Symbol, _Period, i);
      ArrayResize(levels, cnt + 4);
      levels[cnt++] = o;
      levels[cnt++] = c;
      levels[cnt++] = h;
      levels[cnt++] = l;
      if(h > wHigh) wHigh = h;
      if(l < wLow) wLow = l;
   }

   ArraySort(levels);
   double tol = atr * InpShelfTolerance;
   double mid = (wHigh + wLow) / 2;

   double bestUpper = levels[cnt - 1];
   double bestLower = levels[0];
   int bestUC = 1, bestLC = 1;

   bool used[];
   ArrayResize(used, cnt);
   for(int x = 0; x < cnt; x++) used[x] = false;

   for(int i = 0; i < cnt; i++)
   {
      if(used[i]) continue;
      double s = levels[i];
      int c2 = 1;
      used[i] = true;
      for(int j = i + 1; j < cnt; j++)
      {
         if(used[j]) continue;
         if(levels[j] - levels[i] <= tol)
         {
            s += levels[j]; c2++; used[j] = true;
         }
         else break;
      }
      if(c2 >= 2)
      {
         double avg = s / c2;
         if(avg < mid && c2 > bestLC) { bestLower = avg; bestLC = c2; }
         if(avg >= mid && c2 > bestUC) { bestUpper = avg; bestUC = c2; }
      }
   }

   // Fallback: body band
   if(bestUC < 2 || bestLC < 2)
   {
      bestUpper = 0;
      bestLower = 999999;
      for(int i = sIdx; i >= eIdx; i--)
      {
         double bt = MathMax(iOpen(_Symbol, _Period, i), iClose(_Symbol, _Period, i));
         double bb = MathMin(iOpen(_Symbol, _Period, i), iClose(_Symbol, _Period, i));
         if(bt > bestUpper) bestUpper = bt;
         if(bb < bestLower) bestLower = bb;
      }
   }
   upperShelf = bestUpper;
   lowerShelf = bestLower;
}

//+------------------------------------------------------------------+
bool DetectBase(int impIdx, double atr, int &bStart, int &bEnd,
                double &zH, double &zL, double &wH, double &wL,
                int &pins, int &cCnt)
{
   int bars = Bars(_Symbol, _Period);
   if(impIdx + 1 >= bars) return false;

   int maxB = InpMaxClusterCandles;
   int count = 0;
   bEnd = impIdx + 1;

   for(int i = bEnd; i <= bEnd + maxB - 1 && i < bars; i++)
   {
      double o = iOpen(_Symbol, _Period, i);
      double c = iClose(_Symbol, _Period, i);
      double h = iHigh(_Symbol, _Period, i);
      double l = iLow(_Symbol, _Period, i);
      double range = h - l;
      double body = MathAbs(c - o);
      double bodyPct = (range > _Point) ? body / range : 1.0;
      if(bodyPct > 0.7 && range > atr) break;
      count++;
   }
   if(count < 2) return false;

   bStart = bEnd + count - 1;
   cCnt = count;

   ClusterShelves(bStart, bEnd, atr, zH, zL, wH, wL);

   double height = zH - zL;
   if(height > InpMaxClusterHeightATR * atr)
   {
      int trim = MathMin(3, count);
      bStart = bEnd + trim - 1;
      cCnt = trim;
      ClusterShelves(bStart, bEnd, atr, zH, zL, wH, wL);
      height = zH - zL;
      if(height > InpMaxClusterHeightATR * atr || cCnt < 2) return false;
   }

   pins = 0;
   bool isBull = (iClose(_Symbol, _Period, impIdx) > iOpen(_Symbol, _Period, impIdx));
   for(int i = bStart; i >= bEnd; i--)
   {
      if(isBull && IsBuyPin(i)) pins++;
      if(!isBull && IsSellPin(i)) pins++;
   }
   return true;
}

//+------------------------------------------------------------------+
double CalcDisplacement(int impEnd, double zH, double zL, int dir)
{
   double zh = zH - zL;
   if(zh <= 0) return 0;
   double mx = 0;
   for(int j = impEnd; j >= MathMax(impEnd - 10, 0); j--)
   {
      double d = 0;
      if(dir == 0) d = iHigh(_Symbol, _Period, j) - zH;
      else         d = zL - iLow(_Symbol, _Period, j);
      if(d > mx) mx = d;
   }
   return mx / zh;
}

//+------------------------------------------------------------------+
double CalcConf(int pins, int gaps, double mag, double disp, int cCnt, bool hasGap)
{
   double c = 30;
   if(pins >= 2) c += 20;
   if(pins >= 3) c += 10;
   if(pins < 2)  c -= 10;
   if(gaps > 0)  c += 10;
   if(hasGap)    c += 10;
   if(mag > 3)   c += 10;
   if(mag > 5)   c += 5;
   if(disp >= 3) c += 10;
   if(disp >= 5) c += 5;
   if(cCnt <= 3) c += 5;
   if(cCnt >= 6) c -= 5;
   return MathMin(MathMax(c, 0), 100);
}

//+------------------------------------------------------------------+
void DrawZone(int zi)
{
   string name = g_prefix + IntegerToString(zi);

   datetime t1 = iTime(_Symbol, _Period, g_startIdx[zi]);
   datetime t2;
   if(g_status[zi] <= 1) // fresh or active: extend to current bar
      t2 = iTime(_Symbol, _Period, 0) + PeriodSeconds() * 3;
   else
      t2 = iTime(_Symbol, _Period, g_endIdx[zi]) + PeriodSeconds() * 5;

   color clr;
   if(g_status[zi] == 2) clr = InpStaleColor;
   else if(g_type[zi] == 0) clr = InpDemandColor;
   else clr = InpSupplyColor;

   // Rectangle
   string rn = name + "r";
   ObjectDelete(0, rn);
   ObjectCreate(0, rn, OBJ_RECTANGLE, 0, t1, g_zHigh[zi], t2, g_zLow[zi]);
   ObjectSetInteger(0, rn, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, rn, OBJPROP_STYLE, STYLE_SOLID);
   ObjectSetInteger(0, rn, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, rn, OBJPROP_FILL, true);
   ObjectSetInteger(0, rn, OBJPROP_BACK, true);
   ObjectSetInteger(0, rn, OBJPROP_SELECTABLE, false);

   // Label
   string ln = name + "l";
   ObjectDelete(0, ln);
   string lbl = (g_type[zi] == 0 ? "D" : "S") +
                " P:" + IntegerToString(g_pins[zi]) +
                " " + DoubleToString(g_conf[zi], 0) + "%" +
                (g_gaps[zi] > 0 ? " FVG" : "") +
                " x" + DoubleToString(g_disp[zi], 1);
   double ly = (g_type[zi] == 0) ? g_zLow[zi] : g_zHigh[zi];
   ObjectCreate(0, ln, OBJ_TEXT, 0, t1, ly);
   ObjectSetString(0, ln, OBJPROP_TEXT, lbl);
   ObjectSetInteger(0, ln, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, ln, OBJPROP_FONTSIZE, 7);
   ObjectSetString(0, ln, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, ln, OBJPROP_SELECTABLE, false);

   // Wick envelope dashed lines
   if(g_wHigh[zi] > g_zHigh[zi] + _Point)
   {
      string wn = name + "wh";
      ObjectDelete(0, wn);
      ObjectCreate(0, wn, OBJ_TREND, 0, t1, g_wHigh[zi], t2, g_wHigh[zi]);
      ObjectSetInteger(0, wn, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, wn, OBJPROP_STYLE, STYLE_DOT);
      ObjectSetInteger(0, wn, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, wn, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, wn, OBJPROP_BACK, true);
      ObjectSetInteger(0, wn, OBJPROP_SELECTABLE, false);
   }
   if(g_wLow[zi] < g_zLow[zi] - _Point)
   {
      string wn = name + "wl";
      ObjectDelete(0, wn);
      ObjectCreate(0, wn, OBJ_TREND, 0, t1, g_wLow[zi], t2, g_wLow[zi]);
      ObjectSetInteger(0, wn, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, wn, OBJPROP_STYLE, STYLE_DOT);
      ObjectSetInteger(0, wn, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, wn, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, wn, OBJPROP_BACK, true);
      ObjectSetInteger(0, wn, OBJPROP_SELECTABLE, false);
   }
}

//+------------------------------------------------------------------+
void ResizeArrays(int size)
{
   ArrayResize(g_zHigh, size);
   ArrayResize(g_zLow, size);
   ArrayResize(g_wHigh, size);
   ArrayResize(g_wLow, size);
   ArrayResize(g_disp, size);
   ArrayResize(g_conf, size);
   ArrayResize(g_mag, size);
   ArrayResize(g_startIdx, size);
   ArrayResize(g_endIdx, size);
   ArrayResize(g_impIdx, size);
   ArrayResize(g_pins, size);
   ArrayResize(g_gaps, size);
   ArrayResize(g_cCount, size);
   ArrayResize(g_touch, size);
   ArrayResize(g_type, size);
   ArrayResize(g_status, size);
   ArrayResize(g_valid, size);
}

//+------------------------------------------------------------------+
bool HasOverlap(double h1, double l1, double h2, double l2)
{
   double oh = MathMin(h1, h2);
   double ol = MathMax(l1, l2);
   if(oh <= ol) return false;
   double ht = h1 - l1;
   if(ht <= 0) return false;
   return ((oh - ol) / ht) > 0.5;
}

//+------------------------------------------------------------------+
void DetectAllZones()
{
   ObjectsDeleteAll(0, g_prefix);
   g_zoneCount = 0;

   int totalBars = Bars(_Symbol, _Period);
   if(totalBars < 50) return;

   double atr = CalcATR(InpATRPeriod, 0);
   if(atr <= 0) return;

   int scanEnd = MathMin(InpLookback, totalBars - 20);

   for(int i = 3; i < scanEnd && g_zoneCount < InpMaxZones; i++)
   {
      int dir;
      double mag;
      int impEnd;
      bool hasGap;

      if(!DetectImpulse(i, atr, dir, mag, impEnd, hasGap))
         continue;

      int bStart, bEnd, pins, cCnt;
      double zH, zL, wH, wL;

      if(!DetectBase(i, atr, bStart, bEnd, zH, zL, wH, wL, pins, cCnt))
      { i = MathMax(i, impEnd); continue; }

      // Adjacency
      int gap = bEnd - i - 1;
      if(gap > InpMaxBarsToImpulse)
      { i = MathMax(i, impEnd); continue; }

      // Left side
      if(!CheckLeftSide(bStart, InpLeftSideMaxSwings))
      { i = MathMax(i, impEnd); continue; }

      // Trend filter
      if(InpUseTrendFilter && !CheckTrendExtreme(bStart, dir))
      { i = MathMax(i, impEnd); continue; }

      // Price overlap
      bool ovlp = false;
      for(int z = 0; z < g_zoneCount; z++)
      {
         if(g_valid[z] && HasOverlap(zH, zL, g_zHigh[z], g_zLow[z]))
         { ovlp = true; break; }
      }
      if(ovlp) { i = MathMax(i, impEnd); continue; }

      // Displacement
      double disp = CalcDisplacement(impEnd, zH, zL, dir);
      double conf = CalcConf(pins, hasGap ? 1 : 0, mag, disp, cCnt, hasGap);

      // Status
      int status = 0; // fresh
      if(disp >= InpMinDisplacement) status = 1; // active

      // Touch check
      int touch = 0;
      for(int k = bEnd - 1; k >= 0; k--)
      {
         if(iLow(_Symbol, _Period, k) <= zH && iHigh(_Symbol, _Period, k) >= zL)
         { touch++; break; }
      }

      // Stale
      if(bEnd > 200) { status = 2; if(!InpShowStale) { i = MathMax(i, impEnd); continue; } }

      // Store
      int zi = g_zoneCount;
      ResizeArrays(zi + 1);
      g_zHigh[zi] = zH;
      g_zLow[zi] = zL;
      g_wHigh[zi] = wH;
      g_wLow[zi] = wL;
      g_disp[zi] = NormalizeDouble(disp, 1);
      g_conf[zi] = NormalizeDouble(conf, 0);
      g_mag[zi] = mag;
      g_startIdx[zi] = bStart;
      g_endIdx[zi] = bEnd;
      g_impIdx[zi] = i;
      g_pins[zi] = pins;
      g_gaps[zi] = hasGap ? 1 : 0;
      g_cCount[zi] = cCnt;
      g_touch[zi] = touch;
      g_type[zi] = dir == 0 ? 0 : 1; // 0=demand(up), 1=supply(down)
      g_status[zi] = status;
      g_valid[zi] = true;
      g_zoneCount++;

      i = MathMax(i, impEnd);
   }

   // Conflict resolution
   for(int a = 0; a < g_zoneCount; a++)
   {
      if(!g_valid[a]) continue;
      for(int b = a + 1; b < g_zoneCount; b++)
      {
         if(!g_valid[b]) continue;
         if(g_type[a] == g_type[b]) continue;
         double midA = (g_zHigh[a] + g_zLow[a]) / 2;
         double midB = (g_zHigh[b] + g_zLow[b]) / 2;
         if(MathAbs(midA - midB) < atr * 0.5)
         {
            if(g_conf[a] >= g_conf[b]) g_valid[b] = false;
            else g_valid[a] = false;
         }
      }
   }

   // Draw
   int drawn = 0;
   for(int z = 0; z < g_zoneCount && drawn < InpMaxZones; z++)
   {
      if(!g_valid[z]) continue;
      DrawZone(z);
      drawn++;
   }
}

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
                const int &spread[])
{
   static datetime lastBar = 0;
   datetime curBar = iTime(_Symbol, _Period, 0);
   if(curBar != lastBar)
   {
      lastBar = curBar;
      DetectAllZones();
   }
   return(rates_total);
}
//+------------------------------------------------------------------+
