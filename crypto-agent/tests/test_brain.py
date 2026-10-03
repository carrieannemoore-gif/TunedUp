import json
from types import SimpleNamespace

from agent.brain import ClaudeBrain, parse_decisions


def test_malformed_json_means_hold():
    r = parse_decisions("Sure! Buy all the BTC you can.")
    assert r.decisions == [] and "invalid JSON" in r.errors[0]


def test_invalid_entries_are_dropped_valid_ones_kept():
    payload = {"market_view": "x", "decisions": [
        {"symbol": "BTC-USD", "action": "buy", "usd_amount": 10, "confidence": 0.7,
         "stop_loss_pct": 5, "take_profit_pct": 10, "rationale": "ok"},
        {"symbol": "ETH-USD", "action": "short", "usd_amount": 10, "confidence": 0.7,
         "stop_loss_pct": 5, "take_profit_pct": 10, "rationale": "not allowed"},
        {"symbol": "SOL-USD", "action": "buy", "usd_amount": -5, "confidence": 3,
         "stop_loss_pct": 5, "take_profit_pct": 10, "rationale": "bad numbers"},
        {"symbol": "SOL-USD", "action": "buy", "usd_amount": 5, "confidence": 0.9,
         "stop_loss_pct": 5, "take_profit_pct": 10, "rationale": "x", "leverage": 100},
    ]}
    r = parse_decisions(json.dumps(payload))
    assert [d.symbol for d in r.decisions] == ["BTC-USD"] and len(r.errors) == 3


def _response(stop_reason, text=None):
    content = [SimpleNamespace(type="text", text=text)] if text else []
    return SimpleNamespace(stop_reason=stop_reason, content=content, model="m",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_details=None)


class _Client:
    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self._response = response

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def test_refusal_and_truncation_mean_hold():
    for reason in ("refusal", "max_tokens"):
        brain = ClaudeBrain("claude-opus-5-5", "high", False, 0, client=_Client(_response(reason, "{}")))
        r = brain.decide({})
        assert r.decisions == [] and r.errors


def test_request_shape_uses_structured_output_and_fallbacks():
    ok = json.dumps({"market_view": "calm", "decisions": []})
    client = _Client(_response("end_turn", ok))
    ClaudeBrain("claude-opus-5-5", "high", True, 3, client=client).decide({"a": 1})
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "high"
    assert call["fallbacks"] == "default"
    assert call["tools"][0]["type"] == "web_search_20260209" and call["tools"][0]["max_uses"] == 3
    assert "tool_choice" not in call
