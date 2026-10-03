"""Signed client for Robinhood's official Crypto Trading API.

Auth: every request carries x-api-key, x-timestamp and x-signature, where the signature is an
ED25519 signature (base64) over f"{api_key}{timestamp}{path}{method}{body}".

Verify paths and payloads against https://docs.robinhood.com/crypto/trading/ before going live;
Robinhood can change them.
"""

from __future__ import annotations

import base64
import json
import time
from decimal import Decimal
from typing import Any

import requests
from nacl.signing import SigningKey

from .broker import Fill, OrderRejected, OrderStateUnknown, PairInfo, Quote

BASE_URL = "https://trading.robinhood.com"
_RETRYABLE = {429, 500, 502, 503, 504}
_TERMINAL_OK = {"filled"}
_TERMINAL_BAD = {"canceled", "cancelled", "failed", "rejected"}


def sign_request(api_key: str, private_key_b64: str, path: str, method: str, body: str, timestamp: int) -> dict[str, str]:
    signing_key = SigningKey(base64.b64decode(private_key_b64))
    message = f"{api_key}{timestamp}{path}{method}{body}"
    signature = signing_key.sign(message.encode("utf-8")).signature
    return {
        "x-api-key": api_key,
        "x-timestamp": str(timestamp),
        "x-signature": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json; charset=utf-8",
    }


class RobinhoodCrypto:
    def __init__(self, api_key: str, private_key_b64: str, session: requests.Session | None = None,
                 base_url: str = BASE_URL, fill_timeout_s: float = 30.0):
        if not api_key or not private_key_b64:
            raise ValueError("Robinhood API key and private key are required")
        SigningKey(base64.b64decode(private_key_b64))  # fail fast on a malformed key
        self._api_key = api_key
        self._private_key_b64 = private_key_b64
        self._session = session or requests.Session()
        self._base_url = base_url
        self._fill_timeout_s = fill_timeout_s
        self._pairs: dict[str, PairInfo] = {}

    def __repr__(self) -> str:  # never leak keys into logs or tracebacks
        return "RobinhoodCrypto(<credentials redacted>)"

    # --- transport -------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, retry: bool = True) -> dict:
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        attempts = 4 if retry else 1
        for attempt in range(attempts):
            headers = sign_request(self._api_key, self._private_key_b64, path, method, body_str, int(time.time()))
            resp = self._session.request(method, self._base_url + path, headers=headers,
                                         data=body_str or None, timeout=15)
            if resp.status_code in _RETRYABLE and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise requests.HTTPError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}", response=resp)
            return resp.json() if resp.content else {}
        raise RuntimeError("unreachable")

    def _get_all(self, path: str) -> list[dict]:
        results, next_path = [], path
        while next_path:
            page = self._request("GET", next_path)
            results.extend(page.get("results", []))
            nxt = page.get("next")
            next_path = nxt.replace(self._base_url, "") if nxt else None
        return results

    # --- Broker interface ------------------------------------------------

    def account(self) -> dict:
        return self._request("GET", "/api/v1/crypto/trading/accounts/")

    def buying_power(self) -> float:
        return float(self.account().get("buying_power", 0))

    def holdings(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in self._get_all("/api/v1/crypto/trading/holdings/"):
            qty = h.get("quantity_available_for_trading", h.get("total_quantity", 0))
            out[f"{h['asset_code']}-USD"] = float(qty)
        return out

    def quote(self, symbol: str) -> Quote:
        data = self._request("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={symbol}")
        r = data["results"][0]
        bid = float(r.get("bid_inclusive_of_sell_spread", r["price"]))
        ask = float(r.get("ask_inclusive_of_buy_spread", r["price"]))
        return Quote(symbol=symbol, bid=bid, ask=ask)

    def pair_info(self, symbol: str) -> PairInfo:
        if symbol not in self._pairs:
            data = self._request("GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={symbol}")
            r = data["results"][0]
            self._pairs[symbol] = PairInfo(
                symbol=symbol,
                asset_increment=Decimal(str(r["asset_increment"])),
                min_order_size=Decimal(str(r["min_order_size"])),
            )
        return self._pairs[symbol]

    def market_order(self, symbol: str, side: str, asset_quantity: Decimal, client_order_id: str) -> Fill:
        body = {
            "client_order_id": client_order_id,
            "side": side,
            "type": "market",
            "symbol": symbol,
            "market_order_config": {"asset_quantity": format(asset_quantity, "f")},
        }
        # Order POSTs are never retried: a timeout could mean the order went through.
        try:
            order = self._request("POST", "/api/v1/crypto/trading/orders/", body, retry=False)
        except requests.HTTPError as e:
            if e.response is not None and 400 <= e.response.status_code < 500 and e.response.status_code != 408:
                raise OrderRejected(str(e)) from e
            raise OrderStateUnknown(f"Order {client_order_id} POST failed ambiguously: {e}") from e
        except requests.RequestException as e:
            raise OrderStateUnknown(f"Order {client_order_id} POST failed ambiguously: {e}") from e
        return self._await_fill(order, symbol, side, client_order_id)

    def _await_fill(self, order: dict, symbol: str, side: str, client_order_id: str) -> Fill:
        order_id = order.get("id", "")
        deadline = time.time() + self._fill_timeout_s
        while True:
            state = str(order.get("state", "")).lower()
            if state in _TERMINAL_OK:
                return self._to_fill(order, symbol, side, client_order_id)
            if state in _TERMINAL_BAD:
                raise OrderRejected(f"Order {order_id} ended in state {state}")
            if time.time() > deadline:
                raise OrderStateUnknown(f"Order {order_id} not filled within {self._fill_timeout_s}s (state={state})")
            time.sleep(1.5)
            order = self._request("GET", f"/api/v1/crypto/trading/orders/{order_id}/")

    @staticmethod
    def _to_fill(order: dict, symbol: str, side: str, client_order_id: str) -> Fill:
        qty = float(order.get("filled_asset_quantity") or 0)
        price = float(order.get("average_price") or 0)
        if (not qty or not price) and order.get("executions"):
            ex = order["executions"]
            qty = sum(float(e["quantity"]) for e in ex)
            price = sum(float(e["quantity"]) * float(e["effective_price"]) for e in ex) / qty
        if not qty or not price:
            raise OrderStateUnknown(f"Order {order.get('id')} reported filled without quantity/price")
        return Fill(symbol=symbol, side=side, quantity=qty, price=price,
                    order_id=str(order.get("id", "")), client_order_id=client_order_id)
