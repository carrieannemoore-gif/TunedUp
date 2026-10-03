"""Broker interface shared by the live Robinhood client and the paper simulator."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Protocol


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class PairInfo:
    symbol: str
    asset_increment: Decimal
    min_order_size: Decimal


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    price: float
    order_id: str
    client_order_id: str

    @property
    def notional(self) -> float:
        return self.quantity * self.price


class OrderRejected(Exception):
    """The broker definitively did not execute the order."""


class OrderStateUnknown(Exception):
    """We cannot tell whether the order executed. The agent must halt and a human must check."""


class Broker(Protocol):
    def quote(self, symbol: str) -> Quote: ...
    def buying_power(self) -> float: ...
    def holdings(self) -> dict[str, float]: ...
    def pair_info(self, symbol: str) -> PairInfo: ...
    def market_order(self, symbol: str, side: str, asset_quantity: Decimal, client_order_id: str) -> Fill: ...


def quantize_down(qty: float | Decimal, increment: Decimal) -> Decimal:
    """Round a quantity DOWN to the asset increment, so we never order more than intended."""
    q = Decimal(str(qty))
    if increment <= 0:
        return q
    return (q / increment).to_integral_value(rounding=ROUND_DOWN) * increment
