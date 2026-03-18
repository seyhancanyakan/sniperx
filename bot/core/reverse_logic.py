"""PinShot Bot — Reverse Trade Logic

When a zone is broken by force, generate a reverse trade signal.
"""

import logging
from .detector import Zone, Candle, calculate_atr, detect_zone_break, zone_direction
from .trade_manager import Signal, _get_pip, _pip_value_per_lot

logger = logging.getLogger("pinshot.reverse")


def validate_reverse_breakout(zone: Zone, candles: list, atr: float) -> bool:
    """Validate that the zone break is strong enough for a reverse trade."""
    if atr <= 0:
        return False

    # Must have at least 2 strong candles breaking through
    search_start = zone.end_idx + 3
    strong_break_candles = 0
    total_break_move = 0.0

    for i in range(search_start, min(len(candles), search_start + 10)):
        c = candles[i]
        body = abs(c.close - c.open)

        if zone.zone_type == "demand" and c.close < zone.low and body > atr:
            strong_break_candles += 1
            total_break_move += body
        elif zone.zone_type == "supply" and c.close > zone.high and body > atr:
            strong_break_candles += 1
            total_break_move += body

    return strong_break_candles >= 2 and total_break_move >= 2 * atr


def generate_reverse_signal(zone: Zone, candles: list, atr: float,
                            settings: dict, account_balance: float,
                            symbol: str = "") -> Signal:
    """Generate reverse trade signal when zone is broken.

    demand zone broken → sell entry at zone.low (pullback)
    supply zone broken → buy entry at zone.high (pullback)
    """
    if not validate_reverse_breakout(zone, candles, atr):
        return None

    pip = _get_pip(symbol) if symbol else (0.01 if zone.high > 10 else 0.0001)
    # Use zone dimensions for calculation
    zone_height = zone.high - zone.low
    buffer_pips = settings.get("sl_buffer_pips", 3)

    if zone.zone_type == "demand":
        # Zone was demand, it broke down → sell signal
        direction = "sell"
        entry = zone.low  # Wait for pullback to zone bottom
        # SL above the break: find the highest point before break
        search_start = zone.end_idx + 3
        break_high = zone.high
        for i in range(search_start, min(len(candles), search_start + 10)):
            if candles[i].high > break_high:
                break_high = candles[i].high
        sl = break_high + buffer_pips * pip
        risk = sl - entry
        tp1 = entry - risk * settings.get("tp1_rr", 2.0)
        tp2 = entry - risk * settings.get("tp2_rr", 3.0)
    else:
        # Zone was supply, it broke up → buy signal
        direction = "buy"
        entry = zone.high  # Wait for pullback to zone top
        search_start = zone.end_idx + 3
        break_low = zone.low
        for i in range(search_start, min(len(candles), search_start + 10)):
            if candles[i].low < break_low:
                break_low = candles[i].low
        sl = break_low - buffer_pips * pip
        risk = entry - sl
        tp1 = entry + risk * settings.get("tp1_rr", 2.0)
        tp2 = entry + risk * settings.get("tp2_rr", 3.0)

    # Calculate lot size
    risk_pct = settings.get("risk_percent", 3.0)
    risk_amount = account_balance * risk_pct / 100.0
    sl_pips = risk / pip if pip > 0 else 1
    pip_val = _pip_value_per_lot(symbol) if symbol else (6.50 if pip == 0.01 else 10.0)
    lot = max(0.01, round(risk_amount / (sl_pips * pip_val), 2))

    return Signal(
        symbol=symbol,  # Will be set by caller if empty
        direction=direction,
        entry_price=round(entry, 5),
        stop_loss=round(sl, 5),
        take_profit_1=round(tp1, 5),
        take_profit_2=round(tp2, 5),
        lot_size=lot,
        confidence=60.0,
        zone_type=zone.zone_type,
        is_reverse=True,
    )


def check_reverse_entry(zone: Zone, current_price: float) -> bool:
    """Check if price has pulled back to zone for reverse entry."""
    zone_range = zone.high - zone.low
    tolerance = zone_range * 0.1

    if zone.zone_type == "demand":
        # Zone broke down, waiting for pullback UP to zone.low
        return abs(current_price - zone.low) <= tolerance
    else:
        # Zone broke up, waiting for pullback DOWN to zone.high
        return abs(current_price - zone.high) <= tolerance
