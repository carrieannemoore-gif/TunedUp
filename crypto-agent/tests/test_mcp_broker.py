"""MCP broker tests against an in-process fake of robinhood-crypto-mcp (FastMCP + in-memory transport).

The fake mirrors the real server: same tool names and parameters, Robinhood API JSON returned as text,
confirm=false meaning preview-only, and errors returned as isError text. The broker is driven by the
mapping shipped in config.yaml, so these tests also prove that mapping works.
"""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Optional

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
REAL_TOOLS = json.loads((ROOT / "tests/fixtures/robinhood_crypto_mcp_tools.json").read_text())


class FakeRobinhoodCryptoMCP:
    """Behaves like carrieannemoore-gif-robinhood-crypto-mcp in front of the Robinhood API."""

    def __init__(self, cash=50_000.0, order_mode="fill"):
        self.cash = cash
        self.holdings = {"BTC": 0.5, "DOGE": 1000.0}  # pre-existing coins the agent must never sell
        self.orders: dict[str, dict] = {}
        self.order_mode = order_mode
        self.calls: list[str] = []
        self.server = self._build()

    def _fill(self, symbol, side, quantity, client_order_id, state="filled"):
        qty = float(quantity)
        price = PRICES[symbol] * (1.001 if side == "buy" else 0.999)
        asset = symbol.split("-")[0]
        self.cash += -qty * price if side == "buy" else qty * price
        self.holdings[asset] = self.holdings.get(asset, 0) + (qty if side == "buy" else -qty)
        oid = f"ord-{len(self.orders) + 1}"
        self.orders[oid] = {"id": oid, "client_order_id": client_order_id, "symbol": symbol, "side": side,
                            "type": "market", "state": state, "filled_asset_quantity": quantity,
                            "average_price": str(price), "created_at": "2026-10-01T12:00:00Z"}
        return self.orders[oid]

    def _build(self) -> FastMCP:
        mcp = FastMCP("robinhood-crypto")
        me = self

        @mcp.tool(structured_output=False)
        def get_account() -> str:
            me.calls.append("get_account")
            return json.dumps({"account_number": "X1", "status": "active", "buying_power": f"{me.cash:.2f}",
                               "buying_power_currency": "USD"})

        @mcp.tool(structured_output=False)
        def get_best_bid_ask(symbols: Optional[list[str]] = None) -> str:
            me.calls.append("get_best_bid_ask")
            return json.dumps({"results": [{"symbol": s, "price": str(PRICES[s]),
                                            "bid_inclusive_of_sell_spread": str(PRICES[s] * 0.999),
                                            "ask_inclusive_of_buy_spread": str(PRICES[s] * 1.001)}
                                           for s in symbols or []]})

        @mcp.tool(structured_output=False)
        def get_holdings(asset_codes: Optional[list[str]] = None, cursor: Optional[str] = None,
                         limit: Optional[int] = None) -> str:
            me.calls.append("get_holdings")
            rows = [{"account_number": "X1", "asset_code": k, "total_quantity": str(v),
                     "quantity_available_for_trading": str(v)} for k, v in sorted(me.holdings.items()) if v > 0]
            if cursor is None:  # two pages, like Robinhood's cursor pagination
                return json.dumps({"next": "https://trading.robinhood.com/api/v1/crypto/trading/holdings/?cursor=p2",
                                   "previous": None, "results": rows[:1]})
            assert cursor == "p2"
            return json.dumps({"next": None, "previous": None, "results": rows[1:]})

        @mcp.tool(structured_output=False)
        def get_trading_pairs(symbols: Optional[list[str]] = None) -> str:
            me.calls.append("get_trading_pairs")
            return json.dumps({"results": [{"symbol": s, "asset_increment": "0.000001",
                                            "min_order_size": "0.00001"} for s in symbols or []]})

        @mcp.tool(structured_output=False)
        def list_orders(id: Optional[str] = None, symbol: Optional[str] = None, side: Optional[str] = None,
                        created_at_start: Optional[str] = None) -> str:
            me.calls.append("list_orders")
            rows = [o for o in me.orders.values()
                    if (id is None or o["id"] == id) and (symbol is None or o["symbol"] == symbol)
                    and (side is None or o["side"] == side)]
            return json.dumps({"next": None, "results": rows})

        @mcp.tool(structured_output=False)
        async def place_order(symbol: str, side: str, type: str, asset_quantity: Optional[str] = None,
                              client_order_id: Optional[str] = None, confirm: bool = False) -> str:
            me.calls.append("place_order")
            mode = me.order_mode
            if mode == "cap":
                raise ValueError("Estimated notional $9000.00 exceeds RH_MAX_ORDER_NOTIONAL_USD ($50.00)")
            if mode == "http400":
                raise ValueError('Robinhood API returned HTTP 400: {"detail":"insufficient buying power"}')
            if mode == "sent_then_503":  # Robinhood took the order, but the reply was lost
                me._fill(symbol, side, asset_quantity, client_order_id)
                raise ValueError("Robinhood API returned HTTP 503: upstream timeout")
            if mode == "timeout_not_sent":
                raise ValueError("The operation was aborted due to timeout")
            if mode == "prefixed_not_sent":  # newer server: estimate failed before the order request
                raise ValueError("Order NOT sent: Robinhood API returned HTTP 503: {}")
            if mode == "prefixed_unknown":  # an explicit unknown marker wins over any other pattern
                raise ValueError("Order outcome UNKNOWN, it may have been executed (client_order_id x; check "
                                 "list_orders): Robinhood API returned HTTP 400 is not in RH_ALLOWED_SYMBOLS")
            if mode == "hang":
                await asyncio.sleep(5)
            if not confirm:
                return json.dumps({"status": "preview_only_not_sent", "order": {"symbol": symbol}})
            order = me._fill(symbol, side, asset_quantity, client_order_id)
            return json.dumps({k: order[k] for k in ("id", "client_order_id", "symbol", "side", "type")}
                              | {"state": "open"})

        @mcp.tool(structured_output=False)
        def cancel_order(id: str) -> str:
            me.calls.append("cancel_order")  # must never be reachable from the agent
            return json.dumps({})

        return mcp

    def factory(self):
        @asynccontextmanager
        async def _f():
            async with create_connected_server_and_client_session(self.server) as session:
                yield session
        return _f


def shipped_mcp_cfg():
    cfg = copy.deepcopy(yaml.safe_load((ROOT / "config.yaml").read_text())["mcp"])
    cfg["order_fill_timeout_seconds"] = 5
    cfg["tools"]["place_order"]["reconcile"]["wait_seconds"] = 0.05
    return cfg


def make_broker(fake, cfg=None):
    return RobinhoodMCPBroker(cfg or shipped_mcp_cfg(), fake.factory(), call_timeout_s=1)


@pytest.fixture
def fake():
    return FakeRobinhoodCryptoMCP()


@pytest.fixture
def broker(fake):
    b = make_broker(fake)
    yield b
    b.close()


def test_helpers():
    assert fill_template({"a": "{x}", "b": "lit", "c": ["{y}"], "d": True}, {"x": 1, "y": "z"}) == \
        {"a": 1, "b": "lit", "c": ["z"], "d": True}
    assert dig({"a": [{"b": 2}]}, "a.0.b") == 2
    with pytest.raises(KeyError):
        dig({"a": 1}, "missing")


def test_shipped_mapping_matches_the_real_server_schemas():
    assert mapping_problems(shipped_mcp_cfg()["tools"], REAL_TOOLS) == []


def test_mapping_without_confirm_or_with_unknown_args_is_refused():
    tools = shipped_mcp_cfg()["tools"]
    del tools["place_order"]["args"]["confirm"]
    tools["quote"]["args"]["sym"] = "x"
    problems = mapping_problems(tools, REAL_TOOLS)
    assert any("confirm: true" in p for p in problems)
    assert any("args.sym is not a parameter" in p for p in problems)


def test_mapping_to_a_tool_the_server_lacks_is_refused():
    tools = shipped_mcp_cfg()["tools"]
    tools["get_order"]["name"] = "get_order"
    assert "not offered by the server" in mapping_problems(tools, REAL_TOOLS)[0]


def test_reads_parse_quotes_cash_pairs_and_paginated_holdings(broker, fake):
    q = broker.quote("ETH-USD")
    assert q.bid == pytest.approx(2997) and q.ask == pytest.approx(3003)
    assert broker.buying_power() == 50_000
    assert broker.holdings() == {"BTC-USD": 0.5, "DOGE-USD": 1000.0}
    assert fake.calls.count("get_holdings") == 2  # followed the `next` cursor
    pair = broker.pair_info("SOL-USD")
    assert pair.asset_increment == Decimal("0.000001") and pair.min_order_size == Decimal("0.00001")


def test_market_order_confirms_and_fills(broker, fake):
    fill = broker.market_order("SOL-USD", "buy", Decimal("2.5"), "11111111-1111-4111-8111-111111111111")
    assert fill.quantity == 2.5 and fill.price == pytest.approx(150.15) and fill.order_id == "ord-1"
    assert fake.orders["ord-1"]["client_order_id"] == "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("mode", ["cap", "http400", "prefixed_not_sent"])
def test_errors_that_prove_nothing_was_sent_are_clean_rejections(fake, mode):
    fake.order_mode = mode
    b = make_broker(fake)
    try:
        with pytest.raises(OrderRejected):
            b.market_order("SOL-USD", "buy", Decimal("1"), "c-1")
        assert "list_orders" not in fake.calls and fake.calls.count("place_order") == 1
    finally:
        b.close()


def test_order_sent_but_reply_lost_is_found_by_client_order_id(fake):
    fake.order_mode = "sent_then_503"
    b = make_broker(fake)
    try:
        fill = b.market_order("SOL-USD", "buy", Decimal("1"), "c-503")
        assert fill.order_id == "ord-1" and fill.quantity == 1
        assert fake.calls.count("place_order") == 1  # looked up, never re-sent
    finally:
        b.close()


def test_ambiguous_failure_with_no_order_found_halts(fake):
    fake.order_mode = "timeout_not_sent"
    b = make_broker(fake)
    try:
        with pytest.raises(OrderStateUnknown, match="not found by client_order_id"):
            b.market_order("SOL-USD", "buy", Decimal("1"), "c-t")
        assert fake.calls.count("place_order") == 1 and fake.calls.count("list_orders") == 3
    finally:
        b.close()


def test_explicit_unknown_marker_always_triggers_a_lookup(fake):
    fake.order_mode = "prefixed_unknown"
    b = make_broker(fake)
    try:
        with pytest.raises(OrderStateUnknown):
            b.market_order("SOL-USD", "buy", Decimal("1"), "c-u")
        assert fake.calls.count("list_orders") == 3 and fake.calls.count("place_order") == 1
    finally:
        b.close()


def test_transport_timeout_is_reconciled_not_retried(fake):
    fake.order_mode = "hang"
    b = make_broker(fake)
    try:
        with pytest.raises(OrderStateUnknown):
            b.market_order("SOL-USD", "buy", Decimal("1"), "c-h")
        assert fake.calls.count("place_order") == 1
    finally:
        b.close()


def test_preview_only_response_never_counts_as_a_fill(fake):
    cfg = shipped_mcp_cfg()
    del cfg["tools"]["place_order"]["args"]["confirm"]
    b = make_broker(fake, cfg)
    try:
        with pytest.raises(OrderStateUnknown, match="preview"):
            b.market_order("SOL-USD", "buy", Decimal("1"), "c-p")
        assert fake.orders == {}
    finally:
        b.close()


def _payload(*decisions):
    return json.dumps({"market_view": "t", "decisions": [
        {"symbol": s, "action": a, "usd_amount": u, "confidence": 0.8, "stop_loss_pct": 5,
         "take_profit_pct": 10, "rationale": "t"} for s, a, u in decisions]})


def _live(cfg, allowance=1000.0):
    return replace(cfg, live=True, mcp=shipped_mcp_cfg(), limits=replace(cfg.limits, allowance_usd=allowance))


def test_full_live_cycle_spends_the_allowance_not_the_account(cfg, broker, fake):
    cfg = _live(cfg, allowance=1000)
    ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
    brain = FakeBrain(_payload(("ETH-USD", "buy", 20_000), ("BTC-USD", "sell", 30_000)))
    summary = run_cycle(cfg, broker, SyntheticMarketData(), brain, ledger, audit, now=NOW)

    assert summary["status"] == "ok"
    assert [(o["symbol"], o["side"], o["status"]) for o in summary["orders"]] == [("ETH-USD", "buy", "filled")]
    assert summary["orders"][0]["usd"] <= 1000 * cfg.limits.max_order_pct / 100 + 0.01  # $250, not $12,500
    assert brain.contexts[0]["cash_available_usd"] == 1000  # the account has $50,000
    assert fake.holdings["BTC"] == 0.5  # pre-existing BTC untouched: the agent never bought it
    assert "cancel_order" not in fake.calls


def test_allowance_is_never_exceeded_across_many_cycles(cfg, broker, fake):
    from datetime import timedelta
    cfg = _live(cfg, allowance=1000)
    ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
    brain = FakeBrain(_payload(*[(s, "buy", 10_000) for s in cfg.allowed_symbols]))
    for i in range(10):
        run_cycle(cfg, broker, SyntheticMarketData(), brain, ledger, audit, now=NOW + timedelta(hours=2 * i))
        assert ledger.cost_basis() <= 1000 + 0.01
    assert fake.cash >= 50_000 - 1000 - 0.01


def test_unknown_order_state_halts_the_agent(cfg, fake):
    fake.order_mode = "timeout_not_sent"
    b = make_broker(fake)
    try:
        cfg = _live(cfg)
        ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
        brain = FakeBrain(_payload(("ETH-USD", "buy", 500), ("SOL-USD", "buy", 500)))
        summary = run_cycle(cfg, b, SyntheticMarketData(), brain, ledger, audit, now=NOW)
        assert summary["status"] == "halted"
        assert fake.calls.count("place_order") == 1  # stopped after the first unknown order
        assert "state unknown" in ledger.state["halt_reason"]
    finally:
        b.close()
