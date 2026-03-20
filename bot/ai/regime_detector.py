"""Regime Detector — Classifies market conditions for AI filtering.

Regimes:
  trend_up, trend_down, range, expansion, choppy, low_liquidity

Uses multiple signals: EMA alignment, ATR expansion, directional efficiency,
session time, and volatility patterns.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger("pinshot.ai.regime")


@dataclass
class Regime:
    label: str              # trend_up/trend_down/range/expansion/choppy/low_liquidity
    confidence: float       # 0-1
    ema_aligned: bool
    atr_expanding: bool
    efficiency: float       # directional efficiency 0-1
    risk_multiplier: float  # 0.5 to 1.25


def _ema(candles, period: int) -> float:
    if not candles or period <= 0:
        return 0
    if len(candles) < period:
        return sum(c.close for c in candles) / len(candles)
    sma = sum(c.close for c in candles[:period]) / period
    mult = 2.0 / (period + 1)
    ema = sma
    for c in candles[period:]:
        ema = (c.close - ema) * mult + ema
    return ema


def _atr(candles, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0
    tr_sum = 0
    for i in range(1, min(period + 1, len(candles))):
        c = candles[-i]
        prev = candles[-(i + 1)]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        tr_sum += tr
    return tr_sum / period


def _efficiency(candles, lookback: int = 20) -> float:
    if len(candles) < lookback + 1:
        lookback = len(candles) - 1
    if lookback <= 0:
        return 0
    sub = candles[-lookback - 1:]
    net = abs(sub[-1].close - sub[0].close)
    total = sum(abs(sub[i].close - sub[i - 1].close) for i in range(1, len(sub)))
    return min(1.0, net / total) if total > 0 else 0


def _direction_changes(candles, lookback: int = 20) -> int:
    if len(candles) < lookback + 1:
        lookback = len(candles) - 1
    if lookback < 2:
        return 0
    sub = candles[-lookback - 1:]
    changes = 0
    prev_dir = None
    for i in range(1, len(sub)):
        d = 1 if sub[i].close > sub[i - 1].close else -1
        if prev_dir is not None and d != prev_dir:
            changes += 1
        prev_dir = d
    return changes


def detect_regime(candles, session_hour: int = -1) -> Regime:
    """Detect current market regime from candle data."""
    if len(candles) < 50:
        return Regime("unknown", 0, False, False, 0, 1.0)

    ema20 = _ema(candles, 20)
    ema50 = _ema(candles, 50)
    current_atr = _atr(candles, 14)
    older_atr = _atr(candles[:-36], 14) if len(candles) > 50 else current_atr
    atr_ratio = current_atr / older_atr if older_atr > 0 else 1.0

    eff = _efficiency(candles, 20)
    dir_changes = _direction_changes(candles, 20)

    # Slope
    ema_now = _ema(candles, 20)
    ema_prev = _ema(candles[:-10], 20) if len(candles) > 30 else ema_now
    slope = (ema_now - ema_prev) / (10 * current_atr) if current_atr > 0 else 0

    ema_aligned = (ema20 > ema50 and slope > 0) or (ema20 < ema50 and slope < 0)

    # Low liquidity detection
    if 0 <= session_hour < 6 or 22 <= session_hour <= 23:
        if atr_ratio < 0.6 and eff < 0.15:
            return Regime("low_liquidity", 0.7, False, False, eff, 0.3)

    # Expansion
    if atr_ratio > 1.3:
        return Regime("expansion", min(1.0, atr_ratio / 2),
                       ema_aligned, True, eff, 0.9)

    # Choppy
    if dir_changes >= 12 and eff < 0.2:
        return Regime("choppy", 0.7, False, False, eff, 0.5)

    # Trend
    if ema20 > ema50 and slope > 0 and eff > 0.3:
        conf = min(1.0, 0.5 + eff)
        risk_m = 1.25 if eff > 0.5 else 1.0
        return Regime("trend_up", conf, True, atr_ratio > 1.0, eff, risk_m)

    if ema20 < ema50 and slope < 0 and eff > 0.3:
        conf = min(1.0, 0.5 + eff)
        risk_m = 1.25 if eff > 0.5 else 1.0
        return Regime("trend_down", conf, True, atr_ratio > 1.0, eff, risk_m)

    # Range
    return Regime("range", 0.5, False, False, eff, 1.0)
