//+------------------------------------------------------------------+
//|                                                  PinShotEA.mq5   |
//|                                         Sozy AI Technology 2026  |
//|                  Market Maker Footprint Detector & Sniper Entry   |
//+------------------------------------------------------------------+
#property copyright "Sozy AI Technology"
#property link      "https://github.com/openclaw/pinshot"
#property version   "1.00"
#property strict

#include <../Include/PinShotCore.mqh>

//--- Input: Accumulation
input int    InpATRPeriod         = 14;
input int    InpMinAccumBars      = 4;
input int    InpMaxAccumBars      = 20;
input double InpRangeMult         = 0.6;
input int    InpMinPins           = 2;

//--- Input: Breakout
input double InpBreakoutBodyMult  = 1.5;
input int    InpBreakoutMinCandles= 2;
input double InpBreakoutTotalMult = 3.0;

//--- Input: Filters
input int    InpMaxReturnBars     = 50;
input double InpMinTriScore       = 60;
input double InpMinWSScore        = 50;
input int    InpMaxLeftSwings     = 3;

//--- Input: Trade
input double InpRiskPercent       = 3.0;
input double InpTP1_RR            = 2.0;
input double InpTP2_RR            = 3.0;
input int    InpMaxPositions      = 3;
input int    InpSLBufferPips      = 3;
input bool   InpUseBreakeven      = true;
input double InpTrailingActivation= 1.5;
input double InpTrailingDistMult  = 0.8;

//--- Input: Reverse
input bool   InpReverseEnabled    = true;
input double InpReverseMinMult    = 2.0;

//--- Input: General
input long   InpMagicNumber       = 20260316;
input int    InpMaxZones          = 20;
input bool   InpDrawRects         = true;
input bool   InpAlerts            = true;

//--- Input: Colors
input color  InpBuyZoneColor      = clrLimeGreen;
input color  InpSellZoneColor     = clrRed;
input color  InpStaleColor        = clrGray;
input color  InpGapColor          = clrGold;

//--- Global arrays
SZone        Zones[];
SManagedPos  MPositions[];
int          ZoneCount = 0;
int          MPosCount = 0;
datetime     LastBarTime = 0;
double       TotalR = 0;
int          TotalTrades = 0;
int          Wins = 0;

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    ArrayResize(Zones, InpMaxZones);
    ArrayResize(MPositions, InpMaxPositions * 2);
    ZoneCount = 0;
    MPosCount = 0;
    TotalR = 0;
    TotalTrades = 0;
    Wins = 0;

    Print("PinShot EA initialized | Magic: ", InpMagicNumber);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    ObjectsDeleteAll(0, "PS_");
    Comment("");
}

//+------------------------------------------------------------------+
//| IsNewBar                                                         |
//+------------------------------------------------------------------+
bool IsNewBar()
{
    datetime current = iTime(_Symbol, PERIOD_CURRENT, 0);
    if(current != LastBarTime)
    {
        LastBarTime = current;
        return true;
    }
    return false;
}

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
{
    ManagePositions();

    if(InpReverseEnabled)
        CheckReverse();

    if(IsNewBar())
    {
        UpdateZoneStatuses();
        ScanForZones();
        PlaceLimitOrders();
        if(InpDrawRects)
            DrawAllRectangles();
        UpdateComment();
    }
}

//+------------------------------------------------------------------+
//| ScanForZones — find accumulation zones                           |
//+------------------------------------------------------------------+
void ScanForZones()
{
    double atr = CalcATR(_Symbol, PERIOD_CURRENT, InpATRPeriod, 1);
    if(atr <= 0) return;

    double maxRange = atr * InpRangeMult;
    int barsAvailable = Bars(_Symbol, PERIOD_CURRENT);
    int scanLimit = MathMin(barsAvailable - InpMaxAccumBars - 10, 200);

    for(int start = InpMaxAccumBars + 5; start < scanLimit; start++)
    {
        if(ZoneCount >= InpMaxZones) break;

        // Check if bar already in a zone
        bool used = false;
        for(int z = 0; z < ZoneCount; z++)
        {
            if(start >= Zones[z].endBar && start <= Zones[z].startBar)
            { used = true; break; }
        }
        if(used) continue;

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

            // Count pins
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
            int    pins = 0;
            if(buyPins >= InpMinPins && buyPins >= sellPins)
            { zType = "BUY"; pins = buyPins; }
            else if(sellPins >= InpMinPins)
            { zType = "SELL"; pins = sellPins; }
            else continue;

            // Left side check
            if(!CheckLeftSideClean(startBar + 1, 15)) continue;

            // Validate breakout
            int bIdx = ZoneCount;
            if(ZoneCount >= InpMaxZones) break;

            Zones[ZoneCount].symbol    = _Symbol;
            Zones[ZoneCount].timeframe = PERIOD_CURRENT;
            Zones[ZoneCount].zoneType  = zType;
            Zones[ZoneCount].status    = "ACTIVE";
            Zones[ZoneCount].high      = zHigh;
            Zones[ZoneCount].low       = zLow;
            Zones[ZoneCount].startBar  = startBar;
            Zones[ZoneCount].endBar    = endBar;
            Zones[ZoneCount].startTime = iTime(_Symbol, PERIOD_CURRENT, startBar);
            Zones[ZoneCount].endTime   = iTime(_Symbol, PERIOD_CURRENT, endBar);
            Zones[ZoneCount].pinCount  = pins;
            Zones[ZoneCount].orderPlaced = false;
            Zones[ZoneCount].orderTicket = 0;
            Zones[ZoneCount].reversed    = false;
            Zones[ZoneCount].gapCount    = 0;
            Zones[ZoneCount].gapTotal    = 0;

            if(!ValidateBreakout(ZoneCount, atr))
                continue;

            // Filters
            double tri = CalcTriangularScore(ZoneCount, 0);
            double ws  = CalcWhiteSpaceScore(ZoneCount, 0);
            if(tri < InpMinTriScore || ws < InpMinWSScore)
                continue;

            Zones[ZoneCount].triScore = tri;
            Zones[ZoneCount].wsScore  = ws;
            Zones[ZoneCount].confidence = (tri + ws) / 2.0;
            if(Zones[ZoneCount].gapCount > 0)
                Zones[ZoneCount].confidence += 15;
            if(Zones[ZoneCount].confidence > 100)
                Zones[ZoneCount].confidence = 100;

            ZoneCount++;

            if(InpAlerts)
                Alert("PinShot: New ", zType, " zone detected | ",
                      _Symbol, " | Confidence: ",
                      DoubleToString(Zones[ZoneCount-1].confidence, 0), "%");

            break; // next start position
        }
    }
}

//+------------------------------------------------------------------+
//| ValidateBreakout                                                 |
//+------------------------------------------------------------------+
bool ValidateBreakout(int zIdx, double atr)
{
    double minBody = atr * InpBreakoutBodyMult;
    double minTotal = atr * InpBreakoutTotalMult;
    int checkStart = Zones[zIdx].endBar - 1; // bars before zone end (more recent)

    if(checkStart < 1) return false;

    // Try both directions
    string dirs[2] = {"UP", "DOWN"};
    for(int d = 0; d < 2; d++)
    {
        int consecutive = 0;
        double totalMove = 0;

        for(int i = checkStart; i >= MathMax(checkStart - 8, 1); i--)
        {
            double o = iOpen(_Symbol, PERIOD_CURRENT, i);
            double c = iClose(_Symbol, PERIOD_CURRENT, i);
            double body = MathAbs(c - o);
            bool bullish = c > o;
            bool bearish = c < o;

            if(dirs[d] == "UP" && bullish && body > minBody)
            {
                consecutive++;
                totalMove += body;
            }
            else if(dirs[d] == "DOWN" && bearish && body > minBody)
            {
                consecutive++;
                totalMove += body;
            }
            else if(consecutive > 0)
                break;
            else
                break;
        }

        if(consecutive >= InpBreakoutMinCandles && totalMove >= minTotal)
        {
            // Check direction matches zone type
            if((dirs[d] == "UP" && Zones[zIdx].zoneType == "BUY") ||
               (dirs[d] == "DOWN" && Zones[zIdx].zoneType == "SELL"))
            {
                Zones[zIdx].breakoutDir = dirs[d];
                Zones[zIdx].breakoutMag = NormalizeDouble(totalMove / atr, 1);
                Zones[zIdx].breakoutCandles = consecutive;

                // Detect gaps
                DetectGapsFVG(zIdx, checkStart, MathMax(checkStart - 8, 1));
                return true;
            }
        }
    }
    return false;
}

//+------------------------------------------------------------------+
//| DetectGapsFVG                                                    |
//+------------------------------------------------------------------+
void DetectGapsFVG(int zIdx, int startBar, int endBar)
{
    int gaps = 0;
    double gapTotal = 0;

    for(int i = startBar - 2; i >= endBar; i--)
    {
        double h0 = iHigh(_Symbol, PERIOD_CURRENT, i + 2);
        double l2 = iLow(_Symbol, PERIOD_CURRENT, i);

        // Bullish FVG
        if(l2 > h0)
        {
            gaps++;
            gapTotal += (l2 - h0);
        }

        double l0 = iLow(_Symbol, PERIOD_CURRENT, i + 2);
        double h2 = iHigh(_Symbol, PERIOD_CURRENT, i);

        // Bearish FVG
        if(h2 < l0)
        {
            gaps++;
            gapTotal += (l0 - h2);
        }
    }

    Zones[zIdx].gapCount = gaps;
    Zones[zIdx].gapTotal = gapTotal;
}

//+------------------------------------------------------------------+
//| CalcTriangularScore                                              |
//+------------------------------------------------------------------+
double CalcTriangularScore(int zIdx, int currentBar)
{
    double atr = CalcATR(_Symbol, PERIOD_CURRENT, InpATRPeriod, 1);
    double threshold = atr * 0.5;
    int breakoutEnd = Zones[zIdx].endBar - 3;
    if(breakoutEnd < 1) breakoutEnd = 1;

    int returnBars = breakoutEnd - currentBar;
    if(returnBars <= 0) return 50;

    int swings = CountSwings(_Symbol, PERIOD_CURRENT, breakoutEnd, currentBar, threshold);

    // Direction consistency
    int toward = 0, away = 0;
    for(int i = breakoutEnd - 1; i >= currentBar; i--)
    {
        double move = iClose(_Symbol, PERIOD_CURRENT, i) - iClose(_Symbol, PERIOD_CURRENT, i + 1);
        if(Zones[zIdx].zoneType == "BUY")
        { if(move < 0) toward++; else away++; }
        else
        { if(move > 0) toward++; else away++; }
    }
    double total = toward + away;
    double dirCons = (total > 0) ? (double)toward / total : 0.5;

    double score = 100.0;
    score -= MathMax(0, (swings - 2)) * 15.0;
    score *= (0.3 + 0.7 * dirCons);
    score -= ((double)returnBars / InpMaxReturnBars) * 30.0;

    return MathMax(0, MathMin(100, score));
}

//+------------------------------------------------------------------+
//| CalcWhiteSpaceScore                                              |
//+------------------------------------------------------------------+
double CalcWhiteSpaceScore(int zIdx, int currentBar)
{
    int breakoutEnd = Zones[zIdx].endBar - 3;
    if(breakoutEnd < 1) breakoutEnd = 1;

    int barCount = breakoutEnd - currentBar;
    if(barCount <= 0) return 100;
    if(barCount > InpMaxReturnBars) return 0;

    // Chaos ratio
    int dirChanges = 0;
    for(int i = breakoutEnd - 2; i >= currentBar; i--)
    {
        double m1 = iClose(_Symbol, PERIOD_CURRENT, i) - iClose(_Symbol, PERIOD_CURRENT, i + 1);
        double m2 = iClose(_Symbol, PERIOD_CURRENT, i + 1) - iClose(_Symbol, PERIOD_CURRENT, i + 2);
        if((m1 > 0 && m2 < 0) || (m1 < 0 && m2 > 0))
            dirChanges++;
    }
    double chaosRatio = (barCount > 1) ? (double)dirChanges / barCount : 0;

    // Efficiency
    double netDisp = MathAbs(iClose(_Symbol, PERIOD_CURRENT, currentBar) -
                             iClose(_Symbol, PERIOD_CURRENT, breakoutEnd));
    double totalPath = 0;
    for(int i = breakoutEnd - 1; i >= currentBar; i--)
        totalPath += MathAbs(iClose(_Symbol, PERIOD_CURRENT, i) -
                             iClose(_Symbol, PERIOD_CURRENT, i + 1));
    double efficiency = (totalPath > 0) ? netDisp / totalPath : 1;

    double score = 100.0;
    score -= ((double)barCount / InpMaxReturnBars) * 40.0;
    if(chaosRatio > 0.7) score -= 30;
    if(efficiency < 0.3) score -= 20;

    return MathMax(0, MathMin(100, score));
}

//+------------------------------------------------------------------+
//| CheckLeftSideClean                                               |
//+------------------------------------------------------------------+
bool CheckLeftSideClean(int bar, int lookback)
{
    double atr = CalcATR(_Symbol, PERIOD_CURRENT, InpATRPeriod, bar);
    double threshold = atr * 0.5;
    int swings = CountSwings(_Symbol, PERIOD_CURRENT, bar + lookback, bar, threshold);
    return (swings <= InpMaxLeftSwings);
}

//+------------------------------------------------------------------+
//| CalcLotSize                                                      |
//+------------------------------------------------------------------+
double CalcLotSize(double slPips)
{
    if(slPips <= 0) return SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);

    double balance = AccountInfoDouble(ACCOUNT_BALANCE);
    double riskAmount = balance * InpRiskPercent / 100.0;
    double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    double pointVal  = (tickSize > 0) ? tickValue / tickSize * _Point : 10;

    double lot = riskAmount / (slPips * _Point * pointVal);
    return NormalizeLot(_Symbol, lot);
}

//+------------------------------------------------------------------+
//| PlaceLimitOrders                                                 |
//+------------------------------------------------------------------+
void PlaceLimitOrders()
{
    // Count existing positions
    int openCount = 0;
    for(int i = 0; i < MPosCount; i++)
    {
        if(PositionSelectByTicket(MPositions[i].ticket))
            openCount++;
    }
    if(openCount >= InpMaxPositions) return;

    for(int z = 0; z < ZoneCount; z++)
    {
        if(Zones[z].status != "ACTIVE" || Zones[z].orderPlaced)
            continue;
        if(openCount >= InpMaxPositions) break;

        double buffer = InpSLBufferPips * _Point * 10;
        double entry, sl, risk, tp1, tp2;

        if(Zones[z].zoneType == "BUY")
        {
            entry = Zones[z].high;
            sl    = Zones[z].low - buffer;
            risk  = entry - sl;
            tp1   = entry + risk * InpTP1_RR;
            tp2   = entry + risk * InpTP2_RR;
        }
        else
        {
            entry = Zones[z].low;
            sl    = Zones[z].high + buffer;
            risk  = sl - entry;
            tp1   = entry - risk * InpTP1_RR;
            tp2   = entry - risk * InpTP2_RR;
        }

        double slPips = risk / (_Point * 10);
        double lot = CalcLotSize(slPips);

        MqlTradeRequest request = {};
        MqlTradeResult  result  = {};

        request.action   = TRADE_ACTION_PENDING;
        request.symbol   = _Symbol;
        request.volume   = lot;
        request.price    = NormalizeDouble(entry, _Digits);
        request.sl       = NormalizeDouble(sl, _Digits);
        request.tp       = NormalizeDouble(tp2, _Digits);
        request.magic    = InpMagicNumber;
        request.comment  = "PinShot";
        request.type_time = ORDER_TIME_GTC;

        if(Zones[z].zoneType == "BUY")
            request.type = ORDER_TYPE_BUY_LIMIT;
        else
            request.type = ORDER_TYPE_SELL_LIMIT;

        if(OrderSend(request, result))
        {
            Zones[z].orderPlaced = true;
            Zones[z].orderTicket = result.order;
            Zones[z].status = "TRIGGERED";

            // Track managed position
            if(MPosCount < ArraySize(MPositions))
            {
                MPositions[MPosCount].ticket      = result.order;
                MPositions[MPosCount].symbol       = _Symbol;
                MPositions[MPosCount].direction    = Zones[z].zoneType;
                MPositions[MPosCount].entryPrice   = entry;
                MPositions[MPosCount].stopLoss     = sl;
                MPositions[MPosCount].takeProfit1  = tp1;
                MPositions[MPosCount].takeProfit2  = tp2;
                MPositions[MPosCount].initialLot   = lot;
                MPositions[MPosCount].currentLot   = lot;
                MPositions[MPosCount].tp1Hit       = false;
                MPositions[MPosCount].beSet        = false;
                MPositions[MPosCount].trailingActive = false;
                MPositions[MPosCount].currentSL    = sl;
                MPositions[MPosCount].zoneHeight   = Zones[z].high - Zones[z].low;
                MPositions[MPosCount].zoneIndex    = z;
                MPosCount++;
            }

            Print("PinShot: ", Zones[z].zoneType, " LIMIT @ ",
                  DoubleToString(entry, _Digits), " lot=", lot);
            openCount++;
        }
        else
        {
            Print("PinShot: Order failed: ", GetLastError());
        }
    }
}

//+------------------------------------------------------------------+
//| ManagePositions — TP1, BE, trailing, TP2                         |
//+------------------------------------------------------------------+
void ManagePositions()
{
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

    for(int i = MPosCount - 1; i >= 0; i--)
    {
        if(!PositionSelectByTicket(MPositions[i].ticket))
            continue;

        double price = (MPositions[i].direction == "BUY") ? bid : ask;
        double entry = MPositions[i].entryPrice;
        double risk  = MathAbs(entry - MPositions[i].stopLoss);
        if(risk <= 0) continue;

        // Check TP1 — close half
        if(!MPositions[i].tp1Hit)
        {
            bool tp1Hit = false;
            if(MPositions[i].direction == "BUY" && bid >= MPositions[i].takeProfit1)
                tp1Hit = true;
            if(MPositions[i].direction == "SELL" && ask <= MPositions[i].takeProfit1)
                tp1Hit = true;

            if(tp1Hit)
            {
                double halfLot = NormalizeLot(_Symbol, MPositions[i].initialLot / 2.0);
                if(halfLot > 0)
                {
                    // Close half
                    MqlTradeRequest req = {};
                    MqlTradeResult  res = {};
                    req.action   = TRADE_ACTION_DEAL;
                    req.symbol   = _Symbol;
                    req.volume   = halfLot;
                    req.position = MPositions[i].ticket;
                    req.magic    = InpMagicNumber;
                    req.comment  = "PinShot TP1";

                    if(MPositions[i].direction == "BUY")
                    { req.type = ORDER_TYPE_SELL; req.price = bid; }
                    else
                    { req.type = ORDER_TYPE_BUY; req.price = ask; }

                    if(OrderSend(req, res))
                    {
                        MPositions[i].tp1Hit = true;
                        MPositions[i].currentLot -= halfLot;
                        Print("PinShot: TP1 hit, half closed: ", MPositions[i].ticket);

                        // Move SL to breakeven
                        if(InpUseBreakeven)
                        {
                            MqlTradeRequest mReq = {};
                            MqlTradeResult  mRes = {};
                            mReq.action   = TRADE_ACTION_SLTP;
                            mReq.position = MPositions[i].ticket;
                            mReq.symbol   = _Symbol;
                            mReq.sl       = NormalizeDouble(entry, _Digits);
                            mReq.tp       = NormalizeDouble(MPositions[i].takeProfit2, _Digits);
                            mReq.magic    = InpMagicNumber;

                            if(OrderSend(mReq, mRes))
                            {
                                MPositions[i].beSet = true;
                                MPositions[i].currentSL = entry;
                                Print("PinShot: SL moved to breakeven");
                            }
                        }
                    }
                }
            }
        }

        // Trailing stop after TP1
        if(MPositions[i].tp1Hit && MPositions[i].currentLot > 0)
        {
            double rCurrent = 0;
            if(MPositions[i].direction == "BUY")
                rCurrent = (bid - entry) / risk;
            else
                rCurrent = (entry - ask) / risk;

            if(rCurrent >= InpTrailingActivation)
            {
                MPositions[i].trailingActive = true;
                double trailDist = MPositions[i].zoneHeight * InpTrailingDistMult;
                double newSL = 0;

                if(MPositions[i].direction == "BUY")
                    newSL = bid - trailDist;
                else
                    newSL = ask + trailDist;

                newSL = NormalizeDouble(newSL, _Digits);

                bool shouldUpdate = false;
                if(MPositions[i].direction == "BUY" && newSL > MPositions[i].currentSL)
                    shouldUpdate = true;
                if(MPositions[i].direction == "SELL" && newSL < MPositions[i].currentSL)
                    shouldUpdate = true;

                if(shouldUpdate)
                {
                    MqlTradeRequest tReq = {};
                    MqlTradeResult  tRes = {};
                    tReq.action   = TRADE_ACTION_SLTP;
                    tReq.position = MPositions[i].ticket;
                    tReq.symbol   = _Symbol;
                    tReq.sl       = newSL;
                    tReq.tp       = NormalizeDouble(MPositions[i].takeProfit2, _Digits);
                    tReq.magic    = InpMagicNumber;

                    if(OrderSend(tReq, tRes))
                        MPositions[i].currentSL = newSL;
                }
            }
        }
    }
}

//+------------------------------------------------------------------+
//| CheckReverse — detect zone breaks and place reverse orders       |
//+------------------------------------------------------------------+
void CheckReverse()
{
    if(!InpReverseEnabled) return;

    double atr = CalcATR(_Symbol, PERIOD_CURRENT, InpATRPeriod, 1);
    double minBody = atr * InpReverseMinMult;

    for(int z = 0; z < ZoneCount; z++)
    {
        if(Zones[z].status != "TRIGGERED" && Zones[z].status != "ACTIVE")
            continue;
        if(Zones[z].reversed) continue;

        // Check for zone break
        int breakCount = 0;
        for(int i = 1; i <= 3; i++)
        {
            double o = iOpen(_Symbol, PERIOD_CURRENT, i);
            double c = iClose(_Symbol, PERIOD_CURRENT, i);
            double body = MathAbs(c - o);

            if(Zones[z].zoneType == "BUY" && c < o && body > minBody && c < Zones[z].low)
                breakCount++;
            if(Zones[z].zoneType == "SELL" && c > o && body > minBody && c > Zones[z].high)
                breakCount++;
        }

        if(breakCount >= 2)
        {
            Zones[z].status = "BROKEN";
            Zones[z].reversed = true;

            // Cancel original order if pending
            if(Zones[z].orderTicket > 0)
            {
                MqlTradeRequest cReq = {};
                MqlTradeResult  cRes = {};
                cReq.action = TRADE_ACTION_REMOVE;
                cReq.order  = Zones[z].orderTicket;
                OrderSend(cReq, cRes);
            }

            Print("PinShot: Zone BROKEN — reverse opportunity");
        }
    }
}

//+------------------------------------------------------------------+
//| UpdateZoneStatuses                                               |
//+------------------------------------------------------------------+
void UpdateZoneStatuses()
{
    double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

    for(int z = 0; z < ZoneCount; z++)
    {
        if(Zones[z].status != "ACTIVE") continue;

        // Check stale
        int barsSince = Zones[z].endBar;
        if(barsSince > InpMaxReturnBars)
        {
            Zones[z].status = "STALE";
            if(Zones[z].orderTicket > 0)
            {
                MqlTradeRequest req = {};
                MqlTradeResult  res = {};
                req.action = TRADE_ACTION_REMOVE;
                req.order  = Zones[z].orderTicket;
                OrderSend(req, res);
            }
        }

        // Check broken
        if(Zones[z].zoneType == "BUY" && bid < Zones[z].low - (Zones[z].high - Zones[z].low))
            Zones[z].status = "BROKEN";
        if(Zones[z].zoneType == "SELL" && bid > Zones[z].high + (Zones[z].high - Zones[z].low))
            Zones[z].status = "BROKEN";
    }
}

//+------------------------------------------------------------------+
//| DrawAllRectangles                                                |
//+------------------------------------------------------------------+
void DrawAllRectangles()
{
    for(int z = 0; z < ZoneCount; z++)
    {
        string name = "PS_Zone_" + IntegerToString(z);
        color  clr;

        if(Zones[z].status == "STALE" || Zones[z].status == "BROKEN")
            clr = InpStaleColor;
        else if(Zones[z].zoneType == "BUY")
            clr = InpBuyZoneColor;
        else
            clr = InpSellZoneColor;

        datetime t1 = Zones[z].startTime;
        datetime t2 = Zones[z].endTime + PeriodSeconds(PERIOD_CURRENT) * 20;

        // Draw rectangle
        if(ObjectFind(0, name) < 0)
            ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, Zones[z].high, t2, Zones[z].low);

        ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
        ObjectSetInteger(0, name, OBJPROP_FILL, true);
        ObjectSetInteger(0, name, OBJPROP_BACK, true);
        ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
        ObjectSetDouble(0, name, OBJPROP_PRICE, 0, Zones[z].high);
        ObjectSetDouble(0, name, OBJPROP_PRICE, 1, Zones[z].low);

        // Zone label
        string lblName = "PS_Lbl_" + IntegerToString(z);
        string lblText = Zones[z].zoneType + " | PIN:" + IntegerToString(Zones[z].pinCount)
                       + " | " + DoubleToString(Zones[z].confidence, 0) + "%";
        if(Zones[z].gapCount > 0)
            lblText += " | GAP:" + IntegerToString(Zones[z].gapCount);

        if(ObjectFind(0, lblName) < 0)
            ObjectCreate(0, lblName, OBJ_TEXT, 0, t1, Zones[z].high);

        ObjectSetString(0, lblName, OBJPROP_TEXT, lblText);
        ObjectSetInteger(0, lblName, OBJPROP_COLOR, clr);
        ObjectSetInteger(0, lblName, OBJPROP_FONTSIZE, 8);
    }

    ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| UpdateComment                                                    |
//+------------------------------------------------------------------+
void UpdateComment()
{
    int activeZones = 0;
    for(int z = 0; z < ZoneCount; z++)
        if(Zones[z].status == "ACTIVE" || Zones[z].status == "TRIGGERED")
            activeZones++;

    int openPos = 0;
    for(int i = 0; i < MPosCount; i++)
        if(PositionSelectByTicket(MPositions[i].ticket))
            openPos++;

    string txt = "=== PINSHOT EA ===\n";
    txt += "Zones: " + IntegerToString(activeZones) + "/" + IntegerToString(ZoneCount) + "\n";
    txt += "Positions: " + IntegerToString(openPos) + "/" + IntegerToString(InpMaxPositions) + "\n";
    txt += "Total R: " + DoubleToString(TotalR, 1) + "\n";
    txt += "Trades: " + IntegerToString(TotalTrades) + " | Wins: " + IntegerToString(Wins) + "\n";

    Comment(txt);
}
//+------------------------------------------------------------------+
