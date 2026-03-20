"""Feature Logger — Logs zone and trade features for AI learning.

Every detected zone and every executed trade produces a structured feature record.
This data feeds the offline learning pipeline.
"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, asdict

logger = logging.getLogger("pinshot.ai.features")

FEATURES_DIR = Path(__file__).parent.parent / "config" / "ai_data"
ZONE_FEATURES_FILE = FEATURES_DIR / "zone_features.jsonl"
TRADE_FEATURES_FILE = FEATURES_DIR / "trade_features.jsonl"


def _ensure_dir():
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ZoneFeature:
    """Feature record for a detected zone."""
    timestamp: float
    symbol: str
    timeframe: str
    zone_type: str          # demand / supply
    zone_id: str

    # Geometry
    zone_high: float
    zone_low: float
    zone_width: float
    zone_width_atr: float
    core_high: float
    core_low: float
    wick_high: float
    wick_low: float
    candle_count: int

    # Quality
    pin_count: int
    displacement: float
    impulse_magnitude: float
    gap_count: int
    confidence: float
    left_side_swings: int = 0
    shelf_strength: int = 0      # how many boundaries in strongest shelf

    # Context
    atr: float = 0
    spread: float = 0
    session: str = ""            # tokyo/london/newyork/overlap
    weekday: int = 0
    hour: int = 0

    # Regime
    regime: str = ""             # trend_up/trend_down/range/expansion/choppy
    higher_tf_bias: str = ""     # bullish/bearish/neutral

    # State
    status: str = ""
    touch_count: int = 0
    near_miss_count: int = 0
    is_flipped: bool = False

    # Outcome (filled after trade closes)
    outcome: str = ""            # win/loss/breakeven/skipped_good/skipped_bad/no_touch
    r_result: float = 0
    max_favorable_excursion: float = 0
    max_adverse_excursion: float = 0


@dataclass
class TradeFeature:
    """Feature record for an executed trade."""
    timestamp: float
    symbol: str
    timeframe: str
    direction: str              # buy / sell
    zone_id: str

    # Entry
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_amount: float
    lot_size: float

    # Zone quality at entry
    zone_confidence: float
    zone_pin_count: int
    zone_displacement: float
    zone_gap_count: int
    zone_width_atr: float
    zone_age_bars: int

    # Context at entry
    atr: float
    spread: float
    session: str
    weekday: int
    hour: int
    regime: str
    higher_tf_bias: str

    # Return quality
    return_bars: int = 0         # bars from departure to touch
    return_messiness: float = 0  # direction changes ratio
    return_swing_count: int = 0

    # Outcome
    result: str = ""             # TP1/TP2/SL/BE/TIME/TRAIL
    exit_price: float = 0
    pnl: float = 0
    r_result: float = 0
    hold_bars: int = 0
    partial_taken: bool = False
    max_favorable: float = 0
    max_adverse: float = 0

    # AI scoring at entry
    champion_score: float = 0    # champion model's prediction
    challenger_score: float = 0  # challenger model's prediction


def log_zone(zone, symbol: str, timeframe: str, atr: float,
             regime: str = "", bias: str = "", spread: float = 0) -> ZoneFeature:
    """Log a zone's features to disk."""
    _ensure_dir()

    now = time.time()
    from datetime import datetime
    dt = datetime.utcfromtimestamp(now)

    # Session detection
    h = dt.hour
    if 0 <= h < 9:
        session = "tokyo"
    elif 8 <= h < 17:
        session = "london"
    elif 13 <= h < 22:
        session = "newyork"
    else:
        session = "offhours"
    if 13 <= h < 17:
        session = "overlap"

    zone_width = zone.high - zone.low
    feature = ZoneFeature(
        timestamp=now,
        symbol=symbol,
        timeframe=timeframe,
        zone_type=zone.zone_type,
        zone_id=zone.id,
        zone_high=zone.high,
        zone_low=zone.low,
        zone_width=zone_width,
        zone_width_atr=zone_width / atr if atr > 0 else 0,
        core_high=zone.core_high,
        core_low=zone.core_low,
        wick_high=getattr(zone, 'wick_high', zone.high),
        wick_low=getattr(zone, 'wick_low', zone.low),
        candle_count=zone.base_candle_count,
        pin_count=zone.pin_count,
        displacement=zone.displacement,
        impulse_magnitude=zone.impulse_magnitude,
        gap_count=zone.gap_count,
        confidence=zone.confidence,
        atr=atr,
        spread=spread,
        session=session,
        weekday=dt.weekday(),
        hour=h,
        regime=regime,
        higher_tf_bias=bias,
        status=zone.status,
        touch_count=zone.touch_count,
        near_miss_count=getattr(zone, 'near_miss_count', 0),
        is_flipped=zone.is_flipped,
    )

    try:
        with open(ZONE_FEATURES_FILE, "a") as f:
            f.write(json.dumps(asdict(feature)) + "\n")
    except Exception as e:
        logger.error(f"Zone feature log error: {e}")

    return feature


def log_trade(trade_data: dict) -> TradeFeature:
    """Log a trade's features to disk."""
    _ensure_dir()

    feature = TradeFeature(**{k: trade_data.get(k, None) for k in TradeFeature.__dataclass_fields__ if k in trade_data})
    feature.timestamp = time.time()

    try:
        with open(TRADE_FEATURES_FILE, "a") as f:
            f.write(json.dumps(asdict(feature)) + "\n")
    except Exception as e:
        logger.error(f"Trade feature log error: {e}")

    return feature


def load_zone_features(limit: int = 5000) -> list:
    """Load zone features from JSONL file."""
    try:
        with open(ZONE_FEATURES_FILE) as f:
            lines = f.readlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
    except FileNotFoundError:
        return []


def load_trade_features(limit: int = 5000) -> list:
    """Load trade features from JSONL file."""
    try:
        with open(TRADE_FEATURES_FILE) as f:
            lines = f.readlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
    except FileNotFoundError:
        return []


def get_feature_stats() -> dict:
    """Get summary stats of logged features."""
    zones = load_zone_features()
    trades = load_trade_features()

    wins = [t for t in trades if t.get("r_result", 0) > 0]
    losses = [t for t in trades if t.get("r_result", 0) <= 0]

    return {
        "total_zones_logged": len(zones),
        "total_trades_logged": len(trades),
        "trade_wins": len(wins),
        "trade_losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_r": round(sum(t.get("r_result", 0) for t in trades) / len(trades), 2) if trades else 0,
    }
