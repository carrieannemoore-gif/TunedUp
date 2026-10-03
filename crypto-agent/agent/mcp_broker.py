"""Broker backed by the Robinhood Trading MCP server.

The agent's own code is the MCP client. Claude never sees or calls these tools: it only proposes
decisions, and this broker places an order only after the risk engine has approved it.

Safety properties:
- Only the five tools mapped in config.yaml (mcp.tools) can ever be called. Transfers,
  withdrawals or any other server tool are unreachable from this code.
- Order placement is never retried. If we cannot tell whether an order executed, we raise
  OrderStateUnknown and the agent halts until a human checks the Robinhood app.
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
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from mcp import ClientSession

from .broker import Fill, OrderRejected, OrderStateUnknown, PairInfo, Quote

OPERATIONS = ("quote", "buying_power", "holdings", "place_order", "get_order")
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

def mapping_problems(tools_cfg: dict, discovered: set[str] | None = None) -> list[str]:
    """Everything that must be fixed before the MCP broker may trade live."""
    problems = []
    for op in OPERATIONS:
        entry = tools_cfg.get(op)
        if not isinstance(entry, dict) or not entry.get("name"):
            problems.append(f"mcp.tools.{op} is missing")
        elif entry["name"] == UNMAPPED:
            problems.append(f"mcp.tools.{op}.name is still TBD")
        elif discovered is not None and entry["name"] not in discovered:
            problems.append(f"mcp.tools.{op}.name={entry['name']!r} is not offered by the server")
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
        self._allowed_names = {self._tools[op]["name"] for op in OPERATIONS if op in self._tools}
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

    def _call(self, op: str, **params: Any) -> Any:
        entry = self._tools.get(op)
        if not entry or entry.get("name") in (None, UNMAPPED):
            raise MCPMappingError(f"mcp.tools.{op} is not mapped; run --discover and finish config.yaml")
        name = entry["name"]
        if name not in self._allowed_names:  # defensive: only mapped tools are callable
            raise MCPMappingError(f"tool {name!r} is not in the allow-list")
        args = fill_template(entry.get("args") or {}, params)
        self.connect()

        async def _invoke(session: ClientSession) -> Any:
            return await session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=self._call_timeout_s))

        result = self._runner.run(_invoke, self._call_timeout_s + 5)
        if result.isError:
            raise ToolError(f"{name} failed: {_error_text(result)}")
        return result_payload(result)

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
        rows = dig(self._call("holdings"), spec.get("list"))
        out: dict[str, float] = {}
        for row in rows or []:
            sym = str(dig(row, spec.get("symbol"))).upper()
            if not sym.endswith("-USD"):
                sym = f"{sym}-USD"
            out[sym] = out.get(sym, 0.0) + float(dig(row, spec.get("qty")))
        return out

    def pair_info(self, symbol: str) -> PairInfo:
        inc = self._increments.get(symbol, self._default_increment)
        return PairInfo(symbol=symbol, asset_increment=inc, min_order_size=inc)

    def market_order(self, symbol: str, side: str, asset_quantity: Decimal, client_order_id: str) -> Fill:
        try:
            self.connect()
        except Exception as e:  # nothing was sent
            raise OrderRejected(f"MCP session unavailable; order not sent: {e}") from e
        spec = self._tools["place_order"]
        params = {"symbol": symbol, "side": side, "quantity": format(asset_quantity, "f"),
                  "client_order_id": client_order_id}
        try:
            data = self._call("place_order", **params)
        except (MCPMappingError, SessionNotConnected) as e:
            raise OrderRejected(f"order not sent: {e}") from e
        except ToolError as e:  # the server answered and refused the order
            raise OrderRejected(str(e)) from e
        except Exception as e:  # timeout / transport failure after sending: outcome unknown
            raise OrderStateUnknown(f"order {client_order_id} outcome unknown: {e!r}") from e

        try:
            order_id = str(dig(data, spec.get("order_id")))
        except (KeyError, IndexError, ValueError) as e:
            raise OrderStateUnknown(f"order {client_order_id} accepted but no order id in response") from e
        state_data = data
        deadline = time.time() + self._fill_timeout_s
        get_spec = self._tools["get_order"]
        while True:
            state = str(dig(state_data, spec.get("state") if state_data is data else get_spec.get("state"))).lower()
            if state in self._filled:
                if state_data is data:  # place_order may not carry fill details; fetch them
                    state_data = self._safe_get_order(order_id, client_order_id)
                return self._to_fill(state_data, symbol, side, order_id, client_order_id)
            if state in self._failed:
                raise OrderRejected(f"order {order_id} ended in state {state}")
            if time.time() > deadline:
                raise OrderStateUnknown(f"order {order_id} not filled within {self._fill_timeout_s}s (state={state})")
            time.sleep(1.5)
            state_data = self._safe_get_order(order_id, client_order_id)

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
