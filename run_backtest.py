#!/usr/bin/env python3
"""PinShot Bot - 1 Month Backtest with Real Data (using existing LiveDataBroker)"""

import sys
sys.path.insert(0, "/root/pinshot")

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("backtest")

# Use existing broker for data
from bot.broker.live_data import LiveDataBroker
from bot.core.detector import (
    Candle, Zone, ZoneConfig, calculate_atr,
    detect_impulse_move, detect_base_before_impulse, build_zone_from_base
)

# ─── CONFIG ───

INITIAL_CAPITAL = 200.0
RISK_PER_TRADE = 0.05  # 5%
TP_RATIO = 2.5  # Fixed TP at 2.5R

SYMBOLS = [
    # Majör
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    # Cross
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURCAD", "EURAUD", "GBPAUD", "GBPCAD", "AUDCAD", "AUDNZD", "AUDCHF",
    # Metaller
    "XAUUSD", "XAGUSD",
    # Endeksler
    "US100", "US30", "US500", "DE40",
]

TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4"]

# Session times (UTC)
SESSIONS = {
    "Tokyo": (0, 9),
    "London": (8, 17),
    "NewYork": (13, 22),
    "Overlap": (13, 17),
}


@dataclass
class Trade:
    symbol: str
    timeframe: str
    direction: str
    entry_time: float
    entry_price: float
    stop_loss: float
    take_profit: float
    risk: float
    lot_size: float
    exit_time: float = 0
    exit_price: float = 0
    pnl: float = 0
    pnl_pct: float = 0
    r_multiple: float = 0
    result: str = "open"
    session: str = ""
    session_hour: int = 0
    hold_bars: int = 0
    is_flipped: bool = False


@dataclass
class BacktestResult:
    initial_capital: float = INITIAL_CAPITAL
    final_capital: float = 0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0
    win_rate: float = 0
    profit_factor: float = 0
    max_drawdown: float = 0
    max_losing_streak: int = 0
    max_winning_streak: int = 0
    avg_hold_bars: float = 0
    avg_winner_r: float = 0
    avg_loser_r: float = 0
    expectancy: float = 0
    trades: List[Dict] = field(default_factory=list)
    session_stats: Dict = field(default_factory=dict)
    symbol_stats: Dict = field(default_factory=dict)
    timeframe_stats: Dict = field(default_factory=dict)


class BacktestEngine:
    def __init__(self):
        self.capital = INITIAL_CAPITAL
        self.trades: List[Trade] = []
        self.peak_capital = INITIAL_CAPITAL
        self.zone_config = ZoneConfig()
        self.broker = LiveDataBroker()
        
    def get_session(self, hour_utc: int) -> str:
        for name, (start, end) in SESSIONS.items():
            if start <= hour_utc < end:
                return name
        return "OffHours"
    
    async def fetch_data(self, symbol: str, timeframe: str) -> List[Candle]:
        """Fetch real data using existing LiveDataBroker."""
        try:
            candles = await self.broker.get_candles(symbol, timeframe, 2000)
            logger.info(f"Fetched {len(candles)} candles for {symbol} {timeframe}")
            return candles
        except Exception as e:
            logger.error(f"Failed to fetch {symbol} {timeframe}: {e}")
            return []
    
    def detect_zones(self, candles: List[Candle]) -> List[Zone]:
        if len(candles) < 50:
            return []
        
        atr = calculate_atr(candles, 14)
        if atr <= 0:
            return []
        
        zones = []
        
        for i in range(20, len(candles) - 10):
            impulse = detect_impulse_move(candles, atr, i, self.zone_config.spike_atr_mult)
            if impulse is None:
                continue
            
            base = detect_base_before_impulse(candles, i, atr, self.zone_config)
            if base is None:
                continue
            
            zone = build_zone_from_base(base, impulse, candles, atr)
            if zone is None:
                continue
            
            zone.displacement = zone.impulse_magnitude
            if zone.displacement < self.zone_config.min_displacement:
                continue
            
            zones.append(zone)
        
        return zones
    
    def simulate_trade(self, zone: Zone, candles: List[Candle], entry_idx: int, 
                       symbol: str, timeframe: str) -> Optional[Trade]:
        if entry_idx >= len(candles) - 5:
            return None
        
        entry_candle = candles[entry_idx]
        zone_height = zone.high - zone.low
        mid = (zone.high + zone.low) / 2
        
        if zone.zone_type == "demand":
            direction = "buy"
            entry = mid
            sl = zone.low - (zone_height * 0.1)
            risk = entry - sl
            if risk <= 0:
                return None
            tp = entry + (risk * TP_RATIO)
        else:
            direction = "sell"
            entry = mid
            sl = zone.high + (zone_height * 0.1)
            risk = sl - entry
            if risk <= 0:
                return None
            tp = entry - (risk * TP_RATIO)
        
        risk_amount = self.capital * RISK_PER_TRADE
        lot_size = risk_amount / risk if risk > 0 else 0
        
        entry_time = entry_candle.time
        dt = datetime.fromtimestamp(entry_time)
        session = self.get_session(dt.hour)
        
        trade = Trade(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            entry_time=entry_time,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            risk=risk,
            lot_size=lot_size,
            session=session,
            session_hour=dt.hour,
        )
        
        for i in range(entry_idx + 1, min(entry_idx + 100, len(candles))):
            c = candles[i]
            
            if direction == "buy":
                if c.low <= sl:
                    trade.exit_time = c.time
                    trade.exit_price = sl
                    trade.pnl = -risk_amount
                    trade.pnl_pct = -RISK_PER_TRADE * 100
                    trade.r_multiple = -1.0
                    trade.result = "sl"
                    trade.hold_bars = i - entry_idx
                    return trade

                if c.high >= tp:
                    trade.exit_time = c.time
                    trade.exit_price = tp
                    trade.pnl = risk_amount * TP_RATIO
                    trade.pnl_pct = RISK_PER_TRADE * TP_RATIO * 100
                    trade.r_multiple = TP_RATIO
                    trade.result = "tp"
                    trade.hold_bars = i - entry_idx
                    return trade
            else:
                if c.high >= sl:
                    trade.exit_time = c.time
                    trade.exit_price = sl
                    trade.pnl = -risk_amount
                    trade.pnl_pct = -RISK_PER_TRADE * 100
                    trade.r_multiple = -1.0
                    trade.result = "sl"
                    trade.hold_bars = i - entry_idx
                    return trade

                if c.low <= tp:
                    trade.exit_time = c.time
                    trade.exit_price = tp
                    trade.pnl = risk_amount * TP_RATIO
                    trade.pnl_pct = RISK_PER_TRADE * TP_RATIO * 100
                    trade.r_multiple = TP_RATIO
                    trade.result = "tp"
                    trade.hold_bars = i - entry_idx
                    return trade
        
        last_candle = candles[min(entry_idx + 99, len(candles) - 1)]
        trade.exit_time = last_candle.time
        trade.exit_price = last_candle.close
        
        if direction == "buy":
            pnl_raw = (trade.exit_price - entry) / risk
        else:
            pnl_raw = (entry - trade.exit_price) / risk
        
        trade.pnl = risk_amount * pnl_raw
        trade.pnl_pct = pnl_raw * RISK_PER_TRADE * 100
        trade.r_multiple = round(pnl_raw, 2)
        trade.result = "timeout"
        
        return trade
    
    async def run_backtest(self, symbol: str, timeframe: str) -> List[Trade]:
        candles = await self.fetch_data(symbol, timeframe)
        if not candles:
            return []
        
        zones = self.detect_zones(candles)
        logger.info(f"Found {len(zones)} zones for {symbol} {timeframe}")
        
        trades = []
        processed_zones = set()
        
        for zone in zones:
            entry_idx = None
            for i in range(zone.impulse_idx + 1, min(len(candles) - 5, zone.impulse_idx + 50)):
                c = candles[i]
                
                if zone.zone_type == "demand":
                    if c.low <= zone.high and c.low >= zone.low:
                        entry_idx = i
                        break
                else:
                    if c.high >= zone.low and c.high <= zone.high:
                        entry_idx = i
                        break
            
            if entry_idx is None:
                continue
            
            zone_key = (symbol, timeframe, zone.start_idx, zone.zone_type)
            if zone_key in processed_zones:
                continue
            processed_zones.add(zone_key)
            
            trade = self.simulate_trade(zone, candles, entry_idx, symbol, timeframe)
            if trade:
                trades.append(trade)
        
        return trades
    
    async def run_full_backtest(self) -> BacktestResult:
        result = BacktestResult()
        all_trades = []
        
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES:
                logger.info(f"Backtesting {symbol} {tf}...")
                trades = await self.run_backtest(symbol, tf)
                all_trades.extend(trades)
                
                for t in trades:
                    self.capital += t.pnl
        
        result.final_capital = round(self.capital, 2)
        result.total_trades = len(all_trades)
        result.trades = [asdict(t) for t in all_trades]
        
        if all_trades:
            wins = [t for t in all_trades if t.r_multiple > 0]
            losses = [t for t in all_trades if t.r_multiple <= 0]
            
            result.wins = len(wins)
            result.losses = len(losses)
            result.win_rate = round(len(wins) / len(all_trades) * 100, 1)
            result.total_r = round(sum(t.r_multiple for t in all_trades), 2)
            
            gross_profit = sum(t.r_multiple for t in wins)
            gross_loss = abs(sum(t.r_multiple for t in losses))
            result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0
            result.expectancy = round(result.total_r / result.total_trades, 2)

            # Avg winner/loser R
            result.avg_winner_r = round(gross_profit / len(wins), 2) if wins else 0
            result.avg_loser_r = round(sum(t.r_multiple for t in losses) / len(losses), 2) if losses else 0

            # Avg hold bars
            result.avg_hold_bars = round(sum(t.hold_bars for t in all_trades) / len(all_trades), 1)

            # Streaks
            cur_win, cur_loss, max_win, max_loss = 0, 0, 0, 0
            for t in all_trades:
                if t.r_multiple > 0:
                    cur_win += 1; cur_loss = 0
                    max_win = max(max_win, cur_win)
                else:
                    cur_loss += 1; cur_win = 0
                    max_loss = max(max_loss, cur_loss)
            result.max_winning_streak = max_win
            result.max_losing_streak = max_loss
        
        for session in SESSIONS.keys():
            session_trades = [t for t in all_trades if t.session == session]
            if session_trades:
                wins = len([t for t in session_trades if t.r_multiple > 0])
                result.session_stats[session] = {
                    "trades": len(session_trades),
                    "wins": wins,
                    "win_rate": round(wins / len(session_trades) * 100, 1),
                    "total_r": round(sum(t.r_multiple for t in session_trades), 2),
                }
        
        for symbol in SYMBOLS:
            symbol_trades = [t for t in all_trades if t.symbol == symbol]
            if symbol_trades:
                wins = len([t for t in symbol_trades if t.r_multiple > 0])
                result.symbol_stats[symbol] = {
                    "trades": len(symbol_trades),
                    "wins": wins,
                    "win_rate": round(wins / len(symbol_trades) * 100, 1),
                    "total_r": round(sum(t.r_multiple for t in symbol_trades), 2),
                }
        
        for tf in TIMEFRAMES:
            tf_trades = [t for t in all_trades if t.timeframe == tf]
            if tf_trades:
                wins = len([t for t in tf_trades if t.r_multiple > 0])
                result.timeframe_stats[tf] = {
                    "trades": len(tf_trades),
                    "wins": wins,
                    "win_rate": round(wins / len(tf_trades) * 100, 1),
                    "total_r": round(sum(t.r_multiple for t in tf_trades), 2),
                }
        
        return result


async def main():
    logger.info("=" * 60)
    logger.info("PinShot Bot - 1 Month Backtest")
    logger.info("=" * 60)
    
    engine = BacktestEngine()
    result = await engine.run_full_backtest()
    
    output_dir = Path("/root/pinshot/backtest-results")
    output_dir.mkdir(exist_ok=True)
    
    with open(output_dir / "backtest_results.json", "w") as f:
        json.dump(asdict(result), f, indent=2, default=str)
    
    report = f"""# PinShot Bot - 1 Month Backtest Results

## Overview
- **Period:** Last 30 days (real Yahoo Finance data)
- **Initial Capital:** ${result.initial_capital}
- **Final Capital:** ${result.final_capital}
- **Total PnL:** ${round(result.final_capital - result.initial_capital, 2)}
- **Total R:** {result.total_r}R

## Performance Metrics
| Metric | Value |
|--------|-------|
| Total Trades | {result.total_trades} |
| Wins | {result.wins} |
| Losses | {result.losses} |
| Win Rate | {result.win_rate}% |
| Profit Factor | {result.profit_factor} |
| Expectancy | {result.expectancy}R |
| Avg Winner | {result.avg_winner_r}R |
| Avg Loser | {result.avg_loser_r}R |
| Max Winning Streak | {result.max_winning_streak} |
| Max Losing Streak | {result.max_losing_streak} |
| Avg Hold (bars) | {result.avg_hold_bars} |

## Session Analysis
| Session | Trades | Win Rate | Total R |
|---------|--------|----------|---------|
"""
    for session, stats in sorted(result.session_stats.items(), key=lambda x: x[1]['total_r'], reverse=True):
        report += f"| {session} | {stats['trades']} | {stats['win_rate']}% | {stats['total_r']}R |\n"
    
    report += "\n## Symbol Analysis\n| Symbol | Trades | Win Rate | Total R |\n|--------|--------|----------|--------|\n"
    for symbol, stats in sorted(result.symbol_stats.items(), key=lambda x: x[1]['total_r'], reverse=True)[:10]:
        report += f"| {symbol} | {stats['trades']} | {stats['win_rate']}% | {stats['total_r']}R |\n"
    
    report += "\n## Timeframe Analysis\n| Timeframe | Trades | Win Rate | Total R |\n|-----------|--------|----------|--------|\n"
    for tf, stats in result.timeframe_stats.items():
        report += f"| {tf} | {stats['trades']} | {stats['win_rate']}% | {stats['total_r']}R |\n"
    
    with open(output_dir / "backtest_report.md", "w") as f:
        f.write(report)
    
    logger.info("=" * 60)
    logger.info("BACKTEST COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Initial Capital: ${result.initial_capital}")
    logger.info(f"Final Capital: ${result.final_capital}")
    logger.info(f"Total PnL: ${round(result.final_capital - result.initial_capital, 2)}")
    logger.info(f"Total R: {result.total_r}R")
    logger.info(f"Win Rate: {result.win_rate}%")
    logger.info(f"Total Trades: {result.total_trades}")
    logger.info(f"Profit Factor: {result.profit_factor}")
    logger.info(f"Avg Winner: {result.avg_winner_r}R | Avg Loser: {result.avg_loser_r}R")
    logger.info(f"Max Win Streak: {result.max_winning_streak} | Max Loss Streak: {result.max_losing_streak}")
    logger.info(f"Avg Hold: {result.avg_hold_bars} bars")
    logger.info("")
    logger.info("Session Results:")
    for session, stats in sorted(result.session_stats.items(), key=lambda x: x[1]['total_r'], reverse=True):
        logger.info(f"  {session}: {stats['trades']} trades, {stats['win_rate']}% WR, {stats['total_r']}R")
    logger.info("")
    logger.info(f"Results saved to: {output_dir}")


if __name__ == "__main__":
    asyncio.run(main())
