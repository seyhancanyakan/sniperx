"""SniperX — Live Market Data Broker (Yahoo Finance)

Provides real market data for chart display and zone detection.
Uses yfinance for historical data (free, no API key needed).
Trading operations fall back to paper mode.
"""

import logging
import time
import yfinance as yf
from .base import BaseBroker, AccountInfo, OrderResult
from .paper import PaperBroker

logger = logging.getLogger("sniperx.live_data")

# Symbol mapping: our format → Yahoo Finance format
SYMBOL_MAP = {
    # Forex Majör
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X", "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X",
    "USDCAD": "USDCAD=X",
    # Forex Cross
    "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X", "GBPJPY": "GBPJPY=X",
    "AUDJPY": "AUDJPY=X", "NZDJPY": "NZDJPY=X", "CADJPY": "CADJPY=X",
    "CHFJPY": "CHFJPY=X", "EURCAD": "EURCAD=X", "EURAUD": "EURAUD=X",
    "GBPAUD": "GBPAUD=X", "GBPCAD": "GBPCAD=X", "AUDCAD": "AUDCAD=X",
    "AUDNZD": "AUDNZD=X", "AUDCHF": "AUDCHF=X",
    # Metals
    "XAUUSD": "GC=F", "XAGUSD": "SI=F",
    # Indices
    "US100": "NQ=F", "US30": "YM=F", "US500": "ES=F", "DE40": "^GDAXI",
    # Crypto
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
}

# Timeframe mapping: our format → yfinance interval + period
TF_MAP = {
    "M1":  {"interval": "1m",  "period": "7d"},
    "M5":  {"interval": "5m",  "period": "60d"},
    "M15": {"interval": "15m", "period": "60d"},
    "M30": {"interval": "30m", "period": "60d"},
    "H1":  {"interval": "1h",  "period": "730d"},
    "H4":  {"interval": "1h",  "period": "730d"},  # Will resample
    "D1":  {"interval": "1d",  "period": "2y"},
}


class LiveDataBroker(BaseBroker):
    """Real market data via Yahoo Finance + paper trading for orders."""

    def __init__(self, balance: float = 10000.0):
        self.paper = PaperBroker(balance)
        self._cache = {}  # (symbol, tf) → (timestamp, candles)
        self._cache_ttl = 30  # seconds

    def _yf_symbol(self, symbol: str) -> str:
        return SYMBOL_MAP.get(symbol, symbol)

    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list:
        from ..core.detector import Candle

        cache_key = (symbol, timeframe)
        now = time.time()

        # Return cached if fresh
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached_data[-count:]

        yf_sym = self._yf_symbol(symbol)
        tf_info = TF_MAP.get(timeframe, TF_MAP["M15"])

        try:
            ticker = yf.Ticker(yf_sym)
            df = ticker.history(
                period=tf_info["period"],
                interval=tf_info["interval"],
            )

            if df.empty:
                logger.warning(f"No data for {symbol} ({yf_sym}) {timeframe}")
                return await self.paper.get_candles(symbol, timeframe, count)

            # Resample for H4 if needed
            if timeframe == "H4":
                df = df.resample("4h").agg({
                    "Open": "first", "High": "max", "Low": "min",
                    "Close": "last", "Volume": "sum",
                }).dropna()

            candles = []
            for idx, row in df.iterrows():
                ts = idx.timestamp() if hasattr(idx, 'timestamp') else float(idx)
                candles.append(Candle(
                    time=ts,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                ))

            if candles:
                self._cache[cache_key] = (now, candles)
                logger.info(f"Fetched {len(candles)} real candles: {symbol} {timeframe}")

                # Update paper broker's current price
                last = candles[-1]
                pip = self.paper.PIP_VALUES.get(symbol, 0.0001)
                self.paper.update_price(symbol, last.close, last.close + 2 * pip)

            return candles[-count:]

        except Exception as e:
            logger.error(f"Yahoo Finance error for {symbol}: {e}")
            return await self.paper.get_candles(symbol, timeframe, count)

    async def get_price(self, symbol: str) -> dict:
        pip = self.paper.PIP_VALUES.get(symbol, 0.0001)
        price = 0.0

        # Try cache first
        for tf in ["M1", "M5", "M15", "M30", "H1"]:
            key = (symbol, tf)
            if key in self._cache:
                _, candles = self._cache[key]
                if candles:
                    price = candles[-1].close
                    break

        # Fallback: quick fetch
        if not price:
            yf_sym = self._yf_symbol(symbol)
            try:
                info = yf.Ticker(yf_sym).fast_info
                price = info.get("lastPrice", 0) or info.get("regularMarketPrice", 0)
            except Exception:
                pass

        if price:
            bid = price
            ask = price + 2 * pip
            # CRITICAL: update paper broker so equity calculates correctly
            self.paper.update_price(symbol, bid, ask)
            return {"bid": bid, "ask": ask, "spread": 2 * pip}

        return await self.paper.get_price(symbol)

    # Trading operations delegate to paper broker
    async def place_limit_order(self, symbol, direction, price, sl, tp, lot):
        return await self.paper.place_limit_order(symbol, direction, price, sl, tp, lot)

    async def place_market_order(self, symbol, direction, sl, tp, lot):
        return await self.paper.place_market_order(symbol, direction, sl, tp, lot)

    async def modify_order(self, ticket, sl=None, tp=None):
        return await self.paper.modify_order(ticket, sl, tp)

    async def close_partial(self, ticket, lot):
        return await self.paper.close_partial(ticket, lot)

    async def close_full(self, ticket):
        return await self.paper.close_full(ticket)

    async def cancel_order(self, ticket):
        return await self.paper.cancel_order(ticket)

    async def get_account(self):
        return await self.paper.get_account()

    async def get_open_positions(self):
        return await self.paper.get_open_positions()

    async def get_pending_orders(self):
        return await self.paper.get_pending_orders()
