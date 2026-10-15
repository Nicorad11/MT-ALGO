"""Time handling: UTC everywhere internally.

MT5 reports broker *server* time as if it were a UTC epoch. Most MT5 brokers,
OANDA TMS included, run their servers on "New York + 7 h" so that the daily
candle closes at 17:00 New York; that is UTC+2 in winter and UTC+3 in summer.
`ServerTime` detects the offset from a live tick and, when it matches that
convention, converts every historical bar with the correct DST offset.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
HALF_HOUR = 1800
MAX_TICK_SKEW_S = 15 * 60
MAX_OFFSET_S = 14 * 3600


class Clock:
    """Wall clock. Tests and the simulator swap in `SimClock`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def epoch(self) -> float:
        return time.time()


class SimClock(Clock):
    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC)
        self._anchor_real: float | None = None
        self._anchor_sim: datetime | None = None

    def now(self) -> datetime:
        if self._anchor_real is not None:
            return self._anchor_sim + timedelta(seconds=time.time() - self._anchor_real)
        return self._now

    def epoch(self) -> float:
        return self.now().timestamp()

    def set(self, t: datetime) -> None:
        self._anchor_real = None
        self._now = t.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        self.set(self.now() + timedelta(seconds=seconds))

    def run_realtime(self) -> None:
        """Let simulated time flow with the wall clock from the current point."""
        self._anchor_sim = self.now()
        self._anchor_real = time.time()


def ny_utc_offset_s(t_utc: datetime) -> int:
    return int(t_utc.astimezone(NY).utcoffset().total_seconds())


def ny7_offset_s(t_utc: datetime) -> int:
    """Server offset under the "New York + 7 h" convention (+7200 or +10800)."""
    return ny_utc_offset_s(t_utc) + 7 * 3600


# Broker server clocks seen on MT5, in order of preference when several match the
# current offset: New York + 7 h (most brokers), then European zones with EU DST
# rules (OANDA TMS is a Warsaw-based broker), then London.
ZONE_MODELS = ("Europe/Athens", "Europe/Warsaw", "Europe/London")


def zone_offset_s(tz: str, t_utc: datetime) -> int:
    return int(t_utc.astimezone(ZoneInfo(tz)).utcoffset().total_seconds())


@dataclass
class OffsetModel:
    mode: str  # "ny7", "tz" (an IANA zone with its DST rules) or "fixed"
    seconds: int = 0  # used when mode == "fixed"
    detected: bool = False  # False when assumed (no fresh tick seen yet)
    tz: str | None = None  # used when mode == "tz"

    def to_dict(self) -> dict:
        return {"mode": self.mode, "seconds": self.seconds, "detected": self.detected, "tz": self.tz}

    @classmethod
    def from_dict(cls, d: dict | None) -> "OffsetModel":
        if not d:
            return cls("ny7", 0, False)
        return cls(d.get("mode", "ny7"), int(d.get("seconds", 0)), bool(d.get("detected", False)), d.get("tz"))

    def offset_at(self, t_utc: datetime) -> int:
        if self.mode == "ny7":
            return ny7_offset_s(t_utc)
        if self.mode == "tz" and self.tz:
            return zone_offset_s(self.tz, t_utc)
        return self.seconds

    def describe(self) -> str:
        if self.mode == "ny7":
            return "New York + 7 h (UTC+2 winter / UTC+3 summer, US DST dates)"
        if self.mode == "tz":
            return f"{self.tz} time (with its DST rules)"
        return f"fixed UTC{self.seconds / 3600:+g} h"

    def same(self, other: "OffsetModel") -> bool:
        return (self.mode, self.seconds, self.tz) == (other.mode, other.seconds, other.tz)


def candidate_models(offset_s: int, now_utc: datetime) -> list[OffsetModel]:
    """Known server-clock models whose offset right now equals `offset_s`, best first."""
    out = []
    if ny7_offset_s(now_utc) == offset_s:
        out.append(OffsetModel("ny7", 0, True))
    for tz in ZONE_MODELS:
        if zone_offset_s(tz, now_utc) == offset_s:
            out.append(OffsetModel("tz", 0, True, tz))
    return out


class ServerTime:
    def __init__(self, model: OffsetModel | None = None):
        self.model = model or OffsetModel("ny7", 0, False)
        self._last_tick_time: int | None = None

    def offset_at(self, t_utc: datetime) -> int:
        return self.model.offset_at(t_utc)

    def current_offset(self, now_utc: datetime) -> int:
        return self.offset_at(now_utc)

    def observe_tick(self, tick_time: int, now_utc: datetime) -> OffsetModel | None:
        """Feed the latest tick's server time. Returns the new model if it changed.

        The implied offset (rounded to 30 min) is matched against known broker
        clocks so DST is handled for history too. The current model is kept while
        it still matches. Switching to a different model needs a fresh tick (the
        tick time advanced since the last look, so the market is live), except
        for the very first detection when the offset is exactly New York + 7.
        """
        if tick_time is None or tick_time <= 0:
            return None
        fresh = self._last_tick_time is not None and tick_time > self._last_tick_time
        self._last_tick_time = tick_time
        raw = tick_time - now_utc.timestamp()
        candidate = int(round(raw / HALF_HOUR)) * HALF_HOUR
        if abs(candidate) > MAX_OFFSET_S or abs(raw - candidate) > MAX_TICK_SKEW_S:
            return None
        if self.model.offset_at(now_utc) == candidate:
            if self.model.detected:
                return None
            if fresh or self.model.mode == "ny7":
                self.model = OffsetModel(self.model.mode, self.model.seconds, True, self.model.tz)
                return self.model
            return None
        models = candidate_models(candidate, now_utc)
        first_ny7 = not self.model.detected and models and models[0].mode == "ny7"
        if not (fresh or first_ny7):
            return None
        self.model = models[0] if models else OffsetModel("fixed", candidate, True)
        return self.model

    def to_utc(self, server_epoch: float) -> datetime:
        if self.model.mode == "fixed":
            return datetime.fromtimestamp(server_epoch - self.model.seconds, UTC)
        # Server time is local time in the model's zone: try each plausible offset.
        # DST changes at most once a day, so the offsets in force 12 h either side cover every case.
        guesses = sorted({self.model.offset_at(datetime.fromtimestamp(server_epoch + d, UTC)) for d in (-12 * 3600, 0, 12 * 3600)})
        for guess in guesses:
            t = datetime.fromtimestamp(server_epoch - guess, UTC)
            if self.model.offset_at(t) == guess:
                return t
        return datetime.fromtimestamp(server_epoch - guesses[0], UTC)

    def to_server_epoch(self, t_utc: datetime) -> int:
        return int(t_utc.timestamp()) + self.offset_at(t_utc)


# ---------------------------------------------------------------- trading day

def trading_day_start(t_utc: datetime) -> datetime:
    """Most recent 17:00 New York at or before t (the forex day rollover)."""
    local = t_utc.astimezone(NY)
    start = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    # Re-resolve the wall time in case the previous day had a different DST offset.
    start = datetime(start.year, start.month, start.day, 17, 0, tzinfo=NY)
    return start.astimezone(UTC)


def next_trading_day_start(t_utc: datetime) -> datetime:
    local = trading_day_start(t_utc).astimezone(NY) + timedelta(days=1)
    return datetime(local.year, local.month, local.day, 17, 0, tzinfo=NY).astimezone(UTC)


def week_start(t_utc: datetime) -> datetime:
    """Sunday 17:00 New York that opened the current trading week."""
    start = trading_day_start(t_utc)
    local = start.astimezone(NY)
    # weekday(): Monday=0 ... Sunday=6. The trading week starts on Sunday 17:00.
    days_back = (local.weekday() - 6) % 7
    local = local - timedelta(days=days_back)
    return datetime(local.year, local.month, local.day, 17, 0, tzinfo=NY).astimezone(UTC)


def month_start(t_utc: datetime) -> datetime:
    return t_utc.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------- bars

def floor_bar(t_utc: datetime, minutes: int = 15) -> datetime:
    t = t_utc.astimezone(UTC).replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % minutes)


def last_closed_bar_open(t_utc: datetime, minutes: int = 15) -> datetime:
    """Open time of the bar that most recently closed at or before t."""
    return floor_bar(t_utc, minutes) - timedelta(minutes=minutes)


def next_bar_close(t_utc: datetime, minutes: int = 15) -> datetime:
    return floor_bar(t_utc, minutes) + timedelta(minutes=minutes)


# ---------------------------------------------------------------- sessions

def session_of(t_utc: datetime) -> str:
    """Approximate FX session for a UTC time (fixed UTC hours, DST ignored)."""
    h = t_utc.astimezone(UTC).hour
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 16:
        return "London/NY overlap"
    if 16 <= h < 21:
        return "New York"
    return "Asia"


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def weekend_window(t_utc: datetime, no_entries_fri: str, resume_sun: str) -> bool:
    """True from Friday `no_entries_fri` until Sunday `resume_sun` (UTC)."""
    t = t_utc.astimezone(UTC)
    wd = t.weekday()  # Mon=0 ... Sun=6
    hm = (t.hour, t.minute)
    if wd == 4:
        return hm >= parse_hhmm(no_entries_fri)
    if wd == 5:
        return True
    if wd == 6:
        return hm < parse_hhmm(resume_sun)
    return False


def friday_flat_due(t_utc: datetime, flat_fri: str, resume_sun: str) -> bool:
    """True from Friday `flat_fri` until the weekend ends."""
    t = t_utc.astimezone(UTC)
    wd = t.weekday()
    hm = (t.hour, t.minute)
    if wd == 4:
        return hm >= parse_hhmm(flat_fri)
    if wd == 5:
        return True
    if wd == 6:
        return hm < parse_hhmm(resume_sun)
    return False


def fmt_utc(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def fx_market_open(t_utc: datetime) -> bool:
    """FX trading hours: Sunday 17:00 New York to Friday 17:00 New York."""
    ny = t_utc.astimezone(NY)
    wd = ny.weekday()
    if wd == 5:
        return False
    if wd == 4 and ny.hour >= 17:
        return False
    if wd == 6 and ny.hour < 17:
        return False
    return True
