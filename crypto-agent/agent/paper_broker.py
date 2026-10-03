"""Simulated broker: same interface as RobinhoodCrypto, priced off live public market data."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .broker import Fill, OrderRejected, PairInfo, Quote

_INCREMENTS = {"BTC-USD": Decimal("0.00000001"), "ETH-USD": Decimal("0.000001")}
_DEFAULT_INCREMENT = Decimal("0.0001")


class PaperBroker:
    def __init__(self, state_path: Path, starting_cash: float, slippage_pct: float,
                 price_fn: Callable[[str], float]):
        self._path = state_path
        self._slip = slippage_pct / 100
        self._price_fn = price_fn
        if state_path.exists():
            self._state = json.loads(state_path.read_text())
        else:
            self._state = {"cash": starting_cash, "holdings": {}}
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    def quote(self, symbol: str) -> Quote:
        p = float(self._price_fn(symbol))
        return Quote(symbol=symbol, bid=p * (1 - self._slip), ask=p * (1 + self._slip))

    def buying_power(self) -> float:
        return float(self._state["cash"])

    def holdings(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._state["holdings"].items() if float(v) > 0}

    def pair_info(self, symbol: str) -> PairInfo:
        inc = _INCREMENTS.get(symbol, _DEFAULT_INCREMENT)
        return PairInfo(symbol=symbol, asset_increment=inc, min_order_size=inc)

    def market_order(self, symbol: str, side: str, asset_quantity: Decimal, client_order_id: str) -> Fill:
        qty = float(asset_quantity)
        if qty <= 0:
            raise OrderRejected("quantity must be positive")
        q = self.quote(symbol)
        held = float(self._state["holdings"].get(symbol, 0))
        if side == "buy":
            cost = qty * q.ask
            if cost > self._state["cash"] + 1e-9:
                raise OrderRejected(f"insufficient paper cash: need {cost:.2f}, have {self._state['cash']:.2f}")
            self._state["cash"] -= cost
            self._state["holdings"][symbol] = held + qty
            price = q.ask
        elif side == "sell":
            if qty > held + 1e-12:
                raise OrderRejected(f"insufficient paper {symbol}: need {qty}, have {held}")
            self._state["cash"] += qty * q.bid
            self._state["holdings"][symbol] = held - qty
            price = q.bid
        else:
            raise OrderRejected(f"unknown side {side}")
        self._save()
        return Fill(symbol=symbol, side=side, quantity=qty, price=price,
                    order_id=f"paper-{uuid.uuid4()}", client_order_id=client_order_id)
