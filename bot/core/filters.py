"""PinShot Bot — Zone Filters

Triangular return filter, white space/freshness filter, left side cleanliness,
and near-miss detection.
"""

from dataclasses import dataclass
from .detector import Zone, Candle, count_swings, calculate_atr


@dataclass
class FilterResult:
    valid: bool
    scores: dict  # {"triangular": float, "white_space": float, "left_side": float}
    reason: str = ""


def triangular_filter(zone: Zone, candles: list, current_bar: int,
                      max_return_bars: int = 12,
                      max_alternating: int = 3,
                      max_swing_flips: int = 2) -> float:
    """Score the triangular return pattern (0-100).

    Ideal: clean V-shape return with max 2 swing flips, fast return.
    Deterministic thresholds from PinShot config.
    """
    # Spike index = breakout point (new backward detection)
    breakout_end = getattr(zone, 'spike_idx', zone.end_idx + 5)
    if current_bar <= breakout_end or current_bar >= len(candles):
        return 50.0

    return_bars = current_bar - breakout_end
    atr = calculate_atr(candles, 14)
    threshold = atr * 0.5 if atr > 0 else 0.001

    # Count swings in return path
    swing_count = count_swings(candles, breakout_end, current_bar, threshold)

    # Direction consistency: what ratio of bars move toward zone vs away
    toward_count = 0
    away_count = 0
    for i in range(breakout_end + 1, min(current_bar + 1, len(candles))):
        c = candles[i]
        prev = candles[i - 1]
        move = c.close - prev.close

        if zone.zone_type == "demand":
            # Returning to buy zone = price moving down
            if move < 0:
                toward_count += 1
            else:
                away_count += 1
        else:
            # Returning to sell zone = price moving up
            if move > 0:
                toward_count += 1
            else:
                away_count += 1

    total = toward_count + away_count
    direction_consistency = toward_count / total if total > 0 else 0.5

    # Calculate score
    score = 100.0
    score -= max(0, (swing_count - 2)) * 15  # Penalty per extra swing
    score *= (0.3 + 0.7 * direction_consistency)  # Direction multiplier
    score -= (return_bars / max_return_bars) * 30  # Time penalty

    # Near-miss penalty
    if near_miss_filter(zone, candles, lookback=return_bars):
        score -= 25

    return max(0.0, min(100.0, score))


def white_space_filter(zone: Zone, candles: list, current_bar: int,
                       max_return_bars: int = 12) -> float:
    """Score zone freshness/white space (0-100).

    Fresh: quick clean return. Stale: too many bars, too much chaos.
    """
    breakout_end = getattr(zone, 'spike_idx', zone.end_idx + 5)
    if current_bar <= breakout_end:
        return 100.0

    bar_count = current_bar - breakout_end

    # Already stale
    if bar_count > max_return_bars:
        return 0.0

    # Chaos ratio: direction changes / total bars
    direction_changes = 0
    for i in range(breakout_end + 2, min(current_bar + 1, len(candles))):
        c = candles[i]
        prev = candles[i - 1]
        prev2 = candles[i - 2]
        curr_dir = 1 if c.close > prev.close else -1
        prev_dir = 1 if prev.close > prev2.close else -1
        if curr_dir != prev_dir:
            direction_changes += 1

    total_bars = max(1, bar_count)
    chaos_ratio = direction_changes / total_bars

    # Efficiency: net displacement / total path
    if bar_count > 0 and breakout_end < len(candles) and current_bar < len(candles):
        net_disp = abs(candles[current_bar].close - candles[breakout_end].close)
        total_path = 0.0
        for i in range(breakout_end + 1, min(current_bar + 1, len(candles))):
            total_path += abs(candles[i].close - candles[i - 1].close)
        efficiency = net_disp / total_path if total_path > 0 else 0.0
    else:
        efficiency = 1.0

    # Calculate score
    score = 100.0
    score -= (bar_count / max_return_bars) * 40  # Time penalty
    if chaos_ratio > 0.7:
        score -= 30  # Too chaotic
    if efficiency < 0.3:
        score -= 20  # Too much sideways movement

    return max(0.0, min(100.0, score))


def left_side_filter(candles: list, zone_start: int, lookback: int = 10,
                     max_zigzag: int = 3) -> float:
    """Score left side cleanliness (0-100).
    Deterministic: max 3 zigzags (direction changes) allowed.
    """
    atr = calculate_atr(candles, 14)
    threshold = atr * 0.5 if atr > 0 else 0.001

    start = max(0, zone_start - lookback)
    swings = count_swings(candles, start, zone_start, threshold)

    if swings <= 2:
        return 100.0  # Very clean
    elif swings <= max_zigzag:
        return 70.0   # Acceptable
    else:
        return 0.0    # M/W pattern, invalid


def near_miss_filter(zone: Zone, candles: list, lookback: int = 10,
                     threshold_atr_mult: float = 0.2) -> bool:
    """Check if price approached zone but bounced away without touching.

    Returns True if near-miss detected (bad signal).
    Threshold: 0.2x ATR (deterministic from PinShot config).
    """
    atr = calculate_atr(candles, 14)
    zone_range = zone.high - zone.low
    if zone_range <= 0:
        return False

    threshold = atr * threshold_atr_mult if atr > 0 else zone_range * 0.2

    end_check = min(len(candles), zone.end_idx + lookback + 5)
    for i in range(zone.end_idx + 3, end_check):
        c = candles[i]

        if zone.zone_type == "demand":
            # Price should come down to zone.high
            # Near miss: came close but bounced up
            distance = c.low - zone.high
            if 0 < distance <= threshold:
                # Check if it bounced away (next bars go up)
                if i + 2 < len(candles):
                    bounce = candles[i + 2].close - c.low
                    if bounce > zone_range:
                        return True
        else:
            # Price should come up to zone.low
            distance = zone.low - c.high
            if 0 < distance <= threshold:
                if i + 2 < len(candles):
                    bounce = c.high - candles[i + 2].close
                    if bounce > zone_range:
                        return True

    return False


def combined_filter(zone: Zone, candles: list, current_bar: int,
                    settings: dict = None) -> FilterResult:
    """Run all filters and return combined result."""
    if settings is None:
        settings = {}

    min_tri = settings.get("min_triangular_score", 60)
    min_ws = settings.get("min_white_space_score", 50)
    max_return = settings.get("max_return_bars", 12)
    max_alt = settings.get("v_return_max_alternating", 3)
    max_sf = settings.get("v_return_max_swing_flips", 2)

    tri_score = triangular_filter(zone, candles, current_bar, max_return,
                                  max_alt, max_sf)
    ws_score = white_space_filter(zone, candles, current_bar, max_return)
    ls_score = left_side_filter(candles, zone.start_idx, lookback=10, max_zigzag=3)

    scores = {
        "triangular": round(tri_score, 1),
        "white_space": round(ws_score, 1),
        "left_side": round(ls_score, 1),
    }

    reasons = []
    if tri_score < min_tri:
        reasons.append(f"Triangular score {tri_score:.0f} < {min_tri}")
    if ws_score < min_ws:
        reasons.append(f"White space score {ws_score:.0f} < {min_ws}")
    # Left side: iğnelerin SOLU temiz olmalı (zig-zak olmamalı)
    # Ama iğnelerin kendisi zone kalitesini artırır (detector'da kontrol edilir)
    if ls_score <= 0:
        reasons.append("Left side not clean (M/W pattern)")

    valid = tri_score >= min_tri and ws_score >= min_ws and ls_score > 0

    return FilterResult(
        valid=valid,
        scores=scores,
        reason="; ".join(reasons) if reasons else "All filters passed",
    )
