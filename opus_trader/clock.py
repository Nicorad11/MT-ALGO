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


@dataclass
class OffsetModel:
    mode: str  # "ny7" or "fixed"
    seconds: int = 0  # used when mode == "fixed"
    detected: bool = False  # False when assumed (no fresh tick seen yet)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "seconds": self.seconds, "detected": self.detected}

    @classmethod
    def from_dict(cls, d: dict | None) -> "OffsetModel":
        if not d:
            return cls("ny7", 0, False)
        return cls(d.get("mode", "ny7"), int(d.get("seconds", 0)), bool(d.get("detected", False)))


class ServerTime:
    def __init__(self, model: OffsetModel | None = None):
        self.model = model or OffsetModel("ny7", 0, False)
        self._last_tick_time: int | None = None

    def offset_at(self, t_utc: datetime) -> int:
        if self.model.mode == "ny7":
            return ny7_offset_s(t_utc)
        return self.model.seconds

    def current_offset(self, now_utc: datetime) -> int:
        return self.offset_at(now_utc)

    def observe_tick(self, tick_time: int, now_utc: datetime) -> OffsetModel | None:
        """Feed the latest tick's server time. Returns a new model if it changed.

        A tick is trusted when the implied offset matches the NY+7 convention
        (a stale tick would not land exactly on it), or when the tick time has
        advanced since the previous observation (the market is live).
        """
        if tick_time is None or tick_time <= 0:
            return None
        fresh = self._last_tick_time is not None and tick_time > self._last_tick_time
        self._last_tick_time = tick_time
        raw = tick_time - now_utc.timestamp()
        candidate = int(round(raw / HALF_HOUR)) * HALF_HOUR
        if abs(candidate) > MAX_OFFSET_S or abs(raw - candidate) > MAX_TICK_SKEW_S:
            return None
        matches_ny7 = candidate == ny7_offset_s(now_utc)
        if not (matches_ny7 or fresh):
            return None
        new = OffsetModel("ny7", 0, True) if matches_ny7 else OffsetModel("fixed", candidate, True)
        if (new.mode, new.seconds) != (self.model.mode, self.model.seconds) or not self.model.detected:
            self.model = new
            return new
        return None

    def to_utc(self, server_epoch: float) -> datetime:
        if self.model.mode == "fixed":
            return datetime.fromtimestamp(server_epoch - self.model.seconds, UTC)
        for guess in (3 * 3600, 2 * 3600):
            t = datetime.fromtimestamp(server_epoch - guess, UTC)
            if ny7_offset_s(t) == guess:
                return t
        return datetime.fromtimestamp(server_epoch - 2 * 3600, UTC)

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
