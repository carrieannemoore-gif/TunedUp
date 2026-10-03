"""Loads config.yaml + .env and validates every limit before the agent may start."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
LIVE_RISK_ACK = "yes"
LIVE_BROKERS = ("mcp", "rest")
MCP_TRANSPORTS = ("stdio", "http")
# Copied from .env into the stdio MCP server's environment (and nowhere else).
MCP_SERVER_SECRET_ENV = ("ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY_BASE64", "RH_MAX_ORDER_NOTIONAL_USD")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_total_exposure_usd: float | None  # None = bounded only by the allowance (buying power)
    max_order_pct: float  # percentages are of agent equity (allowance cash + agent positions)
    min_order_usd: float
    max_daily_loss_pct: float
    max_drawdown_pct: float
    max_trades_per_day: int
    cooldown_minutes_per_symbol: int
    min_confidence: float
    default_stop_loss_pct: float
    default_take_profit_pct: float
    min_stop_loss_pct: float
    max_stop_loss_pct: float
    min_take_profit_pct: float
    max_take_profit_pct: float
    # The agent's own budget. Everything it may spend is measured against this, never against
    # the whole account's buying power. Required for live trading.
    allowance_usd: float | None = None


@dataclass(frozen=True)
class Config:
    limits: Limits
    allowed_symbols: tuple[str, ...]
    cycle_minutes: int
    candle_granularity_seconds: int
    candle_count: int
    model: str
    effort: str
    news_search: bool
    max_searches_per_cycle: int
    paper_starting_cash_usd: float
    paper_slippage_pct: float
    live: bool
    live_broker: str = "mcp"
    mcp: dict = field(default_factory=dict)
    mcp_server_env: dict = field(repr=False, default_factory=dict)
    rh_api_key: str = field(repr=False, default="")
    rh_private_key_b64: str = field(repr=False, default="")
    root: Path = ROOT

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"


def _validate_limits(lim: Limits) -> None:
    positive = [
        "max_order_pct", "min_order_usd", "max_daily_loss_pct", "max_drawdown_pct",
        "max_trades_per_day", "min_stop_loss_pct", "min_take_profit_pct",
    ]
    for name in positive:
        if getattr(lim, name) <= 0:
            raise ConfigError(f"limits.{name} must be > 0")
    for name in ("max_order_pct", "max_daily_loss_pct", "max_drawdown_pct"):
        if getattr(lim, name) > 100:
            raise ConfigError(f"limits.{name} is a percentage and cannot exceed 100")
    if lim.max_total_exposure_usd is not None and lim.max_total_exposure_usd <= 0:
        raise ConfigError("limits.max_total_exposure_usd must be > 0 or null")
    if lim.allowance_usd is not None and lim.allowance_usd <= 0:
        raise ConfigError("limits.allowance_usd must be > 0 or null")
    if not 0 < lim.min_confidence <= 1:
        raise ConfigError("limits.min_confidence must be in (0, 1]")
    if lim.cooldown_minutes_per_symbol < 0:
        raise ConfigError("limits.cooldown_minutes_per_symbol must be >= 0")
    if not lim.min_stop_loss_pct <= lim.default_stop_loss_pct <= lim.max_stop_loss_pct < 100:
        raise ConfigError("stop-loss settings must satisfy min <= default <= max < 100")
    if not lim.min_take_profit_pct <= lim.default_take_profit_pct <= lim.max_take_profit_pct:
        raise ConfigError("take-profit settings must satisfy min <= default <= max")


def mcp_stdio_problems(mcp: dict, server_env: dict, need_trading: bool) -> list[str]:
    """What is missing before the agent can start the stdio MCP server."""
    problems = []
    args = mcp.get("args") or []
    if not mcp.get("command") or not args or any("/ABSOLUTE/PATH" in str(a) for a in args):
        problems.append("set mcp.command and mcp.args to start your server (e.g. node /abs/path/dist/index.js)")
    for key in ("ROBINHOOD_API_KEY", "ROBINHOOD_PRIVATE_KEY_BASE64"):
        if key not in server_env:
            problems.append(f"{key} missing from .env")
    if need_trading:
        try:
            if float(server_env.get("RH_MAX_ORDER_NOTIONAL_USD", "0")) <= 0:
                raise ValueError
        except ValueError:
            problems.append("RH_MAX_ORDER_NOTIONAL_USD missing from .env (the server's own per-order cap)")
    return problems


def load_config(config_path: Path | None = None, env: dict[str, str] | None = None, root: Path = ROOT) -> Config:
    if env is None:
        load_dotenv(root / ".env")
        env = dict(os.environ)
    raw = yaml.safe_load((config_path or root / "config.yaml").read_text())

    lim_raw = raw["limits"]
    cap = lim_raw.get("max_total_exposure_usd")
    allowance = lim_raw.get("allowance_usd")
    lim = Limits(
        max_total_exposure_usd=None if cap is None else float(cap),
        max_order_pct=float(lim_raw["max_order_pct"]),
        min_order_usd=float(lim_raw["min_order_usd"]),
        max_daily_loss_pct=float(lim_raw["max_daily_loss_pct"]),
        max_drawdown_pct=float(lim_raw["max_drawdown_pct"]),
        max_trades_per_day=int(lim_raw["max_trades_per_day"]),
        cooldown_minutes_per_symbol=int(lim_raw["cooldown_minutes_per_symbol"]),
        min_confidence=float(lim_raw["min_confidence"]),
        default_stop_loss_pct=float(lim_raw["default_stop_loss_pct"]),
        default_take_profit_pct=float(lim_raw["default_take_profit_pct"]),
        min_stop_loss_pct=float(lim_raw["min_stop_loss_pct"]),
        max_stop_loss_pct=float(lim_raw["max_stop_loss_pct"]),
        min_take_profit_pct=float(lim_raw["min_take_profit_pct"]),
        max_take_profit_pct=float(lim_raw["max_take_profit_pct"]),
        allowance_usd=None if allowance is None else float(allowance),
    )
    _validate_limits(lim)

    symbols = tuple(s.upper() for s in raw["allowed_symbols"])
    if not symbols or any(not s.endswith("-USD") for s in symbols):
        raise ConfigError("allowed_symbols must be a non-empty list of *-USD pairs")

    mode = env.get("TRADING_MODE", "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ConfigError("TRADING_MODE must be 'paper' or 'live'")
    live = mode == "live"
    if live and env.get("I_ACCEPT_LIVE_TRADING_RISK", "").strip().lower() != LIVE_RISK_ACK:
        raise ConfigError("TRADING_MODE=live also requires I_ACCEPT_LIVE_TRADING_RISK=yes")
    live_broker = str(raw.get("live_broker", "mcp")).lower()
    if live_broker not in LIVE_BROKERS:
        raise ConfigError(f"live_broker must be one of {LIVE_BROKERS}")
    if live and live_broker == "rest" and not (env.get("RH_API_KEY") and env.get("RH_PRIVATE_KEY_B64")):
        raise ConfigError("live_broker: rest requires RH_API_KEY and RH_PRIVATE_KEY_B64 in .env")
    if live and lim.allowance_usd is None:
        raise ConfigError("Live trading requires limits.allowance_usd in config.yaml: the most the agent may "
                          "ever have invested. It is never inferred from your account's buying power.")

    mcp = dict(raw.get("mcp") or {})
    transport = str(mcp.get("transport", "stdio")).lower()
    if transport not in MCP_TRANSPORTS:
        raise ConfigError(f"mcp.transport must be one of {MCP_TRANSPORTS}")
    mcp["transport"] = transport
    server_env = {k: env[k].strip() for k in MCP_SERVER_SECRET_ENV if env.get(k, "").strip()}
    if live and live_broker == "mcp" and transport == "stdio":
        problems = mcp_stdio_problems(mcp, server_env, need_trading=True)
        if problems:
            raise ConfigError("Live trading through the stdio MCP server is not configured: " + "; ".join(problems))

    agent, brain, paper = raw["agent"], raw["brain"], raw["paper"]
    return Config(
        limits=lim,
        allowed_symbols=symbols,
        cycle_minutes=int(agent["cycle_minutes"]),
        candle_granularity_seconds=int(agent["candle_granularity_seconds"]),
        candle_count=int(agent["candle_count"]),
        model=str(brain["model"]),
        effort=str(brain["effort"]),
        news_search=bool(brain["news_search"]),
        max_searches_per_cycle=int(brain["max_searches_per_cycle"]),
        paper_starting_cash_usd=float(paper["starting_cash_usd"]),
        paper_slippage_pct=float(paper["slippage_pct"]),
        live=live,
        live_broker=live_broker,
        mcp=mcp,
        mcp_server_env=server_env,
        rh_api_key=env.get("RH_API_KEY", ""),
        rh_private_key_b64=env.get("RH_PRIVATE_KEY_B64", ""),
        root=root,
    )
