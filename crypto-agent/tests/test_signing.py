import base64
from decimal import Decimal

import pytest
from nacl.signing import SigningKey

from agent.robinhood import RobinhoodCrypto, sign_request

SEED = bytes(range(32))
KEY_B64 = base64.b64encode(SEED).decode()


def test_signature_verifies_over_documented_message_format():
    headers = sign_request("rh-key", KEY_B64, "/api/v1/crypto/trading/orders/", "POST", '{"a":1}', 1700000000)
    assert headers["x-api-key"] == "rh-key"
    assert headers["x-timestamp"] == "1700000000"
    verify_key = SigningKey(SEED).verify_key
    message = b'rh-key1700000000/api/v1/crypto/trading/orders/POST{"a":1}'
    verify_key.verify(message, base64.b64decode(headers["x-signature"]))  # raises if invalid


def test_signature_changes_with_body():
    a = sign_request("k", KEY_B64, "/p", "POST", "{}", 1)["x-signature"]
    b = sign_request("k", KEY_B64, "/p", "POST", '{"x":1}', 1)["x-signature"]
    assert a != b


def test_client_never_reveals_credentials_in_repr():
    client = RobinhoodCrypto("secret-api-key", KEY_B64)
    assert "secret" not in repr(client) and KEY_B64 not in repr(client)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, data))
        return self.responses.pop(0)


def test_market_order_body_and_fill_parsing():
    session = _Session([_Resp(201, {"id": "o1", "state": "filled", "filled_asset_quantity": "0.001",
                                    "average_price": "60000"})])
    client = RobinhoodCrypto("k", KEY_B64, session=session)
    fill = client.market_order("BTC-USD", "buy", Decimal("0.00100000"), "coid-1")
    method, url, data = session.calls[0]
    assert method == "POST" and url.endswith("/api/v1/crypto/trading/orders/")
    assert '"client_order_id":"coid-1"' in data and '"asset_quantity":"0.00100000"' in data
    assert fill.quantity == pytest.approx(0.001) and fill.price == 60000


def test_order_post_is_never_retried_on_server_error():
    from agent.broker import OrderStateUnknown
    session = _Session([_Resp(503, {"err": "down"})])
    client = RobinhoodCrypto("k", KEY_B64, session=session)
    with pytest.raises(OrderStateUnknown):
        client.market_order("BTC-USD", "buy", Decimal("0.001"), "coid-2")
    assert len(session.calls) == 1


def test_order_4xx_is_a_clean_rejection():
    from agent.broker import OrderRejected
    client = RobinhoodCrypto("k", KEY_B64, session=_Session([_Resp(400, {"err": "bad"})]))
    with pytest.raises(OrderRejected):
        client.market_order("BTC-USD", "buy", Decimal("0.001"), "coid-3")
