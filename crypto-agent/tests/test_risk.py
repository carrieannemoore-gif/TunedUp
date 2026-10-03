from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from agent.broker import Fill, PairInfo, Quote
from agent.config import ConfigError, load_config
from agent.ledger import Ledger
from agent.models import Decision
from agent.risk import RiskContext, evaluate, protective_exits
from conftest import NOW, ROOT

PAIRS = {s: PairInfo(s, Decimal("0.00000001"), Decimal("0.00000001")) for s in ("BTC-USD", "ETH-USD", "SOL-USD")}
QUOTES = {"BTC-USD": Quote("BTC-USD", 59900, 60100), "ETH-USD": Quote("ETH-USD", 2990, 3010),
          "SOL-USD": Quote("SOL-USD", 149, 151)}


def d(symbol="BTC-USD", action="buy", usd=20.0, conf=0.8, sl=5.0, tp=10.0):
    return Decision(symbol=symbol, action=action, usd_amount=usd, confidence=conf,
                    stop_loss_pct=sl, take_profit_pct=tp, rationale="test")


ALLOWANCE = 5000.0


def ctx(cfg, ledger, cash=ALLOWANCE, quotes=QUOTES):
    ledger.roll_day(NOW, quotes, cash + ledger.exposure(quotes))
    return RiskContext(cfg.limits, cfg.allowed_symbols, quotes, PAIRS, ledger, cash, NOW)


def test_fault_injection_500_dollar_doge_is_rejected(cfg, tmp_path):
    c = ctx(cfg, Ledger(tmp_path / "l.json"))
    assert evaluate([d("DOGE-USD", usd=500)], c) == []
    assert c.verdicts[0]["reason"] == "symbol not in allowed_symbols"


def test_no_100_dollar_cap_orders_scale_with_allowance(cfg, tmp_path):
    assert cfg.limits.max_total_exposure_usd is None
    intents = evaluate([d(usd=1000)], ctx(cfg, Ledger(tmp_path / "l.json")))
    assert intents[0].est_usd == pytest.approx(1000, abs=0.01)  # 20% of a $5,000 allowance: allowed


def test_oversized_buy_is_clipped_to_max_order_pct(cfg, tmp_path):
    intents = evaluate([d(usd=4000)], ctx(cfg, Ledger(tmp_path / "l.json")))
    cap = ALLOWANCE * cfg.limits.max_order_pct / 100  # $1,250
    assert intents[0].est_usd <= cap and intents[0].est_usd == pytest.approx(cap, abs=0.01)


def test_buys_never_exceed_the_allowance(cfg, tmp_path):
    many = [d(s, usd=1e6) for s in ("BTC-USD", "ETH-USD", "SOL-USD")]
    intents = evaluate(many, ctx(cfg, Ledger(tmp_path / "l.json"), cash=1500))
    assert sum(i.est_usd for i in intents) <= 1500 + 1e-6


def test_optional_dollar_exposure_cap_still_works(cfg, tmp_path):
    lim = replace(cfg.limits, max_total_exposure_usd=300)
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("ETH-USD", "buy", 0.09, 3000, "o", "c"), NOW - timedelta(days=1))  # ~$269 at bid
    c = ctx(cfg, ledger)
    c.limits = lim
    intents = evaluate([d("BTC-USD", usd=500), d("SOL-USD", usd=500)], c)
    assert ledger.exposure(QUOTES) + sum(i.est_usd for i in intents) <= 300 + 1e-6


def test_low_confidence_rejected(cfg, tmp_path):
    assert evaluate([d(conf=0.3)], ctx(cfg, Ledger(tmp_path / "l.json"))) == []


def test_cannot_sell_what_agent_does_not_hold(cfg, tmp_path):
    c = ctx(cfg, Ledger(tmp_path / "l.json"))
    assert evaluate([d(action="sell")], c) == []
    assert "pre-existing" in c.verdicts[0]["reason"]


def test_sell_never_exceeds_agent_position(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("BTC-USD", "buy", 0.0002, 60000, "o", "c"), NOW - timedelta(days=1))
    intents = evaluate([d(action="sell", usd=10_000)], ctx(cfg, ledger))
    assert float(intents[0].asset_quantity) == pytest.approx(0.0002)


def test_daily_loss_blocks_buys_but_not_sells(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("ETH-USD", "buy", 0.01, 3000, "o", "c"), NOW - timedelta(days=1))
    ledger.roll_day(NOW, QUOTES, ALLOWANCE)
    ledger.state["realized_today"] = -(ALLOWANCE * cfg.limits.max_daily_loss_pct / 100) - 1
    intents = evaluate([d("BTC-USD"), d("ETH-USD", action="sell", usd=100)], ctx(cfg, ledger))
    assert [i.side for i in intents] == ["sell"]


def test_halt_blocks_everything(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.halt("test")
    assert evaluate([d()], ctx(cfg, ledger)) == []


def test_max_trades_and_cooldown(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("BTC-USD", "buy", 0.0001, 60000, "o", "c"), NOW - timedelta(minutes=10))
    c = ctx(cfg, ledger)
    assert evaluate([d("BTC-USD")], c) == []
    assert c.verdicts[0]["reason"] == "symbol cooldown active"
    ledger.state["trades_today"] = cfg.limits.max_trades_per_day
    assert evaluate([d("SOL-USD")], ctx(cfg, ledger)) == []


def test_stop_loss_and_take_profit_are_clamped(cfg, tmp_path):
    intents = evaluate([d(sl=90, tp=0.1)], ctx(cfg, Ledger(tmp_path / "l.json")))
    assert intents[0].stop_loss_pct == cfg.limits.max_stop_loss_pct
    assert intents[0].take_profit_pct == cfg.limits.min_take_profit_pct


def test_protective_stop_loss_fires(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("BTC-USD", "buy", 0.0003, 70000, "o", "c"), NOW, stop_loss_pct=5)
    exits = protective_exits(ctx(cfg, ledger))  # bid 59900 is ~-14%
    assert len(exits) == 1 and exits[0].source == "stop_loss" and exits[0].side == "sell"


def test_protective_take_profit_fires(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.apply_fill(Fill("SOL-USD", "buy", 0.1, 100, "o", "c"), NOW, take_profit_pct=10)
    exits = protective_exits(ctx(cfg, ledger))
    assert exits[0].source == "take_profit"


def test_daily_loss_threshold_scales_with_equity(cfg, tmp_path):
    ledger = Ledger(tmp_path / "l.json")
    ledger.roll_day(NOW, QUOTES, ALLOWANCE)
    ledger.state["realized_today"] = -100  # 2% of $5,000: under the 10% limit, buys still allowed
    assert evaluate([d()], ctx(cfg, ledger))


def test_config_rejects_percentages_over_100(tmp_path):
    text = (ROOT / "config.yaml").read_text().replace("max_order_pct: 25", "max_order_pct: 250")
    (tmp_path / "config.yaml").write_text(text)
    with pytest.raises(ConfigError, match="percentage"):
        load_config(env={"TRADING_MODE": "paper"}, root=tmp_path)


def test_live_mode_requires_explicit_risk_acknowledgement(tmp_path):
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text())
    env = {"TRADING_MODE": "live"}  # MCP broker: no API keys needed, OAuth tokens instead
    with pytest.raises(ConfigError, match="I_ACCEPT_LIVE_TRADING_RISK"):
        load_config(env=env, root=tmp_path)
    assert load_config(env={**env, "I_ACCEPT_LIVE_TRADING_RISK": "yes"}, root=tmp_path).live
