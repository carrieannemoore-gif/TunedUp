"""Broker backed by an MCP trading server (your robinhood-crypto-mcp over stdio, or an HTTP server).

The agent's own code is the MCP client. Claude never sees or calls these tools: it only proposes
decisions, and this broker places an order only after the risk engine has approved it.

Safety properties:
- Only the tools mapped in config.yaml (mcp.tools) can ever be called. Cancels, transfers,
  withdrawals or any other server tool are unreachable from this code.
- Order placement is never retried. When a failure could mean the order was sent, we look the
  order up by client_order_id; if it can't be found we raise OrderStateUnknown and the agent
  halts until a human checks the Robinhood app.
- If the session is down BEFORE an order is sent, the order is reported as not sent (rejected).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import threading
import time
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import parse_qs, urlparse

from mcp import ClientSession

from .broker import Fill, OrderRejected, OrderStateUnknown, PairInfo, Quote

OPERATIONS = ("quote", "buying_power", "holdings", "place_order", "get_order")
OPTIONAL_OPERATIONS = ("pair_info",)
UNMAPPED = "TBD"
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")

SessionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]


class MCPMappingError(ValueError):
    pass


class SessionNotConnected(Exception):
    """Raised before anything is sent, when there is no live MCP session."""


class ToolError(RuntimeError):
    """The server answered the tool call with isError=true."""


# --- mapping helpers --------------------------------------------------------------------------

def _mapped_tools(tools_cfg: dict) -> dict[str, dict]:
    """Every tool call the mapping can make, keyed by a label (op, or op.reconcile)."""
    out = {}
    for op in OPERATIONS + OPTIONAL_OPERATIONS:
        entry = tools_cfg.get(op)
        if isinstance(entry, dict) and entry.get("name") and entry["name"] != UNMAPPED:
            out[op] = entry
    reconcile = (tools_cfg.get("place_order") or {}).get("reconcile")
    if isinstance(reconcile, dict) and reconcile.get("name"):
        out["place_order.reconcile"] = reconcile
    return out


def _arg_problems(label: str, entry: dict, tool: dict) -> list[str]:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    args = entry.get("args") or {}
    problems = [f"mcp.tools.{label}.args.{k} is not a parameter of {entry['name']}" for k in args if k not in props]
    problems += [f"mcp.tools.{label}.args is missing required parameter {r!r} of {entry['name']}"
                 for r in schema.get("required") or [] if r not in args]
    if label == "place_order" and "confirm" in props and args.get("confirm") is not True:
        problems.append("mcp.tools.place_order.args must set confirm: true, otherwise orders are only previewed")
    return problems


def mapping_problems(tools_cfg: dict, discovered: Iterable[str] | list[dict] | None = None) -> list[str]:
    """Everything that must be fixed before the MCP broker may trade live.

    `discovered` is the server's tool list: names only, or full tool definitions (then every mapped
    call's arguments are also checked against that tool's input schema).
    """
    problems = []
    for op in OPERATIONS:
        entry = tools_cfg.get(op)
        if not isinstance(entry, dict) or not entry.get("name"):
            problems.append(f"mcp.tools.{op} is missing")
        elif entry["name"] == UNMAPPED:
            problems.append(f"mcp.tools.{op}.name is still TBD")
    if discovered is None or problems:
        return problems
    discovered = list(discovered)
    defs = {t["name"]: t for t in discovered if isinstance(t, dict)}
    names = set(defs) or set(discovered)
    for label, entry in _mapped_tools(tools_cfg).items():
        if entry["name"] not in names:
            problems.append(f"mcp.tools.{label}.name={entry['name']!r} is not offered by the server")
        elif entry["name"] in defs:
            problems += _arg_problems(label, entry, defs[entry["name"]])
    return problems


def fill_template(template: Any, params: dict[str, Any]) -> Any:
    """Substitute "{name}" placeholders in an argument template (recursively)."""
    if isinstance(template, dict):
        return {k: fill_template(v, params) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_template(v, params) for v in template]
    if isinstance(template, str):
        m = _PLACEHOLDER.match(template)
        if m:
            if m.group(1) not in params:
                raise MCPMappingError(f"template needs {{{m.group(1)}}} but it was not provided")
            return params[m.group(1)]
        return template.format_map(params) if "{" in template else template
    return template


def dig(obj: Any, path: str | None) -> Any:
    """Follow a dotted path ("data.results.0.price") into parsed JSON. Empty path = the object."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            if part not in obj:
                raise KeyError(f"result has no field {part!r} (path {path!r})")
            obj = obj[part]
        else:
            raise KeyError(f"cannot follow {part!r} into {type(obj).__name__} (path {path!r})")
    return obj


def result_payload(result: Any) -> Any:
    """Prefer structuredContent; otherwise parse JSON from the text content blocks."""
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    texts = [c.text for c in (result.content or []) if getattr(c, "type", None) == "text"]
    joined = "".join(texts).strip()
    try:
        return json.loads(joined)
    except json.JSONDecodeError as e:
        raise ValueError(f"tool returned non-JSON text: {joined[:200]!r}") from e


def _error_text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in (result.content or []))[:500] or "tool reported an error"


# --- a single long-lived MCP session on a background event loop ---------------------------------

class _SessionRunner:
    """Owns one MCP ClientSession in one asyncio task, so AnyIO cancel scopes stay in their task."""

    def __init__(self, factory: SessionFactory):
        self._factory = factory
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-session")
        self._thread.start()
        self._queue: asyncio.Queue | None = None
        self._owner: concurrent.futures.Future | None = None

    @property
    def alive(self) -> bool:
        return self._owner is not None and not self._owner.done()

    def start(self, timeout: float = 60) -> None:
        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._owner = asyncio.run_coroutine_threadsafe(self._own(ready), self._loop)
        ready.result(timeout)  # re-raises connection / login errors

    async def _own(self, ready: concurrent.futures.Future) -> None:
        self._queue = asyncio.Queue()
        try:
            async with self._factory() as session:
                ready.set_result(True)
                while True:
                    item = await self._queue.get()
                    if item is None:
                        return
                    fn, fut = item
                    try:
                        fut.set_result(await fn(session))
                    except BaseException as e:  # noqa: BLE001 - hand every failure to the caller
                        fut.set_exception(e)
                        if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt)):
                            raise
        except BaseException as e:  # noqa: BLE001
            if not ready.done():
                ready.set_exception(e)

    def run(self, fn: Callable[[ClientSession], Awaitable[Any]], timeout: float) -> Any:
        if not self.alive or self._queue is None:
            raise SessionNotConnected("MCP session is not connected")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (fn, fut))
        return fut.result(timeout)

    def close(self) -> None:
        if self.alive and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            try:
                self._owner.result(10)
            except Exception:  # noqa: BLE001 - best effort shutdown
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)


# --- the broker ---------------------------------------------------------------------------------

class RobinhoodMCPBroker:
    def __init__(self, mcp_cfg: dict, session_factory: SessionFactory, call_timeout_s: float = 30.0):
        self._tools: dict = mcp_cfg.get("tools") or {}
        self._allowed_names = {e["name"] for e in _mapped_tools(self._tools).values()}
        self._pairs: dict[str, PairInfo] = {}
        self._filled = {s.lower() for s in mcp_cfg.get("filled_states", ["filled"])}
        self._failed = {s.lower() for s in mcp_cfg.get("failed_states", ["canceled", "cancelled", "rejected", "failed"])}
        self._fill_timeout_s = float(mcp_cfg.get("order_fill_timeout_seconds", 30))
        self._increments = {k: Decimal(str(v)) for k, v in (mcp_cfg.get("asset_increments") or {}).items()}
        self._default_increment = Decimal(str(mcp_cfg.get("default_asset_increment", "0.0001")))
        self._call_timeout_s = call_timeout_s
        self._runner = _SessionRunner(session_factory)

    def __repr__(self) -> str:
        return "RobinhoodMCPBroker(<session redacted>)"

    # session lifecycle

    def connect(self) -> None:
        if not self._runner.alive:
            self._runner.start()

    def close(self) -> None:
        self._runner.close()

    def list_tools(self) -> list[dict]:
        self.connect()

        async def _list(session: ClientSession) -> list[dict]:
            tools, cursor = [], None
            while True:
                page = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            return [t.model_dump(mode="json", exclude_none=True) for t in tools]

        return self._runner.run(_list, self._call_timeout_s)

    # raw tool access, restricted to the mapped tools

    def _call_entry(self, label: str, entry: dict | None, extra_args: dict | None = None, **params: Any) -> Any:
        if not entry or entry.get("name") in (None, UNMAPPED):
            raise MCPMappingError(f"mcp.tools.{label} is not mapped; run --discover and finish config.yaml")
        name = entry["name"]
        if name not in self._allowed_names:  # defensive: only mapped tools are callable
            raise MCPMappingError(f"tool {name!r} is not in the allow-list")
        args = fill_template(entry.get("args") or {}, params)
        if extra_args:
            args = {**args, **extra_args}
        self.connect()

        async def _invoke(session: ClientSession) -> Any:
            return await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=self._call_timeout_s))

        result = self._runner.run(_invoke, self._call_timeout_s + 5)
        if result.isError:
            raise ToolError(f"{name} failed: {_error_text(result)}")
        return result_payload(result)

    def _call(self, op: str, extra_args: dict | None = None, **params: Any) -> Any:
        return self._call_entry(op, self._tools.get(op), extra_args, **params)

    # Broker protocol

    def quote(self, symbol: str) -> Quote:
        spec = self._tools["quote"]
        data = self._call("quote", symbol=symbol)
        bid, ask = float(dig(data, spec.get("bid"))), float(dig(data, spec.get("ask")))
        if not (0 < bid <= ask):
            raise ValueError(f"implausible quote for {symbol}: bid={bid} ask={ask}")
        return Quote(symbol=symbol, bid=bid, ask=ask)

    def buying_power(self) -> float:
        return float(dig(self._call("buying_power"), self._tools["buying_power"].get("value")))

    def holdings(self) -> dict[str, float]:
        spec = self._tools["holdings"]
        out: dict[str, float] = {}
        extra: dict | None = None
        for _ in range(int(spec.get("max_pages", 20))):
            data = self._call("holdings", extra)
            for row in dig(data, spec.get("list")) or []:
                sym = str(dig(row, spec.get("symbol"))).upper()
                if not sym.endswith("-USD"):
                    sym = f"{sym}-USD"
                out[sym] = out.get(sym, 0.0) + float(dig(row, spec.get("qty")))
            cursor = _next_cursor(data, spec)
            if not cursor:
                return out
            extra = {spec.get("cursor_arg", "cursor"): cursor}
        raise RuntimeError("holdings pagination did not finish; raise mcp.tools.holdings.max_pages")

    def pair_info(self, symbol: str) -> PairInfo:
        if symbol in self._pairs:
            return self._pairs[symbol]
        spec = self._tools.get("pair_info")
        if spec and spec.get("name") not in (None, UNMAPPED):
            data = self._call("pair_info", symbol=symbol)
            info = PairInfo(symbol=symbol, asset_increment=Decimal(str(dig(data, spec.get("increment")))),
                            min_order_size=Decimal(str(dig(data, spec.get("min_size")))))
        else:
            inc = self._increments.get(symbol, self._default_increment)
            info = PairInfo(symbol=symbol, asset_increment=inc, min_order_size=inc)
        self._pairs[symbol] = info
        return info

    def market_order(self, symbol: str, side: str, asset_quantity: Decimal, client_order_id: str) -> Fill:
        try:
            self.connect()
        except Exception as e:  # nothing was sent
            raise OrderRejected(f"MCP session unavailable; order not sent: {e}") from e
        spec = self._tools["place_order"]
        params = {"symbol": symbol, "side": side, "quantity": format(asset_quantity, "f"),
                  "client_order_id": client_order_id}
        submitted_at = datetime.now(timezone.utc)
        try:
            data = self._call("place_order", **params)
        except (MCPMappingError, SessionNotConnected) as e:
            raise OrderRejected(f"order not sent: {e}") from e
        except ToolError as e:
            if self._definitely_not_sent(str(e)):
                raise OrderRejected(str(e)) from e
            order_id = self._reconcile(symbol, side, client_order_id, submitted_at, cause=e)
            return self._await_fill(order_id, None, symbol, side, client_order_id)
        except Exception as e:  # timeout / transport failure after sending: look the order up
            order_id = self._reconcile(symbol, side, client_order_id, submitted_at, cause=e)
            return self._await_fill(order_id, None, symbol, side, client_order_id)

        try:
            order_id = str(dig(data, spec.get("order_id")))
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise OrderStateUnknown(f"order {client_order_id}: response has no order id (preview only?): "
                                    f"{json.dumps(data)[:300]}") from e
        return self._await_fill(order_id, data, symbol, side, client_order_id)

    def _definitely_not_sent(self, message: str) -> bool:
        """True only for errors that prove the order never reached Robinhood (validation, caps, 4xx)."""
        patterns = (self._tools.get("place_order") or {}).get("not_sent_patterns") or []
        return any(re.search(p, message) for p in patterns)

    def _reconcile(self, symbol: str, side: str, client_order_id: str, submitted_at: datetime,
                   cause: BaseException) -> str:
        """After an ambiguous failure, find our order by client_order_id. Never re-sends anything."""
        spec = (self._tools.get("place_order") or {}).get("reconcile")
        if not spec:
            raise OrderStateUnknown(f"order {client_order_id} outcome unknown ({cause!r}); no reconcile tool mapped")
        since = (submitted_at - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        last_error: BaseException = cause
        for attempt in range(int(spec.get("attempts", 3))):
            if attempt:
                time.sleep(float(spec.get("wait_seconds", 2)))
            try:
                data = self._call_entry("place_order.reconcile", spec, symbol=symbol, side=side, since=since)
                for order in dig(data, spec.get("list")) or []:
                    if str(dig(order, spec.get("client_order_id"))) == client_order_id:
                        return str(dig(order, spec.get("order_id", "id")))
            except Exception as e:  # noqa: BLE001 - keep trying, then give up safely
                last_error = e
        raise OrderStateUnknown(f"order {client_order_id} outcome unknown after {cause!r}; "
                                f"not found by client_order_id (last: {last_error!r})")

    def _await_fill(self, order_id: str, place_data: Any, symbol: str, side: str, client_order_id: str) -> Fill:
        place_spec, get_spec = self._tools["place_order"], self._tools["get_order"]
        if place_data is not None:
            state = str(dig(place_data, place_spec.get("state"))).lower()
            if state in self._failed:
                raise OrderRejected(f"order {order_id} ended in state {state}")
        deadline = time.time() + self._fill_timeout_s
        while True:
            data = self._safe_get_order(order_id, client_order_id)
            state = str(dig(data, get_spec.get("state"))).lower()
            if state in self._filled:
                return self._to_fill(data, symbol, side, order_id, client_order_id)
            if state in self._failed:
                raise OrderRejected(f"order {order_id} ended in state {state}")
            if time.time() > deadline:
                raise OrderStateUnknown(f"order {order_id} not filled within {self._fill_timeout_s}s (state={state})")
            time.sleep(1.5)

    def _safe_get_order(self, order_id: str, client_order_id: str) -> Any:
        try:
            return self._call("get_order", order_id=order_id)
        except Exception as e:  # noqa: BLE001 - the order exists; we just can't see it
            raise OrderStateUnknown(f"order {order_id} ({client_order_id}) status check failed: {e!r}") from e

    def _to_fill(self, data: Any, symbol: str, side: str, order_id: str, client_order_id: str) -> Fill:
        spec = self._tools["get_order"]
        try:
            qty = float(dig(data, spec.get("filled_qty")))
            price = float(dig(data, spec.get("avg_price")))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise OrderStateUnknown(f"order {order_id} filled but quantity/price unreadable: {e}") from e
        if qty <= 0 or price <= 0:
            raise OrderStateUnknown(f"order {order_id} reported filled with qty={qty} price={price}")
        return Fill(symbol=symbol, side=side, quantity=qty, price=price, order_id=order_id,
                    client_order_id=client_order_id)


def _next_cursor(data: Any, spec: dict) -> str | None:
    """Pull the pagination cursor out of a response's `next` URL, if there is another page."""
    try:
        nxt = dig(data, spec.get("next", "next"))
    except (KeyError, IndexError):
        return None
    if not nxt:
        return None
    values = parse_qs(urlparse(str(nxt)).query).get(spec.get("cursor_param", "cursor"))
    return values[0] if values else None


# --- production session factory -----------------------------------------------------------------

def http_session_factory(mcp_cfg: dict, state_dir: Path, interactive: bool) -> SessionFactory:
    from mcp.client.streamable_http import streamablehttp_client

    from .mcp_auth import FileTokenStorage, build_oauth

    url = mcp_cfg["url"]
    storage = FileTokenStorage(state_dir / "mcp_oauth.json")
    auth = build_oauth(url, storage, int(mcp_cfg.get("oauth_callback_port", 8765)), interactive,
                       list(mcp_cfg.get("allowed_auth_domains") or ["robinhood.com"]))

    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        async with streamablehttp_client(url, auth=auth) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    return factory


def stdio_session_factory(mcp_cfg: dict, server_env: dict[str, str], allowed_symbols: Iterable[str],
                          log_dir: Path, trading_enabled: bool) -> SessionFactory:
    """Start your own MCP server (e.g. robinhood-crypto-mcp) as a child process over stdio.

    The child gets only a minimal environment (HOME, PATH, SHELL, TERM) plus the variables set here,
    so unrelated secrets such as ANTHROPIC_API_KEY never reach it. Trading tools are only enabled
    when the agent is trading live; the server's symbol allowlist is set from the agent's own.
    """
    from mcp.client.stdio import StdioServerParameters, stdio_client

    log_dir.mkdir(parents=True, exist_ok=True)
    env = {  # later entries win: the agent's own switches can't be overridden from config
        **{str(k): str(v) for k, v in (mcp_cfg.get("extra_env") or {}).items()},
        **server_env,
        "RH_ENABLE_TRADING": "true" if trading_enabled else "false",
        "RH_ALLOWED_SYMBOLS": ",".join(allowed_symbols),
        "RH_AUDIT_LOG": str(log_dir / "mcp_server_audit.jsonl"),
    }
    params = StdioServerParameters(command=str(mcp_cfg["command"]), args=[str(a) for a in mcp_cfg.get("args") or []],
                                   env=env, cwd=mcp_cfg.get("cwd"))

    @asynccontextmanager
    async def factory() -> AsyncIterator[ClientSession]:
        with (log_dir / "mcp_server_stderr.log").open("a") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    return factory
