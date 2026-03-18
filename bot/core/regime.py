"""PinShot Bot — Market Regime Filter

Classifies the current market regime based on EMA alignment, slope,
ATR expansion, directional efficiency, and direction changes.
"""

from dataclasses import dataclass
from typing import List
from .detector import Candle, calculate_atr


@dataclass
class RegimeResult:
    regime: str          # trend_up, trend_down, range, expansion, choppy
    ema_fast: float
    ema_slow: float
    slope: float
    atr_ratio: float     # current ATR / average ATR
    efficiency: float    # directional efficiency 0-1
    allows_base_zone: bool
    allows_flip: bool
    risk_multiplier: float  # 0.5-1.25


def calculate_ema(candles: List[Candle], period: int) -> float:
    """Simple EMA calculation using close prices."""
    if not candles or period <= 0:
        return 0.0
    if len(candles) < period:
        # Not enough data — use SMA of available
        return sum(c.close for c in candles) / len(candles)

    # SMA seed
    sma = sum(c.close for c in candles[:period]) / period
    mult = 2.0 / (period + 1)
    ema = sma
    for c in candles[period:]:
        ema = (c.close - ema) * mult + ema
    return ema


def _ema_slope(candles: List[Candle], period: int, lookback: int) -> float:
    """Calculate normalized slope of EMA over last `lookback` bars.
    Returns slope per bar as a fraction of ATR.
    """
    if len(candles) < period + lookback:
        return 0.0
    ema_now = calculate_ema(candles, period)
    ema_prev = calculate_ema(candles[:-lookback], period)
    atr = calculate_atr(candles, 14)
    if atr <= 0:
        return 0.0
    return (ema_now - ema_prev) / (lookback * atr)


def _directional_efficiency(candles: List[Candle], lookback: int) -> float:
    """Ratio of |net move| to total path over last `lookback` bars."""
    if len(candles) < lookback + 1:
        lookback = len(candles) - 1
    if lookback <= 0:
        return 0.0
    subset = candles[-lookback - 1:]
    net = abs(subset[-1].close - subset[0].close)
    total_path = sum(abs(subset[i].close - subset[i - 1].close) for i in range(1, len(subset)))
    if total_path <= 0:
        return 0.0
    return min(1.0, net / total_path)


def _direction_changes(candles: List[Candle], lookback: int) -> int:
    """Count direction changes in last `lookback` bars."""
    if len(candles) < lookback + 1:
        lookback = len(candles) - 1
    if lookback < 2:
        return 0
    subset = candles[-lookback - 1:]
    changes = 0
    prev_dir = None
    for i in range(1, len(subset)):
        d = 1 if subset[i].close > subset[i - 1].close else -1
        if prev_dir is not None and d != prev_dir:
            changes += 1
        prev_dir = d
    return changes


def classify_regime(candles: List[Candle], config: dict) -> RegimeResult:
    """Classify current market regime.

    Config keys:
        ema_fast (int, default 20)
        ema_slow (int, default 50)
        slope_lookback (int, default 10)
        atr_expansion_mult (float, default 1.2)
        efficiency_threshold (float, default 0.35)
        allow_countertrend_first_touch (bool, default False)
        prefer_flips_in_strong_breaks (bool, default True)
    """
    ema_fast_period = config.get("ema_fast", 20)
    ema_slow_period = config.get("ema_slow", 50)
    slope_lookback = config.get("slope_lookback", 10)
    atr_expansion_mult = config.get("atr_expansion_mult", 1.2)
    eff_threshold = config.get("efficiency_threshold", 0.35)

    ema_f = calculate_ema(candles, ema_fast_period)
    ema_s = calculate_ema(candles, ema_slow_period)
    slope = _ema_slope(candles, ema_fast_period, slope_lookback)
    efficiency = _directional_efficiency(candles, 20)
    dir_changes = _direction_changes(candles, 20)

    # ATR ratio: current ATR / average ATR (last 50 bars)
    current_atr = calculate_atr(candles, 14)
    if len(candles) >= 50:
        avg_atr = calculate_atr(candles[:-36], 14)  # earlier window
    else:
        avg_atr = current_atr
    atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0

    # --- Classification ---
    regime = "range"
    allows_base = True
    allows_flip = True
    risk_mult = 1.0

    # Expansion first (overrides trend)
    if atr_ratio > atr_expansion_mult:
        regime = "expansion"
        allows_base = True
        allows_flip = True
        risk_mult = 0.9  # slightly cautious on base, flip preferred

    # Choppy: many direction changes, low efficiency
    elif dir_changes >= 12 and efficiency < 0.25:
        regime = "choppy"
        allows_base = True   # base ok but quality threshold raised externally
        allows_flip = False   # flips unreliable in chop
        risk_mult = 0.6

    # Trend up
    elif ema_f > ema_s and slope > 0 and efficiency > eff_threshold:
        regime = "trend_up"
        allows_base = True
        allows_flip = True
        risk_mult = 1.0

    # Trend down
    elif ema_f < ema_s and slope < 0 and efficiency > eff_threshold:
        regime = "trend_down"
        allows_base = True
        allows_flip = True
        risk_mult = 1.0

    # Range (default)
    elif efficiency < 0.2 and abs(slope) < 0.01:
        regime = "range"
        allows_base = True
        allows_flip = True
        risk_mult = 1.0

    # --- Risk multiplier refinement ---
    # Strong trend + aligned direction gets bonus (applied by caller per zone)
    if regime in ("trend_up", "trend_down"):
        if efficiency > 0.5:
            risk_mult = 1.25
        else:
            risk_mult = 1.0
    elif regime == "range":
        risk_mult = 1.0

    return RegimeResult(
        regime=regime,
        ema_fast=round(ema_f, 6),
        ema_slow=round(ema_s, 6),
        slope=round(slope, 6),
        atr_ratio=round(atr_ratio, 3),
        efficiency=round(efficiency, 3),
        allows_base_zone=allows_base,
        allows_flip=allows_flip,
        risk_multiplier=round(risk_mult, 2),
    )
