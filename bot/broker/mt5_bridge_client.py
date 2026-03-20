"""PinShot Bot — MT5 Bridge Client (for Linux/Hetzner)

Connects to mt5_bridge_server.py running on Windows PC alongside MT5.
Uses HTTP to relay all broker operations.
"""

import logging
import httpx
from .base import BaseBroker, AccountInfo, OrderResult

logger = logging.getLogger("pinshot.mt5_bridge")


class MT5BridgeClient(BaseBroker):
    """HTTP client that connects to MT5 bridge server on Windows PC."""

    def __init__(self, bridge_url: str, api_key: str = "", timeout: float = 10.0):
        self.base_url = bridge_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.headers = {}
        if api_key:
            self.headers["X-API-Key"] = api_key

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, url, headers=self.headers, **kwargs)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"Bridge request failed: {method} {path} — {e}")
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"Bridge error: {e}")
            return {"error": str(e)}

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list:
        from ..core.detector import Candle
        data = await self._request("GET", f"/candles/{symbol}/{timeframe}/{count}")
        if "error" in data:
            return []
        candles = []
        for c in data.get("candles", []):
            candles.append(Candle(
                time=float(c["time"]),
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c.get("volume", 0)),
            ))
        return candles

    async def get_price(self, symbol: str) -> dict:
        data = await self._request("GET", f"/price/{symbol}")
        if "error" in data:
            return {"bid": 0, "ask": 0, "spread": 0}
        return data

    async def place_limit_order(self, symbol: str, direction: str, price: float,
                                sl: float, tp: float, lot: float) -> OrderResult:
        data = await self._request("POST", "/order/limit", json={
            "symbol": symbol, "direction": direction, "price": price,
            "lot": lot, "sl": sl, "tp": tp,
        })
        if "error" in data or not data.get("success"):
            return OrderResult(success=False, message=data.get("error", data.get("message", "Unknown")))
        return OrderResult(success=True, ticket=str(data.get("ticket", "")), fill_price=price)

    async def place_market_order(self, symbol: str, direction: str,
                                 sl: float, tp: float, lot: float) -> OrderResult:
        data = await self._request("POST", "/order/market", json={
            "symbol": symbol, "direction": direction,
            "lot": lot, "sl": sl, "tp": tp,
        })
        if "error" in data or not data.get("success"):
            return OrderResult(success=False, message=data.get("error", data.get("message", "Unknown")))
        return OrderResult(
            success=True, ticket=str(data.get("ticket", "")),
            fill_price=data.get("fill_price", 0),
        )

    async def modify_order(self, ticket: str, sl: float = None, tp: float = None) -> bool:
        body = {"ticket": ticket}
        if sl is not None:
            body["sl"] = sl
        if tp is not None:
            body["tp"] = tp
        data = await self._request("PUT", "/position/modify", json=body)
        return data.get("success", False)

    async def close_partial(self, ticket: str, lot: float) -> bool:
        data = await self._request("POST", "/position/close", json={
            "ticket": ticket, "lot": lot,
        })
        return data.get("success", False)

    async def close_full(self, ticket: str) -> bool:
        data = await self._request("POST", "/position/close", json={
            "ticket": ticket,
        })
        return data.get("success", False)

    async def cancel_order(self, ticket: str) -> bool:
        data = await self._request("DELETE", f"/order/{ticket}")
        return data.get("success", False)

    async def get_account(self) -> AccountInfo:
        data = await self._request("GET", "/account")
        if "error" in data:
            return AccountInfo(0, 0, 0, 0)
        return AccountInfo(
            balance=data.get("balance", 0),
            equity=data.get("equity", 0),
            margin_used=data.get("margin", 0),
            margin_available=data.get("free_margin", 0),
            currency=data.get("currency", "USD"),
        )

    async def get_open_positions(self) -> list:
        data = await self._request("GET", "/positions")
        return data.get("positions", [])

    async def get_pending_orders(self) -> list:
        data = await self._request("GET", "/orders")
        return data.get("orders", [])

    async def get_history(self, days: int = 30) -> list:
        """Get closed trade history from MT5."""
        data = await self._request("GET", f"/history?days={days}")
        return data.get("deals", [])
