#!/usr/bin/env python3
"""SniperX — Multi-Profile Backtest Comparison"""
import sys
sys.path.insert(0, "/root/pinshot")

import asyncio
import logging
from datetime import datetime
from dataclasses import asdict
from bot.broker.live_data import LiveDataBroker
from bot.core.detector import (Candle, Zone, ZoneConfig, calculate_atr,
    detect_impulse_move, detect_base_before_impulse, build_zone_from_base)

logging.basicConfig(level=logging.WARNING)

SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY",
    "EURCAD", "GBPAUD", "AUDCAD",
    "XAUUSD", "XAGUSD",
    "US100", "US30", "US500",
]
TIMEFRAMES = ["M15", "M30", "H1", "H4"]

PROFILES = {
    "aggressive": {
        "spike_atr_mult": 1.5, "min_displacement": 1.5,
        "base_max_range_mult": 1.5, "tp_ratio": 2.0,
        "risk_pct": 3.0, "label": "Agresif (düşük eşik, 2R TP)"
    },
    "balanced": {
        "spike_atr_mult": 1.8, "min_displacement": 2.0,
        "base_max_range_mult": 1.2, "tp_ratio": 2.5,
        "risk_pct": 2.0, "label": "Dengeli (mevcut ayar)"
    },
    "conservative": {
        "spike_atr_mult": 2.5, "min_displacement": 3.0,
        "base_max_range_mult": 1.0, "tp_ratio": 3.0,
        "risk_pct": 1.0, "label": "Konservatif (yüksek eşik, 3R TP)"
    },
}

async def run_profile(name, cfg):
    broker = LiveDataBroker()
    zone_cfg = ZoneConfig(
        spike_atr_mult=cfg["spike_atr_mult"],
        min_displacement=cfg["min_displacement"],
        base_max_range_mult=cfg["base_max_range_mult"],
        base_min_candles=2, base_max_candles=20,
        left_side_max_swings=5, lookback=400,
    )
    tp_ratio = cfg["tp_ratio"]
    
    all_trades = []
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                candles = await broker.get_candles(sym, tf, 2000)
                if len(candles) < 50:
                    continue
                
                atr = calculate_atr(candles, 14)
                if atr <= 0:
                    continue
                
                # Detect zones
                zones = []
                for i in range(25, len(candles) - 10):
                    impulse = detect_impulse_move(candles, atr, i, zone_cfg.spike_atr_mult)
                    if not impulse:
                        continue
                    base = detect_base_before_impulse(candles, impulse["index"], atr, zone_cfg)
                    if not base:
                        continue
                    zone = build_zone_from_base(base, impulse, candles, atr)
                    if zone.displacement >= zone_cfg.min_displacement:
                        zones.append(zone)
                
                # Simulate trades
                for zone in zones:
                    mid = (zone.high + zone.low) / 2
                    if zone.zone_type == "demand":
                        entry, sl = mid, zone.low - (zone.high - zone.low) * 0.1
                        risk = entry - sl
                        if risk <= 0: continue
                        tp = entry + risk * tp_ratio
                    else:
                        entry, sl = mid, zone.high + (zone.high - zone.low) * 0.1
                        risk = sl - entry
                        if risk <= 0: continue
                        tp = entry - risk * tp_ratio
                    
                    # Find entry
                    entry_idx = None
                    for i in range(zone.impulse_idx + 1, min(len(candles) - 5, zone.impulse_idx + 50)):
                        c = candles[i]
                        if zone.zone_type == "demand" and c.low <= zone.high and c.low >= zone.low:
                            entry_idx = i; break
                        elif zone.zone_type == "supply" and c.high >= zone.low and c.high <= zone.high:
                            entry_idx = i; break
                    
                    if entry_idx is None: continue
                    
                    # Walk forward
                    for i in range(entry_idx + 1, min(entry_idx + 100, len(candles))):
                        c = candles[i]
                        if zone.zone_type == "demand":
                            if c.low <= sl:
                                all_trades.append({"r": -1.0, "bars": i - entry_idx}); break
                            if c.high >= tp:
                                all_trades.append({"r": tp_ratio, "bars": i - entry_idx}); break
                        else:
                            if c.high >= sl:
                                all_trades.append({"r": -1.0, "bars": i - entry_idx}); break
                            if c.low <= tp:
                                all_trades.append({"r": tp_ratio, "bars": i - entry_idx}); break
            except:
                continue
    
    return all_trades

async def main():
    print("=" * 70)
    print("  SniperX — 3 Profil Karşılaştırma Backtest")
    print(f"  {len(SYMBOLS)} sembol x {len(TIMEFRAMES)} TF | Yahoo Finance gerçek veri")
    print("=" * 70)
    print()
    
    for name, cfg in PROFILES.items():
        trades = await run_profile(name, cfg)
        if not trades:
            print(f"  {cfg['label']}: Veri yok\n"); continue
        
        wins = [t for t in trades if t["r"] > 0]
        losses = [t for t in trades if t["r"] <= 0]
        total_r = sum(t["r"] for t in trades)
        wr = len(wins) / len(trades) * 100 if trades else 0
        gp = sum(t["r"] for t in wins)
        gl = abs(sum(t["r"] for t in losses))
        pf = round(gp / gl, 2) if gl > 0 else 0
        exp = round(total_r / len(trades), 2) if trades else 0
        avg_hold = round(sum(t["bars"] for t in trades) / len(trades), 1) if trades else 0
        
        # Streaks
        cw, cl, mw, ml = 0, 0, 0, 0
        for t in trades:
            if t["r"] > 0: cw += 1; cl = 0; mw = max(mw, cw)
            else: cl += 1; cw = 0; ml = max(ml, cl)
        
        # P&L with $200, fixed %risk
        risk_amt = 200 * cfg["risk_pct"] / 100
        net_pnl = sum(risk_amt * t["r"] for t in trades)
        
        print(f"  ┌─ {cfg['label']} ─┐")
        print(f"  │ Trades:      {len(trades):>6}")
        print(f"  │ Win Rate:    {wr:>5.1f}%")
        print(f"  │ Profit Fct:  {pf:>6.2f}")
        print(f"  │ Expectancy:  {exp:>5.2f}R")
        print(f"  │ Total R:     {total_r:>+8.1f}R")
        print(f"  │ Avg Hold:    {avg_hold:>5.1f} bars")
        print(f"  │ Max Win Str: {mw:>6}")
        print(f"  │ Max Los Str: {ml:>6}")
        print(f"  │ $200 P&L:    ${net_pnl:>+10,.0f} (risk {cfg['risk_pct']}%)")
        print(f"  └{'─' * 30}┘")
        print()

if __name__ == "__main__":
    asyncio.run(main())
