"""PinShot Bot — Paper Trading Engine"""

import uuid
import time
import math
import logging
from dataclasses import dataclass, field
from .base import BaseBroker, AccountInfo, OrderResult

logger = logging.getLogger("pinshot.paper")


@dataclass
class PaperCandle:
    time: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class PaperPosition:
    ticket: str
    symbol: str
    direction: str
    open_price: float
    lot: float
    sl: float
    tp: float
    open_time: float = 0.0
    pnl: float = 0.0


@dataclass
class PaperOrder:
    ticket: str
    symbol: str
    order_type: str  # "buy_limit", "sell_limit"
    price: float
    lot: float
    sl: float
    tp: float
    placed_time: float = 0.0


@dataclass
class TradeRecord:
    ticket: str
    symbol: str
    direction: str
    open_price: float
    close_price: float
    lot: float
    pnl: float
    r_multiple: float
    open_time: float
    close_time: float


class PaperBroker(BaseBroker):
    """Simulated broker for paper trading."""

    # Pip values for common instruments
    PIP_VALUES = {
        "USDJPY": 0.01, "EURJPY": 0.01, "GBPJPY": 0.01, "AUDJPY": 0.01,
        "NZDJPY": 0.01, "CADJPY": 0.01, "CHFJPY": 0.01,
        "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001,
        "NZDUSD": 0.0001, "USDCHF": 0.0001, "USDCAD": 0.0001,
        "EURGBP": 0.0001, "AUDCAD": 0.0001, "GBPCHF": 0.0001,
        "XAUUSD": 0.01, "XAGUSD": 0.001,
        "US100": 0.1, "US30": 0.1, "US500": 0.1, "DE40": 0.1,
    }

    def __init__(self, balance: float = 10000.0, currency: str = "USD"):
        self.initial_balance = balance
        self.balance = balance
        self.equity = balance
        self.currency = currency
        self.positions: list[PaperPosition] = []
        self.pending_orders: list[PaperOrder] = []
        self.history: list[TradeRecord] = []
        self.candle_data: dict[str, list[PaperCandle]] = {}
        self.current_prices: dict[str, dict] = {}
        self._generate_initial_data()

    def _generate_initial_data(self):
        """Generate some initial candle data for paper trading."""
        import random
        symbols = {
            "USDJPY": 157.50, "USDCHF": 0.8850, "GBPUSD": 1.2650,
            "EURUSD": 1.0850, "AUDCAD": 0.9050, "AUDJPY": 97.50,
            "NZDUSD": 0.5750,
            "XAUUSD": 2985.00, "XAGUSD": 33.50,
            "US100": 19850.0, "US30": 41200.0, "US500": 5680.0, "DE40": 22800.0,
        }
        for sym, base_price in symbols.items():
            candles = []
            price = base_price
            pip = self.PIP_VALUES.get(sym, 0.0001)
            now = time.time()
            for i in range(500):
                change = random.gauss(0, 15 * pip)
                o = price
                c = price + change
                h = max(o, c) + abs(random.gauss(0, 8 * pip))
                l = min(o, c) - abs(random.gauss(0, 8 * pip))
                vol = random.randint(50, 500)
                candles.append(PaperCandle(
                    time=now - (500 - i) * 60,
                    open=round(o, 5), high=round(h, 5),
                    low=round(l, 5), close=round(c, 5),
                    volume=vol,
                ))
                price = c
            self.candle_data[sym] = candles
            self.current_prices[sym] = {
                "bid": round(price, 5),
                "ask": round(price + 2 * pip, 5),
                "spread": round(2 * pip, 5),
            }

    def _get_pip_value(self, symbol: str) -> float:
        return self.PIP_VALUES.get(symbol, 0.0001)

    def update_price(self, symbol: str, bid: float, ask: float):
        """Update current price for a symbol."""
        self.current_prices[symbol] = {
            "bid": bid, "ask": ask,
            "spread": round(ask - bid, 6),
        }
        self._check_pending_orders(symbol)
        self._update_equity()

    def _check_pending_orders(self, symbol: str):
        price_info = self.current_prices.get(symbol)
        if not price_info:
            return
        filled = []
        for order in self.pending_orders:
            if order.symbol != symbol:
                continue
            bid, ask = price_info["bid"], price_info["ask"]
            fill = False
            fill_price = 0.0
            if order.order_type == "buy_limit" and ask <= order.price:
                fill = True
                fill_price = order.price
            elif order.order_type == "sell_limit" and bid >= order.price:
                fill = True
                fill_price = order.price
            if fill:
                pos = PaperPosition(
                    ticket=order.ticket, symbol=order.symbol,
                    direction="buy" if "buy" in order.order_type else "sell",
                    open_price=fill_price, lot=order.lot,
                    sl=order.sl, tp=order.tp, open_time=time.time(),
                )
                self.positions.append(pos)
                filled.append(order)
                logger.info(f"Paper order filled: {pos.direction.upper()} {pos.symbol} @ {fill_price}")
        for o in filled:
            self.pending_orders.remove(o)

    def _update_equity(self):
        total_pnl = 0.0
        for pos in self.positions:
            price_info = self.current_prices.get(pos.symbol)
            if not price_info:
                continue
            if pos.direction == "buy":
                pnl = (price_info["bid"] - pos.open_price) * pos.lot * 100000
            else:
                pnl = (pos.open_price - price_info["ask"]) * pos.lot * 100000
            pip = self._get_pip_value(pos.symbol)
            if pip == 0.01:  # JPY pairs
                pnl = pnl / 100
            pos.pnl = round(pnl, 2)
            total_pnl += pnl
        self.equity = round(self.balance + total_pnl, 2)

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list:
        from ..core.detector import Candle
        data = self.candle_data.get(symbol, [])
        result = []
        for c in data[-count:]:
            result.append(Candle(
                time=c.time, open=c.open, high=c.high,
                low=c.low, close=c.close, volume=c.volume,
            ))
        return result

    async def get_price(self, symbol: str) -> dict:
        return self.current_prices.get(symbol, {"bid": 0, "ask": 0, "spread": 0})

    async def place_limit_order(self, symbol: str, direction: str, price: float,
                                sl: float, tp: float, lot: float) -> OrderResult:
        ticket = str(uuid.uuid4())[:8]
        order_type = f"{direction}_limit"
        order = PaperOrder(
            ticket=ticket, symbol=symbol, order_type=order_type,
            price=price, lot=lot, sl=sl, tp=tp, placed_time=time.time(),
        )
        self.pending_orders.append(order)
        logger.info(f"Paper limit order: {direction.upper()} {symbol} @ {price} lot={lot}")
        return OrderResult(success=True, ticket=ticket, fill_price=price)

    async def place_market_order(self, symbol: str, direction: str,
                                 sl: float, tp: float, lot: float) -> OrderResult:
        price_info = self.current_prices.get(symbol)
        if not price_info:
            return OrderResult(success=False, message=f"No price for {symbol}")
        fill_price = price_info["ask"] if direction == "buy" else price_info["bid"]
        ticket = str(uuid.uuid4())[:8]
        pos = PaperPosition(
            ticket=ticket, symbol=symbol, direction=direction,
            open_price=fill_price, lot=lot, sl=sl, tp=tp,
            open_time=time.time(),
        )
        self.positions.append(pos)
        logger.info(f"Paper market order: {direction.upper()} {symbol} @ {fill_price} lot={lot}")
        return OrderResult(success=True, ticket=ticket, fill_price=fill_price)

    async def modify_order(self, ticket: str, sl: float = None, tp: float = None) -> bool:
        for pos in self.positions:
            if pos.ticket == ticket:
                if sl is not None:
                    pos.sl = sl
                if tp is not None:
                    pos.tp = tp
                return True
        for order in self.pending_orders:
            if order.ticket == ticket:
                if sl is not None:
                    order.sl = sl
                if tp is not None:
                    order.tp = tp
                return True
        return False

    async def close_partial(self, ticket: str, lot: float) -> bool:
        for pos in self.positions:
            if pos.ticket == ticket:
                if lot >= pos.lot:
                    return await self.close_full(ticket)
                price_info = self.current_prices.get(pos.symbol)
                if not price_info:
                    return False
                close_price = price_info["bid"] if pos.direction == "buy" else price_info["ask"]
                pip = self._get_pip_value(pos.symbol)
                if pos.direction == "buy":
                    pnl = (close_price - pos.open_price) * lot * 100000
                else:
                    pnl = (pos.open_price - close_price) * lot * 100000
                if pip == 0.01:
                    pnl = pnl / 100
                self.balance = round(self.balance + pnl, 2)
                pos.lot = round(pos.lot - lot, 2)
                logger.info(f"Paper partial close: {ticket} lot={lot} pnl={pnl:.2f}")
                return True
        return False

    async def close_full(self, ticket: str) -> bool:
        for pos in self.positions:
            if pos.ticket == ticket:
                price_info = self.current_prices.get(pos.symbol)
                if not price_info:
                    return False
                close_price = price_info["bid"] if pos.direction == "buy" else price_info["ask"]
                pip = self._get_pip_value(pos.symbol)
                if pos.direction == "buy":
                    pnl = (close_price - pos.open_price) * pos.lot * 100000
                else:
                    pnl = (pos.open_price - close_price) * pos.lot * 100000
                if pip == 0.01:
                    pnl = pnl / 100
                self.balance = round(self.balance + pnl, 2)
                sl_dist = abs(pos.open_price - pos.sl)
                r_mult = 0.0
                if sl_dist > 0:
                    if pos.direction == "buy":
                        r_mult = (close_price - pos.open_price) / sl_dist
                    else:
                        r_mult = (pos.open_price - close_price) / sl_dist
                self.history.append(TradeRecord(
                    ticket=ticket, symbol=pos.symbol, direction=pos.direction,
                    open_price=pos.open_price, close_price=close_price,
                    lot=pos.lot, pnl=round(pnl, 2), r_multiple=round(r_mult, 2),
                    open_time=pos.open_time, close_time=time.time(),
                ))
                self.positions.remove(pos)
                logger.info(f"Paper close: {ticket} pnl={pnl:.2f} R={r_mult:.2f}")
                return True
        return False

    async def cancel_order(self, ticket: str) -> bool:
        for order in self.pending_orders:
            if order.ticket == ticket:
                self.pending_orders.remove(order)
                return True
        return False

    async def get_account(self) -> AccountInfo:
        self._update_equity()
        margin = sum(p.lot * 1000 for p in self.positions)
        return AccountInfo(
            balance=self.balance,
            equity=self.equity,
            margin_used=round(margin, 2),
            margin_available=round(self.equity - margin, 2),
            currency=self.currency,
        )

    async def get_open_positions(self) -> list:
        return [{"ticket": p.ticket, "symbol": p.symbol, "direction": p.direction,
                 "open_price": p.open_price, "lot": p.lot, "sl": p.sl, "tp": p.tp,
                 "pnl": p.pnl} for p in self.positions]

    async def get_pending_orders(self) -> list:
        return [{"ticket": o.ticket, "symbol": o.symbol, "type": o.order_type,
                 "price": o.price, "lot": o.lot, "sl": o.sl, "tp": o.tp}
                for o in self.pending_orders]
