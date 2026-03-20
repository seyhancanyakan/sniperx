"""SniperX — PinShot Origin Zone Detector v4

COMPACT ORIGIN ZONE DETECTION — NOT generic supply/demand.

A valid PinShot zone is the smallest micro-cluster (2-6 candles) of wick
rejections immediately before an explosive displacement. The rectangle must
be minimal, visually compact, and attached to the origin of the impulse.

FLOW:
1. detect_impulse_move()         -> Find strong momentum candle(s)
2. detect_base_before_impulse()  -> Find the FINAL micro-rejection cluster
3. build_zone_from_base()        -> Create minimal bounding box
4. _resolve_zone_conflicts()     -> One direction per local structure
5. update_zone_state()           -> State machine per bar
6. is_first_touch()              -> Tradeable on first clean return only
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ═══════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def body_top(self) -> float:
        return max(self.open, self.close)

    @property
    def body_bottom(self) -> float:
        return min(self.open, self.close)


@dataclass
class ZoneConfig:
    # Pin detection
    min_pin_count: int = 2
    wick_ratio_threshold: float = 0.6
    wick_to_body_min_ratio: float = 1.5
    base_max_range_mult: float = 0.4       # max body/range ratio for pin
    max_cluster_candles: int = 6
    max_cluster_height_atr: float = 2.5      # Pin clusters have long wicks
    cluster_window_bars: int = 5
    cluster_dispersion_atr: float = 1.0

    # Displacement / impulse
    spike_atr_mult: float = 1.0            # min impulse body (ATR multiple)
    max_bars_cluster_to_impulse: int = 2    # max gap between cluster end and impulse start
    min_displacement_atr: float = 1.0       # total displacement >= 1.0x ATR
    min_same_color_impulse: int = 1         # min same-color candles in impulse
    departure_volume_ratio: float = 1.3
    volume_required: bool = False

    # FVG
    fvg_enabled: bool = True
    fvg_bonus_score: float = 10.0
    fvg_required: bool = False

    # Left side
    left_side_max_swings: int = 3
    left_side_lookback: int = 15

    # Scan
    lookback: int = 300
    base_min_candles: int = 2
    base_max_candles: int = 6

    # Return cleanliness
    max_return_bars: int = 50
    max_return_swing_flips: int = 4
    max_return_alternating: int = 6

    # Stale / white space
    max_stale_bars: int = 72

    # Near miss
    near_miss_threshold_atr: float = 0.2
    near_miss_invalidate_count: int = 3

    # Hard break / reverse
    hard_break_body_min_atr: float = 1.5
    hard_break_volume_ratio: float = 1.5
    reverse_retest_max_bars: int = 20

    # Displacement requirement
    min_displacement: float = 1.0
    use_trend_filter: bool = True

    # Conflict resolution
    conflict_overlap_pct: float = 0.3
    conflict_distance_atr: float = 0.5
    opposite_cooldown_bars: int = 10
    allow_opposite_only_after_bos: bool = True

    # Min zone score to keep
    min_zone_score: float = 0.0


@dataclass
class Zone:
    zone_type: str              # "demand" or "supply"
    high: float
    low: float
    start_idx: int
    end_idx: int
    start_time: float = 0.0
    end_time: float = 0.0
    impulse_idx: int = 0
    impulse_magnitude: float = 0.0
    pin_count: int = 0
    gap_count: int = 0
    base_candle_count: int = 0
    displacement: float = 0.0

    # Core zone (body-based minimal box)
    core_high: float = 0.0
    core_low: float = 0.0
    midpoint: float = 0.0

    # Quality
    is_fresh: bool = True
    quality_score: float = 0.0

    # State machine
    # States: fresh -> active -> waiting_retest -> tapped -> consumed
    #         active -> stale | invalidated_near_miss | broken
    #         broken -> reversed_candidate -> reversed_traded | expired
    status: str = "fresh"
    is_departed: bool = False
    touch_count: int = 0
    first_touch_taken: bool = False
    is_invalid: bool = False
    is_stale: bool = False
    trade_eligible: bool = False
    bars_since_creation: int = 0
    reject_reasons: list = field(default_factory=list)

    # Unique ID
    id: str = ""

    # Flip fields
    is_flipped: bool = False
    flipped_from_zone_id: str = ""
    breakout_strength_atr: float = 0.0
    broken_at_bar: int = -1
    has_flip_departed: bool = False
    flip_touch_count: int = 0
    flip_trade_eligible: bool = False

    # Display / scoring
    confidence: float = 0.0
    breakout_direction: str = ""
    breakout_magnitude: float = 0.0
    spike_magnitude: float = 0.0
    triangular_score: float = 0.0
    white_space_score: float = 0.0

    # Touch detail
    order_placed: bool = False
    order_ticket: str = ""
    filled_once: bool = False
    shallow_touch_count: int = 0
    deep_touch_count: int = 0
    last_touch_idx: int = -1
    burn_reason: str = ""
    touch_penetration_pct: float = 0.0
    near_miss_count: int = 0

    @property
    def zone_type_legacy(self):
        return "buy" if self.zone_type == "demand" else "sell"


@dataclass
class Gap:
    gap_type: str
    high: float
    low: float
    size: float
    bar_idx: int


@dataclass
class TradeSignal:
    zone_type: str
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    zone_idx: int
    confidence: float


# ═══════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════

SHALLOW_TOUCH_PENETRATION_PCT = 0.15
DEEP_TOUCH_PENETRATION_PCT = 0.45


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def zone_direction(zone_type: str) -> str:
    return "buy" if zone_type == "demand" else "sell"


def calculate_atr(candles: list, period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    tr_sum = 0.0
    for i in range(1, min(period + 1, len(candles))):
        c = candles[-i]
        prev = candles[-(i + 1)]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        tr_sum += tr
    return tr_sum / period


def is_pin_bar(c: Candle, wick_ratio: float = 0.6,
               wick_to_body: float = 1.5,
               max_body_pct: float = 0.5) -> bool:
    """Rejection pin candle detection."""
    if c.range <= 0:
        return False
    body = max(c.body, 1e-10)
    if c.body / c.range > max_body_pct:
        return False
    lower_ok = (c.lower_wick / c.range >= wick_ratio and
                c.lower_wick / body >= wick_to_body)
    upper_ok = (c.upper_wick / c.range >= wick_ratio and
                c.upper_wick / body >= wick_to_body)
    return lower_ok or upper_ok


def _is_buy_pin(c: Candle, cfg: ZoneConfig) -> bool:
    """Lower wick rejection (demand signal).
    Relaxed: lower_wick > body is enough. Strict pin uses cfg thresholds.
    """
    if c.range <= 0:
        return False
    body = max(c.body, 1e-10)
    # Relaxed: any candle with lower wick > body counts as rejection
    return c.lower_wick > body


def _is_sell_pin(c: Candle, cfg: ZoneConfig) -> bool:
    """Upper wick rejection (supply signal).
    Relaxed: upper_wick > body is enough.
    """
    if c.range <= 0:
        return False
    body = max(c.body, 1e-10)
    return c.upper_wick > body


def count_swings(candles: list, start: int, end: int, threshold: float) -> int:
    if start >= end:
        return 0
    swings = 0
    d = 0
    lp = candles[start].close
    for i in range(start + 1, min(end + 1, len(candles))):
        m = candles[i].close - lp
        if abs(m) >= threshold:
            nd = 1 if m > 0 else -1
            if nd != d and d != 0:
                swings += 1
            d = nd
            lp = candles[i].close
    return swings


def _cluster_price_shelves(base_candles: list, atr: float) -> dict:
    """Cluster candle boundaries into horizontal shelves.

    Collects all price boundaries (open, close, high, low) from the micro-base,
    clusters them by proximity, and returns the strongest repeated shelves.

    Returns dict with:
      upper_shelf: highest shelf touched by >= 2 candles
      lower_shelf: lowest shelf touched by >= 2 candles
      wick_high: absolute highest wick (for stop)
      wick_low: absolute lowest wick (for stop)
    """
    if not base_candles or atr <= 0:
        return None

    # Tolerance for clustering: 10% of ATR
    tol = atr * 0.10

    # Collect all price boundaries from each candle
    levels = []
    for c in base_candles:
        levels.append(c.open)
        levels.append(c.close)
        levels.append(c.high)
        levels.append(c.low)

    levels.sort()

    # Cluster nearby levels
    clusters = []
    used = [False] * len(levels)
    for i in range(len(levels)):
        if used[i]:
            continue
        cluster_vals = [levels[i]]
        used[i] = True
        for j in range(i + 1, len(levels)):
            if used[j]:
                continue
            if levels[j] - levels[i] <= tol:
                cluster_vals.append(levels[j])
                used[j] = True
            else:
                break
        clusters.append({
            "price": sum(cluster_vals) / len(cluster_vals),
            "count": len(cluster_vals),
            "min": min(cluster_vals),
            "max": max(cluster_vals),
        })

    # Filter: keep shelves with count >= 2 (touched by at least 2 boundaries)
    strong = [c for c in clusters if c["count"] >= 2]

    if not strong:
        # Fallback: use body band
        return {
            "upper_shelf": max(c.body_top for c in base_candles),
            "lower_shelf": min(c.body_bottom for c in base_candles),
            "wick_high": max(c.high for c in base_candles),
            "wick_low": min(c.low for c in base_candles),
        }

    strong.sort(key=lambda x: x["price"])

    return {
        "upper_shelf": strong[-1]["price"],
        "lower_shelf": strong[0]["price"],
        "wick_high": max(c.high for c in base_candles),
        "wick_low": min(c.low for c in base_candles),
    }


def _avg_volume(candles: list, idx: int, lookback: int = 20) -> float:
    vol_lb = min(lookback, idx)
    if vol_lb <= 0:
        return 0.0
    return sum(candles[idx - j].volume for j in range(1, vol_lb + 1)) / vol_lb


# ═══════════════════════════════════════════════════════════════════
# STEP 1: DETECT IMPULSE MOVE
# ═══════════════════════════════════════════════════════════════════

def detect_impulse_move(candles: list, atr: float, start_idx: int,
                        spike_mult: float = 1.0) -> Optional[dict]:
    """Find explosive momentum candle(s) — the displacement.

    Priority: 3 consecutive > 2 consecutive > single "sword" candle.
    Returns {index, direction, magnitude, end_idx, has_gap, volume_ok} or None.
    """
    if atr <= 0 or start_idx >= len(candles):
        return None
    c = candles[start_idx]

    avg_vol = _avg_volume(candles, start_idx)

    def _vol_ok(idx_s, idx_e):
        if avg_vol <= 0:
            return True
        imp_vol = sum(candles[j].volume for j in range(idx_s, min(idx_e + 1, len(candles))))
        cnt = idx_e - idx_s + 1
        return (imp_vol / cnt) >= avg_vol * 0.8

    def _has_gap(idx_s, idx_e, direction):
        for j in range(idx_s + 1, min(idx_e + 1, len(candles))):
            prev = candles[j - 1]
            curr = candles[j]
            if direction == "up" and curr.low > prev.high:
                return True
            if direction == "down" and curr.high < prev.low:
                return True
            if j >= 2:
                two = candles[j - 2]
                if direction == "up" and curr.low > two.high:
                    return True
                if direction == "down" and curr.high < two.low:
                    return True
        return False

    # 3 consecutive same-direction
    if start_idx + 2 < len(candles):
        c2 = candles[start_idx + 1]
        c3 = candles[start_idx + 2]
        all_bull = c.is_bullish and c2.is_bullish and c3.is_bullish
        all_bear = c.is_bearish and c2.is_bearish and c3.is_bearish
        total = c.body + c2.body + c3.body
        if (all_bull or all_bear) and total > spike_mult * 1.2 * atr:
            d = "up" if all_bull else "down"
            return {"index": start_idx, "direction": d,
                    "magnitude": round(total / atr, 1), "end_idx": start_idx + 2,
                    "has_gap": _has_gap(start_idx, start_idx + 2, d),
                    "volume_ok": _vol_ok(start_idx, start_idx + 2)}

    # 2 consecutive same-direction
    if start_idx + 1 < len(candles):
        c2 = candles[start_idx + 1]
        same = (c.is_bullish and c2.is_bullish) or (c.is_bearish and c2.is_bearish)
        if same and (c.body + c2.body) > spike_mult * atr:
            d = "up" if c.is_bullish else "down"
            return {"index": start_idx, "direction": d,
                    "magnitude": round((c.body + c2.body) / atr, 1),
                    "end_idx": start_idx + 1,
                    "has_gap": _has_gap(start_idx, start_idx + 1, d),
                    "volume_ok": _vol_ok(start_idx, start_idx + 1)}

    # Single "sword" candle
    if c.body > spike_mult * atr:
        d = "up" if c.is_bullish else "down"
        vol_ok = c.volume >= avg_vol * 0.8 if avg_vol > 0 else True
        return {"index": start_idx, "direction": d,
                "magnitude": round(c.body / atr, 1), "end_idx": start_idx,
                "has_gap": False, "volume_ok": vol_ok}

    return None


# ═══════════════════════════════════════════════════════════════════
# STEP 2: DETECT MICRO-ORIGIN BEFORE IMPULSE
# ═══════════════════════════════════════════════════════════════════

def detect_base_before_impulse(candles: list, impulse_idx: int, atr: float,
                                cfg: ZoneConfig) -> Optional[dict]:
    """PinShot origin box: the FINAL micro-rejection cluster before displacement.

    This is NOT a broad consolidation detector.
    It finds only the last 2-6 candles with rejection wicks immediately
    before the impulse. The bounding box must be compact (max 1.5 ATR height).

    If an older wider base and a tighter later base both exist,
    this selects the tighter later base.
    """
    if impulse_idx < cfg.base_min_candles or atr <= 0:
        return None

    max_base = min(cfg.max_cluster_candles, 6)
    base_indices = []
    i = impulse_idx - 1

    # Walk left from impulse, collecting overlapping body cluster candles.
    # Accept: any candle that is NOT a large directional momentum candle.
    # The key idea: bodies should overlap (compressed participation band).
    while i >= max(impulse_idx - max_base, 0):
        c = candles[i]

        # Reject: large directional candle (body > 70% of range AND range > ATR)
        body_pct = c.body / c.range if c.range > 0 else 1.0
        is_momentum = body_pct > 0.7 and c.range > atr
        if is_momentum:
            break

        # Accept: compact candle, small body, doji, or has wick rejection
        base_indices.insert(0, i)
        i -= 1

    if len(base_indices) < cfg.base_min_candles:
        return None

    base_candles = [candles[idx] for idx in base_indices]

    # Count directional pins (relaxed)
    impulse_dir = "up" if candles[impulse_idx].is_bullish else "down"
    if impulse_dir == "up":
        pin_count = sum(1 for c in base_candles if _is_buy_pin(c, cfg))
    else:
        pin_count = sum(1 for c in base_candles if _is_sell_pin(c, cfg))

    # ─── SHELF CLUSTERING (consensus decision band) ───
    # Zone boundary = repeated price shelf, NOT absolute wick extremes
    shelves = _cluster_price_shelves(base_candles, atr)
    if not shelves:
        return None

    box_high = shelves["upper_shelf"]
    box_low = shelves["lower_shelf"]
    wick_high = shelves["wick_high"]
    wick_low = shelves["wick_low"]
    box_height = box_high - box_low

    # Sanity: box must have some height
    if box_height <= 0:
        box_high = max(c.body_top for c in base_candles)
        box_low = min(c.body_bottom for c in base_candles)
        box_height = box_high - box_low

    # If box too tall, trim to last 2-3 candles (freshest micro-base)
    if box_height > cfg.max_cluster_height_atr * atr:
        trim = min(3, len(base_indices))
        base_indices = base_indices[-trim:]
        base_candles = [candles[idx] for idx in base_indices]
        shelves = _cluster_price_shelves(base_candles, atr)
        if not shelves:
            return None
        box_high = shelves["upper_shelf"]
        box_low = shelves["lower_shelf"]
        wick_high = shelves["wick_high"]
        wick_low = shelves["wick_low"]
        box_height = box_high - box_low
        if impulse_dir == "up":
            pin_count = sum(1 for c in base_candles if _is_buy_pin(c, cfg))
        else:
            pin_count = sum(1 for c in base_candles if _is_sell_pin(c, cfg))
        if len(base_indices) < cfg.base_min_candles:
            return None
        if box_height > cfg.max_cluster_height_atr * atr:
            return None

    return {
        "start_idx": base_indices[0],
        "end_idx": base_indices[-1],
        "high": box_high,          # consensus upper shelf
        "low": box_low,            # consensus lower shelf
        "wick_high": wick_high,    # for stop placement
        "wick_low": wick_low,      # for stop placement
        "start_time": candles[base_indices[0]].time,
        "end_time": candles[base_indices[-1]].time,
        "pin_count": pin_count,
        "candle_count": len(base_indices),
    }


# ═══════════════════════════════════════════════════════════════════
# GAP / FVG DETECTION
# ═══════════════════════════════════════════════════════════════════

def detect_gaps(candles: list, start_idx: int, end_idx: int,
                direction: str) -> list:
    gaps = []
    for i in range(start_idx, min(end_idx + 2, len(candles))):
        if i < 1:
            continue
        prev = candles[i - 1]
        curr = candles[i]
        if direction == "up" and curr.low > prev.high:
            gaps.append(Gap("bullish", curr.low, prev.high, curr.low - prev.high, i))
        elif direction == "down" and curr.high < prev.low:
            gaps.append(Gap("bearish", prev.low, curr.high, prev.low - curr.high, i))
        if i >= 2:
            two = candles[i - 2]
            if direction == "up" and curr.low > two.high:
                gaps.append(Gap("fvg_bull", curr.low, two.high, curr.low - two.high, i))
            elif direction == "down" and curr.high < two.low:
                gaps.append(Gap("fvg_bear", two.low, curr.high, two.low - curr.high, i))
    return gaps


# ═══════════════════════════════════════════════════════════════════
# LEFT SIDE CLEANLINESS
# ═══════════════════════════════════════════════════════════════════

def check_left_side(candles: list, base_start: int, max_swings: int = 3) -> bool:
    check_start = max(0, base_start - 15)
    if base_start - check_start < 3:
        return True
    swings = 0
    prev_dir = None
    for i in range(check_start + 1, base_start):
        curr_dir = "up" if candles[i].close > candles[i - 1].close else "down"
        if prev_dir and curr_dir != prev_dir:
            swings += 1
        prev_dir = curr_dir
    return swings <= max_swings


# ═══════════════════════════════════════════════════════════════════
# TREND EXTREME CHECK
# ═══════════════════════════════════════════════════════════════════

def check_trend_extreme(candles: list, base_start: int, direction: str) -> bool:
    """Demand at dips, supply at peaks."""
    trend_bars = min(20, base_start)
    if trend_bars < 5:
        return True
    trend_start = candles[base_start - trend_bars].close
    trend_end = candles[base_start].close
    move = trend_end - trend_start
    if direction == "up" and move > 0:
        return False
    if direction == "down" and move < 0:
        return False
    return True


# ═══════════════════════════════════════════════════════════════════
# STEP 3: BUILD ZONE FROM BASE
# ═══════════════════════════════════════════════════════════════════

def build_zone_from_base(base: dict, impulse: dict, candles: list,
                          atr: float) -> Zone:
    """Build minimal PinShot origin zone from base + impulse."""
    zone_type = "demand" if impulse["direction"] == "up" else "supply"

    # Displacement: how far price moved from zone in first 10 bars
    zone_height = base["high"] - base["low"]
    displacement = 0.0
    if zone_height > 0:
        imp_end = impulse["end_idx"]
        for j in range(imp_end, min(imp_end + 10, len(candles))):
            c = candles[j]
            if impulse["direction"] == "up":
                d = c.high - base["high"]
            else:
                d = base["low"] - c.low
            if d > displacement:
                displacement = d
        displacement = displacement / zone_height

    # Gap/FVG
    gaps = detect_gaps(candles, impulse["index"], impulse["end_idx"],
                       impulse["direction"])

    # Core zone = same as main zone (already body-based from detect_base)
    # Wick extremes stored separately for stop placement
    c_high = base["high"]    # body band top
    c_low = base["low"]      # body band bottom
    mid = (c_high + c_low) / 2

    zone = Zone(
        zone_type=zone_type,
        high=base["high"],
        low=base["low"],
        start_idx=base["start_idx"],
        end_idx=base["end_idx"],
        start_time=base["start_time"],
        end_time=base["end_time"],
        impulse_idx=impulse["index"],
        impulse_magnitude=impulse["magnitude"],
        pin_count=base["pin_count"],
        gap_count=len(gaps),
        base_candle_count=base["candle_count"],
        displacement=round(displacement, 1),
        core_high=c_high,
        core_low=c_low,
        midpoint=mid,
        breakout_direction=impulse["direction"],
        breakout_magnitude=impulse["magnitude"],
        spike_magnitude=impulse["magnitude"],
    )

    # ─── CONFIDENCE SCORE ───
    conf = 30.0

    # Pin bonus (critical for PinShot)
    if zone.pin_count >= 2:
        conf += 20
    if zone.pin_count >= 3:
        conf += 10
    if zone.pin_count < 2:
        conf -= 10

    # FVG/gap bonus
    if zone.gap_count > 0:
        conf += 10
    if zone.gap_count >= 2:
        conf += 5
    if impulse.get("has_gap", False):
        conf += 10

    # Impulse strength
    if impulse["magnitude"] > 3:
        conf += 10
    if impulse["magnitude"] > 5:
        conf += 5

    # Displacement
    if displacement >= 3:
        conf += 10
    if displacement >= 5:
        conf += 5

    # Volume
    if impulse.get("volume_ok", True):
        conf += 5
    else:
        conf -= 10

    # Structural break check
    lookback_struct = min(50, impulse["index"])
    if lookback_struct > 5:
        struct_candles = candles[impulse["index"] - lookback_struct:impulse["index"]]
        if impulse["direction"] == "up":
            prev_high = max(c.high for c in struct_candles)
            if candles[min(impulse["end_idx"], len(candles) - 1)].high > prev_high:
                conf += 10
        else:
            prev_low = min(c.low for c in struct_candles)
            if candles[min(impulse["end_idx"], len(candles) - 1)].low < prev_low:
                conf += 10

    # Compactness bonus: fewer candles = tighter = better
    if zone.base_candle_count <= 3:
        conf += 5
    elif zone.base_candle_count >= 6:
        conf -= 5

    zone.confidence = min(100.0, max(0.0, conf))
    zone.quality_score = conf
    # Timestamp-based deterministic ID (stable across lookback shifts)
    zone.id = f"{zone_type}_{int(base['start_time'])}_{int(base['end_time'])}_{round(base['high'],5)}_{round(base['low'],5)}"

    return zone


# ═══════════════════════════════════════════════════════════════════
# ZONE STATE MACHINE
# ═══════════════════════════════════════════════════════════════════

def update_zone_state(zone: Zone, candle: Candle, current_idx: int,
                      cfg: ZoneConfig) -> None:
    """Update zone state per bar. Deterministic, no lookahead."""
    if zone.is_invalid or zone.is_stale:
        return

    zone.bars_since_creation = current_idx - zone.end_idx

    # ─── QUALITY-BASED LIFESPAN ───
    bars_ref = current_idx - (zone.impulse_idx if zone.impulse_idx > 0 else zone.end_idx)
    base_width = max(zone.base_candle_count, 2)

    multiplier = 3.5 if zone.is_flipped else 5.0
    if zone.displacement >= 4.0:
        multiplier += 1.0
    elif zone.displacement >= 2.5:
        multiplier += 0.5
    if zone.gap_count > 0:
        multiplier += 0.5
    if zone.confidence >= 75:
        multiplier += 0.5
    if zone.is_flipped:
        multiplier -= 0.5
    multiplier = max(multiplier, 2.0)
    raw_lifespan = int(base_width * multiplier)

    # TF-based cap
    zone_bar_spacing = 0
    if zone.base_candle_count > 1 and zone.start_time > 0 and zone.end_time > 0:
        zone_bar_spacing = (zone.end_time - zone.start_time) / (zone.base_candle_count - 1)

    if zone_bar_spacing <= 90:
        tf_cap = 60
    elif zone_bar_spacing <= 400:
        tf_cap = 36
    elif zone_bar_spacing <= 1200:
        tf_cap = 28
    elif zone_bar_spacing <= 2400:
        tf_cap = 20
    elif zone_bar_spacing <= 5000:
        tf_cap = 14
    else:
        tf_cap = 10

    hard_stale = min(raw_lifespan, tf_cap, cfg.max_stale_bars)
    soft_stale = int(hard_stale * 0.6)

    # Soft stale
    if bars_ref > soft_stale:
        penalty = ((bars_ref - soft_stale) / max(hard_stale - soft_stale, 1)) * 30
        zone.quality_score = max(0, zone.quality_score - penalty)
        zone.is_fresh = False

    # Hard stale
    if bars_ref > hard_stale:
        zone.is_stale = True
        zone.status = "stale"
        zone.trade_eligible = False
        return

    # Invalid: close beyond zone
    if zone.zone_type == "demand":
        if candle.close < zone.low:
            zone.is_invalid = True
            zone.status = "invalid"
            zone.trade_eligible = False
            return
    else:
        if candle.close > zone.high:
            zone.is_invalid = True
            zone.status = "invalid"
            zone.trade_eligible = False
            return

    # Departure check
    if not zone.is_departed:
        if zone.displacement >= cfg.min_displacement:
            zone.is_departed = True
            zone.status = "active"

    # ─── WHITE SPACE / RETURN QUALITY CHECK ───
    # If price moved far from zone and return path is messy, kill the zone
    if zone.is_departed and zone.touch_count == 0:
        # Track how far price moved from zone (max distance seen)
        if not hasattr(zone, '_max_distance'):
            zone._max_distance = 0.0
            zone._direction_changes = 0
            zone._last_dir = 0
            zone._bars_since_departure = 0

        zone._bars_since_departure += 1

        # Calculate distance from zone
        if zone.zone_type == "demand":
            dist = candle.close - zone.high  # positive = price above zone
        else:
            dist = zone.low - candle.close   # positive = price below zone

        if dist > zone._max_distance:
            zone._max_distance = dist

        # Track direction changes (messy return = many alternations)
        curr_dir = 1 if candle.close > candle.open else -1
        if zone._last_dir != 0 and curr_dir != zone._last_dir:
            zone._direction_changes += 1
        zone._last_dir = curr_dir

        # WHITE SPACE: price far away + too many bars = stale
        zone_height = zone.high - zone.low
        if zone_height > 0 and zone._bars_since_departure > 5:
            distance_ratio = zone._max_distance / zone_height
            messiness = zone._direction_changes / max(zone._bars_since_departure, 1)

            # PinShot requires FAST V-return or contracting triangle return.
            # But thresholds must be TF-aware — M5 zones need more bars than H1 zones.
            # TF detection: use zone bar spacing
            bar_spacing = 0
            if hasattr(zone, 'start_time') and hasattr(zone, 'end_time') and zone.base_candle_count > 1:
                bar_spacing = (zone.end_time - zone.start_time) / max(zone.base_candle_count - 1, 1)

            # TF-adaptive limits
            if bar_spacing <= 90:       # M1
                max_messy_bars = 30
                max_ws_bars = 60
                max_slow_bars = 80
            elif bar_spacing <= 400:    # M5
                max_messy_bars = 20
                max_ws_bars = 40
                max_slow_bars = 60
            elif bar_spacing <= 1200:   # M15
                max_messy_bars = 15
                max_ws_bars = 30
                max_slow_bars = 45
            elif bar_spacing <= 2400:   # M30
                max_messy_bars = 12
                max_ws_bars = 24
                max_slow_bars = 36
            elif bar_spacing <= 5000:   # H1
                max_messy_bars = 10
                max_ws_bars = 18
                max_slow_bars = 28
            else:                       # H4+
                max_messy_bars = 8
                max_ws_bars = 14
                max_slow_bars = 20

            # Rule 1: Messy return — too many direction changes
            if messiness > 0.45 and zone._bars_since_departure > max_messy_bars:
                zone.is_stale = True
                zone.status = "stale"
                zone.trade_eligible = False
                zone.reject_reasons.append(
                    f"rejected_messy_return: mess={messiness:.2f} bars={zone._bars_since_departure}"
                )
                return

            # Rule 2: White space — price far + no return
            if distance_ratio > 5.0 and zone._bars_since_departure > max_ws_bars:
                zone.is_stale = True
                zone.status = "stale"
                zone.trade_eligible = False
                zone.reject_reasons.append(
                    f"rejected_white_space: dist={distance_ratio:.1f}x bars={zone._bars_since_departure}"
                )
                return

            # Rule 3: Slow return — too many bars
            if zone._bars_since_departure > max_slow_bars and distance_ratio > 2.0:
                zone.is_stale = True
                zone.status = "stale"
                zone.trade_eligible = False
                zone.reject_reasons.append(
                    f"rejected_slow_return: bars={zone._bars_since_departure} dist={distance_ratio:.1f}x"
                )
                return

    # ─── NEAR-MISS CHECK ───
    if zone.is_departed and zone.touch_count == 0:
        threshold = cfg.near_miss_threshold_atr
        # Calculate approximate ATR from zone height
        approx_atr = (zone.high - zone.low) * 2  # rough estimate
        nm_dist = threshold * approx_atr if approx_atr > 0 else 0

        is_near_miss = False
        if zone.zone_type == "demand":
            if candle.low > zone.high and (candle.low - zone.high) <= nm_dist:
                is_near_miss = True
        else:
            if candle.high < zone.low and (zone.low - candle.high) <= nm_dist:
                is_near_miss = True

        if is_near_miss:
            zone.near_miss_count += 1
            if zone.near_miss_count >= cfg.near_miss_invalidate_count:
                zone.is_invalid = True
                zone.status = "invalidated_near_miss"
                zone.trade_eligible = False
                zone.reject_reasons.append(
                    f"rejected_near_miss_escape: {zone.near_miss_count} near misses"
                )
                return

    # ─── TOUCH DETECTION ───
    if zone.is_departed:
        in_zone = False
        if zone.zone_type == "demand":
            in_zone = candle.low <= zone.high and candle.high >= zone.low
        else:
            in_zone = candle.high >= zone.low and candle.low <= zone.high

        if in_zone:
            # Penetration depth
            zone_height = zone.high - zone.low
            if zone_height > 0:
                if zone.zone_type == "demand":
                    penetration = (zone.high - candle.low) / zone_height
                else:
                    penetration = (candle.high - zone.low) / zone_height
                penetration = max(0.0, min(1.0, penetration))
            else:
                penetration = 0.0
            zone.touch_penetration_pct = penetration

            if not hasattr(zone, '_in_touch') or not zone._in_touch:
                zone.touch_count += 1
                zone.last_touch_idx = current_idx
                zone._in_touch = True

                if penetration < SHALLOW_TOUCH_PENETRATION_PCT:
                    zone.shallow_touch_count += 1
                elif penetration > DEEP_TOUCH_PENETRATION_PCT:
                    zone.deep_touch_count += 1
                    zone.burn_reason = f"deep_touch_pct={penetration:.2f}_at_bar={current_idx}"
        else:
            if hasattr(zone, '_in_touch'):
                zone._in_touch = False

        # Trade eligibility
        if zone.deep_touch_count > 0:
            zone.trade_eligible = False
            zone.status = "burned"
        elif zone.touch_count == 1 and not zone.first_touch_taken:
            zone.trade_eligible = True
            zone.status = "active"
        elif zone.touch_count > 1:
            zone.trade_eligible = False
            zone.status = "consumed"


def is_first_touch(zone: Zone) -> bool:
    return (zone.is_departed and
            zone.touch_count == 1 and
            not zone.first_touch_taken and
            not zone.is_invalid and
            not zone.is_stale)


def generate_entry(zone: Zone, pip: float, sl_buffer_pips: int = 3) -> Optional[TradeSignal]:
    if not is_first_touch(zone):
        return None

    buf = sl_buffer_pips * pip

    if zone.zone_type == "demand":
        entry = zone.high
        sl = zone.low - buf
        risk = entry - sl
        if risk <= 0:
            return None
        return TradeSignal("demand", "buy", entry, sl,
                           entry + risk * 2.0, entry + risk * 3.0,
                           0, zone.confidence)
    else:
        entry = zone.low
        sl = zone.high + buf
        risk = sl - entry
        if risk <= 0:
            return None
        return TradeSignal("supply", "sell", entry, sl,
                           entry - risk * 2.0, entry - risk * 3.0,
                           0, zone.confidence)


# ═══════════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════════

def score_zone(zone: Zone) -> float:
    score = zone.confidence
    if zone.is_invalid:
        score = 0
    if zone.is_stale:
        score *= 0.5
    if zone.touch_count > 1:
        score *= 0.3
    return round(score, 1)


# ═══════════════════════════════════════════════════════════════════
# ZONE BREAK / FLIP / REVERSE
# ═══════════════════════════════════════════════════════════════════

def _candle_body_ratio(c: Candle) -> float:
    rng = max(c.high - c.low, 1e-9)
    return c.body / rng


def detect_zone_break(zone: Zone, candles: list, idx: int, atr: float,
                      min_closes: int = 2, min_disp_atr: float = 1.5,
                      min_body_ratio: float = 0.6,
                      cfg: ZoneConfig = None) -> tuple:
    """Detect hard break through zone. Returns (broke, displacement_atr)."""
    if idx < 1 or atr <= 0:
        return False, 0.0

    check_range = min(5, idx)
    start = max(0, idx - check_range + 1)

    vol_lb = min(20, start)
    avg_vol = sum(candles[start - j].volume for j in range(1, vol_lb + 1)) / max(vol_lb, 1) if vol_lb > 0 else 0

    if zone.zone_type == "demand":
        strong_closes = 0
        total_body = 0.0
        has_volume = False
        for j in range(start, idx + 1):
            c = candles[j]
            if c.close < zone.low and _candle_body_ratio(c) >= min_body_ratio:
                strong_closes += 1
                total_body += c.body
                if avg_vol > 0 and c.volume >= avg_vol * 0.8:
                    has_volume = True
        disp = max(0.0, (zone.low - candles[idx].close) / atr)
    else:
        strong_closes = 0
        total_body = 0.0
        has_volume = False
        for j in range(start, idx + 1):
            c = candles[j]
            if c.close > zone.high and _candle_body_ratio(c) >= min_body_ratio:
                strong_closes += 1
                total_body += c.body
                if avg_vol > 0 and c.volume >= avg_vol * 0.8:
                    has_volume = True
        disp = max(0.0, (candles[idx].close - zone.high) / atr)

    broke = (strong_closes >= min_closes and (has_volume or avg_vol == 0)) or disp >= min_disp_atr
    return broke, disp


def create_flipped_zone(old: Zone, break_idx: int, disp_atr: float) -> Zone:
    new_type = "supply" if old.zone_type == "demand" else "demand"
    return Zone(
        zone_type=new_type,
        high=old.high, low=old.low,
        start_idx=old.start_idx, end_idx=old.end_idx,
        start_time=old.start_time, end_time=old.end_time,
        impulse_idx=break_idx,
        impulse_magnitude=disp_atr,
        pin_count=old.pin_count, gap_count=old.gap_count,
        base_candle_count=old.base_candle_count,
        displacement=old.displacement,
        core_high=old.core_high, core_low=old.core_low,
        midpoint=old.midpoint,
        quality_score=old.quality_score * 0.8,
        id=f"{old.id}:flip:{break_idx}",
        is_flipped=True,
        flipped_from_zone_id=old.id,
        breakout_direction="down" if new_type == "supply" else "up",
        breakout_strength_atr=disp_atr,
        broken_at_bar=break_idx,
        status="flipped_fresh",
        is_fresh=False,
        trade_eligible=False,
        confidence=old.confidence * 0.8,
        breakout_magnitude=disp_atr,
        spike_magnitude=disp_atr,
    )


def check_flip_departure(zone: Zone, candle: Candle, atr: float,
                          min_dep: float = 0.5) -> bool:
    if atr <= 0:
        return False
    if zone.zone_type == "supply":
        return (zone.core_low - candle.low) / atr >= min_dep
    else:
        return (candle.high - zone.core_high) / atr >= min_dep


def update_zone_with_flip(zone: Zone, candles: list, idx: int,
                          atr: float) -> Optional[Zone]:
    if zone.is_invalid or zone.is_stale:
        return None

    c = candles[idx]

    if not zone.is_flipped:
        if zone.first_touch_taken or zone.touch_count > 0:
            broke, disp = detect_zone_break(zone, candles, idx, atr)
            if broke:
                zone.status = "broken"
                zone.is_invalid = True
                zone.trade_eligible = False
                zone.broken_at_bar = idx
                return create_flipped_zone(zone, idx, disp)
        return None

    if zone.status == "flipped_fresh":
        if check_flip_departure(zone, c, atr):
            zone.has_flip_departed = True
            zone.status = "flipped_armed"
            zone.flip_trade_eligible = True

    elif zone.status == "flipped_armed":
        touching = c.low <= zone.high and c.high >= zone.low
        if touching:
            zone.flip_touch_count += 1
            zone.status = "flipped_touched"
            zone.trade_eligible = (zone.flip_touch_count == 1)
            zone.flip_trade_eligible = (zone.flip_touch_count == 1)

    elif zone.status == "flipped_touched":
        if zone.flip_touch_count > 1:
            zone.trade_eligible = False
            zone.flip_trade_eligible = False
            zone.is_invalid = True
            zone.status = "invalid"

    return None


def is_flip_first_touch(zone: Zone) -> bool:
    return (zone.is_flipped and
            zone.has_flip_departed and
            zone.flip_touch_count == 1 and
            zone.flip_trade_eligible and
            not zone.is_invalid and
            not zone.is_stale)


# ═══════════════════════════════════════════════════════════════════
# CONFLICT RESOLUTION
# ═══════════════════════════════════════════════════════════════════

def _detect_local_structure(candles: list, idx: int, lookback: int = 30) -> str:
    """Determine local structure: bullish, bearish, neutral."""
    start = max(0, idx - lookback)
    if idx - start < 10:
        return "neutral"

    highs = []
    lows = []
    for i in range(start + 2, min(idx, len(candles) - 1)):
        prev_ok = i - 1 >= 0
        next_ok = i + 1 < len(candles)
        if prev_ok and next_ok:
            if candles[i].high > candles[i - 1].high and candles[i].high > candles[i + 1].high:
                highs.append(candles[i].high)
            if candles[i].low < candles[i - 1].low and candles[i].low < candles[i + 1].low:
                lows.append(candles[i].low)

    if len(highs) < 2 or len(lows) < 2:
        return "neutral"

    hh = highs[-1] > highs[-2]
    hl = lows[-1] > lows[-2]
    lh = highs[-1] < highs[-2]
    ll = lows[-1] < lows[-2]

    if hh and hl:
        return "bullish"
    elif lh and ll:
        return "bearish"
    return "neutral"


def _zone_strength(zone: Zone) -> float:
    s = 0.0
    s += zone.pin_count * 10
    s += min(zone.displacement, 5) * 8
    s += zone.gap_count * 12
    s += zone.confidence * 0.3
    s += zone.impulse_magnitude * 5
    # Compactness bonus
    if zone.base_candle_count <= 3:
        s += 10
    return s


def _zones_conflict(z1: Zone, z2: Zone, atr: float, cfg: ZoneConfig) -> bool:
    overlap_high = min(z1.high, z2.high)
    overlap_low = max(z1.low, z2.low)
    if overlap_high > overlap_low:
        min_height = min(z1.high - z1.low, z2.high - z2.low)
        if min_height > 0:
            overlap_pct = (overlap_high - overlap_low) / min_height
            if overlap_pct >= cfg.conflict_overlap_pct:
                return True

    z1_mid = (z1.high + z1.low) / 2
    z2_mid = (z2.high + z2.low) / 2
    if atr > 0 and abs(z1_mid - z2_mid) < cfg.conflict_distance_atr * atr:
        return True

    return False


def _resolve_zone_conflicts(zones: list, candles: list, atr: float,
                             cfg: ZoneConfig) -> list:
    """Multi-layer conflict resolution. One dominant side per local structure."""
    if len(zones) <= 1:
        return zones

    # Layer 1: Same-cluster single direction (handled by impulse direction in detection)

    # Layer 2: Opposite zone overlap — keep stronger
    to_remove = set()
    for i in range(len(zones)):
        if i in to_remove:
            continue
        for j in range(i + 1, len(zones)):
            if j in to_remove:
                continue
            if zones[i].zone_type == zones[j].zone_type:
                # Same direction: keep if price overlap (dedup)
                if _zones_conflict(zones[i], zones[j], atr, cfg):
                    si = _zone_strength(zones[i])
                    sj = _zone_strength(zones[j])
                    to_remove.add(j if si >= sj else i)
                continue
            # Opposite direction conflict
            if _zones_conflict(zones[i], zones[j], atr, cfg):
                si = _zone_strength(zones[i])
                sj = _zone_strength(zones[j])
                loser = j if si >= sj else i
                to_remove.add(loser)
                zones[loser].reject_reasons.append(
                    f"rejected_opposite_conflict_weaker_score"
                )

    zones = [z for i, z in enumerate(zones) if i not in to_remove]

    # Layer 3: Structure filter — suppress countertrend zones
    if cfg.allow_opposite_only_after_bos and len(candles) > 30:
        structure = _detect_local_structure(candles, len(candles) - 1)
        if structure in ("bearish", "bullish"):
            suppress_type = "demand" if structure == "bearish" else "supply"
            filtered = [z for z in zones if z.zone_type != suppress_type]
            if filtered:
                for z in zones:
                    if z.zone_type == suppress_type:
                        z.reject_reasons.append(
                            f"rejected_countertrend_no_structure_break: "
                            f"{z.zone_type} in {structure} structure"
                        )
                zones = filtered

    # Layer 4: Opposite cooldown
    if cfg.opposite_cooldown_bars > 0 and len(zones) > 1:
        zones.sort(key=lambda z: z.impulse_idx)
        final = [zones[0]]
        for z in zones[1:]:
            last = final[-1]
            if z.zone_type != last.zone_type:
                bar_gap = abs(z.impulse_idx - last.impulse_idx)
                if bar_gap < cfg.opposite_cooldown_bars:
                    if _zone_strength(z) > _zone_strength(last):
                        final[-1] = z
                    else:
                        z.reject_reasons.append("rejected_opposite_cooldown")
                    continue
            final.append(z)
        zones = final

    return zones


# ═══════════════════════════════════════════════════════════════════
# MAIN: DETECT ZONES
# ═══════════════════════════════════════════════════════════════════

def detect_zones(candles: list, atr_period: int = 14,
                 cfg: ZoneConfig = None, **kwargs) -> list:
    """Main zone detection — PinShot compact origin zones.

    Two-stage: detect all candidate zones, then filter by displacement
    and resolve conflicts.
    """
    if cfg is None:
        cfg = ZoneConfig()
        for k, v in kwargs.items():
            if k == 'spike_mult':
                cfg.spike_atr_mult = v
            elif k == 'min_displacement':
                cfg.min_displacement = v
            elif k == 'min_base_bars':
                cfg.base_min_candles = v
            elif k == 'max_base_bars':
                cfg.base_max_candles = v
            elif k == 'spike_lookback':
                cfg.lookback = v
            elif k == 'max_left_swings':
                cfg.left_side_max_swings = v

    if len(candles) < 50:
        return []

    atr = calculate_atr(candles, atr_period)
    if atr <= 0:
        return []

    zones = []
    used = set()
    scan_start = max(cfg.base_max_candles + 5, len(candles) - cfg.lookback)

    i = scan_start
    while i < len(candles) - 3:
        impulse = detect_impulse_move(candles, atr, i, cfg.spike_atr_mult)
        if impulse is None:
            i += 1
            continue

        base = detect_base_before_impulse(candles, impulse["index"], atr, cfg)
        if base is None:
            i = impulse["end_idx"] + 1
            continue

        # Index overlap check
        overlap = any(base["start_idx"] <= e and base["end_idx"] >= s
                      for s, e in used)
        if overlap:
            i = impulse["end_idx"] + 1
            continue

        # Price overlap check — no duplicate zones at same level
        price_overlap = False
        for ez in zones:
            oh = min(base["high"], ez.high)
            ol = max(base["low"], ez.low)
            if oh > ol:
                bh = base["high"] - base["low"]
                if bh > 0 and (oh - ol) / bh > 0.5:
                    price_overlap = True
                    break
        if price_overlap:
            i = impulse["end_idx"] + 1
            continue

        # Impulse must be immediately after base (max 2 bars gap)
        gap_bars = impulse["index"] - base["end_idx"]
        if gap_bars > cfg.max_bars_cluster_to_impulse:
            i = impulse["end_idx"] + 1
            continue

        # Left side cleanliness
        if not check_left_side(candles, base["start_idx"], cfg.left_side_max_swings):
            i = impulse["end_idx"] + 1
            continue

        # Trend extreme filter
        if cfg.use_trend_filter:
            if not check_trend_extreme(candles, base["start_idx"], impulse["direction"]):
                i = impulse["end_idx"] + 1
                continue

        # Build zone
        zone = build_zone_from_base(base, impulse, candles, atr)

        # Displacement gate
        if zone.displacement >= cfg.min_displacement:
            zone.is_departed = True
            zone.status = "active"
        else:
            zone.status = "watching"

        zones.append(zone)
        used.add((base["start_idx"], base["end_idx"]))
        i = impulse["end_idx"] + 1

    # ─── CONFLICT RESOLUTION ───
    zones = _resolve_zone_conflicts(zones, candles, atr, cfg)

    # NOTE: detect_zones does NOT update state.
    # It returns structural zone candidates only.
    # State management (touch, stale, burned) is handled by bot.py zone store.

    return zones


# ═══════════════════════════════════════════════════════════════════
# LEGACY COMPAT
# ═══════════════════════════════════════════════════════════════════

# Keep old names working for any code that imports them
is_buy_pin = _is_buy_pin
is_sell_pin = _is_sell_pin

def detect_accumulation_zones(candles, atr_period=14, **kw):
    return detect_zones(candles, atr_period, **kw)

def detect_breakout(zone, candles, atr, **kw):
    @dataclass
    class BI:
        direction: str; magnitude: float; candle_count: int = 1
        start_idx: int = 0; end_idx: int = 0; breaks_structure: bool = True
    return BI(zone.breakout_direction, zone.breakout_magnitude, 1,
              zone.impulse_idx, zone.impulse_idx)

def detect_pins(candles, start, end, direction="both"):
    return sum(1 for i in range(start, min(end + 1, len(candles))) if is_pin_bar(candles[i]))

def detect_gaps_fvg(candles, idx, direction):
    return detect_gaps(candles, idx, idx + 2, direction)
