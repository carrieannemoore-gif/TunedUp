import json
from datetime import timedelta
from decimal import Decimal

import pytest

from agent.audit import AuditLog
from agent.broker import OrderRejected
from agent.ledger import Ledger
from agent.main import run_cycle
from agent.market_data import SyntheticMarketData
from agent.paper_broker import PaperBroker
from conftest import NOW, FakeBrain


def make_broker(tmp_path, price=100.0):
    return PaperBroker(tmp_path / "pb.json", starting_cash=100, slippage_pct=0.5, price_fn=lambda s: price)


def test_paper_round_trip_pnl(tmp_path):
    b = make_broker(tmp_path)
    ledger = Ledger(tmp_path / "l.json")
    buy = b.market_order("SOL-USD", "buy", Decimal("0.5"), "c1")
    assert buy.price == pytest.approx(100.5) and b.buying_power() == pytest.approx(100 - 50.25)
    ledger.apply_fill(buy, NOW)
    sell = b.market_order("SOL-USD", "sell", Decimal("0.5"), "c2")
    realized = ledger.apply_fill(sell, NOW)
    assert realized == pytest.approx(0.5 * (99.5 - 100.5))  # slippage costs money both ways
    assert ledger.qty("SOL-USD") == 0 and ledger.state["realized_total"] == pytest.approx(realized)


def test_paper_rejects_overspend_and_oversell(tmp_path):
    b = make_broker(tmp_path)
    with pytest.raises(OrderRejected):
        b.market_order("SOL-USD", "buy", Decimal("5"), "c")
    with pytest.raises(OrderRejected):
        b.market_order("SOL-USD", "sell", Decimal("0.1"), "c")


def _payload(*decisions):
    return json.dumps({"market_view": "test", "decisions": list(decisions)})


def _dec(symbol, action, usd, conf=0.8):
    return {"symbol": symbol, "action": action, "usd_amount": usd, "confidence": conf,
            "stop_loss_pct": 5, "take_profit_pct": 10, "rationale": "test"}


def _setup(cfg):
    market = SyntheticMarketData()
    broker = PaperBroker(cfg.state_dir / "pb.json", 1000, 0.5, market.last_price)
    return market, broker, Ledger(cfg.state_dir / "ledger.json"), AuditLog(cfg.log_dir / "audit.jsonl")


def test_full_cycle_executes_risk_approved_trades_and_audits(cfg):
    market, broker, ledger, audit = _setup(cfg)
    brain = FakeBrain(_payload(_dec("BTC-USD", "buy", 5000), _dec("DOGE-USD", "buy", 500), _dec("ETH-USD", "hold", 0)))
    summary = run_cycle(cfg, broker, market, brain, ledger, audit, now=NOW)

    assert summary["status"] == "ok"
    assert [o["symbol"] for o in summary["orders"]] == ["BTC-USD"]
    assert summary["orders"][0]["usd"] <= 1000 * cfg.limits.max_order_pct / 100  # clipped to 25% of equity
    assert brain.contexts[0]["agent_equity_usd"] == 1000
    assert set(brain.contexts[0]["market"]) == set(cfg.allowed_symbols)
    events = [json.loads(line)["event"] for line in (cfg.log_dir / "audit.jsonl").read_text().splitlines()]
    assert events == ["brain_decision", "risk_verdicts", "order_submitted", "order_filled", "cycle_summary"]


def test_stop_file_blocks_all_trading(cfg):
    market, broker, ledger, audit = _setup(cfg)
    cfg.stop_file.write_text("")
    brain = FakeBrain(_payload(_dec("BTC-USD", "buy", 20)))
    assert run_cycle(cfg, broker, market, brain, ledger, audit, now=NOW)["status"] == "stopped"
    assert brain.contexts == [] and broker.holdings() == {}


def test_drawdown_halts_agent(cfg):
    market, broker, ledger, audit = _setup(cfg)
    ledger.state["starting_equity"] = 1000
    ledger.state["realized_total"] = -(1000 * cfg.limits.max_drawdown_pct / 100) - 1
    brain = FakeBrain(_payload(_dec("BTC-USD", "buy", 20)))
    summary = run_cycle(cfg, broker, market, brain, ledger, audit, now=NOW)
    assert summary["status"] == "halted" and summary["orders"] == [] and brain.contexts == []


def test_allowance_is_never_overspent_across_many_cycles(cfg):
    market, broker, ledger, audit = _setup(cfg)
    brain = FakeBrain(_payload(*[_dec(s, "buy", 10_000) for s in cfg.allowed_symbols]))
    for i in range(10):
        run_cycle(cfg, broker, market, brain, ledger, audit, now=NOW + timedelta(hours=2 * i))
        assert broker.buying_power() >= -1e-9
        quotes = {s: broker.quote(s) for s in cfg.allowed_symbols}
        assert ledger.exposure(quotes) <= 1000 + 1e-6


def test_rebaseline_restarts_drawdown_but_keeps_realized_history(cfg):
    market, broker, ledger, audit = _setup(cfg)
    quotes = {s: broker.quote(s) for s in cfg.allowed_symbols}
    ledger.state["realized_total"] = -250
    ledger.rebaseline(2000, quotes)
    assert ledger.state["starting_equity"] == 2000
    assert ledger.drawdown_pnl(quotes) == 0 and ledger.state["realized_total"] == -250
