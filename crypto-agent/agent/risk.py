"""Deterministic risk engine. It has the final say: it only ever shrinks or rejects orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .broker import PairInfo, Quote, quantize_down
from .config import Limits
from .ledger import Ledger
from .models import Decision, OrderIntent


@dataclass
class RiskContext:
    limits: Limits
    allowed_symbols: tuple[str, ...]
    quotes: dict[str, Quote]
    pairs: dict[str, PairInfo]
    ledger: Ledger
    buying_power: float
    now: datetime
    verdicts: list[dict] = field(default_factory=list)

    def reject(self, d: Decision | None, symbol: str, reason: str) -> None:
        self.verdicts.append({"symbol": symbol, "action": d.action if d else None, "verdict": "rejected",
                              "reason": reason})

    def approve(self, intent: OrderIntent, note: str = "") -> None:
        self.verdicts.append({"symbol": intent.symbol, "action": intent.side, "verdict": "approved",
                              "asset_quantity": str(intent.asset_quantity), "est_usd": round(intent.est_usd, 2),
                              "source": intent.source, "note": note})


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def agent_cash(lim: Limits, ledger: Ledger, broker_buying_power: float) -> float:
    """Cash the agent may spend.

    With an allowance, the agent runs a virtual sub-account: allowance + realized P&L - money tied up in
    its open positions, and never more than the broker actually reports. Without an allowance (paper
    mode only) it is simply the broker's buying power.
    """
    if lim.allowance_usd is None:
        return max(0.0, broker_buying_power)
    budget = lim.allowance_usd + ledger.state["realized_total"] - ledger.cost_basis()
    return max(0.0, min(broker_buying_power, budget))


def equity(ledger: Ledger, quotes: dict[str, Quote], cash: float) -> float:
    """Agent equity: the agent's available cash plus agent-held positions marked at the bid."""
    return cash + ledger.exposure(quotes)


def max_order_usd(lim: Limits, equity_usd: float) -> float:
    return equity_usd * lim.max_order_pct / 100


def daily_loss_limit_usd(lim: Limits, ledger: Ledger, fallback_equity: float) -> float:
    base = ledger.state.get("equity_at_day_start") or fallback_equity
    return base * lim.max_daily_loss_pct / 100


def drawdown_limit_usd(lim: Limits, ledger: Ledger, fallback_equity: float) -> float:
    base = ledger.state.get("starting_equity") or fallback_equity
    return base * lim.max_drawdown_pct / 100


def protective_exits(ctx: RiskContext) -> list[OrderIntent]:
    """Stop-loss / take-profit enforced by code every cycle, independent of Claude."""
    lim, intents = ctx.limits, []
    for symbol, pos in ctx.ledger.positions.items():
        q, pair = ctx.quotes.get(symbol), ctx.pairs.get(symbol)
        if q is None or pair is None:
            continue
        sl = pos.get("stop_loss_pct", lim.default_stop_loss_pct)
        tp = pos.get("take_profit_pct", lim.default_take_profit_pct)
        qty = quantize_down(pos["qty"], pair.asset_increment)
        if qty <= 0 or qty < pair.min_order_size:
            continue
        move_pct = (q.bid / pos["avg_cost"] - 1) * 100
        source = "stop_loss" if move_pct <= -sl else "take_profit" if move_pct >= tp else None
        if source:
            intent = OrderIntent(symbol, "sell", qty, float(qty) * q.bid, source,
                                 f"{source}: price moved {move_pct:.2f}% from entry {pos['avg_cost']:.6f}")
            ctx.approve(intent)
            intents.append(intent)
    return intents


def evaluate(decisions: list[Decision], ctx: RiskContext, already_selling: set[str] | None = None) -> list[OrderIntent]:
    lim = ctx.limits
    already_selling = already_selling or set()
    seen: set[str] = set()
    trades_left = lim.max_trades_per_day - ctx.ledger.state["trades_today"]
    exposure = ctx.ledger.exposure(ctx.quotes)
    cash = ctx.buying_power
    eq = equity(ctx.ledger, ctx.quotes, cash)
    order_cap = max_order_usd(lim, eq)
    day_pnl = ctx.ledger.day_pnl(ctx.quotes)
    day_loss_limit = daily_loss_limit_usd(lim, ctx.ledger, eq)
    sells, buys = [], []

    # Sells first (they free exposure), then buys in descending confidence.
    ordered = sorted(decisions, key=lambda d: (d.action != "sell", -d.confidence))
    for d in ordered:
        symbol = d.symbol.upper()
        if d.action == "hold":
            continue
        if symbol in seen:
            ctx.reject(d, symbol, "duplicate decision for symbol this cycle")
            continue
        seen.add(symbol)
        if ctx.ledger.state["halted"]:
            ctx.reject(d, symbol, f"agent halted: {ctx.ledger.state['halt_reason']}")
            continue
        if symbol not in ctx.allowed_symbols:
            ctx.reject(d, symbol, "symbol not in allowed_symbols")
            continue
        if symbol in already_selling:
            ctx.reject(d, symbol, "protective exit already selling this symbol")
            continue
        if d.confidence < lim.min_confidence:
            ctx.reject(d, symbol, f"confidence {d.confidence:.2f} < min {lim.min_confidence}")
            continue
        if trades_left <= 0:
            ctx.reject(d, symbol, "max_trades_per_day reached")
            continue
        q, pair = ctx.quotes.get(symbol), ctx.pairs.get(symbol)
        if q is None or pair is None:
            ctx.reject(d, symbol, "no quote / pair info")
            continue

        if d.action == "sell":
            held = ctx.ledger.qty(symbol)
            if held <= 0:
                ctx.reject(d, symbol, "agent holds no position (pre-existing holdings are never sold)")
                continue
            # Selling ~all of it? Sell exactly all, avoiding dust.
            want = held if d.usd_amount >= held * q.bid * 0.95 else d.usd_amount / q.bid
            qty = quantize_down(min(want, held), pair.asset_increment)
            est = float(qty) * q.bid
            if qty <= 0 or qty < pair.min_order_size or (est < lim.min_order_usd and qty < quantize_down(held, pair.asset_increment)):
                ctx.reject(d, symbol, f"sell size ${est:.2f} below minimum")
                continue
            intent = OrderIntent(symbol, "sell", qty, est, "claude", d.rationale)
            ctx.approve(intent)
            sells.append(intent)
            exposure -= est
            trades_left -= 1
            continue

        # --- buy ---
        if day_pnl <= -day_loss_limit:
            ctx.reject(d, symbol, f"daily loss limit hit (day P&L ${day_pnl:.2f}, limit -${day_loss_limit:.2f})")
            continue
        last = ctx.ledger.state["last_trade_at"].get(symbol)
        if last and ctx.now - datetime.fromisoformat(last) < timedelta(minutes=lim.cooldown_minutes_per_symbol):
            ctx.reject(d, symbol, "symbol cooldown active")
            continue
        room = float("inf") if lim.max_total_exposure_usd is None else lim.max_total_exposure_usd - exposure
        usd = min(d.usd_amount, order_cap, room, cash)
        qty = quantize_down(usd / q.ask, pair.asset_increment)
        est = float(qty) * q.ask
        if est < lim.min_order_usd or qty < pair.min_order_size:
            ctx.reject(d, symbol, f"buy clipped to ${est:.2f} (order cap ${order_cap:.2f}, exposure room ${room:.2f}, cash ${cash:.2f}) - below minimum")
            continue
        sl = _clamp(d.stop_loss_pct or lim.default_stop_loss_pct, lim.min_stop_loss_pct, lim.max_stop_loss_pct)
        tp = _clamp(d.take_profit_pct or lim.default_take_profit_pct, lim.min_take_profit_pct, lim.max_take_profit_pct)
        intent = OrderIntent(symbol, "buy", qty, est, "claude", d.rationale, sl, tp)
        note = f"clipped from ${d.usd_amount:.2f}" if est < d.usd_amount - 0.01 else ""
        ctx.approve(intent, note)
        buys.append(intent)
        exposure += est
        cash -= est
        trades_left -= 1

    return sells + buys
