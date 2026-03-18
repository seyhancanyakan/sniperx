//+------------------------------------------------------------------+
//|                                               PinShotCore.mqh    |
//|                                         Sozy AI Technology 2026  |
//|                             Shared structures and helper funcs   |
//+------------------------------------------------------------------+
#ifndef PINSHOT_CORE_MQH
#define PINSHOT_CORE_MQH

//--- Zone Structure
struct SZone
{
    string   symbol;
    ENUM_TIMEFRAMES timeframe;
    string   zoneType;       // "BUY" or "SELL"
    string   status;         // "ACTIVE","TRIGGERED","BROKEN","STALE"
    double   high;
    double   low;
    datetime startTime;
    datetime endTime;
    int      startBar;
    int      endBar;
    int      pinCount;
    string   breakoutDir;    // "UP" or "DOWN"
    double   breakoutMag;    // ATR multiples
    int      breakoutCandles;
    int      gapCount;
    double   gapTotal;
    double   triScore;
    double   wsScore;
    double   confidence;
    bool     orderPlaced;
    long     orderTicket;
    bool     reversed;
};

//--- Signal Structure
struct SSignal
{
    string   direction;      // "BUY" or "SELL"
    double   entryPrice;
    double   stopLoss;
    double   takeProfit1;
    double   takeProfit2;
    double   lotSize;
    double   confidence;
    int      zoneIndex;
};

//--- Managed Position
struct SManagedPos
{
    long     ticket;
    string   symbol;
    string   direction;
    double   entryPrice;
    double   stopLoss;
    double   takeProfit1;
    double   takeProfit2;
    double   initialLot;
    double   currentLot;
    bool     tp1Hit;
    bool     beSet;
    bool     trailingActive;
    double   currentSL;
    double   zoneHeight;
    int      zoneIndex;
};

//--- Gap Structure
struct SGap
{
    string   gapType;        // "BULLISH" or "BEARISH"
    double   high;
    double   low;
    double   size;
    int      barIndex;
    datetime time;
};

//+------------------------------------------------------------------+
//| IsPinBar — detect pin bar by wick ratio                          |
//+------------------------------------------------------------------+
bool IsPinBar(double open, double high, double low, double close, string direction)
{
    double body = MathAbs(close - open);
    if(body < _Point * 0.1)
        body = _Point * 0.1;

    double upperWick = high - MathMax(open, close);
    double lowerWick = MathMin(open, close) - low;

    if(direction == "BUY")
        return (lowerWick > 2.0 * body);
    else if(direction == "SELL")
        return (upperWick > 2.0 * body);
    else
        return (lowerWick > 2.0 * body || upperWick > 2.0 * body);
}

//+------------------------------------------------------------------+
//| CalcATR — manual ATR calculation                                 |
//+------------------------------------------------------------------+
double CalcATR(string symbol, ENUM_TIMEFRAMES tf, int period, int shift = 0)
{
    double atr = 0;
    for(int i = shift + 1; i <= shift + period; i++)
    {
        double h  = iHigh(symbol, tf, i);
        double l  = iLow(symbol, tf, i);
        double pc = iClose(symbol, tf, i + 1);
        double tr = MathMax(h - l, MathMax(MathAbs(h - pc), MathAbs(l - pc)));
        atr += tr;
    }
    return (period > 0) ? atr / period : 0;
}

//+------------------------------------------------------------------+
//| CountSwings — count direction changes                            |
//+------------------------------------------------------------------+
int CountSwings(string symbol, ENUM_TIMEFRAMES tf, int startBar, int endBar, double threshold)
{
    if(startBar <= endBar || startBar < 0)
        return 0;

    int swings = 0;
    int direction = 0;
    double lastSwing = iClose(symbol, tf, startBar);

    for(int i = startBar - 1; i >= endBar; i--)
    {
        double price = iClose(symbol, tf, i);
        double move = price - lastSwing;

        if(MathAbs(move) >= threshold)
        {
            int newDir = (move > 0) ? 1 : -1;
            if(newDir != direction && direction != 0)
            {
                swings++;
            }
            direction = newDir;
            lastSwing = price;
        }
    }
    return swings;
}

//+------------------------------------------------------------------+
//| NormalizeLot — round lot to broker specs                         |
//+------------------------------------------------------------------+
double NormalizeLot(string symbol, double lot)
{
    double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
    double maxLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
    double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);

    if(lotStep <= 0) lotStep = 0.01;
    lot = MathMax(minLot, lot);
    lot = MathMin(maxLot, lot);
    lot = MathRound(lot / lotStep) * lotStep;
    return NormalizeDouble(lot, 2);
}

#endif
