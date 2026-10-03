"""Agent loop: kill switch -> quotes -> protective exits -> Claude -> risk engine -> orders -> audit.

Usage:
  python -m agent.main --once          # one cycle
  python -m agent.main                 # loop every cycle_minutes
  python -m agent.main --offline --once  # synthetic prices + paper broker (no network for market data)
  python -m agent.main --status        # print positions / P&L / halt state
  python -m agent.main --reset-halt    # clear a drawdown or unknown-order halt (after you've checked)
  python -m agent.main --check         # read-only test of quotes/balance/holdings through your MCP server
  python -m agent.main --discover      # list the MCP server's tools into state/mcp_tools.json (calls none)
  python -m agent.main --login         # one-time browser login (HTTP MCP servers only)
  python -m agent.main --rebaseline    # after changing your allowance, restart loss limits from current equity
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any

from . import indicators
from .audit import AuditLog
from .brain import Brain, ClaudeBrain
from .broker import Broker, OrderRejected, OrderStateUnknown
from .config import Config, ConfigError, load_config
from .ledger import Ledger
from .market_data import MarketData, SyntheticMarketData
from .models import OrderIntent
from .risk import (RiskContext, agent_cash, daily_loss_limit_usd, drawdown_limit_usd, equity, evaluate,
                   max_order_usd, protective_exits)


def _execute(intents: list[OrderIntent], broker: Broker, ledger: Ledger, audit: AuditLog, now: datetime) -> list[dict]:
    results = []
    for intent in intents:
        coid = str(uuid.uuid4())
        audit.record("order_submitted", client_order_id=coid, **_intent_dict(intent))
        try:
            fill = broker.market_order(intent.symbol, intent.side, intent.asset_quantity, coid)
        except OrderRejected as e:
            audit.record("order_rejected", client_order_id=coid, symbol=intent.symbol, error=str(e))
            results.append({"symbol": intent.symbol, "side": intent.side, "status": "rejected"})
            continue
        except OrderStateUnknown as e:
            ledger.halt(f"order {coid} state unknown - check the Robinhood app, then run --reset-halt")
            ledger.save()
            audit.record("order_state_unknown", client_order_id=coid, symbol=intent.symbol, error=str(e))
            results.append({"symbol": intent.symbol, "side": intent.side, "status": "unknown"})
            break
        realized = ledger.apply_fill(fill, now, intent.stop_loss_pct, intent.take_profit_pct,
                                     counts_toward_daily_limit=intent.source == "claude")
        ledger.save()
        audit.record("order_filled", **asdict(fill), notional_usd=round(fill.notional, 4),
                     realized_pnl_usd=round(realized, 4), source=intent.source)
        results.append({"symbol": fill.symbol, "side": fill.side, "status": "filled",
                        "usd": round(fill.notional, 2), "price": fill.price})
    return results


def _intent_dict(i: OrderIntent) -> dict[str, Any]:
    d = asdict(i)
    d["asset_quantity"] = str(i.asset_quantity)
    return d


def _cash(cfg: Config, ledger: Ledger, broker: Broker) -> float:
    """What the agent may spend: its allowance budget, capped by the broker's real buying power."""
    return agent_cash(cfg.limits, ledger, broker.buying_power())


def _check_drawdown(cfg: Config, ledger: Ledger, quotes, cash: float, audit: AuditLog) -> None:
    pnl = ledger.drawdown_pnl(quotes)
    limit = drawdown_limit_usd(cfg.limits, ledger, equity(ledger, quotes, cash))
    if not ledger.state["halted"] and pnl <= -limit:
        ledger.halt(f"max drawdown hit (P&L ${pnl:.2f}, limit -${limit:.2f}); review, then run --reset-halt")
        ledger.save()
        audit.record("halted", reason=ledger.state["halt_reason"])


def _brain_context(cfg: Config, ledger: Ledger, quotes, cash: float, market_snap: dict, now: datetime) -> dict:
    lim = cfg.limits
    exposure = ledger.exposure(quotes)
    eq = equity(ledger, quotes, cash)
    positions = {}
    for s, p in ledger.positions.items():
        q = quotes.get(s)
        positions[s] = {
            "qty": p["qty"], "avg_cost": p["avg_cost"],
            "value_usd": round(p["qty"] * q.bid, 2) if q else None,
            "unrealized_pct": round((q.bid / p["avg_cost"] - 1) * 100, 2) if q else None,
            "stop_loss_pct": p.get("stop_loss_pct"), "take_profit_pct": p.get("take_profit_pct"),
        }
    return {
        "timestamp_utc": now.isoformat(),
        "allowed_symbols": list(cfg.allowed_symbols),
        "limits": asdict(lim),
        "agent_positions": positions,
        "agent_equity_usd": round(eq, 2),
        "max_order_usd_now": round(max_order_usd(lim, eq), 2),
        "daily_loss_limit_usd": round(daily_loss_limit_usd(lim, ledger, eq), 2),
        "exposure_usd": round(exposure, 2),
        "exposure_room_usd": (None if lim.max_total_exposure_usd is None
                              else round(max(0.0, lim.max_total_exposure_usd - exposure), 2)),
        "cash_available_usd": round(cash, 2),
        "day_pnl_usd": round(ledger.day_pnl(quotes), 2),
        "total_pnl_usd": round(ledger.total_pnl(quotes), 2),
        "trades_left_today": max(0, lim.max_trades_per_day - ledger.state["trades_today"]),
        "market": market_snap,
    }


def run_cycle(cfg: Config, broker: Broker, market, brain: Brain, ledger: Ledger, audit: AuditLog,
              now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if cfg.stop_file.exists():
        audit.record("kill_switch", detail="STOP file present; no orders placed")
        return {"status": "stopped", "reason": "STOP file present"}

    symbols = cfg.allowed_symbols
    tracked = sorted(set(symbols) | set(ledger.positions))
    quotes = {s: broker.quote(s) for s in tracked}
    pairs = {s: broker.pair_info(s) for s in tracked}
    if cfg.live:
        adjustments = ledger.reconcile(broker.holdings())
        if adjustments:
            audit.record("reconciled", adjustments=adjustments)
    cash = _cash(cfg, ledger, broker)
    ledger.roll_day(now, quotes, equity(ledger, quotes, cash))

    # 1) Code-enforced stop-loss / take-profit, regardless of Claude or halt state.
    ctx = RiskContext(cfg.limits, symbols, quotes, pairs, ledger, cash, now)
    exits = protective_exits(ctx)
    executed = _execute(exits, broker, ledger, audit, now)
    _check_drawdown(cfg, ledger, quotes, _cash(cfg, ledger, broker), audit)

    # 2) Claude proposals, filtered by the risk engine.
    brain_result = None
    if not ledger.state["halted"]:
        market_snap = {}
        for s in symbols:
            candles = market.candles(s, cfg.candle_granularity_seconds, cfg.candle_count)
            market_snap[s] = {"bid": quotes[s].bid, "ask": quotes[s].ask,
                              **indicators.snapshot(candles, cfg.candle_granularity_seconds)}
        cash = _cash(cfg, ledger, broker)
        brain_result = brain.decide(_brain_context(cfg, ledger, quotes, cash, market_snap, now))
        audit.record("brain_decision", market_view=brain_result.market_view,
                     decisions=[d.model_dump() for d in brain_result.decisions],
                     errors=brain_result.errors, usage=brain_result.usage)
        ctx = RiskContext(cfg.limits, symbols, quotes, pairs, ledger, cash, now)
        intents = evaluate(brain_result.decisions, ctx, already_selling={i.symbol for i in exits})
        audit.record("risk_verdicts", verdicts=ctx.verdicts)
        executed += _execute(intents, broker, ledger, audit, now)
        _check_drawdown(cfg, ledger, quotes, _cash(cfg, ledger, broker), audit)
    else:
        audit.record("skipped_brain", reason=ledger.state["halt_reason"])

    ledger.save()
    summary = {
        "status": "halted" if ledger.state["halted"] else "ok",
        "mode": "LIVE" if cfg.live else "paper",
        "orders": executed,
        "exposure_usd": round(ledger.exposure(quotes), 2),
        "day_pnl_usd": round(ledger.day_pnl(quotes), 2),
        "total_pnl_usd": round(ledger.total_pnl(quotes), 2),
        "brain_errors": brain_result.errors if brain_result else [],
    }
    audit.record("cycle_summary", **summary)
    return summary


def _mcp_broker(cfg: Config, interactive: bool = False, trading: bool = False):
    from .mcp_broker import RobinhoodMCPBroker, http_session_factory, stdio_session_factory
    if cfg.mcp.get("transport") == "http":
        factory = http_session_factory(cfg.mcp, cfg.state_dir, interactive)
    else:
        factory = stdio_session_factory(cfg.mcp, cfg.mcp_server_env, cfg.allowed_symbols, cfg.log_dir, trading)
    return RobinhoodMCPBroker(cfg.mcp, factory)


def _check_mcp_ready(cfg: Config, broker) -> list[str]:
    """Fail closed: live MCP trading needs a complete mapping that matches the server's real schemas."""
    from .mcp_broker import mapping_problems
    if cfg.mcp.get("transport") == "http":
        from .mcp_auth import FileTokenStorage
        if not FileTokenStorage(cfg.state_dir / "mcp_oauth.json").has_tokens():
            return ["no Robinhood MCP login found; run: python -m agent.main --login"]
    problems = mapping_problems(cfg.mcp.get("tools") or {})
    if problems:
        return problems + ["run: python -m agent.main --discover, then map the tools in config.yaml"]
    return mapping_problems(cfg.mcp.get("tools") or {}, broker.list_tools())


def build(cfg: Config, offline: bool):
    market = SyntheticMarketData() if offline else MarketData()
    if cfg.live and cfg.live_broker == "mcp":
        broker = _mcp_broker(cfg, trading=True)
    elif cfg.live:
        from .robinhood import RobinhoodCrypto
        broker = RobinhoodCrypto(cfg.rh_api_key, cfg.rh_private_key_b64)
    else:
        from .paper_broker import PaperBroker
        broker = PaperBroker(cfg.state_dir / "paper_broker.json", cfg.paper_starting_cash_usd,
                             cfg.paper_slippage_pct, market.last_price)
    brain = ClaudeBrain(cfg.model, cfg.effort, cfg.news_search, cfg.max_searches_per_cycle)
    return broker, market, brain


def _server_label(cfg: Config) -> str:
    if cfg.mcp.get("transport") == "http":
        return str(cfg.mcp.get("url"))
    return " ".join([str(cfg.mcp.get("command"))] + [str(a) for a in cfg.mcp.get("args") or []])


def _discover(cfg: Config, interactive: bool) -> int:
    """Log in if needed, then save the server's tool list. Calls no tools at all."""
    from .mcp_broker import mapping_problems
    broker = _mcp_broker(cfg, interactive, trading=cfg.live)
    try:
        tools = broker.list_tools()
    finally:
        broker.close()
    out = cfg.state_dir / "mcp_tools.json"
    out.write_text(json.dumps(tools, indent=2))
    print(f"Connected to {_server_label(cfg)}. {len(tools)} tools saved to {out}:")
    for t in tools:
        print(f"  - {t['name']}: {(t.get('description') or '').splitlines()[0][:100] if t.get('description') else ''}")
    problems = mapping_problems(cfg.mcp.get("tools") or {}, tools)
    if not cfg.live and any("not offered" in p and "place_order" in p for p in problems):
        problems.append("(trading tools only appear in live mode, where the agent enables them)")
    print("\nTool mapping matches the server." if not problems else "\nMapping problems:\n  " + "\n  ".join(problems))
    return 0


def _check(cfg: Config) -> int:
    """Read-only smoke test of the mapping against your real account. Trading tools stay disabled."""
    broker = _mcp_broker(cfg, trading=False)
    ledger = Ledger(cfg.state_dir / "ledger_live.json")
    try:
        print(f"Server: {_server_label(cfg)}")
        for s in cfg.allowed_symbols:
            q, pair = broker.quote(s), broker.pair_info(s)
            print(f"  {s}: bid {q.bid:,.2f} / ask {q.ask:,.2f} (spread {100 * (q.ask - q.bid) / q.ask:.2f}%), "
                  f"increment {pair.asset_increment}, min order {pair.min_order_size}")
        bp = broker.buying_power()
        print(f"  Account buying power: ${bp:,.2f}")
        allowance = cfg.limits.allowance_usd
        if allowance is None:
            print("  Agent allowance: NOT SET -- live trading is refused until limits.allowance_usd is set")
        else:
            print(f"  Agent allowance: ${allowance:,.2f}; agent may spend now: ${agent_cash(cfg.limits, ledger, bp):,.2f}")
        holdings = broker.holdings()
        for sym, qty in sorted(holdings.items()):
            mine = ledger.qty(sym)
            print(f"  Holding {sym}: {qty} ({'agent-managed: ' + str(mine) if mine else 'not agent-managed; never sold'})")
        if not holdings:
            print("  No crypto holdings.")
    finally:
        broker.close()
    print("Read-only check passed: every mapped read tool returned data the agent could parse.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Risk-limited autonomous crypto agent")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--offline", action="store_true", help="synthetic prices + paper broker")
    ap.add_argument("--status", action="store_true", help="print ledger state and exit")
    ap.add_argument("--reset-halt", action="store_true", help="clear a halt after you have reviewed it")
    ap.add_argument("--rebaseline", action="store_true",
                    help="restart drawdown/daily-loss measurement from current equity (after changing the allowance)")
    ap.add_argument("--login", action="store_true", help="log in to the Robinhood Trading MCP in your browser")
    ap.add_argument("--discover", action="store_true", help="list the MCP server's tools (calls none of them)")
    ap.add_argument("--check", action="store_true",
                    help="read-only test of quotes/balance/holdings through the MCP server (trading disabled)")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    if args.offline and cfg.live:
        cfg = replace(cfg, live=False)
        print("--offline forces paper mode.")

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(cfg.state_dir / ("ledger_live.json" if cfg.live else "ledger_paper.json"))
    audit = AuditLog(cfg.log_dir / "audit.jsonl")

    if args.login or args.discover or args.check:
        if args.login and cfg.mcp.get("transport") != "http":
            print("--login is only for HTTP MCP servers; your stdio server uses the API keys in .env.")
            return 0
        if cfg.mcp.get("transport") == "stdio":
            from .config import mcp_stdio_problems
            problems = mcp_stdio_problems(cfg.mcp, cfg.mcp_server_env, need_trading=cfg.live)
            if problems:
                print("MCP server not configured:\n  " + "\n  ".join(problems), file=sys.stderr)
                return 2
        try:
            return _check(cfg) if args.check else _discover(cfg, interactive=args.login)
        except Exception as e:  # noqa: BLE001
            print(f"MCP check failed: {e!r}", file=sys.stderr)
            return 4
    if args.status:
        print(json.dumps(ledger.state, indent=2))
        return 0
    if args.reset_halt:
        audit.record("halt_reset", previous_reason=ledger.state["halt_reason"])
        ledger.reset_halt()
        ledger.save()
        print("Halt cleared.")
        return 0

    # Only one agent instance may trade at a time.
    lock = open(cfg.state_dir / "agent.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another agent instance is already running.", file=sys.stderr)
        return 3

    broker, market, brain = build(cfg, args.offline)
    try:
        if cfg.live and cfg.live_broker == "mcp":
            problems = _check_mcp_ready(cfg, broker)
            if problems:
                print("Not trading live. Fix first:\n  " + "\n  ".join(problems), file=sys.stderr)
                audit.record("startup_refused", problems=problems)
                return 4
        if args.rebaseline:
            quotes = {s: broker.quote(s) for s in sorted(set(cfg.allowed_symbols) | set(ledger.positions))}
            eq = equity(ledger, quotes, _cash(cfg, ledger, broker))
            ledger.rebaseline(eq, quotes)
            ledger.save()
            audit.record("rebaseline", equity_usd=round(eq, 2))
            print(f"Baseline reset to agent equity ${eq:,.2f}.")
            return 0

        cap = cfg.limits.max_total_exposure_usd
        print(f"Mode: {'LIVE - REAL MONEY' if cfg.live else 'paper (simulated)'}"
              f"{' via MCP: ' + _server_label(cfg) if cfg.live and cfg.live_broker == 'mcp' else ''}"
              f" | allowance: {'n/a' if cfg.limits.allowance_usd is None else f'${cfg.limits.allowance_usd:,.0f}'}"
              f"{'' if cap is None else f' | exposure cap ${cap:,.0f}'}")
        audit.record("agent_start", mode="live" if cfg.live else "paper", broker=cfg.live_broker if cfg.live else "paper",
                     offline=args.offline, limits=asdict(cfg.limits), symbols=list(cfg.allowed_symbols),
                     model=cfg.model)
        while True:
            try:
                print(run_cycle(cfg, broker, market, brain, ledger, audit))
            except Exception as e:  # fail safe: log, place nothing further this cycle
                audit.record("cycle_error", error=repr(e))
                print(f"Cycle error (no further orders this cycle): {e!r}", file=sys.stderr)
            if args.once:
                return 0
            time.sleep(cfg.cycle_minutes * 60)
    finally:
        if hasattr(broker, "close"):
            broker.close()


if __name__ == "__main__":
    sys.exit(main())
