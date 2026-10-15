"""Preview mode (--sim): the full app against a fake terminal and a rule-based stand-in for Opus.

Everything lives under data/sim/ (its own database, config and events file),
so a preview never touches your real journal or config.yaml. The badge in
the top bar reads SIM instead of DEMO.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import SimClock, next_bar_close
from .config import Config, ConfigManager, save_config
from .fake_mt5 import FakeMT5, synthetic_market
from .llm import FakeLLM
from .mt5_gateway import MT5Gateway
from .news import NewsCalendar, YamlEventSource
from .store import Store

log = logging.getLogger(__name__)
UTC = timezone.utc


def write_sim_events(path: Path, now: datetime) -> None:
    def at(delta: timedelta) -> str:
        t = (now + delta).replace(second=0, microsecond=0)
        t -= timedelta(minutes=t.minute % 15)
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")

    path.write_text(
        "# Simulated events for --sim. Your real calendar is events.yaml in the project folder.\n"
        "events:\n"
        f"  - {{time: \"{at(timedelta(hours=2, minutes=14))}\", currency: USD, impact: high, title: CPI}}\n"
        f"  - {{time: \"{at(timedelta(days=1, hours=1))}\", currency: EUR, impact: high, title: ECB press conference}}\n"
        f"  - {{time: \"{at(timedelta(days=2, hours=3))}\", currency: USD, impact: high, title: Retail Sales}}\n",
        encoding="utf-8",
    )


class SimEnv:
    def __init__(self, root: Path, now: datetime | None = None, fresh: bool = True, seed: int = 7):
        self.dir = Path(root) / "data" / "sim"
        if fresh and self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        now = (now or datetime.now(UTC)).astimezone(UTC)
        self.clock = SimClock(now)
        self.real_now = now
        log.info("Building the synthetic market (a few seconds)...")
        self.market = synthetic_market(now - timedelta(days=240), now + timedelta(days=30), seed=seed)
        self.fake = FakeMT5(self.clock, self.market)
        self.store = Store(self.dir / "opus_trader.sqlite")
        cfg_path = self.dir / "config.yaml"
        if not cfg_path.exists():
            save_config(Config(), cfg_path)
        self.config = ConfigManager(cfg_path, self.store)
        events = self.dir / "events.yaml"
        write_sim_events(events, now)
        self.news = NewsCalendar(YamlEventSource(events), self.config.cfg.news.currencies)
        self.llm = FakeLLM(seed=seed)
        self.gateway = MT5Gateway(self.fake, self.config.cfg.app.symbol, self.config.cfg.app.magic)


async def seed_history(engine, env: SimEnv, days: float) -> int:
    """Fast-forward the real pipeline over the last `days` days of synthetic bars."""
    end = env.real_now
    start = end - timedelta(days=days)
    t = next_bar_close(start)
    env.clock.set(t)
    await engine.ensure_connected(force=True)
    n = 0
    while t <= end:
        env.clock.set(t + timedelta(seconds=engine.cfg.app.bar_delay_s))
        bar_open = t - timedelta(minutes=15)
        if await engine.safe_process_bar(bar_open, wait=False):
            n += 1
        t += timedelta(minutes=15)
    env.clock.set(end)
    env.clock.run_realtime()
    return n
