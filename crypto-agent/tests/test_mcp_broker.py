"""MCP broker tests against an in-process fake Robinhood MCP server (FastMCP + in-memory transport)."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
import yaml
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from agent.audit import AuditLog
from agent.broker import OrderRejected, OrderStateUnknown
from agent.ledger import Ledger
from agent.main import run_cycle
from agent.market_data import SyntheticMarketData
from agent.mcp_broker import RobinhoodMCPBroker, dig, fill_template, mapping_problems
from conftest import NOW, ROOT, FakeBrain

PRICES = {"BTC-USD": 60000.0, "ETH-USD": 3000.0, "SOL-USD": 150.0}


class FakeRobinhood:
    def __init__(self, cash=5000.0, order_mode="fill"):
        self.cash = cash
        self.holdings = {"BTC": 0.5}  # pre-existing coins the agent must never sell
        self.orders = {}
        self.order_mode = order_mode
        self.calls = []
        self.server = self._build()

    def _build(self) -> FastMCP:
        mcp = FastMCP("fake-robinhood")

        @mcp.tool()
        def get_crypto_quote(symbol: str) -> dict:
            self.calls.append("get_crypto_quote")
            p = PRICES[symbol]
            return {"quote": {"bid_price": p * 0.999, "ask_price": p * 1.001}}

        @mcp.tool()
        def get_account() -> dict:
            self.calls.append("get_account")
            return {"buying_power": self.cash}

        @mcp.tool()
        def get_crypto_holdings() -> dict:
            self.calls.append("get_crypto_holdings")
            return {"results": [{"asset_code": k, "quantity_available": v} for k, v in self.holdings.items()]}

        @mcp.tool()
        async def place_crypto_order(symbol: str, side: str, type: str, quantity: str, client_order_id: str) -> dict:
            self.calls.append("place_crypto_order")
            if self.order_mode == "error":
                raise ValueError("insufficient buying power")
            if self.order_mode == "hang":
                await asyncio.sleep(5)
            qty, price = float(quantity), PRICES[symbol] * (1.001 if side == "buy" else 0.999)
            asset = symbol.split("-")[0]
            if side == "buy":
                self.cash -= qty * price
                self.holdings[asset] = self.holdings.get(asset, 0) + qty
            else:
                self.cash += qty * price
                self.holdings[asset] -= qty
            oid = f"ord-{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "state": "filled", "filled_asset_quantity": quantity,
                                "average_price": str(price)}
            return {"id": oid, "state": "pending" if self.order_mode == "pending_then_fill" else "filled"}

        @mcp.tool()
        def get_crypto_order(order_id: str) -> dict:
            self.calls.append("get_crypto_order")
            return self.orders[order_id]

        @mcp.tool()
        def withdraw_funds(amount: float) -> dict:
            self.calls.append("withdraw_funds")  # must never be reachable from the agent
            return {"ok": True}

        return mcp

    def factory(self):
        @asynccontextmanager
        async def _f():
            async with create_connected_server_and_client_session(self.server) as session:
                yield session
        return _f


def mcp_cfg():
    cfg = copy.deepcopy(yaml.safe_load((ROOT / "config.yaml").read_text())["mcp"])
    t = cfg["tools"]
    t["quote"].update(name="get_crypto_quote", bid="quote.bid_price", ask="quote.ask_price")
    t["buying_power"].update(name="get_account")
    t["holdings"].update(name="get_crypto_holdings", list="results", symbol="asset_code", qty="quantity_available")
    t["place_order"].update(name="place_crypto_order")
    t["get_order"].update(name="get_crypto_order", filled_qty="filled_asset_quantity")
    cfg["order_fill_timeout_seconds"] = 5
    return cfg


@pytest.fixture
def fake():
    return FakeRobinhood()


@pytest.fixture
def broker(fake):
    b = RobinhoodMCPBroker(mcp_cfg(), fake.factory(), call_timeout_s=1)
    yield b
    b.close()


def test_helpers():
    assert fill_template({"a": "{x}", "b": "lit", "c": ["{y}"]}, {"x": 1, "y": "z"}) == {"a": 1, "b": "lit", "c": ["z"]}
    assert dig({"a": [{"b": 2}]}, "a.0.b") == 2
    with pytest.raises(KeyError):
        dig({"a": 1}, "missing")


def test_shipped_config_is_unmapped_so_live_trading_is_refused():
    shipped = yaml.safe_load((ROOT / "config.yaml").read_text())["mcp"]["tools"]
    problems = mapping_problems(shipped)
    assert len(problems) == 5 and all("TBD" in p for p in problems)


def test_mapping_must_match_server_tools(broker):
    names = {t["name"] for t in broker.list_tools()}
    assert mapping_problems(mcp_cfg()["tools"], names) == []
    bad = mcp_cfg()["tools"]
    bad["quote"]["name"] = "no_such_tool"
    assert "not offered by the server" in mapping_problems(bad, names)[0]


def test_reads_parse_quotes_cash_and_holdings(broker):
    q = broker.quote("ETH-USD")
    assert q.bid == pytest.approx(2997) and q.ask == pytest.approx(3003)
    assert broker.buying_power() == 5000
    assert broker.holdings() == {"BTC-USD": 0.5}


def test_market_order_fills(broker, fake):
    fill = broker.market_order("SOL-USD", "buy", Decimal("2.5"), "coid-1")
    assert fill.quantity == 2.5 and fill.price == pytest.approx(150.15) and fill.order_id == "ord-1"
    assert fake.holdings["SOL"] == 2.5


def test_pending_order_is_polled_until_filled(fake):
    fake.order_mode = "pending_then_fill"
    b = RobinhoodMCPBroker(mcp_cfg(), fake.factory(), call_timeout_s=1)
    try:
        fill = b.market_order("SOL-USD", "buy", Decimal("1"), "coid-p")
        assert fill.quantity == 1 and "get_crypto_order" in fake.calls
    finally:
        b.close()


def test_server_error_is_a_clean_rejection(fake):
    fake.order_mode = "error"
    b = RobinhoodMCPBroker(mcp_cfg(), fake.factory(), call_timeout_s=1)
    try:
        with pytest.raises(OrderRejected, match="insufficient"):
            b.market_order("SOL-USD", "buy", Decimal("1"), "coid-e")
    finally:
        b.close()


def test_order_timeout_is_unknown_and_never_retried(fake):
    fake.order_mode = "hang"
    b = RobinhoodMCPBroker(mcp_cfg(), fake.factory(), call_timeout_s=1)
    try:
        with pytest.raises(OrderStateUnknown):
            b.market_order("SOL-USD", "buy", Decimal("1"), "coid-h")
        assert fake.calls.count("place_crypto_order") == 1
    finally:
        b.close()


def test_unmapped_operation_cannot_be_called(fake):
    cfg = mcp_cfg()
    cfg["tools"]["place_order"]["name"] = "TBD"
    b = RobinhoodMCPBroker(cfg, fake.factory(), call_timeout_s=1)
    try:
        with pytest.raises(OrderRejected, match="not mapped"):
            b.market_order("SOL-USD", "buy", Decimal("1"), "coid-x")
        assert "place_crypto_order" not in fake.calls
    finally:
        b.close()


def _payload(*decisions):
    return json.dumps({"market_view": "t", "decisions": [
        {"symbol": s, "action": a, "usd_amount": u, "confidence": 0.8, "stop_loss_pct": 5,
         "take_profit_pct": 10, "rationale": "t"} for s, a, u in decisions]})


def _live(cfg):
    return replace(cfg, live=True, mcp=mcp_cfg())


def test_full_live_cycle_through_mcp(cfg, broker, fake):
    cfg = _live(cfg)
    ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
    brain = FakeBrain(_payload(("ETH-USD", "buy", 2000), ("BTC-USD", "sell", 30000)))
    summary = run_cycle(cfg, broker, SyntheticMarketData(), brain, ledger, audit, now=NOW)

    assert summary["status"] == "ok"
    assert [(o["symbol"], o["side"], o["status"]) for o in summary["orders"]] == [("ETH-USD", "buy", "filled")]
    assert summary["orders"][0]["usd"] <= 5000 * cfg.limits.max_order_pct / 100
    assert fake.holdings["BTC"] == 0.5  # pre-existing BTC untouched: the agent never bought it
    assert ledger.qty("ETH-USD") > 0
    assert "withdraw_funds" not in fake.calls


def test_unknown_order_state_halts_the_agent(cfg, fake):
    fake.order_mode = "hang"
    b = RobinhoodMCPBroker(mcp_cfg(), fake.factory(), call_timeout_s=1)
    try:
        cfg = _live(cfg)
        ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
        brain = FakeBrain(_payload(("ETH-USD", "buy", 500), ("SOL-USD", "buy", 500)))
        summary = run_cycle(cfg, b, SyntheticMarketData(), brain, ledger, audit, now=NOW)
        assert summary["status"] == "halted"
        assert fake.calls.count("place_crypto_order") == 1  # stopped after the first unknown order
        assert "state unknown" in ledger.state["halt_reason"]
    finally:
        b.close()
