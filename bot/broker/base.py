"""PinShot Bot — Abstract Broker Interface"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class AccountInfo:
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    currency: str = "USD"


@dataclass
class OrderResult:
    success: bool
    ticket: str = ""
    message: str = ""
    fill_price: float = 0.0


class BaseBroker(ABC):

    @abstractmethod
    async def get_candles(self, symbol: str, timeframe: str, count: int) -> list:
        ...

    @abstractmethod
    async def get_price(self, symbol: str) -> dict:
        ...

    @abstractmethod
    async def place_limit_order(self, symbol: str, direction: str, price: float,
                                sl: float, tp: float, lot: float) -> OrderResult:
        ...

    @abstractmethod
    async def place_market_order(self, symbol: str, direction: str,
                                 sl: float, tp: float, lot: float) -> OrderResult:
        ...

    @abstractmethod
    async def modify_order(self, ticket: str, sl: float = None, tp: float = None) -> bool:
        ...

    @abstractmethod
    async def close_partial(self, ticket: str, lot: float) -> bool:
        ...

    @abstractmethod
    async def close_full(self, ticket: str) -> bool:
        ...

    @abstractmethod
    async def cancel_order(self, ticket: str) -> bool:
        ...

    @abstractmethod
    async def get_account(self) -> AccountInfo:
        ...

    @abstractmethod
    async def get_open_positions(self) -> list:
        ...

    @abstractmethod
    async def get_pending_orders(self) -> list:
        ...
