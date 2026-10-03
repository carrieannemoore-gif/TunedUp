"""Tracks ONLY the positions this agent opened, plus P&L, trade counts and halt state.

Crypto you already held before starting the agent is never counted as agent-managed and
will never be sold by it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .broker import Fill, Quote


def _empty_state() -> dict:
    return {
        "positions": {},
        "realized_total": 0.0,
        "day": None,
        "realized_today": 0.0,
        "trades_today": 0,
        "unrealized_at_day_start": 0.0,
        "equity_at_day_start": None,
        "starting_equity": None,
        "pnl_offset": 0.0,
        "last_trade_at": {},
        "halted": False,
        "halt_reason": None,
    }


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text()) if path.exists() else _empty_state()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    # --- valuation (marked at the bid: what we could actually sell for) ---

    @property
    def positions(self) -> dict[str, dict]:
        return self.state["positions"]

    def qty(self, symbol: str) -> float:
        return float(self.positions.get(symbol, {}).get("qty", 0.0))

    def exposure(self, quotes: dict[str, Quote]) -> float:
        return sum(p["qty"] * quotes[s].bid for s, p in self.positions.items() if s in quotes)

    def unrealized(self, quotes: dict[str, Quote]) -> float:
        return sum(p["qty"] * (quotes[s].bid - p["avg_cost"]) for s, p in self.positions.items() if s in quotes)

    def day_pnl(self, quotes: dict[str, Quote]) -> float:
        return self.state["realized_today"] + self.unrealized(quotes) - self.state["unrealized_at_day_start"]

    def total_pnl(self, quotes: dict[str, Quote]) -> float:
        return self.state["realized_total"] + self.unrealized(quotes)

    def drawdown_pnl(self, quotes: dict[str, Quote]) -> float:
        """P&L since the drawdown baseline (first run or last --rebaseline)."""
        return self.total_pnl(quotes) - self.state.get("pnl_offset", 0.0)

    # --- lifecycle --------------------------------------------------------

    def roll_day(self, now: datetime, quotes: dict[str, Quote], equity: float) -> None:
        """Start a new UTC day's counters, and set the drawdown baseline on the very first cycle."""
        if self.state.get("starting_equity") is None:
            self.state["starting_equity"] = equity
        today = now.date().isoformat()
        if self.state["day"] != today:
            self.state.update(day=today, realized_today=0.0, trades_today=0,
                              unrealized_at_day_start=self.unrealized(quotes), equity_at_day_start=equity)

    def rebaseline(self, equity: float, quotes: dict[str, Quote]) -> None:
        """Restart drawdown measurement from now, e.g. after you change the allowance.

        Realized P&L history is kept (it matters for taxes); only the drawdown reference moves.
        """
        self.state["starting_equity"] = equity
        self.state["equity_at_day_start"] = equity
        self.state["pnl_offset"] = self.total_pnl(quotes)

    def reconcile(self, broker_holdings: dict[str, float]) -> list[dict]:
        """Shrink agent positions that no longer exist at the broker (e.g. you sold manually)."""
        adjustments = []
        for symbol, pos in list(self.positions.items()):
            available = broker_holdings.get(symbol, 0.0)
            if pos["qty"] > available + 1e-12:
                adjustments.append({"symbol": symbol, "ledger_qty": pos["qty"], "broker_qty": available})
                if available <= 0:
                    del self.positions[symbol]
                else:
                    pos["qty"] = available
        return adjustments

    def apply_fill(self, fill: Fill, now: datetime, stop_loss_pct: float | None = None,
                   take_profit_pct: float | None = None, counts_toward_daily_limit: bool = True) -> float:
        """Update positions from a fill. Returns realized P&L (0 for buys)."""
        realized = 0.0
        pos = self.positions.get(fill.symbol)
        if fill.side == "buy":
            if pos:
                new_qty = pos["qty"] + fill.quantity
                pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + fill.notional) / new_qty
                pos["qty"] = new_qty
            else:
                pos = self.positions[fill.symbol] = {"qty": fill.quantity, "avg_cost": fill.price,
                                                     "opened_at": now.isoformat()}
            if stop_loss_pct is not None:
                pos["stop_loss_pct"] = stop_loss_pct
            if take_profit_pct is not None:
                pos["take_profit_pct"] = take_profit_pct
        else:
            if not pos:
                raise ValueError(f"sell fill for {fill.symbol} but the agent holds no position")
            sold = min(fill.quantity, pos["qty"])
            realized = sold * (fill.price - pos["avg_cost"])
            pos["qty"] -= sold
            if pos["qty"] <= 1e-12:
                del self.positions[fill.symbol]
            self.state["realized_total"] += realized
            self.state["realized_today"] += realized
        if counts_toward_daily_limit:
            self.state["trades_today"] += 1
        self.state["last_trade_at"][fill.symbol] = now.isoformat()
        return realized

    def halt(self, reason: str) -> None:
        self.state["halted"] = True
        self.state["halt_reason"] = reason

    def reset_halt(self) -> None:
        self.state["halted"] = False
        self.state["halt_reason"] = None
