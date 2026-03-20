"""PinShot Bot — Trade Manager with Partial TP

Handles position lifecycle: entry, partial TP, breakeven, trailing stop, exit.
"""

import time
import logging
from dataclasses import dataclass

logger = logging.getLogger("pinshot.trade")


@dataclass
class Signal:
    symbol: str
    direction: str  # "buy" or "sell"
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    lot_size: float
    confidence: float = 0.0
    zone_type: str = ""
    is_reverse: bool = False


@dataclass
class Position:
    ticket: str
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    lot_size: float
    risk_pips: float
    tp1_hit: bool = False
    be_set: bool = False
    trailing_active: bool = False
    current_sl: float = 0.0
    status: str = "open"  # pending, open, partial, closed, cancelled
    r_result: float = 0.0
    open_time: float = 0.0
    initial_lot: float = 0.0
    zone_id: str = ""
    order_bar_count: int = 0       # bars since pending order placed
    order_timeframe: str = ""      # timeframe the order was placed on
    hold_bar_count: int = 0        # bars since position opened (for time stop)


# JPY pairs have pip = 0.01, others 0.0001
JPY_PAIRS = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"}


def _get_pip(symbol: str) -> float:
    sym = symbol.upper().replace("_", "")
    for pair in JPY_PAIRS:
        if pair in sym:
            return 0.01
    return 0.0001


def _pip_value_per_lot(symbol: str) -> float:
    """Approximate pip value per standard lot in USD."""
    sym = symbol.upper().replace("_", "")
    # JPY pairs: ~$6.50 per pip per lot (approx)
    for pair in JPY_PAIRS:
        if pair in sym:
            return 6.50
    # Most USD pairs: ~$10 per pip per lot
    return 10.0


class TradeManager:
    def __init__(self, broker, settings: dict):
        self.broker = broker
        self.settings = settings
        self.positions: list[Position] = []

    def calculate_lot_size(self, symbol: str, sl_pips: float,
                           risk_percent: float, account_balance: float) -> float:
        """Calculate lot size based on risk percentage."""
        risk_amount = account_balance * risk_percent / 100.0
        pip_val = _pip_value_per_lot(symbol)

        if sl_pips <= 0 or pip_val <= 0:
            return 0.01

        lot = risk_amount / (sl_pips * pip_val)
        # Round to 2 decimal places, min 0.01
        lot = max(0.01, round(lot, 2))
        return lot

    def create_signal(self, zone, symbol: str, account_balance: float) -> Signal:
        """Create trade signal from a validated zone."""
        pip = _get_pip(symbol)
        buffer = self.settings.get("sl_buffer_pips", 3) * pip
        tp1_rr = self.settings.get("tp1_rr", 2.0)
        tp2_rr = self.settings.get("tp2_rr", 3.0)
        risk_pct = self.settings.get("risk_percent", 3.0)

        # zone_type is "demand" or "supply", direction is "buy" or "sell"
        direction = "buy" if zone.zone_type == "demand" else "sell"

        if zone.zone_type == "demand":
            entry = zone.high
            sl = zone.low - buffer
            risk = entry - sl
            tp1 = entry + risk * tp1_rr
            tp2 = entry + risk * tp2_rr
        else:  # supply
            entry = zone.low
            sl = zone.high + buffer
            risk = sl - entry
            tp1 = entry - risk * tp1_rr
            tp2 = entry - risk * tp2_rr

        sl_pips = risk / pip if pip > 0 else 0
        lot = self.calculate_lot_size(symbol, sl_pips, risk_pct, account_balance)

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 5),
            stop_loss=round(sl, 5),
            take_profit_1=round(tp1, 5),
            take_profit_2=round(tp2, 5),
            lot_size=lot,
            confidence=zone.confidence,
            zone_type=zone.zone_type,
        )

    async def open_position(self, signal: Signal) -> Position:
        """Place limit order and track position."""
        result = await self.broker.place_limit_order(
            signal.symbol, signal.direction, signal.entry_price,
            signal.stop_loss, signal.take_profit_2, signal.lot_size,
        )

        pos = Position(
            ticket=result.ticket if result.success else "",
            symbol=signal.symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            status="pending",  # Limit order — not yet filled
            lot_size=signal.lot_size,
            risk_pips=abs(signal.entry_price - signal.stop_loss) / _get_pip(signal.symbol),
            current_sl=signal.stop_loss,
            open_time=time.time(),
            initial_lot=signal.lot_size,
        )

        if result.success:
            self.positions.append(pos)
            logger.info(f"Position opened: {signal.direction.upper()} {signal.symbol} "
                        f"@ {signal.entry_price} lot={signal.lot_size}")
        else:
            logger.error(f"Failed to open position: {result.message}")

        return pos

    def check_tp1(self, position: Position, current_price: float) -> bool:
        """Check if TP1 has been hit."""
        if position.tp1_hit:
            return False
        if position.direction == "buy":
            return current_price >= position.take_profit_1
        else:
            return current_price <= position.take_profit_1

    async def close_half(self, position: Position):
        """Close 50% of position at TP1."""
        half_lot = round(position.initial_lot / 2, 2)
        half_lot = max(0.01, half_lot)

        success = await self.broker.close_partial(position.ticket, half_lot)
        if success:
            position.tp1_hit = True
            position.lot_size = round(position.lot_size - half_lot, 2)
            position.status = "partial"
            logger.info(f"TP1 hit, half closed: {position.ticket} | "
                        f"Remaining lot: {position.lot_size}")
        else:
            logger.error(f"Failed to close half: {position.ticket}")

    async def move_sl_to_breakeven(self, position: Position):
        """Move SL to entry price (breakeven)."""
        if not self.settings.get("use_breakeven", True):
            return

        success = await self.broker.modify_order(
            position.ticket, sl=position.entry_price
        )
        if success:
            position.current_sl = position.entry_price
            position.be_set = True
            logger.info(f"SL moved to breakeven: {position.ticket} @ {position.entry_price}")

    def check_tp2(self, position: Position, current_price: float) -> bool:
        """Check if TP2 has been hit."""
        if position.direction == "buy":
            return current_price >= position.take_profit_2
        else:
            return current_price <= position.take_profit_2

    def check_sl_hit(self, position: Position, current_price: float) -> bool:
        """Check if current SL has been hit."""
        sl = position.current_sl if position.current_sl != 0 else position.stop_loss
        if position.direction == "buy":
            return current_price <= sl
        else:
            return current_price >= sl

    async def update_trailing_stop(self, position: Position, current_price: float,
                                   zone_height: float):
        """Update trailing stop after TP1 hit.

        Runner modes (from settings.runner_mode):
        - atrTrail: trail at 1.5x zone_height distance (default)
        - fixed3R / fixed5R: close at fixed R target
        - swingTrail: trail to swing level
        """
        if not position.tp1_hit:
            return

        activation_rr = self.settings.get("trailing_activation_rr", 2.0)
        trail_mult = self.settings.get("trailing_distance_mult", 1.5)
        runner_mode = self.settings.get("runner_mode", "atrTrail")
        risk = abs(position.entry_price - position.stop_loss)

        r_current = self.get_r_result(position, current_price)
        if r_current < activation_rr:
            return

        # Fixed R runner modes
        if runner_mode in ("fixed3R", "fixed5R"):
            target_r = 3.0 if runner_mode == "fixed3R" else float(self.settings.get("tp2_rr", 5.0))
            if r_current >= target_r:
                await self.broker.close_full(position.ticket)
                position.status = "closed"
                position.r_result = round(r_current, 2)
                logger.info(f"RUNNER TP: {position.ticket} R={r_current:.1f} (mode={runner_mode})")
            return

        # ATR trail mode (default)
        position.trailing_active = True
        trailing_distance = zone_height * trail_mult

        if position.direction == "buy":
            new_sl = current_price - trailing_distance
            current = position.current_sl if position.current_sl > 0 else position.stop_loss
            if new_sl > current:
                success = await self.broker.modify_order(position.ticket, sl=round(new_sl, 5))
                if success:
                    position.current_sl = round(new_sl, 5)
        else:
            new_sl = current_price + trailing_distance
            current = position.current_sl if position.current_sl > 0 else position.stop_loss
            if new_sl < current:
                success = await self.broker.modify_order(position.ticket, sl=round(new_sl, 5))
                if success:
                    position.current_sl = round(new_sl, 5)

    def get_r_result(self, position: Position, current_price: float) -> float:
        """Calculate current R-multiple."""
        risk = abs(position.entry_price - position.stop_loss)
        if risk <= 0:
            return 0.0

        if position.direction == "buy":
            return (current_price - position.entry_price) / risk
        else:
            return (position.entry_price - current_price) / risk

    async def manage_all_positions(self, current_prices: dict, zone_heights: dict,
                                   exit_style: str = "fixed"):
        """Manage all open/partial positions.

        exit_style:
          - "fixed": default R:R with partial TP / BE / trailing per config
          - "conservative": TP1 = 1.5R, immediate BE, no trailing
          - "adaptive": TP1 = 1.5R, TP2 = 3R, BE then trailing
        """
        use_partial_tp = self.settings.get("use_partial_tp", True)
        use_breakeven = self.settings.get("use_breakeven", True)
        trailing_enabled = self.settings.get("trailing_enabled", True)

        # Override flags based on exit_style
        if exit_style == "conservative":
            use_partial_tp = True
            use_breakeven = True
            trailing_enabled = False
        elif exit_style == "adaptive":
            use_partial_tp = True
            use_breakeven = True
            trailing_enabled = True

        # Time stop config
        time_stop_cfg = self.settings.get("time_stop_bars", {})

        for pos in self.positions:
            if pos.status == "closed":
                continue

            price = current_prices.get(pos.symbol, 0)
            if price <= 0:
                continue

            pos.r_result = round(self.get_r_result(pos, price), 2)

            # ─── TIME STOP: close if held too long without progress ───
            if pos.status in ("open", "partial") and time_stop_cfg:
                pos.hold_bar_count += 1
                tf = pos.order_timeframe or "H1"
                max_hold = time_stop_cfg.get(tf, 12)
                if pos.hold_bar_count >= max_hold:
                    await self.broker.close_full(pos.ticket)
                    pos.status = "closed"
                    pos.r_result = round(self.get_r_result(pos, price), 2)
                    logger.info(f"TIME STOP: {pos.ticket} after {pos.hold_bar_count} bars "
                                f"(tf={tf}, limit={max_hold}) R={pos.r_result}")
                    continue

            # Check SL hit
            if self.check_sl_hit(pos, price):
                await self.broker.close_full(pos.ticket)
                pos.status = "closed"
                logger.info(f"SL hit: {pos.ticket} R={pos.r_result}")
                continue

            # Check TP1 — only if partial TP is enabled
            if use_partial_tp and not pos.tp1_hit and self.check_tp1(pos, price):
                await self.close_half(pos)
                if use_breakeven:
                    await self.move_sl_to_breakeven(pos)
                continue

            # Single TP mode (no partial): close full at TP1
            if not use_partial_tp and not pos.tp1_hit and self.check_tp1(pos, price):
                await self.broker.close_full(pos.ticket)
                pos.status = "closed"
                pos.r_result = round(self.get_r_result(pos, price), 2)
                logger.info(f"TP hit (single): {pos.ticket} R={pos.r_result}")
                continue

            # Check TP2
            if pos.tp1_hit and self.check_tp2(pos, price):
                await self.broker.close_full(pos.ticket)
                pos.status = "closed"
                pos.r_result = round(self.get_r_result(pos, price), 2)
                logger.info(f"TP2 hit: {pos.ticket} R={pos.r_result}")
                continue

            # Breakeven without partial TP: move to BE at TP1 level
            if use_breakeven and not use_partial_tp and not pos.be_set and self.check_tp1(pos, price):
                await self.move_sl_to_breakeven(pos)

            # Update trailing stop — only if enabled
            if trailing_enabled:
                zh = zone_heights.get(pos.symbol, 0)
                if zh > 0 and pos.tp1_hit:
                    await self.update_trailing_stop(pos, price, zh)

    def get_open_count(self) -> int:
        return len([p for p in self.positions if p.status in ("open", "partial")])
