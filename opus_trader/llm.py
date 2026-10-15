"""The model call: system prompt, cached message blocks, the submit_decision tool, and parsing.

Opus 5.5 rejects forced tool_choice, so the tool is offered with
tool_choice=auto (one call at most) and `strict: true`, and the prompt asks
for exactly one call. Anything other than a valid call (timeout, API error,
refusal, max_tokens, no tool call) is recorded and treated as HOLD.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .risk import Decision

log = logging.getLogger(__name__)

CACHE_1H = {"type": "ephemeral", "ttl": "1h"}

_NULLABLE_NUMBER = {"anyOf": [{"type": "number"}, {"type": "null"}]}

TOOL: dict = {
    "name": "submit_decision",
    "description": "Submit your decision for the M15 bar that just closed. Call this exactly once per bar.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["OPEN_LONG", "OPEN_SHORT", "HOLD", "CLOSE", "MOVE_STOP"],
                       "description": "CLOSE closes the open position or cancels the pending order. MOVE_STOP can only tighten the stop."},
            "order_type": {"anyOf": [{"type": "string", "enum": ["MARKET", "LIMIT"]}, {"type": "null"}],
                           "description": "MARKET or LIMIT for OPEN_LONG / OPEN_SHORT; null otherwise."},
            "entry": {**_NULLABLE_NUMBER, "description": "Limit price for LIMIT orders; null otherwise."},
            "stop_loss": {**_NULLABLE_NUMBER, "description": "Absolute stop-loss price for entries, or the new stop for MOVE_STOP; null otherwise."},
            "take_profit": {**_NULLABLE_NUMBER, "description": "Absolute take-profit price for entries; null otherwise."},
            "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5], "description": "1 = marginal, 5 = exceptional."},
            "setup": {"type": "string", "description": "Short label for the pattern (2-4 words), or what you are waiting for when holding."},
            "reasoning": {"type": "string", "description": "At most about 150 words: what you see, why this action, what would change your mind."},
            "invalidation": {"type": "string", "description": "One concrete, observable condition that would prove this call wrong."},
        },
        "required": ["action", "order_type", "entry", "stop_loss", "take_profit", "confidence", "setup", "reasoning", "invalidation"],
    },
}


def system_prompt(cfg: Config) -> str:
    r, s, sig = cfg.risk, cfg.schedule, cfg.signal
    if sig.mode == "filter":
        mode_txt = (f"Filter mode is on: you may only open a trade in the direction of a flip that happened within the last "
                    f"{sig.filter_bars} closed bars. HOLD, CLOSE and MOVE_STOP are always allowed.")
    else:
        mode_txt = "Treat it as one input among many. You may use it or ignore it."
    return f"""You are an experienced discretionary EUR/USD trader running a small, disciplined book on a MetaTrader 5 demo account. Each time an M15 candle closes you receive a snapshot of the market and of your book, and you make one decision.

How you trade
- HOLD is the default. No trade beats a weak trade. Act only when the setup is clear, the location is good, and the reward clearly outweighs the risk.
- Read the timeframes together: H4 and H1 for context and levels, M15 for timing.
- Stay consistent with your recent reasoning unless something has actually changed, and say what changed when you change your view.
- Put the stop where the trade idea is proven wrong and the target at a level price can realistically reach.

Risk rules (enforced by a separate risk engine; an order that breaks one is rejected, and you see the rejection on the next bar)
- Risk per trade is {r.risk_per_trade_pct:.2f}% of equity. The engine sizes the position from your stop distance; you never choose the lot size.
- At most one open position or pending order at a time.
- Stop distance between {r.min_stop_atr:g}× and {r.max_stop_atr:g}× ATR(14) on M15.
- Reward:risk of at least {r.min_reward_risk:g}: take-profit distance divided by stop distance, measured from the executable price (the ask for longs, the bid for shorts, or your limit price).
- Spread at most {r.max_spread_pips:g} pips. At most {r.max_trades_per_day} entries per trading day; the trading day rolls over at 17:00 New York. After a losing trade, no new entry for {r.cooldown_bars_after_loss} bars.
- Daily loss limit {r.daily_loss_limit_pct:g}% of start-of-day equity: when it is hit the book is closed and trading halts until 17:00 New York.
- MOVE_STOP only tightens the stop, never loosens it, and leaves the take-profit unchanged.
- No new entries after Friday {s.friday_no_entries_utc} UTC; open positions are closed at Friday {s.friday_flat_utc} UTC. You are not asked for decisions around high-impact news.
- Limit orders expire after {r.limit_expiry_bars} bars.

The Trend Signals indicator
The snapshot includes an ATR trailing-stop trend indicator computed on closed M15 bars. In an uptrend a line sits {sig.multiplier:g}× ATR({sig.atr_length}) below the bar's midpoint and only moves up; in a downtrend it sits above and only moves down. The trend flips when a close crosses the previous bar's line. It is strong in sustained trends and prone to whipsaws in ranges. Its line is offered as a natural stop with 1R, 2R and 3R targets, and its last flips are listed with how the indicator alone would have done. {mode_txt}

Your answer
Call the submit_decision tool exactly once, with:
- action: OPEN_LONG, OPEN_SHORT, HOLD, CLOSE (closes the open position or cancels the pending order) or MOVE_STOP.
- order_type: MARKET or LIMIT for entries, null otherwise. entry: the limit price for LIMIT orders, null otherwise.
- stop_loss and take_profit: absolute prices with 5 decimals for entries. For MOVE_STOP, stop_loss is the new stop. Null when they don't apply.
- confidence: 1 (marginal) to 5 (exceptional). For HOLD, how sure you are that standing aside is right.
- setup: a short label for the pattern, such as "Asian range breakout" or "H1 pullback to EMA20". For HOLD, what you are waiting for.
- reasoning: at most about 150 words. What you see, why this action, and what would change your mind.
- invalidation: one concrete, observable condition that would prove this call wrong.

Reading the snapshot
- Candles are CSV in UTC, oldest first, and the time is the bar's open time. Every candle is closed; the bar still forming is never included.
- Prices are bid prices with 5 decimals. A pip is 0.0001. ATR values are given in pips."""


def prompt_hash(system_text: str) -> str:
    return hashlib.sha256((system_text + json.dumps(TOOL, sort_keys=True)).encode()).hexdigest()[:16]


def build_request(cfg: Config, system_text: str, h4_text: str, h1_text: str, live_text: str) -> dict:
    """Render order is tools -> system -> messages; the three 1-hour breakpoints sit on the stable prefix."""
    return {
        "model": cfg.model.model,
        "max_tokens": cfg.model.max_tokens,
        "system": [{"type": "text", "text": system_text, "cache_control": CACHE_1H}],
        "tools": [TOOL],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "output_config": {"effort": cfg.model.effort},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": h4_text, "cache_control": CACHE_1H},
                {"type": "text", "text": h1_text, "cache_control": CACHE_1H},
                {"type": "text", "text": live_text},
            ],
        }],
    }


@dataclass
class LLMResult:
    status: str  # ok | no_decision | refusal | max_tokens | timeout | error | invalid
    decision: Decision | None = None
    raw: dict | None = None
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str | None = None
    stop_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.decision is not None

    def hold_reason(self) -> str:
        return {
            "no_decision": "Opus did not call submit_decision",
            "refusal": "Opus declined to answer" + (f" ({self.error})" if self.error else ""),
            "max_tokens": "Opus ran out of output tokens before deciding",
            "timeout": "The Opus call timed out",
            "error": f"The Opus call failed: {self.error}",
            "invalid": f"Opus returned an invalid decision: {self.error}",
        }.get(self.status, self.status)


def usage_dict(usage: Any) -> dict:
    if usage is None:
        return {}
    cc = getattr(usage, "cache_creation", None)
    w1h = getattr(cc, "ephemeral_1h_input_tokens", None) if cc is not None else None
    w5m = getattr(cc, "ephemeral_5m_input_tokens", None) if cc is not None else None
    total_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if w1h is None and w5m is None:
        w1h, w5m = total_write, 0
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": total_write,
        "cache_write_1h_tokens": w1h or 0,
        "cache_write_5m_tokens": w5m or 0,
    }


def cost_usd(u: dict, cfg: Config) -> float:
    m = cfg.model
    return round((u.get("input_tokens", 0) * m.price_input_per_mtok
                  + u.get("output_tokens", 0) * m.price_output_per_mtok
                  + u.get("cache_read_tokens", 0) * m.price_cache_read_per_mtok
                  + u.get("cache_write_1h_tokens", 0) * m.price_cache_write_1h_per_mtok
                  + u.get("cache_write_5m_tokens", 0) * m.price_input_per_mtok * 1.25) / 1_000_000, 6)


def parse_response(resp: Any, cfg: Config, latency_ms: int) -> LLMResult:
    raw = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else dict(resp)
    u = usage_dict(getattr(resp, "usage", None))
    base = dict(raw=raw, usage=u, cost_usd=cost_usd(u, cfg), latency_ms=latency_ms, model=getattr(resp, "model", None), stop_reason=getattr(resp, "stop_reason", None))
    stop = base["stop_reason"]
    if stop == "refusal":
        details = getattr(resp, "stop_details", None)
        category = getattr(details, "category", None) if details is not None else None
        return LLMResult("refusal", error=category, **base)
    tool_input = None
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_decision":
            tool_input = block.input
            break
    if tool_input is None:
        return LLMResult("max_tokens" if stop == "max_tokens" else "no_decision", **base)
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except json.JSONDecodeError as exc:
            return LLMResult("invalid", error=f"tool input is not JSON ({exc})", **base)
    try:
        decision = Decision.from_dict(tool_input)
    except (ValueError, TypeError) as exc:
        return LLMResult("invalid", error=str(exc), **base)
    if decision.action in ("OPEN_LONG", "OPEN_SHORT") and decision.order_type is None:
        decision.order_type = "MARKET"
    return LLMResult("ok", decision, **base)


class OpusClient:
    """Calls the Claude API. Never raises: failures come back as an LLMResult."""

    def __init__(self, api_key: str | None = None):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()

    async def decide(self, request: dict, cfg: Config) -> LLMResult:
        a = self._anthropic
        t0 = time.monotonic()
        timeout = float(cfg.model.timeout_s)
        try:
            # timeout_s bounds the whole call, including the SDK's one retry.
            resp = await asyncio.wait_for(
                self.client.with_options(timeout=timeout, max_retries=1).messages.create(**request),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, a.APITimeoutError):
            return LLMResult("timeout", latency_ms=int((time.monotonic() - t0) * 1000), error=f"no answer within {int(timeout)} s")
        except a.AuthenticationError:
            return LLMResult("error", latency_ms=int((time.monotonic() - t0) * 1000), error="invalid ANTHROPIC_API_KEY")
        except a.RateLimitError:
            return LLMResult("error", latency_ms=int((time.monotonic() - t0) * 1000), error="rate limited (429)")
        except a.APIStatusError as exc:
            return LLMResult("error", latency_ms=int((time.monotonic() - t0) * 1000), error=f"HTTP {exc.status_code}: {getattr(exc, 'message', exc)}")
        except a.APIConnectionError as exc:
            return LLMResult("error", latency_ms=int((time.monotonic() - t0) * 1000), error=f"network error: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad call never stops the loop
            log.exception("Unexpected error calling the model")
            return LLMResult("error", latency_ms=int((time.monotonic() - t0) * 1000), error=f"{type(exc).__name__}: {exc}")
        return parse_response(resp, cfg, int((time.monotonic() - t0) * 1000))


# ---------------------------------------------------------------- fake model for tests and --sim

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, mode: str = "json") -> dict:
        def conv(v):
            if isinstance(v, _Obj):
                return v.model_dump()
            if isinstance(v, list):
                return [conv(x) for x in v]
            return v

        return {k: conv(v) for k, v in self.__dict__.items()}


def fake_response(tool_input: dict | None, stop_reason: str = "tool_use", model: str = "sim-scripted",
                  usage: dict | None = None, category: str | None = None) -> _Obj:
    u = usage or {"input_tokens": 3900, "output_tokens": 1450, "cache_read_input_tokens": 3600, "cache_creation_input_tokens": 0}
    content = [_Obj(type="thinking", thinking="", signature="")]
    if tool_input is not None:
        content.append(_Obj(type="tool_use", id="toolu_sim", name="submit_decision", input=tool_input))
    return _Obj(id="msg_sim", type="message", role="assistant", model=model, content=content, stop_reason=stop_reason,
                stop_details=_Obj(type="refusal", category=category, explanation=None) if stop_reason == "refusal" else None,
                usage=_Obj(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                           cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
                           cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
                           cache_creation=_Obj(ephemeral_1h_input_tokens=u.get("cache_creation_input_tokens", 0), ephemeral_5m_input_tokens=0)))


class FakeLLM:
    """Stand-in for OpusClient.

    With `script`, returns the scripted items in order (a dict = tool input,
    "timeout" / "error" / "refusal" / "no_tool" = that failure, an Exception
    instance is raised). Without a script it plays a simple rule-based trader
    on the snapshot so --sim produces realistic-looking activity.
    """

    def __init__(self, script: list | None = None, latency_s: tuple[float, float] = (0.0, 0.0), seed: int = 11):
        self.script = list(script) if script is not None else None
        self.latency_s = latency_s
        self.calls: list[dict] = []
        self.rng = random.Random(seed)
        self.snapshot_provider = None  # set by the engine in --sim: () -> snapshot data dict

    async def decide(self, request: dict, cfg: Config) -> LLMResult:
        self.calls.append(request)
        lo, hi = self.latency_s
        delay = self.rng.uniform(lo, hi) if hi > 0 else 0.0
        if delay:
            await asyncio.sleep(delay)
        latency = int(delay * 1000) or 1
        if self.script is not None:
            if not self.script:
                item = {"action": "HOLD", "order_type": None, "entry": None, "stop_loss": None, "take_profit": None,
                        "confidence": 3, "setup": "No clear edge", "reasoning": "Nothing to do.", "invalidation": "n/a"}
            else:
                item = self.script.pop(0)
            if callable(item):
                item = item(self.snapshot_provider() if self.snapshot_provider else {})
            if isinstance(item, Exception):
                raise item
            if item == "timeout":
                return LLMResult("timeout", latency_ms=latency, error="no answer within the timeout")
            if item == "error":
                return LLMResult("error", latency_ms=latency, error="HTTP 529: overloaded")
            if item == "refusal":
                return parse_response(fake_response(None, "refusal", category="cyber"), cfg, latency)
            if item == "no_tool":
                return parse_response(fake_response(None, "end_turn"), cfg, latency)
            return parse_response(fake_response(item), cfg, latency)
        data = self.snapshot_provider() if self.snapshot_provider else {}
        return parse_response(fake_response(self._rule_based(data)), cfg, latency)

    def _rule_based(self, d: dict) -> dict:
        hold = {"action": "HOLD", "order_type": None, "entry": None, "stop_loss": None, "take_profit": None, "invalidation": "A clean break with momentum would change the picture."}
        sig = d.get("signal") or {}
        m15 = (d.get("indicators") or {}).get("M15") or {}
        mkt = d.get("market") or {}
        pos = d.get("position")
        rsi = m15.get("rsi14") or 50
        pip = d.get("pip", 0.0001)
        bid, ask = mkt.get("bid"), mkt.get("ask")
        rng = self.rng
        if pos:
            line = sig.get("line")
            against = (pos["side"] == "long" and sig.get("trend") == "down") or (pos["side"] == "short" and sig.get("trend") == "up")
            if against:
                return {**hold, "action": "CLOSE", "confidence": 4, "setup": "Trend flipped",
                        "reasoning": f"The Trend Signals line flipped against the {pos['side']} and M15 momentum faded (RSI {rsi}). Taking the trade off rather than waiting for the stop.",
                        "invalidation": "A close back through the line would have kept the trade valid."}
            if line and ((pos["side"] == "long" and line > (pos.get("sl") or 0) + 2 * pip) or (pos["side"] == "short" and line < (pos.get("sl") or 9) - 2 * pip)) and rng.random() < 0.5:
                return {**hold, "action": "MOVE_STOP", "stop_loss": round(line, 5), "confidence": 3, "setup": "Trail to signal line",
                        "reasoning": f"Price has moved in favour; trailing the stop to the indicator line at {line:.5f} locks in part of the move while leaving room for the trend.",
                        "invalidation": "A close beyond the new stop."}
            return {**hold, "confidence": 3, "setup": "Managing position",
                    "reasoning": f"The {pos['side']} is working; structure intact and RSI {rsi} is not stretched. Leaving the stop and target where they are.",
                    "invalidation": "A close beyond the signal line."}
        if sig.get("flipped_now") and bid and ask:
            side = "long" if sig.get("flip_now") == "BUY" else "short"
            line = sig["line"]
            entry = ask if side == "long" else bid
            dist = abs(entry - line)
            atr = (m15.get("atr14") or 0.001)
            # Occasionally propose a stop that is too tight so the risk engine's rejections show up in --sim.
            if rng.random() < 0.15:
                dist = 0.3 * atr
            stop = entry - dist if side == "long" else entry + dist
            tp = entry + 2 * dist if side == "long" else entry - 2 * dist
            setup = rng.choice(["Signal flip with momentum", "Range breakout", "Pullback continuation"])
            cloud = (sig.get("cloud") or {}).get("price_vs_cloud", "inside")
            return {"action": "OPEN_LONG" if side == "long" else "OPEN_SHORT", "order_type": "MARKET", "entry": None,
                    "stop_loss": round(stop, 5), "take_profit": round(tp, 5), "confidence": rng.choice([3, 3, 4, 4, 5]), "setup": setup,
                    "reasoning": (f"The indicator just flipped {sig.get('flip_now')} and price is {cloud} the cloud. "
                                  f"H1 structure supports a {side}, RSI {rsi} leaves room, and the signal line gives a clean stop "
                                  f"{dist / pip:.1f} pips away. Targeting 2R at {tp:.5f}."),
                    "invalidation": f"A close back through {line:.5f}."}
        reason = rng.choice([
            f"No clear edge. Price is chopping around the M15 EMAs with RSI {rsi}; waiting for a decisive break.",
            f"Trend is {sig.get('trend', 'unclear')} but price is extended from the signal line; a pullback would offer a better entry.",
            "Spread and volatility are fine, but the H1 picture is mixed. Standing aside until the session gives direction.",
        ])
        return {**hold, "confidence": rng.choice([2, 3, 3, 4]), "setup": rng.choice(["No clear edge", "Waiting for pullback", "Range-bound"]), "reasoning": reason}
