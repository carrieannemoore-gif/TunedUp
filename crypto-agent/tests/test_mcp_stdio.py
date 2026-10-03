"""The stdio launcher: a real child process, with a controlled environment."""

import os
import sys

from agent.mcp_broker import RobinhoodMCPBroker, stdio_session_factory
from conftest import ROOT


def _broker(tmp_path, trading, extra_env=None):
    cfg = {"command": sys.executable, "args": [str(ROOT / "tests/stdio_echo_server.py")],
           "extra_env": extra_env or {},
           "tools": {"buying_power": {"name": "get_account", "args": {}, "value": "buying_power"}}}
    secrets = {"ROBINHOOD_API_KEY": "rh-api-test", "ROBINHOOD_PRIVATE_KEY_BASE64": "c2VjcmV0",
               "RH_MAX_ORDER_NOTIONAL_USD": "300"}
    factory = stdio_session_factory(cfg, secrets, ("BTC-USD", "ETH-USD"), tmp_path / "logs", trading)
    return RobinhoodMCPBroker(cfg, factory, call_timeout_s=20)


def test_child_gets_keys_and_agent_controlled_switches_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    b = _broker(tmp_path, trading=True, extra_env={"RH_TIMEOUT_MS": "5000", "RH_ENABLE_TRADING": "false"})
    try:
        assert b.buying_power() == 123.45
        env = b._call("buying_power")["env"]
    finally:
        b.close()
    assert env["ROBINHOOD_API_KEY"] == "rh-api-test" and env["RH_MAX_ORDER_NOTIONAL_USD"] == "300"
    assert env["RH_ENABLE_TRADING"] == "true"  # config extra_env cannot override the agent's switch
    assert env["RH_ALLOWED_SYMBOLS"] == "BTC-USD,ETH-USD"
    assert env["RH_AUDIT_LOG"] == str(tmp_path / "logs" / "mcp_server_audit.jsonl")
    assert env["RH_TIMEOUT_MS"] == "5000"
    assert env["ANTHROPIC_API_KEY"] is None  # unrelated secrets never reach the server


def test_trading_disabled_unless_live(tmp_path):
    b = _broker(tmp_path, trading=False)
    try:
        assert b._call("buying_power")["env"]["RH_ENABLE_TRADING"] == "false"
    finally:
        b.close()
    assert os.path.exists(tmp_path / "logs" / "mcp_server_stderr.log")
