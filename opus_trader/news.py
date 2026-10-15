"""High-impact event calendar for the news blackout. Pluggable: implement `EventSource`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import yaml

from .clock import parse_utc

log = logging.getLogger(__name__)
UTC = timezone.utc


@dataclass(frozen=True)
class Event:
    time: datetime
    currency: str
    impact: str
    title: str

    def to_dict(self, now: datetime | None = None) -> dict:
        d = {"time": self.time.strftime("%Y-%m-%dT%H:%M:%SZ"), "currency": self.currency, "impact": self.impact, "title": self.title}
        if now is not None:
            d["minutes_away"] = int((self.time - now).total_seconds() // 60)
        return d


class EventSource(Protocol):
    def events(self) -> list[Event]: ...


class YamlEventSource:
    """Reads events.yaml; re-reads when the file changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime: float | None = None
        self._cache: list[Event] = []
        self.error: str | None = None

    def events(self) -> list[Event]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._cache, self.error = [], f"{self.path.name} not found"
            return []
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                items = data.get("events") or []
                out = []
                for item in items:
                    out.append(Event(parse_utc(str(item["time"])), str(item.get("currency", "")).upper(), str(item.get("impact", "high")).lower(), str(item.get("title", "Event"))))
                self._cache = sorted(out, key=lambda e: e.time)
                self.error = None
            except Exception as exc:  # noqa: BLE001 - a bad file must not stop the bot
                log.warning("Could not read %s: %s", self.path, exc)
                self.error = f"Could not read {self.path.name}: {exc}"
        return self._cache


class NewsCalendar:
    def __init__(self, source: EventSource, currencies: list[str]):
        self.source = source
        self.currencies = {c.upper() for c in currencies}

    def high_impact(self) -> list[Event]:
        return [e for e in self.source.events() if e.impact == "high" and e.currency in self.currencies]

    def upcoming(self, now: datetime, hours: float = 4) -> list[Event]:
        end = now + timedelta(hours=hours)
        return [e for e in self.high_impact() if now <= e.time <= end]

    def next_event(self, now: datetime) -> Event | None:
        return next((e for e in self.high_impact() if e.time >= now), None)

    def blackout(self, now: datetime, before_min: int, after_min: int) -> Event | None:
        for e in self.high_impact():
            if e.time - timedelta(minutes=before_min) <= now <= e.time + timedelta(minutes=after_min):
                return e
        return None
