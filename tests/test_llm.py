"""The model call through the real Anthropic SDK, with the HTTP layer mocked (no network, no key)."""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from opus_trader.config import Config, apply_changes
from opus_trader.llm import TOOL, FakeLLM, OpusClient, build_request, cost_usd, parse_response, system_prompt

DECISION = {"action": "OPEN_LONG", "order_type": "MARKET", "entry": None, "stop_loss": 1.15808, "take_profit": 1.16408,
            "confidence": 4, "setup": "Asian range breakout", "reasoning": "Clean break.", "invalidation": "Close back inside the range."}


def api_message(content, stop_reason="tool_use", usage=None, stop_details=None):
    return {
        "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": usage or {"input_tokens": 2100, "output_tokens": 1800, "cache_read_input_tokens": 5200, "cache_creation_input_tokens": 900,
                           "cache_creation": {"ephemeral_1h_input_tokens": 900, "ephemeral_5m_input_tokens": 0}},
    }


def client_returning(payload, seen):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=payload)

    oc = OpusClient.__new__(OpusClient)
    import anthropic

    oc._anthropic = anthropic
    oc.client = AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    return oc


def request(cfg=None):
    cfg = cfg or Config()
    return build_request(cfg, system_prompt(cfg), "## H4 block", "## H1 block", "## M15 block\nDecide.")


def test_request_shape_sent_over_the_wire():
    seen = []
    oc = client_returning(api_message([{"type": "tool_use", "id": "toolu_1", "name": "submit_decision", "input": DECISION}]), seen)
    res = asyncio.run(oc.decide(request(), Config()))
    assert res.ok, res
    body = seen[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}  # forced tool_choice 400s on Opus 5.5
    assert body["tools"][0]["strict"] is True and body["tools"][0]["name"] == "submit_decision"
    assert body["output_config"] == {"effort": "high"}
    assert "thinking" not in body  # adaptive by default; it can't be disabled on Opus 5.5
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    blocks = body["messages"][0]["content"]
    assert [b.get("cache_control") for b in blocks] == [{"type": "ephemeral", "ttl": "1h"}] * 2 + [None]


def test_decision_and_usage_parsed_from_real_sdk_objects():
    seen = []
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "toolu_1", "name": "submit_decision", "input": DECISION}]
    res = asyncio.run(client_returning(api_message(content), seen).decide(request(), Config()))
    assert res.decision.action == "OPEN_LONG" and res.decision.stop_loss == 1.15808 and res.decision.confidence == 4
    assert res.usage == {"input_tokens": 2100, "output_tokens": 1800, "cache_read_tokens": 5200, "cache_write_tokens": 900,
                         "cache_write_1h_tokens": 900, "cache_write_5m_tokens": 0}
    # 2100*$4 + 1800*$20 + 5200*$0.20 + 900*$8 per million
    assert res.cost_usd == pytest.approx((2100 * 4 + 1800 * 20 + 5200 * 0.2 + 900 * 8) / 1e6)
    assert res.raw["content"][1]["name"] == "submit_decision"
    assert res.model == "claude-opus-5-5"


def test_no_tool_call_is_hold():
    res = asyncio.run(client_returning(api_message([{"type": "text", "text": "I would wait."}], "end_turn"), []).decide(request(), Config()))
    assert res.status == "no_decision" and res.decision is None
    assert res.hold_reason() == "Opus did not call submit_decision"


def test_refusal_is_hold_with_category():
    msg = api_message([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None})
    res = asyncio.run(client_returning(msg, []).decide(request(), Config()))
    assert res.status == "refusal" and res.error == "cyber"


def test_max_tokens_is_hold():
    res = asyncio.run(client_returning(api_message([{"type": "thinking", "thinking": "", "signature": "s"}], "max_tokens"), []).decide(request(), Config()))
    assert res.status == "max_tokens"


def test_http_error_is_hold():
    def handler(request):
        return httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})

    import anthropic

    oc = OpusClient.__new__(OpusClient)
    oc._anthropic = anthropic
    oc.client = AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    res = asyncio.run(oc.decide(request(), Config()))
    assert res.status == "error" and "529" in res.error


def test_timeout_is_hold():
    async def slow(request):
        await asyncio.sleep(2)
        return httpx2.Response(200, json={})

    import anthropic

    cfg, _ = apply_changes(Config(), {"model.timeout_s": 30})
    oc = OpusClient.__new__(OpusClient)
    oc._anthropic = anthropic
    oc.client = AsyncAnthropic(api_key="test", http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(slow)))
    cfg.model.__dict__["timeout_s"] = 0.2  # bypass the 30 s floor for the test
    res = asyncio.run(oc.decide(request(cfg), cfg))
    assert res.status == "timeout"


def test_invalid_action_is_hold():
    bad = dict(DECISION, action="BUY_EVERYTHING")
    res = parse_response(__import__("opus_trader.llm", fromlist=["fake_response"]).fake_response(bad), Config(), 10)
    assert res.status == "invalid" and "Unknown action" in res.error


def test_cached_prefix_is_byte_stable_between_bars():
    cfg = Config()
    a = build_request(cfg, system_prompt(cfg), "H4 same", "H1 same", "M15 bar 1")
    b = build_request(cfg, system_prompt(cfg), "H4 same", "H1 same", "M15 bar 2")
    prefix = lambda r: json.dumps([r["tools"], r["system"], r["messages"][0]["content"][:2]], sort_keys=True)  # noqa: E731
    assert prefix(a) == prefix(b)
    assert a["messages"][0]["content"][2] != b["messages"][0]["content"][2]


def test_system_prompt_states_rules_and_signal_mode():
    cfg = Config()
    text = system_prompt(cfg)
    for needle in ("HOLD is the default", "No trade beats a weak trade", "0.50% of equity", "0.5× and 3× ATR(14)", "at least 1.5", "prone to whipsaws in ranges", "use it or ignore it"):
        assert needle in text
    cfg, _ = apply_changes(cfg, {"signal.mode": "filter", "signal.filter_bars": 3})
    assert "within the last 3 closed bars" in system_prompt(cfg)
    assert "%" not in TOOL["description"]


def test_cost_with_5m_writes():
    u = {"input_tokens": 1000, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_1h_tokens": 0, "cache_write_5m_tokens": 1000}
    assert cost_usd(u, Config()) == pytest.approx(1000 * 4 / 1e6 + 1000 * 5 / 1e6)


def test_fake_llm_script():
    fake = FakeLLM(script=[DECISION, "timeout", "refusal", "no_tool"])
    cfg = Config()
    out = [asyncio.run(fake.decide({}, cfg)) for _ in range(4)]
    assert [r.status for r in out] == ["ok", "timeout", "refusal", "no_decision"]
