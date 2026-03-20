"""Zone Quality Scorer — Predicts zone success probability.

Uses historical feature data to score new zones.
This is the core AI model that learns which zones work.

Two modes:
  - rule_based: deterministic scoring from features (always available)
  - learned: trained model from historical data (after enough trades)
"""

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger("pinshot.ai.scorer")

FEATURES_DIR = Path(__file__).parent.parent / "config" / "ai_data"


def rule_based_score(zone_feature: dict) -> float:
    """Deterministic quality score based on known PinShot principles.

    Returns 0-100 score. This is the baseline "champion" scorer.
    """
    score = 50.0  # neutral start

    # Pin count: 2+ pins = strong rejection signal
    pins = zone_feature.get("pin_count", 0)
    if pins >= 3:
        score += 15
    elif pins >= 2:
        score += 10
    elif pins == 1:
        score += 3
    else:
        score -= 10

    # Displacement: strong departure = strong zone
    disp = zone_feature.get("displacement", 0)
    if disp >= 3:
        score += 12
    elif disp >= 2:
        score += 8
    elif disp >= 1:
        score += 4

    # FVG/gap: institutional footprint
    gaps = zone_feature.get("gap_count", 0)
    if gaps > 0:
        score += 8

    # Compactness: fewer candles = tighter origin
    candles = zone_feature.get("candle_count", 4)
    if candles <= 3:
        score += 6
    elif candles >= 6:
        score -= 4

    # Zone width: narrow = precise
    width_atr = zone_feature.get("zone_width_atr", 1.0)
    if width_atr < 0.5:
        score += 5
    elif width_atr > 1.5:
        score -= 8

    # Regime alignment
    regime = zone_feature.get("regime", "")
    zone_type = zone_feature.get("zone_type", "")
    if regime == "trend_up" and zone_type == "demand":
        score += 8  # trend-aligned demand
    elif regime == "trend_down" and zone_type == "supply":
        score += 8  # trend-aligned supply
    elif regime == "trend_up" and zone_type == "supply":
        score -= 5  # counter-trend
    elif regime == "trend_down" and zone_type == "demand":
        score -= 5
    elif regime == "choppy":
        score -= 6
    elif regime == "low_liquidity":
        score -= 10

    # Higher TF bias alignment
    bias = zone_feature.get("higher_tf_bias", "")
    if bias == "bullish" and zone_type == "demand":
        score += 5
    elif bias == "bearish" and zone_type == "supply":
        score += 5
    elif bias and bias != "neutral":
        if (bias == "bullish" and zone_type == "supply") or \
           (bias == "bearish" and zone_type == "demand"):
            score -= 5

    # Session quality
    session = zone_feature.get("session", "")
    if session == "overlap":
        score += 4  # best liquidity
    elif session == "london" or session == "newyork":
        score += 2
    elif session == "offhours":
        score -= 5

    # Confidence from detector
    conf = zone_feature.get("confidence", 50)
    score += (conf - 50) * 0.2  # slight influence

    return max(0, min(100, round(score, 1)))


def learned_score(zone_feature: dict) -> float:
    """ML-based score using historical pattern matching.

    Compares zone features against historical trade outcomes.
    Returns 0-100. Falls back to rule_based if not enough data.
    """
    trades = _load_trade_features()
    if len(trades) < 20:
        return rule_based_score(zone_feature)

    # Simple nearest-neighbor scoring
    # Compare zone features with historical trades and weight by outcome
    weights = []
    for t in trades:
        sim = _similarity(zone_feature, t)
        r = t.get("r_result", 0)
        weights.append((sim, r))

    if not weights:
        return rule_based_score(zone_feature)

    # Weighted average R from similar trades
    total_w = sum(w[0] for w in weights)
    if total_w <= 0:
        return rule_based_score(zone_feature)

    weighted_r = sum(w[0] * w[1] for w in weights) / total_w

    # Convert R to 0-100 score
    # R=0 -> 50, R=2 -> 80, R=-1 -> 30
    score = 50 + weighted_r * 15
    return max(0, min(100, round(score, 1)))


def _similarity(zone: dict, trade: dict) -> float:
    """Calculate similarity between a zone and a historical trade. 0-1."""
    score = 0
    total = 0

    # Same zone type
    if zone.get("zone_type") == trade.get("zone_type"):
        score += 2
    total += 2

    # Similar timeframe
    if zone.get("timeframe") == trade.get("timeframe"):
        score += 1
    total += 1

    # Similar regime
    if zone.get("regime") == trade.get("regime"):
        score += 2
    total += 2

    # Similar pin count
    pin_diff = abs(zone.get("pin_count", 0) - trade.get("zone_pin_count", 0))
    score += max(0, 1 - pin_diff * 0.3)
    total += 1

    # Similar displacement
    disp_diff = abs(zone.get("displacement", 0) - trade.get("zone_displacement", 0))
    score += max(0, 1 - disp_diff * 0.2)
    total += 1

    # Similar width
    w1 = zone.get("zone_width_atr", 1)
    w2 = trade.get("zone_width_atr", 1)
    if w1 > 0 and w2 > 0:
        ratio = min(w1, w2) / max(w1, w2)
        score += ratio
    total += 1

    return score / total if total > 0 else 0


def _load_trade_features() -> list:
    try:
        path = FEATURES_DIR / "trade_features.jsonl"
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def get_scoring_stats() -> dict:
    """Return info about scorer state."""
    trades = _load_trade_features()
    return {
        "mode": "learned" if len(trades) >= 20 else "rule_based",
        "training_trades": len(trades),
        "min_for_learned": 20,
    }
