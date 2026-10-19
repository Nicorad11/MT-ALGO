"""Broker differences the simulator's defaults don't show: symbol suffixes and server clocks."""

from datetime import datetime, timedelta, timezone

import pytest

from opus_trader.clock import OffsetModel, ServerTime
from opus_trader.fake_mt5 import FakeMT5, SymbolSpec, flat_market
from opus_trader.mt5_gateway import MT5Gateway, SafetyError
from opus_trader.risk import Plan

from .conftest import MAGIC, T0
from .engine_env import HOLD, Env

UTC = timezone.utc


def terminal(clock, name="EURUSD", server_tz="ny7"):
    f = FakeMT5(clock, flat_market(T0 - timedelta(days=2), days=5), spec=SymbolSpec(name=name), server_tz=server_tz)
    f.price_override = (1.16000, 1.16008)
    return f


# ---------------------------------------------------------------- symbol names

def test_suffixed_symbol_is_found_and_used(clock):
    f = terminal(clock, "EURUSD.pro")
    gw = MT5Gateway(f, "EURUSD", MAGIC)
    gw.connect()
    assert gw.symbol == "EURUSD.pro"
    from opus_trader.clock import ServerTime as ST
    from opus_trader.execution import LiveExecutor

    live = LiveExecutor(gw, __import__("opus_trader.store", fromlist=["Store"]).Store(":memory:"), MAGIC, lambda: 10, ST(), gate=lambda p: None)
    assert live.open(Plan("OPEN_MARKET", "long", 0.1, 1.16008, 1.15808, 1.16408), 1).ok
    assert f.sent[0]["symbol"] == "EURUSD.pro"
    assert [p.symbol for p in gw.bot_positions()] == ["EURUSD.pro"]


def test_ambiguous_symbol_stops_with_the_choices(clock, monkeypatch):
    f = terminal(clock, "EURUSD.pro")
    info = f.symbol_info.__func__
    monkeypatch.setattr(f, "symbols_get", lambda group=None: (
        info(f, "EURUSD.pro")._replace(name="EURUSD.pro", visible=False), info(f, "EURUSD.pro")._replace(name="EURUSD.ecn", visible=False)))
    with pytest.raises(SafetyError) as err:
        MT5Gateway(f, "EURUSD", MAGIC).connect()
    assert err.value.fatal and "EURUSD.ecn, EURUSD.pro" in err.value.message and "config.yaml" in err.value.message


def test_missing_symbol_stops_clearly(clock):
    f = terminal(clock, "GBPUSD")
    with pytest.raises(SafetyError) as err:
        MT5Gateway(f, "EURUSD", MAGIC).connect()
    assert "No symbol like EURUSD" in err.value.message


# ---------------------------------------------------------------- server clocks

def tick_at(now, offset_h):
    return int(now.timestamp() + offset_h * 3600)


def test_warsaw_clock_detected_and_dst_handled():
    st = ServerTime()
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)  # CEST, UTC+2 (New York + 7 would be +3)
    assert st.observe_tick(tick_at(now, 2), now) is None  # a single, possibly stale tick isn't enough
    m = st.observe_tick(tick_at(now, 2) + 1, now)  # the tick time advanced: the market is live
    assert (m.mode, m.tz, m.detected) == ("tz", "Europe/Warsaw", True)
    # History on both sides of the EU clock change (25 Oct 2026) converts correctly.
    before = int(datetime(2026, 10, 24, 12, 0, tzinfo=UTC).timestamp())  # server wall clock 12:00 CEST
    after = int(datetime(2026, 10, 26, 12, 0, tzinfo=UTC).timestamp())  # server wall clock 12:00 CET
    assert st.to_utc(before) == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    assert st.to_utc(after) == datetime(2026, 10, 26, 11, 0, tzinfo=UTC)


def test_new_york_plus_7_preferred_then_corrected_when_eu_dst_differs():
    st = ServerTime()
    oct15 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    m = st.observe_tick(tick_at(oct15, 3), oct15)  # +3 fits New York + 7 and Athens; NY+7 wins
    assert m.mode == "ny7"
    # 27 Oct: Europe has left summer time, New York hasn't. An Athens-clock broker now shows +2.
    oct27 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
    m = st.observe_tick(tick_at(oct27, 2), oct27)  # newer than the last tick seen, so it counts as live
    assert (m.mode, m.tz) == ("tz", "Europe/Athens")


def test_stale_tick_never_switches_the_model():
    st = ServerTime(OffsetModel("ny7", detected=True))
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    st.observe_tick(tick_at(now, 3), now)
    assert st.observe_tick(tick_at(now, 3) - 3600, now) is None  # an hour-old tick, not advancing
    assert st.model.mode == "ny7"


def test_engine_on_a_warsaw_clock_broker(tmp_path):
    env = Env(tmp_path, script=[HOLD], server_tz="Europe/Warsaw")
    try:
        assert env.engine.status()["status"] == "running"
        d = env.decision(env.bar())  # the terminal's clock is frozen here, so no live tick yet
        assert d["status"] == "SKIPPED" and d["skip_reason"] == "Waiting for a live tick to confirm the broker's server time"
        env.advance(20 / 60)  # the fake terminal's quotes move every 15 s
        env.run(env.engine.observe_tick())  # the watchdog does this every second
        assert env.engine.server_time.model.tz == "Europe/Warsaw"
        env.advance(15)
        d = env.decision(env.bar())
        assert d["status"] == "CALLED" and d["bar_time"] == "2026-10-14 10:00"
        assert "Decide on EURUSD for the M15 bar that closed at 2026-10-14 10:15 UTC" in d["prompt_text"]
    finally:
        env.close()
