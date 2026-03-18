"""PinShot Bot — HFM MetaTrader 5 Broker (Windows only)

Uses the MetaTrader5 Python package to connect directly to HFM MT5.
For Linux/Hetzner deployment, use mt5_bridge_client instead.
"""

import logging
import time
from .base import BaseBroker, AccountInfo, OrderResult

logger = logging.getLogger("pinshot.mt5_hfm")

# Timeframe mapping
TF_MAP = {
    "M1": None, "M5": None, "M15": None, "M30": None,
    "H1": None, "H4": None, "D1": None, "W1": None, "MN1": None,
}

try:
    import MetaTrader5 as mt5
    TF_MAP = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not available (Windows only)")


MAGIC_NUMBER = 20260316


class MT5HFMBroker(BaseBroker):
    """Direct HFM MT5 connection via Python MetaTrader5 library."""

    def __init__(self, login: int, password: str, server: str,
                 path: str = "", magic: int = MAGIC_NUMBER):
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 package requires Windows. Use MT5BridgeClient for Linux.")
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.magic = magic
        self.connected = False

    async def connect(self) -> bool:
        """Initialize MT5 connection to HFM."""
        kwargs = {"login": self.login, "password": self.password, "server": self.server}
        if self.path:
            kwargs["path"] = self.path

        if not mt5.initialize(**kwargs):
            error = mt5.last_error()
            logger.error(f"MT5 init failed: {error}")
            return False

        info = mt5.account_info()
        if info is None:
            logger.error("Failed to get account info")
            return False

        self.connected = True
        logger.info(f"Connected to HFM MT5: {info.login} | Balance: {info.balance} {info.currency}")
        return True

    def _ensure_connected(self):
        if not self.connected:
            raise RuntimeError("MT5 not connected. Call connect() first.")

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list:
        self._ensure_connected()
        from ..core.detector import Candle

        tf = TF_MAP.get(timeframe)
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"No data for {symbol} {timeframe}")
            return []

        candles = []
        for r in rates:
            candles.append(Candle(
                time=float(r['time']),
                open=float(r['open']),
                high=float(r['high']),
                low=float(r['low']),
                close=float(r['close']),
                volume=float(r['tick_volume']),
            ))
        return candles

    async def get_price(self, symbol: str) -> dict:
        self._ensure_connected()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"bid": 0, "ask": 0, "spread": 0}
        return {
            "bid": tick.bid,
            "ask": tick.ask,
            "spread": round(tick.ask - tick.bid, 6),
        }

    def _get_symbol_info(self, symbol: str) -> dict:
        info = mt5.symbol_info(symbol)
        if info is None:
            return {"digits": 5, "point": 0.00001, "min_lot": 0.01}
        return {
            "digits": info.digits,
            "point": info.point,
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "lot_step": info.volume_step,
            "spread": info.spread,
        }

    async def place_limit_order(self, symbol: str, direction: str, price: float,
                                sl: float, tp: float, lot: float) -> OrderResult:
        self._ensure_connected()
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "buy" else mt5.ORDER_TYPE_SELL_LIMIT

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "magic": self.magic,
            "comment": "PinShot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, message=str(mt5.last_error()))
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(success=False, message=f"Error {result.retcode}: {result.comment}")

        logger.info(f"Limit order placed: {direction.upper()} {symbol} @ {price} lot={lot}")
        return OrderResult(success=True, ticket=str(result.order), fill_price=price)

    async def place_market_order(self, symbol: str, direction: str,
                                 sl: float, tp: float, lot: float) -> OrderResult:
        self._ensure_connected()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(success=False, message="No tick data")

        order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction == "buy" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "magic": self.magic,
            "comment": "PinShot",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, message=str(mt5.last_error()))
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(success=False, message=f"Error {result.retcode}: {result.comment}")

        logger.info(f"Market order: {direction.upper()} {symbol} @ {result.price} lot={lot}")
        return OrderResult(success=True, ticket=str(result.order), fill_price=result.price)

    async def modify_order(self, ticket: str, sl: float = None, tp: float = None) -> bool:
        self._ensure_connected()
        ticket_int = int(ticket)

        # Check if it's an open position
        pos = mt5.positions_get(ticket=ticket_int)
        if pos and len(pos) > 0:
            p = pos[0]
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket_int,
                "symbol": p.symbol,
                "sl": sl if sl is not None else p.sl,
                "tp": tp if tp is not None else p.tp,
                "magic": self.magic,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            logger.error(f"Modify position failed: {result}")
            return False

        # Check if it's a pending order
        orders = mt5.orders_get(ticket=ticket_int)
        if orders and len(orders) > 0:
            o = orders[0]
            request = {
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": ticket_int,
                "symbol": o.symbol,
                "price": o.price_open,
                "sl": sl if sl is not None else o.sl,
                "tp": tp if tp is not None else o.tp,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                return True
            logger.error(f"Modify order failed: {result}")

        return False

    async def close_partial(self, ticket: str, lot: float) -> bool:
        self._ensure_connected()
        ticket_int = int(ticket)
        pos = mt5.positions_get(ticket=ticket_int)
        if not pos or len(pos) == 0:
            return False

        p = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": lot,
            "type": close_type,
            "position": ticket_int,
            "price": price,
            "magic": self.magic,
            "comment": "PinShot partial",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Partial close: {ticket} lot={lot}")
            return True
        logger.error(f"Partial close failed: {result}")
        return False

    async def close_full(self, ticket: str) -> bool:
        self._ensure_connected()
        ticket_int = int(ticket)
        pos = mt5.positions_get(ticket=ticket_int)
        if not pos or len(pos) == 0:
            return False

        p = pos[0]
        close_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": close_type,
            "position": ticket_int,
            "price": price,
            "magic": self.magic,
            "comment": "PinShot close",
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Full close: {ticket}")
            return True
        logger.error(f"Full close failed: {result}")
        return False

    async def cancel_order(self, ticket: str) -> bool:
        self._ensure_connected()
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket),
        }
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        return False

    async def get_account(self) -> AccountInfo:
        self._ensure_connected()
        info = mt5.account_info()
        if info is None:
            return AccountInfo(0, 0, 0, 0)
        return AccountInfo(
            balance=info.balance,
            equity=info.equity,
            margin_used=info.margin,
            margin_available=info.margin_free,
            currency=info.currency,
        )

    async def get_open_positions(self) -> list:
        self._ensure_connected()
        positions = mt5.positions_get()
        if not positions:
            return []
        result = []
        for p in positions:
            if p.magic != self.magic:
                continue
            result.append({
                "ticket": str(p.ticket),
                "symbol": p.symbol,
                "direction": "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                "open_price": p.price_open,
                "lot": p.volume,
                "sl": p.sl,
                "tp": p.tp,
                "pnl": p.profit,
            })
        return result

    async def get_pending_orders(self) -> list:
        self._ensure_connected()
        orders = mt5.orders_get()
        if not orders:
            return []
        result = []
        for o in orders:
            if o.magic != self.magic:
                continue
            dir_map = {
                mt5.ORDER_TYPE_BUY_LIMIT: "buy_limit",
                mt5.ORDER_TYPE_SELL_LIMIT: "sell_limit",
                mt5.ORDER_TYPE_BUY_STOP: "buy_stop",
                mt5.ORDER_TYPE_SELL_STOP: "sell_stop",
            }
            result.append({
                "ticket": str(o.ticket),
                "symbol": o.symbol,
                "type": dir_map.get(o.type, "unknown"),
                "price": o.price_open,
                "lot": o.volume_current,
                "sl": o.sl,
                "tp": o.tp,
            })
        return result

    def shutdown(self):
        if MT5_AVAILABLE and self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info("MT5 disconnected")
