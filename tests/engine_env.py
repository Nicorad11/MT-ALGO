"""A full engine wired to the fake terminal and a scripted model, for engine tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from opus_trader.clock import SimClock
from opus_trader.config import ConfigManager
from opus_trader.engine import Engine
from opus_trader.fake_mt5 import FakeMT5, major_market, major_spec, synthetic_market
from opus_trader.llm import FakeLLM
from opus_trader.mt5_gateway import MT5Gateway
from opus_trader.news import NewsCalendar, YamlEventSource
from opus_trader.store import Store

UTC = timezone.utc
MAGIC = 551155
# Wednesday 14 Oct 2026, 10:00:05 UTC: the 09:45 bar has just closed.
NOW = datetime(2026, 10, 14, 10, 0, 5, tzinfo=UTC)


@lru_cache(maxsize=16)
def market(seed: int = 3, name: str = "EURUSD"):
    if name == "EURUSD":
        return synthetic_market(NOW - timedelta(days=200), NOW + timedelta(days=12), seed=seed)
    return major_market(name, NOW - timedelta(days=200), NOW + timedelta(days=12), seed=seed)


class Env:
    def __init__(self, tmp: Path, script=None, now: datetime = NOW, dry_run: bool = True, store: Store | None = None, events: str = "events: []\n",
                 server_tz: str = "ny7", symbols: tuple[str, ...] = ("EURUSD",)):
        self.tmp = tmp
        (tmp / "events.yaml").write_text(events)
        self.clock = SimClock(now)
        self.fake = FakeMT5(self.clock, market(), server_tz=server_tz)
        for name in symbols:
            if name != "EURUSD":
                self.fake.add_symbol(major_spec(name), market(name=name))
        self.gw = MT5Gateway(self.fake, list(symbols), MAGIC)
        self.store = store or Store(":memory:")
        self.config = ConfigManager(persist=False, store=self.store)
        self.config.update({"app.symbols": list(symbols)}, allow_locked=True)
        if not dry_run:
            self.config.update({"app.dry_run": False}, allow_locked=True)
        self.llm = FakeLLM(script=script if script is not None else [])
        self.news = NewsCalendar(YamlEventSource(tmp / "events.yaml"), ["USD", "EUR"])
        self.engine = Engine(config=self.config, gateway=self.gw, llm=self.llm, store=self.store, news=self.news, clock=self.clock, root=tmp)
        self.loop = asyncio.new_event_loop()
        self.run(self.engine.start(run_tasks=False))

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def bars(self) -> list[int]:
        """Process the bar that just closed on every pair; the decision ids."""
        bar_open = self.clock.now().replace(second=0, microsecond=0)
        bar_open -= timedelta(minutes=bar_open.minute % 15 + 15)
        return self.run(self.engine.safe_process_bar(bar_open, wait=False))

    def bar(self):
        """Process the bar that just closed; with one pair, its decision id (None if nothing was decided)."""
        ids = self.bars()
        return ids[0] if len(ids) == 1 else (ids or None)

    def advance(self, minutes: float):
        self.clock.advance(minutes * 60)

    def decision(self, decision_id):
        return self.store.one("SELECT * FROM decisions WHERE id=?", (decision_id,))

    def close(self):
        self.loop.run_until_complete(self.engine.shutdown())
        self.loop.close()


def entry(side: str, stop_atr: float = 1.5, rr: float = 2.0, setup: str = "Test setup"):
    """A scripted decision that computes its prices from the snapshot the model sees."""

    def make(snapshot: dict) -> dict:
        m = snapshot["market"]
        atr = snapshot["indicators"]["M15"]["atr14"]
        price = m["ask"] if side == "long" else m["bid"]
        dist = atr * stop_atr
        sl = price - dist if side == "long" else price + dist
        tp = price + rr * dist if side == "long" else price - rr * dist
        return {"action": "OPEN_LONG" if side == "long" else "OPEN_SHORT", "order_type": "MARKET", "entry": None,
                "stop_loss": round(sl, 5), "take_profit": round(tp, 5), "confidence": 4, "setup": setup,
                "reasoning": f"Scripted {side}.", "invalidation": "Scripted."}

    return make


HOLD = {"action": "HOLD", "order_type": None, "entry": None, "stop_loss": None, "take_profit": None, "confidence": 3,
        "setup": "No clear edge", "reasoning": "Waiting.", "invalidation": "A break."}
