"""SniperX — Zone Detection Engine v3

DOĞRU AKIŞ:
1. detect_impulse_move()  → Güçlü momentum hareketi bul
2. detect_base_before_impulse() → Impulse öncesi sıkışma/base bul
3. build_zone_from_base() → Zone dikdörtgeni oluştur
4. Zone.is_departed kontrolü → Fiyat zone'dan yeterince uzaklaştı mı?
5. count_zone_retests() → Zone'a kaç kez temas edildi?
6. is_first_touch() → İlk temas mı? → TRADE
7. invalidate_zone() → Zone kırıldı mı / bayatladı mı?

ZONE = impulse öncesindeki base alanı
TRADE = sadece zone'a ilk revisit olduğunda
İPTAL = zone derin kırılırsa veya ikinci temas gelirse
"""

from dataclasses import dataclass, field
from typing import List, Optional


# ─── DATA CLASSES ───

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
    # Stage 1: Zone bulma (geniş ağ)
    spike_atr_mult: float = 2.0        # Impulse min body (ATR katı)
    base_min_candles: int = 2           # Base min mum sayısı
    base_max_candles: int = 20          # Base max mum sayısı
    base_max_range_mult: float = 1.0    # Base mum max range (ATR katı)
    left_side_max_swings: int = 4       # Sol taraf max swing
    lookback: int = 300                 # Geriye bakma bar sayısı

    # Stage 2: Trade alma
    min_displacement: float = 3.0
    max_stale_bars: int = 100
    use_trend_filter: bool = True       # Trend extreme filtresi


@dataclass
class Zone:
    zone_type: str              # "demand" or "supply"
    high: float                 # Base en yüksek wick
    low: float                  # Base en düşük wick
    start_idx: int              # Base ilk mum index
    end_idx: int                # Base son mum index
    start_time: float = 0.0     # Base ilk mum zamanı
    end_time: float = 0.0       # Base son mum zamanı (impulse öncesi)
    impulse_idx: int = 0        # Impulse başlangıç index
    impulse_magnitude: float = 0.0  # Impulse body / ATR
    pin_count: int = 0          # Base içindeki pin bar sayısı
    gap_count: int = 0          # Impulse'taki gap/FVG sayısı
    base_candle_count: int = 0  # Base mum sayısı
    displacement: float = 0.0

    # Core zone (body-based — for execution)
    core_high: float = 0.0
    core_low: float = 0.0
    midpoint: float = 0.0

    # Quality
    is_fresh: bool = True
    quality_score: float = 0.0

    # State machine
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

    # Display
    status: str = "fresh"
    confidence: float = 0.0
    breakout_direction: str = ""
    breakout_magnitude: float = 0.0
    spike_magnitude: float = 0.0
    triangular_score: float = 0.0
    white_space_score: float = 0.0

    # Phase 1: Zone state machine enhancements
    order_placed: bool = False
    order_ticket: str = ""
    filled_once: bool = False
    shallow_touch_count: int = 0
    deep_touch_count: int = 0
    last_touch_idx: int = -1
    burn_reason: str = ""
    touch_penetration_pct: float = 0.0

    # Legacy compat
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
    zone_type: str      # "demand" / "supply"
    direction: str      # "buy" / "sell"
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    zone_idx: int
    confidence: float


# ─── TOUCH PENETRATION THRESHOLDS ───

SHALLOW_TOUCH_PENETRATION_PCT = 0.15
DEEP_TOUCH_PENETRATION_PCT = 0.45


# ─── HELPERS ───

def zone_direction(zone_type: str) -> str:
    """demand -> buy, supply -> sell"""
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


def is_pin_bar(c: Candle) -> bool:
    body = max(c.body, 1e-10)
    return c.upper_wick > 2.0 * body or c.lower_wick > 2.0 * body


# ─── STEP 1: DETECT IMPULSE MOVE ───

def detect_impulse_move(candles: list, atr: float, start_idx: int,
                        spike_mult: float = 2.0) -> Optional[dict]:
    """Tek güçlü mum veya 2-3 ardışık aynı yönlü güçlü mum bul.

    Returns: {index, direction, magnitude, end_idx} or None
    """
    if atr <= 0:
        return None
    c = candles[start_idx]

    # Tek dev mum
    if c.body > spike_mult * atr:
        direction = "up" if c.is_bullish else "down"
        return {"index": start_idx, "direction": direction,
                "magnitude": round(c.body / atr, 1), "end_idx": start_idx}

    # 2 ardışık aynı yönlü
    if start_idx + 1 < len(candles):
        c2 = candles[start_idx + 1]
        same = (c.is_bullish and c2.is_bullish) or (c.is_bearish and c2.is_bearish)
        if same and (c.body + c2.body) > spike_mult * 1.2 * atr:
            direction = "up" if c.is_bullish else "down"
            return {"index": start_idx, "direction": direction,
                    "magnitude": round((c.body + c2.body) / atr, 1), "end_idx": start_idx + 1}

    # 3 ardışık
    if start_idx + 2 < len(candles):
        c2 = candles[start_idx + 1]
        c3 = candles[start_idx + 2]
        all_bull = c.is_bullish and c2.is_bullish and c3.is_bullish
        all_bear = c.is_bearish and c2.is_bearish and c3.is_bearish
        total = c.body + c2.body + c3.body
        if (all_bull or all_bear) and total > spike_mult * 1.5 * atr:
            direction = "up" if all_bull else "down"
            return {"index": start_idx, "direction": direction,
                    "magnitude": round(total / atr, 1), "end_idx": start_idx + 2}

    return None


# ─── STEP 2: DETECT BASE BEFORE IMPULSE ───

def detect_base_before_impulse(candles: list, impulse_idx: int, atr: float,
                                cfg: ZoneConfig) -> Optional[dict]:
    """Impulse'ın hemen SOLUNDAKİ küçük mumları bul = base/sıkışma alanı."""
    if impulse_idx < cfg.base_min_candles or atr <= 0:
        return None

    max_range = cfg.base_max_range_mult * atr
    base_indices = []
    i = impulse_idx - 1

    while i >= max(impulse_idx - cfg.base_max_candles, 0):
        c = candles[i]
        # Base mumu: küçük range VEYA pin bar
        if c.range <= max_range or is_pin_bar(c):
            base_indices.insert(0, i)
            i -= 1
        else:
            break  # Büyük mum = base'in sol sınırı

    if len(base_indices) < cfg.base_min_candles:
        return None

    base_candles = [candles[idx] for idx in base_indices]
    pin_count = sum(1 for c in base_candles if is_pin_bar(c))

    return {
        "start_idx": base_indices[0],
        "end_idx": base_indices[-1],
        "high": max(c.high for c in base_candles),
        "low": min(c.low for c in base_candles),
        "start_time": candles[base_indices[0]].time,
        "end_time": candles[base_indices[-1]].time,
        "pin_count": pin_count,
        "candle_count": len(base_indices),
    }


# ─── STEP 3: BUILD ZONE FROM BASE ───

def build_zone_from_base(base: dict, impulse: dict, candles: list,
                          atr: float) -> Zone:
    """Base ve impulse bilgisinden Zone oluştur."""
    # Demand (buy): impulse yukarı → base dipte
    # Supply (sell): impulse aşağı → base tepede
    zone_type = "demand" if impulse["direction"] == "up" else "supply"

    # Displacement hesapla
    zone_height = base["high"] - base["low"]
    displacement = 0.0
    if zone_height > 0:
        imp_end = impulse["end_idx"]
        for j in range(imp_end, min(imp_end + 30, len(candles))):
            c = candles[j]
            if impulse["direction"] == "up":
                d = c.high - base["high"]
            else:
                d = base["low"] - c.low
            if d > displacement:
                displacement = d
        displacement = displacement / zone_height

    # Gap/FVG tespiti
    gaps = detect_gaps(candles, impulse["index"], impulse["end_idx"],
                       impulse["direction"])

    # Core zone = body-based
    base_cands = candles[base["start_idx"]:base["end_idx"] + 1]
    c_high = max(c.body_top for c in base_cands) if base_cands else base["high"]
    c_low = min(c.body_bottom for c in base_cands) if base_cands else base["low"]
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

    # Confidence hesapla
    conf = 40.0
    if zone.pin_count >= 2:
        conf += 15
    if zone.pin_count >= 3:
        conf += 5
    if zone.gap_count > 0:
        conf += 15
    if impulse["magnitude"] > 3:
        conf += 10
    if displacement >= 3:
        conf += 10
    if displacement >= 5:
        conf += 5
    zone.confidence = min(100.0, conf)
    zone.quality_score = conf
    zone.id = f"{zone_type}_{base['start_idx']}_{base['end_idx']}_{impulse['index']}"

    return zone


# ─── GAP/FVG DETECTION ───

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


# ─── LEFT SIDE CHECK ───

def check_left_side(candles: list, base_start: int, max_swings: int = 4) -> bool:
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


# ─── TREND EXTREME CHECK ───

def check_trend_extreme(candles: list, base_start: int, direction: str) -> bool:
    """Demand zone dipte mi, supply zone tepede mi kontrol et."""
    trend_bars = min(20, base_start)
    if trend_bars < 5:
        return True
    trend_start = candles[base_start - trend_bars].close
    trend_end = candles[base_start].close
    move = trend_end - trend_start
    zone_h = 1.0  # fallback

    # Demand (buy): fiyat düşüş trendinden gelmeli (move < 0)
    # Supply (sell): fiyat yükseliş trendinden gelmeli (move > 0)
    if direction == "up" and move > 0:
        return False  # Yükseliş trendinden gelen demand = yanlış
    if direction == "down" and move < 0:
        return False  # Düşüş trendinden gelen supply = yanlış
    return True


# ─── ZONE STATE MACHINE ───

def update_zone_state(zone: Zone, candle: Candle, current_idx: int,
                      cfg: ZoneConfig) -> None:
    """Her yeni mum için zone state'ini güncelle."""
    if zone.is_invalid or zone.is_stale:
        return

    zone.bars_since_creation = current_idx - zone.end_idx

    # Two-stage stale: soft (quality drops) + hard (no trade)
    # Count from departure, not creation
    bars_ref = current_idx - (zone.impulse_idx if zone.impulse_idx > 0 else zone.end_idx)

    # TF-based defaults (detect from zone bar spacing)
    zone_bar_spacing = 0
    if zone.base_candle_count > 0 and zone.start_time > 0 and zone.end_time > 0:
        total_secs = zone.end_time - zone.start_time
        if zone.base_candle_count > 1:
            zone_bar_spacing = total_secs / (zone.base_candle_count - 1)

    # Determine TF category from bar spacing
    # Relaxed stale — 3x normal (more trades, still high quality)
    if zone_bar_spacing <= 90:       # M1
        soft_stale, hard_stale = 135, 270
    elif zone_bar_spacing <= 400:    # M5
        soft_stale, hard_stale = 72, 144
    elif zone_bar_spacing <= 1200:   # M15
        soft_stale, hard_stale = 48, 96
    elif zone_bar_spacing <= 2400:   # M30
        soft_stale, hard_stale = 18, 36
    elif zone_bar_spacing <= 5000:   # H1
        soft_stale, hard_stale = 24, 48
    else:                            # H4+
        soft_stale, hard_stale = 18, 36

    # Soft stale — quality drops
    if bars_ref > soft_stale:
        penalty = ((bars_ref - soft_stale) / (hard_stale - soft_stale)) * 30
        zone.quality_score = max(0, zone.quality_score - penalty)
        zone.is_fresh = False

    # Hard stale — no trade
    if bars_ref > hard_stale:
        zone.is_stale = True
        zone.status = "stale"
        zone.trade_eligible = False
        return

    # Invalid check: zone derin kırılma
    if zone.zone_type == "demand":
        if candle.close < zone.low:
            zone.is_invalid = True
            zone.status = "invalid"
            zone.trade_eligible = False
            return
    else:  # supply
        if candle.close > zone.high:
            zone.is_invalid = True
            zone.status = "invalid"
            zone.trade_eligible = False
            return

    # Departure check: fiyat zone'dan yeterince uzaklaştı mı?
    if not zone.is_departed:
        if zone.displacement >= cfg.min_displacement:
            zone.is_departed = True
            zone.status = "departed"

    # Touch detection (stateful — ardışık mumları tek temas say)
    # Shallow/deep touch ayrımı: penetration depth'e göre
    if zone.is_departed:
        in_zone = False
        if zone.zone_type == "demand":
            in_zone = candle.low <= zone.high and candle.high >= zone.low
        else:
            in_zone = candle.high >= zone.low and candle.low <= zone.high

        if in_zone:
            # Penetrasyon hesapla
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

            # Yeni temas mı yoksa devam eden temas mı?
            if not hasattr(zone, '_in_touch') or not zone._in_touch:
                zone.touch_count += 1
                zone.last_touch_idx = current_idx
                zone._in_touch = True

                # Shallow vs deep touch classification
                if penetration < SHALLOW_TOUCH_PENETRATION_PCT:
                    zone.shallow_touch_count += 1
                elif penetration > DEEP_TOUCH_PENETRATION_PCT:
                    zone.deep_touch_count += 1
                    zone.burn_reason = f"deep_touch_pct={penetration:.2f}_at_bar={current_idx}"
        else:
            if hasattr(zone, '_in_touch'):
                zone._in_touch = False

        # Trade eligibility
        # Deep touch burns the zone
        if zone.deep_touch_count > 0:
            zone.trade_eligible = False
            zone.status = "burned"
        elif zone.touch_count == 1 and not zone.first_touch_taken:
            zone.trade_eligible = True
            zone.status = "active"
        elif zone.touch_count > 1:
            zone.trade_eligible = False
            zone.status = "used"


def is_first_touch(zone: Zone) -> bool:
    """İlk temas mı kontrol et."""
    return (zone.is_departed and
            zone.touch_count == 1 and
            not zone.first_touch_taken and
            not zone.is_invalid and
            not zone.is_stale)


def generate_entry(zone: Zone, pip: float, sl_buffer_pips: int = 3) -> Optional[TradeSignal]:
    """First touch'ta trade sinyali üret."""
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


# ─── MAIN: DETECT ZONES ───

def detect_zones(candles: list, atr_period: int = 14,
                 cfg: ZoneConfig = None, **kwargs) -> list:
    """Ana zone tespit fonksiyonu — iki aşamalı.

    Stage 1: Geniş ağ ile aday zone'ları bul
    Stage 2: Displacement >= 3x olanları trade'e uygun işaretle
    """
    if cfg is None:
        cfg = ZoneConfig()
        # Override from kwargs
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

    # Stage 1: Impulse bul → base bul → zone oluştur
    i = scan_start
    while i < len(candles) - 3:
        impulse = detect_impulse_move(candles, atr, i, cfg.spike_atr_mult)
        if impulse is None:
            i += 1
            continue

        # Base bul (impulse'ın hemen öncesi)
        base = detect_base_before_impulse(candles, impulse["index"], atr, cfg)
        if base is None:
            i = impulse["end_idx"] + 1
            continue

        # Overlap kontrolü
        overlap = any(base["start_idx"] <= e and base["end_idx"] >= s
                      for s, e in used)
        if overlap:
            i = impulse["end_idx"] + 1
            continue

        # Sol taraf temizliği
        if not check_left_side(candles, base["start_idx"], cfg.left_side_max_swings):
            i = impulse["end_idx"] + 1
            continue

        # Trend extreme — optional filter
        if cfg.use_trend_filter:
            if not check_trend_extreme(candles, base["start_idx"], impulse["direction"]):
                i = impulse["end_idx"] + 1
                continue

        # Zone oluştur
        zone = build_zone_from_base(base, impulse, candles, atr)

        # Stage 2 filtre: displacement kontrolü
        if zone.displacement >= cfg.min_displacement:
            zone.is_departed = True
            zone.status = "departed"
        else:
            zone.status = "watching"

        zones.append(zone)
        used.add((base["start_idx"], base["end_idx"]))
        i = impulse["end_idx"] + 1

    # Zone state update — mevcut fiyata göre
    if zones and len(candles) > 0:
        current = candles[-1]
        current_idx = len(candles) - 1
        for zone in zones:
            zone._in_touch = False
            # Geçmiş mumlarla state güncelle
            for k in range(zone.impulse_idx + 1, len(candles)):
                update_zone_state(zone, candles[k], k, cfg)

    return zones


# ─── SCORING ───

def score_zone(zone: Zone) -> float:
    """Zone kalite skoru (0-100)."""
    score = zone.confidence
    if zone.is_invalid:
        score = 0
    if zone.is_stale:
        score *= 0.5
    if zone.touch_count > 1:
        score *= 0.3
    return round(score, 1)


# ─── ZONE FLIP / ROLE REVERSAL ───

def _candle_body_ratio(c: Candle) -> float:
    rng = max(c.high - c.low, 1e-9)
    return c.body / rng


def detect_zone_break(zone: Zone, candles: list, idx: int, atr: float,
                      min_closes: int = 2, min_disp_atr: float = 1.5,
                      min_body_ratio: float = 0.6):
    """Detect if zone is broken strongly in opposite direction.
    Returns (broke: bool, displacement_atr: float)
    """
    if idx < 1 or atr <= 0:
        return False, 0.0

    start = max(0, idx - min_closes + 1)

    if zone.zone_type == "demand":
        # Bearish break: closes below core_low
        strong_closes = sum(1 for j in range(start, idx + 1)
                           if candles[j].close < zone.core_low
                           and _candle_body_ratio(candles[j]) >= min_body_ratio)
        disp = max(0.0, (zone.core_low - candles[idx].close) / atr)
    else:
        # Bullish break: closes above core_high
        strong_closes = sum(1 for j in range(start, idx + 1)
                           if candles[j].close > zone.core_high
                           and _candle_body_ratio(candles[j]) >= min_body_ratio)
        disp = max(0.0, (candles[idx].close - zone.core_high) / atr)

    broke = strong_closes >= min_closes or disp >= min_disp_atr
    return broke, disp


def create_flipped_zone(old: Zone, break_idx: int, disp_atr: float) -> Zone:
    """Create opposite-direction zone from broken zone."""
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
    """Check if price moved away from flipped zone after break."""
    if atr <= 0:
        return False
    if zone.zone_type == "supply":
        return (zone.core_low - candle.low) / atr >= min_dep
    else:
        return (candle.high - zone.core_high) / atr >= min_dep


def update_zone_with_flip(zone: Zone, candles: list, idx: int,
                          atr: float) -> Optional[Zone]:
    """Update zone state including flip detection.
    Returns new flipped Zone if break detected, else None.
    """
    if zone.is_invalid or zone.is_stale:
        return None

    c = candles[idx]

    # ── ORIGINAL ZONE ──
    if not zone.is_flipped:
        # Check for strong break → flip
        if zone.first_touch_taken or zone.touch_count > 0:
            broke, disp = detect_zone_break(zone, candles, idx, atr)
            if broke:
                zone.status = "broken"
                zone.is_invalid = True
                zone.trade_eligible = False
                zone.broken_at_bar = idx
                return create_flipped_zone(zone, idx, disp)
        return None

    # ── FLIPPED ZONE ──
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
    """Check if flipped zone has its first retest."""
    return (zone.is_flipped and
            zone.has_flip_departed and
            zone.flip_touch_count == 1 and
            zone.flip_trade_eligible and
            not zone.is_invalid and
            not zone.is_stale)


# ─── LEGACY COMPAT ───

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
    return sum(1 for i in range(start, min(end+1, len(candles))) if is_pin_bar(candles[i]))

def count_swings(candles, start, end, threshold):
    if start >= end: return 0
    swings = 0; d = 0; lp = candles[start].close
    for i in range(start+1, min(end+1, len(candles))):
        m = candles[i].close - lp
        if abs(m) >= threshold:
            nd = 1 if m > 0 else -1
            if nd != d and d != 0: swings += 1
            d = nd; lp = candles[i].close
    return swings

def detect_gaps_fvg(candles, idx, direction):
    return detect_gaps(candles, idx, idx + 2, direction)
