from datetime import datetime, timedelta, timezone

import pytest

from opus_trader.clock import next_trading_day_start, trading_day_start
from opus_trader.store import Store

from .engine_env import Env, entry

UTC = timezone.utc


def add_closed_loss(store, mode, pnl, when):
    store.insert("trades", {"mode": mode, "symbol": "EURUSD", "ticket": int(when.timestamp()) + int(-pnl * 100), "side": "long", "open_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "open_price": 1.16, "volume": 0.1, "risk_eur": 50.0, "close_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "close_price": 1.159, "exit_reason": "sl", "pnl_eur": pnl, "r_multiple": pnl / 50, "status": "closed"})


def test_trading_day_follows_new_york_dst():
    summer = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # EDT
    winter = datetime(2026, 12, 14, 12, 0, tzinfo=UTC)  # EST
    assert trading_day_start(summer) == datetime(2026, 10, 13, 21, 0, tzinfo=UTC)
    assert next_trading_day_start(summer) == datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
    assert trading_day_start(winter) == datetime(2026, 12, 13, 22, 0, tzinfo=UTC)
    # Across the switch back to standard time (1 Nov 2026).
    assert next_trading_day_start(datetime(2026, 10, 31, 22, 0, tzinfo=UTC)) == datetime(2026, 11, 1, 22, 0, tzinfo=UTC)


def test_daily_loss_flattens_and_halts_until_17_new_york(tmp_path):
    env = Env(tmp_path, script=[entry("long")])
    try:
        env.bar()
        assert env.store.one("SELECT status FROM trades")["status"] == "open"
        # Two losing trades earlier today take the bot to about -2%.
        for i in range(2):
            add_closed_loss(env.store, "dry", -100.0, env.clock.now() - timedelta(hours=2, minutes=i))
        hit = env.run(env.engine.check_daily_loss())
        assert hit
        halt = env.engine.halt_state()
        assert halt["kind"] == "daily_limit" and "daily loss limit hit" in halt["reason"]
        assert halt["until"] == "2026-10-14T21:00:00Z"  # 17:00 New York (EDT)
        assert env.store.one("SELECT COUNT(*) n FROM trades WHERE status='open'")["n"] == 0
        assert env.engine.resume().startswith("The daily loss limit halt ends at 17:00 New York")
        env.advance(15)
        d = env.decision(env.bar())
        assert d["status"] == "SKIPPED" and d["skip_reason"].startswith("Halted: daily loss limit hit")
        # After 17:00 New York the watchdog lifts the halt.
        env.clock.set(datetime(2026, 10, 14, 21, 0, 30, tzinfo=UTC))
        env.run(env.engine.watch_once())
        assert env.engine.halt_state() is None
    finally:
        env.close()


def test_halt_survives_restart(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    env = Env(tmp_path, store=store)
    try:
        for i in range(3):
            add_closed_loss(store, "dry", -80.0, env.clock.now() - timedelta(hours=1, minutes=i))
        assert env.run(env.engine.check_daily_loss())
    finally:
        env.close()
    env2 = Env(tmp_path, store=Store(tmp_path / "db.sqlite"))
    try:
        assert env2.engine.halt_state()["kind"] == "daily_limit"
        assert env2.engine.status()["status"] == "halted"
    finally:
        env2.close()


def test_losses_before_today_do_not_count(tmp_path):
    env = Env(tmp_path)
    try:
        add_closed_loss(env.store, "dry", -500.0, datetime(2026, 10, 13, 20, 0, tzinfo=UTC))  # before 17:00 NY yesterday
        assert not env.run(env.engine.check_daily_loss())
    finally:
        env.close()


def test_manual_trades_do_not_count(tmp_path):
    env = Env(tmp_path, dry_run=False)
    try:
        bid = env.fake.symbol_info_tick("EURUSD").bid
        env.fake.add_manual_position(env.fake.POSITION_TYPE_BUY, 5.0, bid + 0.0100)  # -500 pips x 5 lots on a manual trade
        assert env.fake.account_info().profit < -2000
        pnl, sod = env.run(env.engine.day_pnl())
        assert pnl == 0.0
        assert not env.run(env.engine.check_daily_loss())
        assert env.fake.positions  # the manual trade is untouched
    finally:
        env.close()


def test_cooldown_after_loss_blocks_entries(tmp_path):
    env = Env(tmp_path, script=[entry("long")])
    try:
        add_closed_loss(env.store, "dry", -50.0, env.clock.now() - timedelta(minutes=10))
        d = env.decision(env.bar())
        assert d["verdict"] == "REJECTED" and d["verdict_reason"] == "Cooldown after a loss on EURUSD: 2 bars left"
        assert "cooldown after a loss on EURUSD, 2 bars left" in d["prompt_text"]
    finally:
        env.close()
