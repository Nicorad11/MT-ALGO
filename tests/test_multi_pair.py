"""Several pairs at once: one decision per pair, shared limits, per-pair news, migrations."""

import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from opus_trader.config import Config, MAJORS, load_config
from opus_trader.store import Store

from .engine_env import HOLD, NOW, Env, entry

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent.parent


def route(**by_symbol):
    """A scripted model call that answers per pair (HOLD for pairs not named)."""

    def make(snapshot: dict) -> dict:
        item = by_symbol.get(snapshot["symbol"], HOLD)
        return item(snapshot) if callable(item) else item

    return make


def decisions(env):
    return {d["symbol"]: d for d in env.store.query("SELECT * FROM decisions ORDER BY id")}


@pytest.fixture
def env3(tmp_path):
    e = Env(tmp_path, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    e.config.update({"model.max_concurrent_calls": 1})  # answers are checked in config order
    yield e
    e.close()


def test_each_pair_gets_its_own_decision(env3):
    env3.llm.script = [route()] * 3
    ids = env3.bars()
    assert len(ids) == 3
    ds = decisions(env3)
    assert set(ds) == {"EURUSD", "GBPUSD", "USDJPY"}
    assert all(d["status"] == "CALLED" and d["verdict"] == "HOLD" and d["bar_time"] == "2026-10-14 09:45" for d in ds.values())
    jpy = ds["USDJPY"]["prompt_text"]
    assert jpy.startswith("# USDJPY") and "Decide on USDJPY for the M15 bar" in jpy
    assert "3 decimals, 1 pip = 0.01" in jpy
    assert "5 decimals, 1 pip = 0.0001" in ds["GBPUSD"]["prompt_text"]
    # The system prompt is identical for every pair, so it is cached once.
    assert len({d["system_hash"] for d in ds.values()}) == 1
    assert env3.bars() == []  # already decided


def test_currency_limit_blocks_doubling_up(env3):
    env3.llm.script = [route(EURUSD=entry("long"), GBPUSD=entry("long"), USDJPY=entry("long"))] * 3
    env3.bars()
    ds = decisions(env3)
    assert ds["EURUSD"]["verdict"] == "APPROVED"
    # Long GBPUSD is short USD too: that would be two trades short USD.
    assert ds["GBPUSD"]["verdict"] == "REJECTED"
    assert ds["GBPUSD"]["verdict_reason"] == "Currency limit: a long would make 2 trades short USD (max 1): EURUSD long is already short USD"
    # Long USDJPY is long USD: it offsets the EURUSD trade, so it is allowed.
    assert ds["USDJPY"]["verdict"] == "APPROVED", ds["USDJPY"]["verdict_reason"]
    trades = {t["symbol"]: t for t in env3.store.query("SELECT * FROM trades WHERE status='open'")}
    assert set(trades) == {"EURUSD", "USDJPY"}
    # Sized to the same risk on a JPY pair (tick value converted to EUR).
    assert trades["USDJPY"]["risk_eur"] == pytest.approx(50.0, rel=0.05)
    assert trades["EURUSD"]["risk_eur"] == pytest.approx(50.0, rel=0.05)


def test_snapshot_shows_the_rest_of_the_book(env3):
    env3.llm.script = [route(EURUSD=entry("long"))] * 3
    env3.bars()
    env3.advance(15)
    env3.llm.script = [route()] * 3
    env3.bars()
    gbp = env3.store.one("SELECT * FROM decisions WHERE symbol='GBPUSD' ORDER BY id DESC LIMIT 1")
    text = gbp["prompt_text"]
    assert "## Bot trades on other pairs\nEURUSD  long @ " in text
    assert "Net currency exposure, in trades (+ long, − short), all pairs: EUR +1 · USD −1" in text
    assert "Long blocked: a long would make 2 trades short USD" in text and "Short allowed" in text
    assert "Open trades: 1 of 3 (all pairs)" in text


def test_max_open_trades_across_pairs(tmp_path):
    env = Env(tmp_path, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    try:
        env.config.update({"model.max_concurrent_calls": 1, "risk.max_open_trades": 2, "risk.max_currency_exposure": 3})
        env.llm.script = [route(EURUSD=entry("long"), GBPUSD=entry("long"), USDJPY=entry("short"))] * 3
        env.bars()
        ds = decisions(env)
        assert ds["EURUSD"]["verdict"] == "APPROVED" and ds["GBPUSD"]["verdict"] == "APPROVED"
        assert ds["USDJPY"]["verdict"] == "REJECTED"
        assert ds["USDJPY"]["verdict_reason"] == "Max 2 open trades reached (EURUSD long, GBPUSD long)"
    finally:
        env.close()


def test_one_trade_per_pair(env3):
    env3.llm.script = [route(EURUSD=entry("long"))] * 3
    env3.bars()
    env3.advance(15)
    env3.llm.script = [route(EURUSD=entry("long"))] * 3
    env3.bars()
    d = env3.store.one("SELECT * FROM decisions WHERE symbol='EURUSD' ORDER BY id DESC LIMIT 1")
    assert d["verdict"] == "REJECTED" and d["verdict_reason"] == "A bot position is already open on EURUSD (one per pair)"


def test_account_wide_skip_is_one_row(tmp_path):
    friday = datetime(2026, 10, 16, 20, 0, 5, tzinfo=UTC)  # after Friday 19:00 UTC: no new entries
    env = Env(tmp_path, now=friday, symbols=("EURUSD", "GBPUSD"))
    try:
        ids = env.bars()
        assert len(ids) == 1
        d = env.decision(ids[0])
        assert d["symbol"] == "ALL" and d["status"] == "SKIPPED" and d["skip_reason"].startswith("Weekend window")
        assert env.bars() == []
    finally:
        env.close()


def test_news_blackout_is_per_pair(tmp_path):
    t = (NOW + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    env = Env(tmp_path, symbols=("EURUSD", "GBPUSD"), events=f'events:\n  - {{time: "{t}", currency: EUR, impact: high, title: ECB rate decision}}\n')
    try:
        env.llm.script = [route()] * 2
        env.bars()
        ds = decisions(env)
        assert ds["EURUSD"]["status"] == "SKIPPED" and ds["EURUSD"]["skip_reason"].startswith("News blackout: EUR ECB rate decision")
        assert ds["GBPUSD"]["status"] == "CALLED"  # a EUR event doesn't touch GBPUSD
        assert len(env.llm.calls) == 1
    finally:
        env.close()


def test_kill_switch_closes_every_pair(tmp_path):
    env = Env(tmp_path, dry_run=False, symbols=("EURUSD", "USDJPY"))
    try:
        env.config.update({"model.max_concurrent_calls": 1})
        env.llm.script = [route(EURUSD=entry("long"), USDJPY=entry("long"))] * 2
        env.bars()
        bot = [p for p in env.fake.positions.values() if p.magic == env.engine.cfg.app.magic]
        assert {p.symbol for p in bot} == {"EURUSD", "USDJPY"}
        manual = env.fake.add_manual_position(env.fake.POSITION_TYPE_SELL, 0.1, 151.0, symbol="USDJPY")
        summary = env.run(env.engine.kill("test", "button"))
        assert summary["closed"] == 2 and summary["failed"] == 0
        assert set(env.fake.positions) == {manual}  # the manual USDJPY trade is untouched
        rows = env.store.query("SELECT symbol, status, exit_reason FROM trades WHERE mode='live' ORDER BY symbol")
        assert [(r["symbol"], r["status"], r["exit_reason"]) for r in rows] == [("EURUSD", "closed", "kill_switch"), ("USDJPY", "closed", "kill_switch")]
    finally:
        env.close()


@pytest.mark.parametrize("parallel", [1, 2, 3])
def test_pairs_are_decided_in_parallel(tmp_path, parallel):
    env = Env(tmp_path, symbols=("EURUSD", "GBPUSD", "USDJPY"))
    try:
        env.config.update({"model.max_concurrent_calls": parallel})
        env.llm.script = [route()] * 3
        env.llm.latency_s = (0.2, 0.2)
        seen = {"now": 0, "max": 0}
        decide = env.llm.decide

        async def counting(request, cfg, context=None):
            seen["now"] += 1
            seen["max"] = max(seen["max"], seen["now"])
            try:
                return await decide(request, cfg, context=context)
            finally:
                seen["now"] -= 1

        env.llm.decide = counting
        assert len(env.bars()) == 3
        assert seen["max"] == parallel  # never more calls at once than the setting allows
    finally:
        env.close()


def test_live_reconcile_labels_pairs(tmp_path):
    env = Env(tmp_path, dry_run=False, symbols=("EURUSD", "USDJPY"))
    try:
        env.config.update({"model.max_concurrent_calls": 1})
        env.llm.script = [route(USDJPY=entry("short"))] * 2
        env.bars()
        row = env.store.one("SELECT * FROM trades WHERE mode='live'")
        assert row["symbol"] == "USDJPY" and row["side"] == "short" and row["risk_eur"] == pytest.approx(50.0, rel=0.05)
        order = env.store.one("SELECT * FROM orders WHERE purpose='open'")
        assert order["symbol"] == "USDJPY" and order["ok"] == 1
    finally:
        env.close()


def test_unknown_pair_is_skipped_not_fatal(tmp_path):
    env = Env(tmp_path, symbols=("EURUSD",))
    try:
        env.config.update({"app.symbols": ["EURUSD", "XAUUSD"]})
        env.run(env.engine.resync_symbols())
        assert list(env.engine.books) == ["EURUSD"]
        assert any("XAUUSD" in b["text"] for b in env.engine.banners())
        assert env.engine.status()["unavailable"] == ["XAUUSD"]
    finally:
        env.close()


def test_pair_with_an_open_trade_cannot_be_switched_off(env3):
    env3.llm.script = [route(GBPUSD=entry("short"))] * 3
    env3.bars()
    assert env3.engine.symbols_change_error(["EURUSD", "USDJPY"]) == "Close the open GBPUSD trade first."
    assert env3.engine.symbols_change_error(["EURUSD", "GBPUSD"]) is None


# ---------------------------------------------------------------- upgrades from the one-pair version

def test_old_database_is_migrated(tmp_path):
    old_store = subprocess.run(["git", "show", "7866c02:opus_trader/store.py"], capture_output=True, text=True, cwd=ROOT).stdout
    if not old_store:
        pytest.skip("git history not available")
    ns: dict = {}
    exec(compile(old_store.split("def now_iso")[0], "old_store", "exec"), ns)
    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(ns["SCHEMA"])
    con.execute("INSERT INTO decisions (created_at, bar_time, mode, status) VALUES ('2026-10-13T10:00:00Z', '2026-10-13 09:45', 'dry', 'CALLED')")
    con.execute("INSERT INTO trades (mode, ticket, side, open_time, open_price, volume, status) VALUES ('dry', 1, 'long', '2026-10-13T10:00:00Z', 1.16, 0.1, 'open')")
    con.execute("INSERT INTO bench_trades (side, entry_time, entry_price, stop_initial, status) VALUES ('long', '2026-10-13T10:00:00Z', 1.16, 1.15, 'open')")
    con.commit()
    con.close()
    store = Store(path)
    assert store.one("SELECT symbol FROM decisions")["symbol"] == "EURUSD"
    assert store.one("SELECT symbol FROM trades")["symbol"] == "EURUSD"
    # Two pairs may now open a benchmark trade on the same bar.
    store.insert("bench_trades", {"symbol": "GBPUSD", "side": "long", "entry_time": "2026-10-13T10:00:00Z", "entry_price": 1.34, "stop_initial": 1.33, "status": "open"})
    assert len(store.query("SELECT * FROM bench_trades")) == 2
    store.close()
    Store(path).close()  # migrating twice is harmless


def test_old_config_is_upgraded(tmp_path):
    old = Config().model_dump()
    old["app"].pop("symbols")
    old["app"]["symbol"] = "EURUSD.pro"
    old["risk"].pop("max_open_trades")
    old["risk"]["max_open_positions"] = 1
    old["news"]["currencies"] = ["USD", "EUR"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.app.symbols == ["EURUSD.pro"] + MAJORS[1:]
    assert cfg.risk.max_open_trades == 3 and cfg.risk.max_currency_exposure == 1
    assert "GBP" in cfg.news.currencies and "JPY" in cfg.news.currencies
    text = path.read_text(encoding="utf-8")
    assert "symbols: [EURUSD.pro, GBPUSD" in text and "max_open_positions" not in text  # rewritten in the new layout


def test_a_pair_still_loading_history_does_not_stop_the_others(env3):
    real = env3.fake.copy_rates_from_pos

    def rates(symbol, *args):
        return None if symbol == "GBPUSD" else real(symbol, *args)  # MT5 hasn't downloaded GBPUSD yet

    env3.fake.copy_rates_from_pos = rates
    env3.llm.script = [route()] * 2
    env3.bars()
    ds = decisions(env3)
    assert ds["GBPUSD"]["status"] == "SKIPPED" and ds["GBPUSD"]["skip_reason"] == "MT5 is still loading the GBPUSD price history"
    assert ds["EURUSD"]["status"] == "CALLED" and ds["USDJPY"]["status"] == "CALLED"
    assert env3.engine.rt.connected


def test_an_empty_quote_is_no_price(env3):
    env3.fake.instruments["USDJPY"].price_override = (0.0, 0.0)  # just added to Market Watch, no quote yet
    env3.llm.script = [route()] * 2
    env3.bars()
    ds = decisions(env3)
    assert ds["USDJPY"]["status"] == "SKIPPED" and ds["USDJPY"]["skip_reason"] == "No price for USDJPY"
    assert ds["EURUSD"]["status"] == "CALLED"


# ---------------------------------------------------------------- the API with several pairs

def test_api_with_several_pairs(env3):
    from fastapi.testclient import TestClient

    from opus_trader.api import create_app

    env3.llm.script = [route(EURUSD=entry("long"), GBPUSD=entry("long"), USDJPY=entry("long"))] * 3
    env3.bars()
    app = create_app(env3.engine, "tok", port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        st = c.get("/api/state", params={"symbol": "USDJPY"}).json()
        assert st["status"]["symbols"] == ["EURUSD", "GBPUSD", "USDJPY"]
        assert [p["symbol"] for p in st["status"]["pairs"]] == ["EURUSD", "GBPUSD", "USDJPY"]
        assert st["latest"]["symbol"] == "USDJPY" and st["latest"]["action"] == "OPEN_LONG"
        assert {(p["symbol"], p["side"], p["kind"]) for p in st["positions"]} == {("EURUSD", "long", "position"), ("USDJPY", "long", "position")}
        assert [r["symbol"] for r in st["board"]] == ["EURUSD", "GBPUSD", "USDJPY"]
        assert st["board"][1]["latest"]["verdict"] == "REJECTED"  # GBPUSD long: would double up on short USD
        jpy = c.get("/api/candles", params={"symbol": "USDJPY"}).json()
        assert jpy["digits"] == 3 and jpy["symbol"] == "USDJPY" and 100 < jpy["candles"][-1]["close"] < 200
        assert {line["kind"] for line in jpy["lines"]} == {"entry", "sl", "tp"}
        assert c.get("/api/candles", params={"symbol": "XAUUSD"}).status_code == 404
        j = c.get("/api/journal", params={"symbol": "GBPUSD"}).json()
        assert j["items"] and all(it["symbol"] in ("GBPUSD", "ALL") for it in j["items"])
        assert "pair" in c.get("/api/journal.csv").text.splitlines()[0]
        # A pair with an open trade can't be switched off; one without can.
        h = {"X-Opus-Token": "tok"}
        r = c.put("/api/settings", json={"changes": {"app.symbols": ["GBPUSD", "USDJPY"]}}, headers=h)
        assert r.status_code == 409 and r.json()["error"] == "Close the open EURUSD trade first."
        r = c.put("/api/settings", json={"changes": {"app.symbols": ["EURUSD", "USDJPY"]}}, headers=h)
        assert r.status_code == 200
        assert c.get("/api/state").json()["status"]["symbols"] == ["EURUSD", "USDJPY"]
        s = c.get("/api/settings").json()
        pairs = next(f for f in s["fields"] if f["key"] == "app.symbols")
        assert pairs["type"] == "pairs" and pairs["value"] == ["EURUSD", "USDJPY"] and "GBPUSD" in pairs["choices"]
        assert s["open_pairs"] == ["EURUSD", "USDJPY"]
