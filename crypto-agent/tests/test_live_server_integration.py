"""End-to-end: the agent drives the REAL robinhood-crypto-mcp server (Node) against a local API mock.

Skipped unless RH_MCP_SERVER_DIST points at the built server, e.g.
  RH_MCP_SERVER_DIST=/path/to/carrieannemoore-gif-robinhood-crypto-mcp/dist/index.js pytest
"""

import base64
import copy
import json
import os
import shutil
from dataclasses import replace
from decimal import Decimal

import pytest
import yaml
from nacl.signing import SigningKey

from agent.audit import AuditLog
from agent.broker import OrderRejected, OrderStateUnknown
from agent.ledger import Ledger
from agent.main import run_cycle
from agent.market_data import SyntheticMarketData
from agent.mcp_broker import RobinhoodMCPBroker, mapping_problems, stdio_session_factory
from conftest import NOW, ROOT, FakeBrain
from robinhood_api_mock import MockRobinhood

DIST = os.environ.get("RH_MCP_SERVER_DIST", "")
pytestmark = pytest.mark.skipif(not (DIST and os.path.exists(DIST) and shutil.which("node")),
                                reason="set RH_MCP_SERVER_DIST to the built robinhood-crypto-mcp dist/index.js")


@pytest.fixture
def env():
    key = SigningKey.generate()
    mock = MockRobinhood("rh-api-test", key.verify_key)
    secrets = {"ROBINHOOD_API_KEY": "rh-api-test",
               "ROBINHOOD_PRIVATE_KEY_BASE64": base64.b64encode(bytes(key)).decode(),
               "RH_MAX_ORDER_NOTIONAL_USD": "300"}
    yield mock, secrets
    mock.close()


def _broker(tmp_path, mock, secrets, trading=True):
    cfg = copy.deepcopy(yaml.safe_load((ROOT / "config.yaml").read_text())["mcp"])
    cfg.update(command="node", args=[DIST], extra_env={"RH_BASE_URL": mock.url})
    cfg["tools"]["place_order"]["reconcile"]["wait_seconds"] = 0.2
    factory = stdio_session_factory(cfg, secrets, ("BTC-USD", "ETH-USD", "SOL-USD"), tmp_path / "logs", trading)
    return RobinhoodMCPBroker(cfg, factory, call_timeout_s=20), cfg


def test_real_server_schemas_match_mapping_and_reads_parse(tmp_path, env):
    mock, secrets = env
    b, cfg = _broker(tmp_path, mock, secrets)
    try:
        assert mapping_problems(cfg["tools"], b.list_tools()) == []
        q = b.quote("ETH-USD")
        assert (q.bid, q.ask) == (pytest.approx(2997), pytest.approx(3003))
        assert b.buying_power() == 50_000
        assert b.holdings() == {"BTC-USD": 0.5, "DOGE-USD": 1000.0}  # two pages
        assert b.pair_info("ETH-USD").asset_increment == Decimal("0.000001")
    finally:
        b.close()
    assert mock.bad_signatures == 0  # the server signed every request correctly


def test_real_server_places_confirmed_orders_and_enforces_its_own_limits(tmp_path, env):
    mock, secrets = env
    b, _ = _broker(tmp_path, mock, secrets)
    try:
        fill = b.market_order("ETH-USD", "buy", Decimal("0.05"), "22222222-2222-4222-8222-222222222222")
        assert fill.quantity == pytest.approx(0.05) and fill.price == pytest.approx(3003)
        assert mock.posts[-1]["market_order_config"] == {"asset_quantity": "0.05"}
        assert mock.posts[-1]["client_order_id"] == "22222222-2222-4222-8222-222222222222"

        with pytest.raises(OrderRejected, match="RH_MAX_ORDER_NOTIONAL_USD"):  # $600 > server cap $300
            b.market_order("ETH-USD", "buy", Decimal("0.2"), "33333333-3333-4333-8333-333333333333")
        with pytest.raises(OrderRejected, match="RH_ALLOWED_SYMBOLS"):  # agent set the allowlist
            b.market_order("DOGE-USD", "sell", Decimal("10"), "44444444-4444-4444-8444-444444444444")
        assert len(mock.posts) == 1  # neither refused order reached the API

        mock.fail_next_post_after_recording = True  # order goes through, reply is a 503
        fill = b.market_order("SOL-USD", "buy", Decimal("1"), "55555555-5555-4555-8555-555555555555")
        assert fill.quantity == 1 and len(mock.posts) == 2  # found by client_order_id, not re-sent
    finally:
        b.close()
    audit = (tmp_path / "logs" / "mcp_server_audit.jsonl").read_text().splitlines()
    assert any(json.loads(line).get("phase") == "submit" for line in audit)


def test_ambiguous_server_error_halts_even_if_nothing_was_sent(tmp_path, env):
    """The server fetches a price estimate before sending; a 5xx there looks identical to a failed send.
    The agent can't tell them apart, so it looks the order up, doesn't find it, and halts (safe side)."""
    mock, secrets = env
    mock.fail_estimates = True
    b, _ = _broker(tmp_path, mock, secrets)
    try:
        with pytest.raises(OrderStateUnknown):
            b.market_order("ETH-USD", "buy", Decimal("0.01"), "66666666-6666-4666-8666-666666666666")
    finally:
        b.close()
    assert mock.posts == []


def test_trading_tools_absent_unless_live(tmp_path, env):
    mock, secrets = env
    b, _ = _broker(tmp_path, mock, secrets, trading=False)
    try:
        names = {t["name"] for t in b.list_tools()}
    finally:
        b.close()
    assert "place_order" not in names and "cancel_order" not in names


def test_full_agent_cycle_through_real_server(tmp_path, env, cfg):
    mock, secrets = env
    b, mcp_cfg = _broker(tmp_path, mock, secrets)
    cfg = replace(cfg, live=True, mcp=mcp_cfg, limits=replace(cfg.limits, allowance_usd=1000))
    ledger, audit = Ledger(cfg.state_dir / "l.json"), AuditLog(cfg.log_dir / "audit.jsonl")
    brain = FakeBrain(json.dumps({"market_view": "t", "decisions": [
        {"symbol": "ETH-USD", "action": "buy", "usd_amount": 5000, "confidence": 0.8, "stop_loss_pct": 5,
         "take_profit_pct": 10, "rationale": "integration"}]}))
    try:
        summary = run_cycle(cfg, b, SyntheticMarketData(), brain, ledger, audit, now=NOW)
    finally:
        b.close()
    assert summary["status"] == "ok"
    assert [(o["symbol"], o["status"]) for o in summary["orders"]] == [("ETH-USD", "filled")]
    assert summary["orders"][0]["usd"] <= 250.01  # 25% of the $1,000 allowance, under the $300 server cap
    assert mock.holdings["BTC"] == 0.5 and mock.bad_signatures == 0
