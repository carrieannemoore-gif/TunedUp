"""Local mock of the Robinhood Crypto Trading API endpoints that robinhood-crypto-mcp calls.

It verifies every request's ED25519 signature (api_key + timestamp + path + method + body), so an
integration test against the real server also proves its request signing.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

PRICES = {"BTC-USD": 60000.0, "ETH-USD": 3000.0, "SOL-USD": 150.0, "DOGE-USD": 0.2}


class MockRobinhood:
    def __init__(self, api_key: str, verify_key: VerifyKey, buying_power: float = 50_000.0):
        self.api_key, self.verify_key = api_key, verify_key
        self.buying_power = buying_power
        self.holdings = {"BTC": 0.5, "DOGE": 1000.0}
        self.orders: dict[str, dict] = {}
        self.posts: list[dict] = []
        self.bad_signatures = 0
        self.fail_next_post_after_recording = False
        self.fail_estimates = False
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _verified(self, body: str) -> bool:
                msg = f"{self.headers.get('x-api-key')}{self.headers.get('x-timestamp')}{self.path}{self.command}{body}"
                try:
                    mock.verify_key.verify(msg.encode(), base64.b64decode(self.headers.get("x-signature", "")))
                    return self.headers.get("x-api-key") == mock.api_key
                except (BadSignatureError, ValueError):
                    mock.bad_signatures += 1
                    return False

            def do_GET(self):  # noqa: N802
                if not self._verified(""):
                    return self._send(401, {"detail": "bad signature"})
                url = urlparse(self.path)
                q = {k: v for k, v in parse_qs(url.query).items()}
                path = url.path
                if path == "/api/v1/crypto/trading/accounts/":
                    return self._send(200, {"account_number": "X1", "status": "active",
                                            "buying_power": f"{mock.buying_power:.2f}", "buying_power_currency": "USD"})
                if path == "/api/v1/crypto/marketdata/best_bid_ask/":
                    return self._send(200, {"results": [
                        {"symbol": s, "price": str(PRICES[s]), "bid_inclusive_of_sell_spread": str(PRICES[s] * 0.999),
                         "ask_inclusive_of_buy_spread": str(PRICES[s] * 1.001)} for s in q.get("symbol", [])]})
                if path == "/api/v1/crypto/marketdata/estimated_price/":
                    s, side = q["symbol"][0], q["side"][0]
                    if mock.fail_estimates:
                        return self._send(503, {"detail": "estimates unavailable"})
                    if s not in PRICES:
                        return self._send(400, {"detail": f"unknown symbol {s}"})
                    price = PRICES[s] * (1.001 if side == "ask" else 0.999)
                    return self._send(200, {"results": [{"symbol": s, "side": side, "price": str(price),
                                                         "quantity": qty} for qty in q["quantity"][0].split(",")]})
                if path == "/api/v1/crypto/trading/trading_pairs/":
                    return self._send(200, {"results": [{"symbol": s, "asset_increment": "0.000001",
                                                         "min_order_size": "0.00001"} for s in q.get("symbol", [])]})
                if path == "/api/v1/crypto/trading/holdings/":
                    rows = [{"account_number": "X1", "asset_code": k, "total_quantity": str(v),
                             "quantity_available_for_trading": str(v)} for k, v in sorted(mock.holdings.items()) if v > 0]
                    if "cursor" not in q:
                        return self._send(200, {"next": "https://trading.robinhood.com/api/v1/crypto/trading/holdings/"
                                                        "?cursor=p2", "previous": None, "results": rows[:1]})
                    return self._send(200, {"next": None, "previous": None, "results": rows[1:]})
                if path == "/api/v1/crypto/trading/orders/":
                    rows = [o for o in mock.orders.values()
                            if all(o.get(k) == q[k][0] for k in ("id", "symbol", "side") if k in q)]
                    return self._send(200, {"next": None, "results": rows})
                return self._send(404, {"detail": f"no mock for {path}"})

            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                if not self._verified(body):
                    return self._send(401, {"detail": "bad signature"})
                if urlparse(self.path).path != "/api/v1/crypto/trading/orders/":
                    return self._send(404, {"detail": "no mock"})
                order = json.loads(body)
                mock.posts.append(order)
                qty = float(order["market_order_config"]["asset_quantity"])
                price = PRICES[order["symbol"]] * (1.001 if order["side"] == "buy" else 0.999)
                if order["side"] == "buy" and qty * price > mock.buying_power:
                    return self._send(400, {"detail": "insufficient buying power"})
                asset = order["symbol"].split("-")[0]
                mock.buying_power += -qty * price if order["side"] == "buy" else qty * price
                mock.holdings[asset] = mock.holdings.get(asset, 0) + (qty if order["side"] == "buy" else -qty)
                oid = str(uuid.uuid4())
                record = {"id": oid, "client_order_id": order["client_order_id"], "symbol": order["symbol"],
                          "side": order["side"], "type": order["type"], "state": "filled",
                          "filled_asset_quantity": str(qty), "average_price": str(price),
                          "created_at": "2026-10-01T12:00:00Z"}
                mock.orders[oid] = record
                if mock.fail_next_post_after_recording:
                    mock.fail_next_post_after_recording = False
                    return self._send(503, {"detail": "upstream timeout"})
                return self._send(201, record)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
