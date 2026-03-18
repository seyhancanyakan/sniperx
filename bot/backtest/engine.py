"""PinShot Bot — Backtest Engine"""

import csv
import logging
from dataclasses import dataclass, field

from ..core.detector import (
    Candle, calculate_atr, detect_accumulation_zones,
    detect_breakout, detect_gaps_fvg, zone_direction,
)
from ..core.filters import combined_filter

logger = logging.getLogger("pinshot.backtest")


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    trades: list = field(default_factory=list)


class BacktestEngine:
    def __init__(self, settings: dict):
        self.settings = settings.get("strategy", settings)
        self.results = BacktestResult()

    def load_data(self, filepath: str) -> list:
        """Load candle data from CSV file."""
        candles = []
        with open(filepath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append(Candle(
                    time=float(row.get("time", 0)),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                ))
        logger.info(f"Loaded {len(candles)} candles from {filepath}")
        return candles

    def run(self, candles: list) -> BacktestResult:
        """Run full backtest on candle data."""
        if len(candles) < 100:
            logger.warning("Not enough candles for backtest")
            return self.results

        atr = calculate_atr(candles, self.settings.get("atr_period", 14))
        zones = detect_accumulation_zones(
            candles,
            atr_period=self.settings.get("atr_period", 14),
            min_pins=self.settings.get("min_pins", 2),
            min_bars=self.settings.get("min_accum_bars", 4),
            max_bars=self.settings.get("max_accum_bars", 20),
            range_mult=self.settings.get("accum_range_mult", 0.6),
        )

        logger.info(f"Found {len(zones)} potential zones")

        gross_profit = 0.0
        gross_loss = 0.0
        peak_r = 0.0
        running_r = 0.0

        for zone in zones:
            # Validate breakout
            breakout = detect_breakout(
                zone, candles, atr,
                min_candles=self.settings.get("breakout_min_candles", 2),
                body_mult=self.settings.get("breakout_body_mult", 1.5),
                total_mult=self.settings.get("breakout_total_mult", 3.0),
            )
            if breakout is None:
                continue

            zone.breakout_direction = breakout.direction
            zone.breakout_magnitude = breakout.magnitude

            # Detect gaps
            gaps = detect_gaps_fvg(candles, breakout.start_idx, breakout.end_idx)
            zone.gap_count = len(gaps)

            # Find return to zone (simulate price returning)
            entry_bar = None
            for i in range(breakout.end_idx + 1, min(len(candles) - 10, breakout.end_idx + self.settings.get("max_return_bars", 50))):
                c = candles[i]
                # Apply filters at this point
                fr = combined_filter(zone, candles, i, self.settings)
                if not fr.valid:
                    break

                zone.triangular_score = fr.scores.get("triangular", 0)
                zone.white_space_score = fr.scores.get("white_space", 0)

                # Check if price touches zone
                if zone.zone_type == "demand" and c.low <= zone.high:
                    entry_bar = i
                    break
                elif zone.zone_type == "supply" and c.high >= zone.low:
                    entry_bar = i
                    break

            if entry_bar is None:
                continue

            # Simulate trade
            trade_result = self._simulate_trade(zone, candles, entry_bar)
            if trade_result is None:
                continue

            self.results.trades.append(trade_result)
            r = trade_result["r_multiple"]
            running_r += r

            if r > 0:
                self.results.wins += 1
                gross_profit += r
            else:
                self.results.losses += 1
                gross_loss += abs(r)

            # Track drawdown
            if running_r > peak_r:
                peak_r = running_r
            dd = peak_r - running_r
            if dd > self.results.max_drawdown:
                self.results.max_drawdown = round(dd, 2)

        self.results.total_trades = len(self.results.trades)
        self.results.total_r = round(running_r, 2)
        self.results.win_rate = round(
            self.results.wins / self.results.total_trades * 100, 1
        ) if self.results.total_trades > 0 else 0.0
        self.results.profit_factor = round(
            gross_profit / gross_loss, 2
        ) if gross_loss > 0 else 0.0
        self.results.expectancy = round(
            running_r / self.results.total_trades, 2
        ) if self.results.total_trades > 0 else 0.0

        return self.results

    def _simulate_trade(self, zone, candles: list, entry_idx: int) -> dict:
        """Walk forward from entry to simulate trade outcome."""
        tp1_rr = self.settings.get("tp1_rr", 2.0)
        tp2_rr = self.settings.get("tp2_rr", 3.0)
        pip_est = 0.01 if zone.high > 10 else 0.0001
        buffer = self.settings.get("sl_buffer_pips", 3) * pip_est

        if zone.zone_type == "demand":
            entry = zone.high
            sl = zone.low - buffer
            risk = entry - sl
            tp1 = entry + risk * tp1_rr
            tp2 = entry + risk * tp2_rr
        else:
            entry = zone.low
            sl = zone.high + buffer
            risk = sl - entry
            tp1 = entry - risk * tp1_rr
            tp2 = entry - risk * tp2_rr

        if risk <= 0:
            return None

        tp1_hit = False
        r_total = 0.0
        current_sl = sl

        for i in range(entry_idx + 1, min(len(candles), entry_idx + 200)):
            c = candles[i]

            if zone.zone_type == "demand":
                # Check SL
                if c.low <= current_sl:
                    if tp1_hit:
                        # After TP1, SL is at breakeven
                        r_total += 0.0  # breakeven on remaining half
                    else:
                        r_total = -1.0
                    return {"r_multiple": round(r_total, 2), "bars": i - entry_idx,
                            "result": "tp1+be" if tp1_hit else "sl",
                            "direction": "buy", "entry": entry, "exit_bar": i}

                # Check TP1
                if not tp1_hit and c.high >= tp1:
                    tp1_hit = True
                    r_total += tp1_rr * 0.5  # Half position at TP1
                    current_sl = entry  # Move to breakeven

                # Check TP2
                if tp1_hit and c.high >= tp2:
                    r_total += tp2_rr * 0.5
                    return {"r_multiple": round(r_total, 2), "bars": i - entry_idx,
                            "result": "tp1+tp2", "direction": "buy",
                            "entry": entry, "exit_bar": i}
            else:
                # SELL
                if c.high >= current_sl:
                    if tp1_hit:
                        r_total += 0.0
                    else:
                        r_total = -1.0
                    return {"r_multiple": round(r_total, 2), "bars": i - entry_idx,
                            "result": "tp1+be" if tp1_hit else "sl",
                            "direction": "sell", "entry": entry, "exit_bar": i}

                if not tp1_hit and c.low <= tp1:
                    tp1_hit = True
                    r_total += tp1_rr * 0.5
                    current_sl = entry

                if tp1_hit and c.low <= tp2:
                    r_total += tp2_rr * 0.5
                    return {"r_multiple": round(r_total, 2), "bars": i - entry_idx,
                            "result": "tp1+tp2", "direction": "sell",
                            "entry": entry, "exit_bar": i}

        # Timed out — close at current price
        last_price = candles[min(entry_idx + 199, len(candles) - 1)].close
        if zone.zone_type == "demand":
            r_total = ((last_price - entry) / risk) * (0.5 if tp1_hit else 1.0)
            if tp1_hit:
                r_total += tp1_rr * 0.5
        else:
            r_total = ((entry - last_price) / risk) * (0.5 if tp1_hit else 1.0)
            if tp1_hit:
                r_total += tp1_rr * 0.5

        return {"r_multiple": round(r_total, 2), "bars": 200,
                "result": "timeout", "direction": zone_direction(zone.zone_type),
                "entry": entry, "exit_bar": entry_idx + 199}

    def generate_report(self) -> dict:
        r = self.results
        return {
            "total_trades": r.total_trades, "wins": r.wins, "losses": r.losses,
            "win_rate": r.win_rate, "total_r": r.total_r,
            "expectancy": r.expectancy, "profit_factor": r.profit_factor,
            "max_drawdown": r.max_drawdown,
        }
