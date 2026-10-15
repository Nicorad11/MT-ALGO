import json

import pytest
from fastapi.testclient import TestClient

from opus_trader.api import create_app

from .engine_env import HOLD, Env, entry

TOKEN = "test-token"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, script=[HOLD, entry("long", stop_atr=0.3), entry("short", stop_atr=2.0)])
    for _ in range(3):
        e.bar()
        e.advance(15)
    yield e
    e.close()


@pytest.fixture
def client(env):
    app = create_app(env.engine, TOKEN, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def post(client, path, body=None):
    return client.post(path, json=body or {}, headers={"X-Opus-Token": TOKEN})


def test_index_embeds_token(client):
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text


def test_foreign_host_rejected(client):
    assert client.get("/api/state", headers={"Host": "evil.example:8765"}).status_code == 403


def test_mutations_need_token(client):
    assert client.post("/api/pause").status_code == 403
    assert client.post("/api/pause", headers={"X-Opus-Token": "wrong"}).status_code == 403
    assert post(client, "/api/pause").status_code == 200
    assert client.get("/api/state").json()["status"]["status"] == "paused"
    assert post(client, "/api/resume").status_code == 200


def test_state_payload(client):
    s = client.get("/api/state").json()
    assert s["status"]["mode"] == "dry" and s["status"]["dry_run"] is True
    assert s["kpis"]["currency"] == "EUR" and s["kpis"]["equity"] > 0
    assert s["position"]["side"] == "short"
    assert s["latest"]["action"] == "OPEN_SHORT" and s["latest"]["verdict"] == "APPROVED"
    assert [d["verdict"] for d in s["recent"]][:3] == ["APPROVED", "REJECTED", "HOLD"]
    assert s["holds"] == 1 and s["api_cost_today"] > 0


def test_candles_with_position_lines(client):
    c = client.get("/api/candles?bars=96").json()
    assert len(c["candles"]) == 96
    assert {l["kind"] for l in c["lines"]} == {"entry", "sl", "tp"}
    assert c["markers"] and c["markers"][-1]["kind"] == "entry"
    assert c["ema20"] and c["ema200"]


def test_journal_filters_and_detail(client, env):
    j = client.get("/api/journal").json()
    kinds = [i["verdict"] for i in j["items"] if i["kind"] == "decision"]
    assert kinds == ["APPROVED", "REJECTED", "HOLD"]
    assert [i["verdict"] for i in client.get("/api/journal?filter=rejected").json()["items"]] == ["REJECTED"]
    assert [i["action"] for i in client.get("/api/journal?filter=holds").json()["items"]] == ["HOLD"]
    assert client.get("/api/journal?q=Scripted%20short").json()["items"][0]["action"] == "OPEN_SHORT"
    did = j["items"][0]["id"]
    d = client.get(f"/api/journal/{did}").json()
    assert d["saw"]["session"] and d["saw"]["signal"]["trend"] in ("up", "down")
    assert d["risk"]["calc"]["rr"] == pytest.approx(2.0, abs=0.01)
    assert [o["purpose"] for o in d["orders"]] == ["check", "open"]
    raw = client.get(f"/api/journal/{did}/raw").json()
    assert "HOLD is the default" in raw["system"] and raw["prompt"].startswith("## H4 candles")


def test_journal_csv(client):
    r = client.get("/api/journal.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("time_utc,kind,mode")


def test_stats_has_both_columns(client):
    s = client.get("/api/stats?period=all").json()
    assert set(s["opus"]) >= {"net_pnl", "trades", "win_rate", "avg_win_r", "avg_loss_r", "profit_factor", "expectancy_r", "max_dd_pct", "api_cost", "with_signal"}
    assert set(s["bench"]) >= {"net_pnl", "trades", "expectancy_r", "max_dd_pct"}
    assert s["opus"]["with_signal"]["total"] == 1
    assert "opus" in s["equity"] and "bench" in s["equity"]


def test_settings_validation_and_log(client, env):
    r = client.put("/api/settings", json={"changes": {"risk.risk_per_trade_pct": 5}}, headers={"X-Opus-Token": TOKEN})
    assert r.status_code == 400 and r.json() == {"error": "Must be between 0.1 and 2.0", "field": "risk.risk_per_trade_pct"}
    r = client.put("/api/settings", json={"changes": {"risk.max_spread_pips": 1.2}}, headers={"X-Opus-Token": TOKEN})
    assert r.status_code == 200 and r.json()["changed"] == [{"key": "risk.max_spread_pips", "old": 1.5, "new": 1.2}]
    assert env.engine.cfg.risk.max_spread_pips == 1.2
    s = client.get("/api/settings").json()
    assert s["changes"][0]["label"] == "Max spread" and s["changes"][0]["new_value"] == 1.2
    locked = client.put("/api/settings", json={"changes": {"app.magic": 1}}, headers={"X-Opus-Token": TOKEN})
    assert locked.status_code == 400


def test_dry_run_switch_blocked_with_open_position(client, env):
    r = post(client, "/api/dry-run", {"value": False})
    assert r.status_code == 409 and "Close the open position" in r.json()["error"]
    post(client, "/api/stop")
    r = post(client, "/api/dry-run", {"value": False})
    assert r.status_code == 200 and env.engine.cfg.app.dry_run is False
    assert env.store.one("SELECT key, source FROM settings_log WHERE key='app.dry_run'")["source"] == "ui"


def test_stop_flattens(client):
    r = post(client, "/api/stop").json()
    assert r["closed"] == 1
    st = client.get("/api/state").json()
    assert st["status"]["status"] == "halted" and st["position"] is None


def test_sse_stream_over_a_real_server(env):
    """TestClient can't end an infinite stream, so run uvicorn for real on a free port."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(env.engine, TOKEN, port=port), host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        events = []
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/stream", timeout=10) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            for line in r.iter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                if line.startswith("data:") and events:
                    payload = json.loads(line.split(":", 1)[1])
                    assert payload["mode"] == "dry" and "next_bar_at" in payload
                    break
        assert events == ["status"]
    finally:
        server.should_exit = True
        t.join(timeout=10)
