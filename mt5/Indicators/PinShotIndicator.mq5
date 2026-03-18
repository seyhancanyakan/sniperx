//+------------------------------------------------------------------+
//|                                          PinShotIndicator.mq5    |
//|                                         Sozy AI Technology 2026  |
//|                        Visual-only indicator — no trading         |
//+------------------------------------------------------------------+
#property copyright "Sozy AI Technology"
#property link      "https://github.com/openclaw/pinshot"
#property version   "1.00"
#property indicator_chart_window

#include <../Include/PinShotCore.mqh>

//--- Inputs
input int    InpATRPeriod    = 14;
input int    InpMinAccumBars = 4;
input int    InpMaxAccumBars = 20;
input double InpRangeMult    = 0.6;
input int    InpMinPins      = 2;
input double InpBreakoutBody = 1.5;
input int    InpBreakoutMin  = 2;
input double InpBreakoutTotal= 3.0;
input int    InpMaxLeftSwings= 3;
input int    InpMaxZones     = 30;
input color  InpBuyColor     = clrLimeGreen;
input color  InpSellColor    = clrRed;
input color  InpGapColor     = clrGold;

//--- Globals
SZone IndZones[];
int   IndZoneCount = 0;

//+------------------------------------------------------------------+
int OnInit()
{
    ArrayResize(IndZones, InpMaxZones);
    IndZoneCount = 0;
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    ObjectsDeleteAll(0, "PSI_");
    Comment("");
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
    if(rates_total < InpMaxAccumBars + 30) return rates_total;

    // Only recalculate on new bars
    static datetime lastTime = 0;
    if(time[rates_total - 1] == lastTime) return rates_total;
    lastTime = time[rates_total - 1];

    // Clear old zones and objects
    ObjectsDeleteAll(0, "PSI_");
    IndZoneCount = 0;

    double atr = CalcATR(_Symbol, PERIOD_CURRENT, InpATRPeriod, 1);
    if(atr <= 0) return rates_total;

    double maxRange = atr * InpRangeMult;
    int scanLimit = MathMin(Bars(_Symbol, PERIOD_CURRENT) - InpMaxAccumBars - 10, 300);

    for(int start = InpMaxAccumBars + 5; start < scanLimit && IndZoneCount < InpMaxZones; start++)
    {
        for(int length = InpMinAccumBars; length <= InpMaxAccumBars; length++)
        {
            int endBar = start;
            int startBar = start + length - 1;

            double zHigh = iHigh(_Symbol, PERIOD_CURRENT, endBar);
            double zLow  = iLow(_Symbol, PERIOD_CURRENT, endBar);
            for(int i = endBar; i <= startBar; i++)
            {
                double h = iHigh(_Symbol, PERIOD_CURRENT, i);
                double l = iLow(_Symbol, PERIOD_CURRENT, i);
                if(h > zHigh) zHigh = h;
                if(l < zLow)  zLow = l;
            }

            if((zHigh - zLow) > maxRange) continue;

            int buyPins = 0, sellPins = 0;
            for(int i = endBar; i <= startBar; i++)
            {
                double o = iOpen(_Symbol, PERIOD_CURRENT, i);
                double h = iHigh(_Symbol, PERIOD_CURRENT, i);
                double l = iLow(_Symbol, PERIOD_CURRENT, i);
                double c = iClose(_Symbol, PERIOD_CURRENT, i);
                if(IsPinBar(o, h, l, c, "BUY"))  buyPins++;
                if(IsPinBar(o, h, l, c, "SELL")) sellPins++;
            }

            string zType = "";
            int pins = 0;
            if(buyPins >= InpMinPins && buyPins >= sellPins)
            { zType = "BUY"; pins = buyPins; }
            else if(sellPins >= InpMinPins)
            { zType = "SELL"; pins = sellPins; }
            else continue;

            // Left side check
            double threshold = atr * 0.5;
            int swings = CountSwings(_Symbol, PERIOD_CURRENT, startBar + 15, startBar, threshold);
            if(swings > InpMaxLeftSwings) continue;

            // Check breakout
            bool hasBreakout = false;
            int checkStart = endBar - 1;
            if(checkStart < 1) continue;

            for(int d = 0; d < 2; d++)
            {
                string dir = (d == 0) ? "UP" : "DOWN";
                int consec = 0;
                double totalMove = 0;

                for(int i = checkStart; i >= MathMax(checkStart - 8, 1); i--)
                {
                    double o2 = iOpen(_Symbol, PERIOD_CURRENT, i);
                    double c2 = iClose(_Symbol, PERIOD_CURRENT, i);
                    double body = MathAbs(c2 - o2);

                    if(dir == "UP" && c2 > o2 && body > atr * InpBreakoutBody)
                    { consec++; totalMove += body; }
                    else if(dir == "DOWN" && c2 < o2 && body > atr * InpBreakoutBody)
                    { consec++; totalMove += body; }
                    else if(consec > 0) break;
                    else break;
                }

                if(consec >= InpBreakoutMin && totalMove >= atr * InpBreakoutTotal)
                {
                    if((dir == "UP" && zType == "BUY") || (dir == "DOWN" && zType == "SELL"))
                    {
                        hasBreakout = true;
                        break;
                    }
                }
            }

            if(!hasBreakout) continue;

            // Draw zone rectangle
            string name = "PSI_Zone_" + IntegerToString(IndZoneCount);
            datetime t1 = iTime(_Symbol, PERIOD_CURRENT, startBar);
            datetime t2 = iTime(_Symbol, PERIOD_CURRENT, endBar) + PeriodSeconds(PERIOD_CURRENT) * 15;
            color clr = (zType == "BUY") ? InpBuyColor : InpSellColor;

            ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, zHigh, t2, zLow);
            ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
            ObjectSetInteger(0, name, OBJPROP_FILL, true);
            ObjectSetInteger(0, name, OBJPROP_BACK, true);

            // Label
            string lbl = "PSI_Lbl_" + IntegerToString(IndZoneCount);
            string text = zType + " | PIN:" + IntegerToString(pins);
            ObjectCreate(0, lbl, OBJ_TEXT, 0, t1, zHigh);
            ObjectSetString(0, lbl, OBJPROP_TEXT, text);
            ObjectSetInteger(0, lbl, OBJPROP_COLOR, clr);
            ObjectSetInteger(0, lbl, OBJPROP_FONTSIZE, 8);

            IndZoneCount++;
            break;
        }
    }

    ChartRedraw(0);
    return rates_total;
}
//+------------------------------------------------------------------+
