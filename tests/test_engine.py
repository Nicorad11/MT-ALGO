import json
from datetime import datetime, timedelta, timezone

import pytest

from .engine_env import HOLD, MAGIC, Env, entry

UTC = timezone.utc


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def test_full_dry_run_cycle(tmp_path):
    env = Env(tmp_path, script=[entry("long")])
    try:
        did = env.bar()
        d = env.decision(did)
        assert d["status"] == "CALLED" and d["action"] == "OPEN_LONG" and d["verdict"] == "APPROVED", d["verdict_reason"]
        assert d["mode"] == "dry" and d["bar_time"] == "2026-10-14 09:45"
        assert d["prompt_text"].startswith("## H4 candles") and "Decide for the M15 bar that closed at 2026-10-14 10:00 UTC" in d["prompt_text"]
        assert json.loads(d["snapshot_json"])["signal"]["trend"] in ("up", "down")
        assert d["signal_agreement"] in ("agree", "disagree")
        assert d["cost_usd"] > 0 and d["input_tokens"] > 0
        # Dry run: order_check only, never order_send.
        assert env.fake.sent == [] and len(env.fake.checked) == 1
        trade = env.store.one("SELECT * FROM trades WHERE mode='dry'")
        assert trade["status"] == "open" and trade["decision_id"] == did and trade["setup"] == "Test setup"
        assert env.store.one("SELECT COUNT(*) n FROM equity")["n"] == 1
        assert env.store.get_state("bench_start") == "2026-10-14 09:45"
        orders = env.store.query("SELECT purpose, ok FROM orders WHERE decision_id=?", (did,))
        assert [o["purpose"] for o in orders] == ["check", "open"] and all(o["ok"] for o in orders)
    finally:
        env.close()


def test_live_cycle_sends_one_order_with_magic(tmp_path):
    env = Env(tmp_path, script=[entry("short")], dry_run=False)
    try:
        did = env.bar()
        assert env.decision(did)["verdict"] == "APPROVED"
        assert len(env.fake.sent) == 1
        req = env.fake.sent[0]
        assert req["magic"] == MAGIC and req["sl"] > req["price"] > req["tp"]
        trade = env.store.one("SELECT * FROM trades WHERE mode='live'")
        assert trade["decision_id"] == did and trade["side"] == "short" and trade["risk_eur"] > 0
    finally:
        env.close()


def test_rejection_is_fed_back_next_bar(tmp_path):
    env = Env(tmp_path, script=[entry("long", stop_atr=0.3), HOLD])
    try:
        d1 = env.decision(env.bar())
        assert d1["verdict"] == "REJECTED" and d1["verdict_reason"] == "Stop is 0.3× ATR; minimum is 0.5×"
        env.advance(15)
        d2 = env.decision(env.bar())
        assert "## Rejection on the previous bar" in d2["prompt_text"]
        assert "Stop is 0.3× ATR; minimum is 0.5×" in d2["prompt_text"]
        env.advance(15)
        env.llm.script.append(HOLD)
        d3 = env.decision(env.bar())
        assert "## Rejection on the previous bar" not in d3["prompt_text"]
    finally:
        env.close()


@pytest.mark.parametrize("failure", ["timeout", "error", "refusal", "no_tool"])
def test_model_failure_is_hold(tmp_path, failure):
    env = Env(tmp_path, script=[failure])
    try:
        d = env.decision(env.bar())
        assert d["action"] == "HOLD" and d["verdict"] == "NO_DECISION" and d["verdict_reason"].endswith("treated as HOLD")
        assert env.store.one("SELECT COUNT(*) n FROM trades")["n"] == 0
    finally:
        env.close()


def test_one_bad_bar_does_not_stop_the_next(tmp_path):
    env = Env(tmp_path, script=[RuntimeError("boom"), HOLD])
    try:
        assert env.bar() is None
        err = env.store.one("SELECT * FROM decisions ORDER BY id DESC LIMIT 1")
        assert err["verdict"] == "ERROR" and "boom" in err["verdict_reason"]
        assert env.store.one("SELECT * FROM engine_events WHERE kind='bar_error'")
        env.advance(15)
        d = env.decision(env.bar())
        assert d["verdict"] == "HOLD"
    finally:
        env.close()


def test_spread_too_wide_skips_model_call(env):
    env.fake.price_override = (1.16000, 1.16021)  # 2.1 pips
    d = env.decision(env.bar())
    assert d["status"] == "SKIPPED" and d["skip_reason"] == "Spread 2.1 pips > 1.5 max"
    assert env.llm.calls == []


def test_news_blackout_skips_model_call(tmp_path):
    events = "events:\n  - time: \"2026-10-14T10:20:00Z\"\n    currency: USD\n    impact: high\n    title: CPI\n"
    env = Env(tmp_path, events=events)
    try:
        d = env.decision(env.bar())
        assert d["skip_reason"] == "News blackout: USD CPI at 10:20 UTC"
        assert env.llm.calls == []
    finally:
        env.close()


def test_weekend_window_skips(tmp_path):
    env = Env(tmp_path, now=datetime(2026, 10, 16, 19, 30, 5, tzinfo=UTC))  # Friday 19:30 UTC
    try:
        d = env.decision(env.bar())
        assert d["skip_reason"].startswith("Weekend window")
    finally:
        env.close()


def test_algo_trading_off_pauses(env):
    env.fake.terminal_trade_allowed = False
    env.run(env.engine.watch_once())
    assert env.engine.status()["status"] == "paused"
    assert any("Algo Trading is off" in b["text"] for b in env.engine.banners())
    d = env.decision(env.bar())
    assert d["skip_reason"] == "Algo Trading is off in MT5"


def test_disconnect_and_reconnect_with_backoff(env):
    env.fake.ipc_ok = False
    env.run(env.engine.watch_once())
    st = env.engine.status()
    assert st["status"] == "disconnected"
    assert env.engine.rt.reconnect_attempt == 1
    env.fake.ipc_ok = True
    env.run(env.engine.watch_once())  # still inside the 2 s backoff
    assert env.engine.status()["status"] == "disconnected"
    env.advance(3 / 60)
    env.run(env.engine.watch_once())
    assert env.engine.status()["status"] == "running"


def test_kill_switch_file_closes_bot_trades_only(tmp_path):
    env = Env(tmp_path, script=[entry("long")], dry_run=False)
    try:
        env.bar()
        manual = env.fake.add_manual_position(env.fake.POSITION_TYPE_SELL, 2.0, 1.17000)
        (tmp_path / "STOP").write_text("")
        env.run(env.engine.watch_once())
        assert env.engine.halt_state()["kind"] == "kill_switch"
        assert set(env.fake.positions) == {manual}
        assert env.engine.resume() == "Delete the STOP file in the Opus Trader folder to resume."
        (tmp_path / "STOP").unlink()
        assert env.engine.resume() is None
        assert env.engine.halt_state() is None
    finally:
        env.close()


def test_stop_button_flattens_and_halts(tmp_path):
    env = Env(tmp_path, script=[entry("short"), entry("long")])
    try:
        env.bar()
        summary = env.run(env.engine.kill("Stopped from the app", "button"))
        assert summary == {"closed": 1, "cancelled": 0, "failed": 0}
        t = env.store.one("SELECT * FROM trades")
        assert t["status"] == "closed" and t["exit_reason"] == "kill_switch"
        env.advance(15)
        d = env.decision(env.bar())
        assert d["status"] == "SKIPPED" and d["skip_reason"].startswith("Halted")
    finally:
        env.close()


def test_broker_stop_loss_reconciled(tmp_path):
    env = Env(tmp_path, script=[entry("long")], dry_run=False)
    try:
        env.bar()
        pos = next(iter(env.fake.positions.values()))
        env.fake.price_override = (round(pos.sl - 0.0002, 5), round(pos.sl - 0.0001, 5))
        env.advance(1)
        env.run(env.engine.watch_once())
        t = env.store.one("SELECT * FROM trades WHERE mode='live'")
        assert t["status"] == "closed" and t["exit_reason"] == "sl"
        assert t["r_multiple"] == pytest.approx(-1.0, abs=0.05)
    finally:
        env.close()


def test_account_switched_to_real_blocks_engine(env):
    env.fake.trade_mode = env.fake.ACCOUNT_TRADE_MODE_REAL
    env.run(env.engine.watch_once())
    st = env.engine.status()
    assert st["status"] == "blocked" and "real-money" in st["blocked"]
    assert env.bar() is None
    assert env.engine.resume() == st["blocked"]


def test_move_stop_only_tightens_end_to_end(tmp_path):
    def tighten(snapshot):
        p = snapshot["position"]
        return {**HOLD, "action": "MOVE_STOP", "stop_loss": round(p["sl"] + 0.0002, 5), "setup": "Trail"} if p else HOLD

    def loosen(snapshot):
        p = snapshot["position"]
        return {**HOLD, "action": "MOVE_STOP", "stop_loss": round(p["sl"] - 0.0005, 5), "setup": "Trail"} if p else HOLD

    env = Env(tmp_path, script=[entry("long", stop_atr=2.9, rr=3.0), tighten, loosen])
    try:
        env.bar()
        sl0 = env.store.one("SELECT sl FROM trades")["sl"]
        env.fake.price_override = None
        env.advance(15)
        d = env.decision(env.bar())
        if d["action"] == "HOLD":
            pytest.skip("the synthetic market closed the position before the second bar")
        assert d["verdict"] == "APPROVED", d["verdict_reason"]
        sl1 = env.store.one("SELECT sl FROM trades")["sl"]
        assert sl1 == pytest.approx(sl0 + 0.0002)
        env.advance(15)
        d = env.decision(env.bar())
        if d["action"] == "MOVE_STOP":
            assert d["verdict"] == "REJECTED" and d["verdict_reason"].startswith("Stops only tighten")
            assert env.store.one("SELECT sl FROM trades")["sl"] == pytest.approx(sl1)
    finally:
        env.close()


def test_benchmark_never_touches_mt5(tmp_path):
    env = Env(tmp_path, script=[HOLD] * 8, dry_run=False)
    try:
        for _ in range(8):
            env.bar()
            env.advance(15)
        assert env.fake.sent == []
        assert env.store.one("SELECT COUNT(*) n FROM bench_equity")["n"] >= 1
    finally:
        env.close()
