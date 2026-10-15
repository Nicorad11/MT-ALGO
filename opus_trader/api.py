"""Local HTTP API and Server-Sent Events for the UI.

Bound to 127.0.0.1 only. The Host header is checked (DNS-rebinding guard)
and every state-changing request needs the per-launch token embedded in the
page, so no other website open in a browser can drive the bot.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import __version__
from . import indicators as ind
from .clock import UTC, fmt_utc, month_start, parse_utc, trading_day_start, week_start
from .config import SettingsError, field_specs
from .engine import Engine
from .execution import EXIT_LABELS
from .mt5_gateway import NotConnected
from .snapshot import Bars

log = logging.getLogger(__name__)
WEB = Path(__file__).parent / "web"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def iso_to_epoch(s: str | None) -> int | None:
    t = parse_utc(s)
    return None if t is None else int(t.timestamp())


def bar_time_epoch(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp())


def first_sentence(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    s = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def period_start(period: str, now: datetime) -> datetime:
    if period == "today":
        return trading_day_start(now)
    if period == "week":
        return week_start(now)
    if period == "month":
        return month_start(now)
    return datetime(1970, 1, 1, tzinfo=UTC)


def drawdown_series(points: list[tuple[int, float]]) -> tuple[list[dict], float, float]:
    """[(time, equity)] -> ([{time, value: dd%}], max_dd_pct, max_dd_eur)."""
    out, peak, max_pct, max_eur = [], None, 0.0, 0.0
    for t, v in points:
        peak = v if peak is None or v > peak else peak
        dd_eur = v - peak
        dd_pct = (dd_eur / peak * 100) if peak else 0.0
        out.append({"time": t, "value": round(dd_pct, 3)})
        if dd_pct < max_pct:
            max_pct, max_eur = dd_pct, dd_eur
    return out, round(max_pct, 2), round(max_eur, 2)


def trade_metrics(rows: list[dict]) -> dict:
    n = len(rows)
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    pnls = [r["pnl_eur"] or 0.0 for r in rows]
    wins = [r for r in rows if (r["pnl_eur"] or 0) > 0]
    win_r = [r["r_multiple"] for r in wins if r["r_multiple"] is not None]
    loss_r = [r["r_multiple"] for r in rows if (r["pnl_eur"] or 0) <= 0 and r["r_multiple"] is not None]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    return {
        "trades": n,
        "wins": len(wins),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "avg_win_r": round(sum(win_r) / len(win_r), 2) if win_r else None,
        "avg_loss_r": round(sum(loss_r) / len(loss_r), 2) if loss_r else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
    }


def group_r(rows: list[dict], key: Callable[[dict], Any], order: list | None = None, top: int = 6) -> list[dict]:
    groups: dict[Any, list[float]] = {}
    for r in rows:
        if r["r_multiple"] is None:
            continue
        groups.setdefault(key(r), []).append(r["r_multiple"])
    items = [{"label": str(k), "r": round(sum(v), 2), "n": len(v)} for k, v in groups.items()]
    if order:
        rank = {str(o): i for i, o in enumerate(order)}
        items.sort(key=lambda x: rank.get(x["label"], 99))
    else:
        items.sort(key=lambda x: -x["n"])
        if len(items) > top:
            rest = items[top - 1 :]
            items = items[: top - 1] + [{"label": "Other", "r": round(sum(i["r"] for i in rest), 2), "n": sum(i["n"] for i in rest)}]
    return items


def create_app(engine: Engine, token: str, on_exit: Callable[[], None] | None = None, port: int = 8765) -> FastAPI:
    app = FastAPI(title="Opus Trader", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    store = engine.store
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost", "testserver"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in allowed_hosts:
            return JSONResponse({"error": "Forbidden host"}, status_code=403)
        if request.method in MUTATING and request.headers.get("x-opus-token") != token:
            return JSONResponse({"error": "Missing or invalid token"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", token).replace("{{VERSION}}", __version__)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ helpers
    def mode() -> str:
        return engine.mode

    def decision_summary(d: dict) -> dict:
        if d["status"] == "SKIPPED":
            summary = d["skip_reason"]
        elif d["verdict"] in ("REJECTED", "FAILED", "NO_DECISION", "ERROR", "DISCARDED"):
            summary = d["verdict_reason"]
        else:
            summary = d["setup"] or first_sentence(d["reasoning"])
        return {
            "id": d["id"], "kind": "decision", "time": d["created_at"], "bar_time": d["bar_time"], "mode": d["mode"],
            "status": d["status"], "action": d["action"], "verdict": d["verdict"], "verdict_reason": d["verdict_reason"],
            "order_type": d["order_type"], "confidence": d["confidence"], "setup": d["setup"], "summary": summary,
            "reasoning": d["reasoning"], "invalidation": d["invalidation"], "agreement": d["signal_agreement"],
            "entry": d["entry"], "stop_loss": d["stop_loss"], "take_profit": d["take_profit"],
        }

    async def kpis() -> dict:
        m = mode()
        currency = engine.rt.account.get("currency", "EUR")
        out: dict[str, Any] = {"currency": currency, "mode": m}
        try:
            if engine.rt.connected:
                out["equity"] = round(await engine.equity_now(), 2)
                pnl, sod = await engine.day_pnl()
                out["today_pnl"] = pnl
                out["today_pct"] = round(pnl / sod * 100, 2) if sod else 0.0
        except NotConnected:
            pass
        if "equity" not in out:
            last = store.one("SELECT equity FROM equity WHERE mode=? ORDER BY time DESC LIMIT 1", (m,))
            out["equity"] = None if last is None else last["equity"]
            out["today_pnl"] = None
            out["today_pct"] = None
        pts = [(bar_time_epoch(r["time"]), r["bot_equity"]) for r in store.query("SELECT time, bot_equity FROM equity WHERE mode=? ORDER BY time", (m,))]
        _, dd_pct, dd_eur = drawdown_series(pts)
        out["max_dd_pct"], out["max_dd_eur"] = dd_pct, dd_eur
        return out

    async def position() -> dict | None:
        m = mode()
        t = store.one("SELECT * FROM trades WHERE mode=? AND status='open' ORDER BY id DESC LIMIT 1", (m,))
        if t:
            unreal = None
            try:
                unreal = await engine.unrealized(m) if engine.rt.connected else None
            except NotConnected:
                pass
            r = round(unreal / t["risk_eur"], 2) if unreal is not None and t["risk_eur"] else None
            return {"kind": "position", "ticket": t["ticket"], "side": t["side"], "volume": t["volume"], "entry": t["open_price"], "sl": t["sl"], "tp": t["tp"],
                    "unrealized": unreal, "r": r, "open_time": t["open_time"], "setup": t["setup"]}
        if m == "dry":
            o = engine.paper.pending_row()
            if o:
                return {"kind": "pending", "ticket": o["ticket"], "side": o["side"], "volume": o["volume"], "entry": o["price"], "sl": o["sl"], "tp": o["tp"], "expires": o["expires_at"]}
        elif engine.rt.connected:
            try:
                pend = await engine.mt(engine.live.pending)
            except NotConnected:
                pend = None
            if pend:
                return {"kind": "pending", "ticket": pend.ticket, "side": pend.side, "volume": pend.volume, "entry": pend.price, "sl": pend.sl, "tp": pend.tp,
                        "expires": engine.store.get_state("live_pending_expiry", {}).get(str(pend.ticket))}
        return None

    def api_cost_since(start: datetime, m: str | None = None) -> float:
        row = store.one("SELECT COALESCE(SUM(cost_usd),0) AS c FROM decisions WHERE created_at>=?" + (" AND mode=?" if m else ""), (fmt_utc(start), m) if m else (fmt_utc(start),))
        return round(float(row["c"]), 4)

    def holds_count(m: str, since: datetime | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM decisions WHERE mode=? AND status='CALLED' AND action='HOLD'"
        params: list = [m]
        if since:
            q += " AND created_at>=?"
            params.append(fmt_utc(since))
        return int(store.one(q, params)["n"])

    # ------------------------------------------------------------ read endpoints
    @app.get("/api/state")
    async def state() -> dict:
        m = mode()
        latest = store.one("SELECT * FROM decisions WHERE mode=? AND status='CALLED' AND verdict!='PENDING' ORDER BY id DESC LIMIT 1", (m,))
        recent = store.query("SELECT * FROM decisions WHERE mode=? AND verdict!='PENDING' ORDER BY id DESC LIMIT 8", (m,))
        now = engine.clock.now()
        return {
            "version": __version__,
            "status": engine.status(),
            "kpis": await kpis(),
            "position": await position(),
            "latest": None if latest is None else decision_summary(latest),
            "recent": [decision_summary(d) for d in recent],
            "holds": holds_count(m),
            "decisions": int(store.one("SELECT COUNT(*) n FROM decisions WHERE mode=?", (m,))["n"]),
            "api_cost_today": api_cost_since(trading_day_start(now)),
            "account": engine.rt.account,
            "magic": engine.cfg.app.magic,
        }

    @app.get("/api/candles")
    async def candles(bars: int = 192) -> dict:
        bars = max(20, min(bars, 1000))
        if not engine.rt.connected:
            raise HTTPException(503, "MT5 disconnected")
        try:
            rates = await engine.mt(engine.gw.rates, engine.gw.mt5.TIMEFRAME_M15, 1, 1000)
        except NotConnected:
            raise HTTPException(503, "MT5 disconnected")
        b = Bars.from_rates(rates, engine.server_time)
        e20, e50, e200 = ind.ema(b.c, 20), ind.ema(b.c, 50), ind.ema(b.c, 200)
        sl = slice(-bars, None)
        t = [int(x) for x in b.t[sl]]
        start = t[0] if t else 0

        def series(arr):
            return [{"time": tt, "value": round(float(v), 5)} for tt, v in zip(t, arr[sl]) if not math.isnan(v)]

        m = mode()
        markers, lines = [], []
        for tr in store.query("SELECT * FROM trades WHERE mode=? AND (close_time IS NULL OR close_time>=?) ORDER BY open_time", (m, fmt_utc(datetime.fromtimestamp(start, UTC)))):
            ot = iso_to_epoch(tr["open_time"])
            if ot >= start:
                markers.append({"time": ot - ot % 900, "kind": "entry", "side": tr["side"], "text": "Long" if tr["side"] == "long" else "Short"})
            if tr["status"] == "closed":
                ct = iso_to_epoch(tr["close_time"])
                r = tr["r_multiple"]
                markers.append({"time": ct - ct % 900, "kind": "exit", "side": tr["side"], "r": r,
                                "text": "" if r is None else (f"{r:+.2f}R".replace("-", "−"))})
            else:
                lines.append({"kind": "entry", "price": tr["open_price"]})
                if tr["sl"]:
                    lines.append({"kind": "sl", "price": tr["sl"]})
                if tr["tp"]:
                    lines.append({"kind": "tp", "price": tr["tp"]})
        pos = await position()
        if pos and pos["kind"] == "pending":
            lines += [{"kind": "limit", "price": pos["entry"]}, {"kind": "sl", "price": pos["sl"]}, {"kind": "tp", "price": pos["tp"]}]
        markers.sort(key=lambda x: x["time"])
        return {
            "candles": [{"time": tt, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)} for tt, o, h, lo, c in zip(t, b.o[sl], b.h[sl], b.l[sl], b.c[sl])],
            "ema20": series(e20), "ema50": series(e50), "ema200": series(e200),
            "markers": markers, "lines": lines, "bid": engine.rt.bid, "digits": engine.spec.digits if engine.spec else 5,
        }

    @app.get("/api/equity")
    async def equity(period: str = "all") -> dict:
        m = mode()
        start = period_start(period, engine.clock.now())
        key = start.strftime("%Y-%m-%d %H:%M")
        pts = [(bar_time_epoch(r["time"]), r["bot_equity"]) for r in store.query("SELECT time, bot_equity FROM equity WHERE mode=? AND time>=? ORDER BY time", (m, key))]
        bpts = [(bar_time_epoch(r["time"]), r["equity"]) for r in store.query("SELECT time, equity FROM bench_equity WHERE time>=? ORDER BY time", (key,))]
        dd, _, _ = drawdown_series(pts)
        bdd, _, _ = drawdown_series(bpts)
        return {"opus": [{"time": t, "value": v} for t, v in pts], "bench": [{"time": t, "value": v} for t, v in bpts], "opus_dd": dd, "bench_dd": bdd}

    def journal_rows(flt: str, q: str, before: str | None, limit: int) -> list[dict]:
        m = mode()
        params: list[Any] = [m]
        where = ["mode=?", "verdict!='PENDING'"]
        if before:
            where.append("created_at<?")
            params.append(before)
        if flt == "rejected":
            where.append("verdict IN ('REJECTED','FAILED')")
        elif flt == "holds":
            where.append("status='CALLED' AND action='HOLD'")
        elif flt == "trades":
            where.append("verdict='APPROVED' AND action!='HOLD'")
        if q:
            where.append("(reasoning LIKE ? OR setup LIKE ? OR verdict_reason LIKE ? OR skip_reason LIKE ? OR invalidation LIKE ?)")
            params += [f"%{q}%"] * 5
        decs = [decision_summary(d) for d in store.query(f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT ?", params + [limit])]
        items = decs
        if flt in ("all", "trades"):
            tp: list[Any] = [m]
            tw = ["mode=?", "status='closed'"]
            if before:
                tw.append("close_time<?")
                tp.append(before)
            if q:
                tw.append("(setup LIKE ? OR exit_reason LIKE ?)")
                tp += [f"%{q}%"] * 2
            for t in store.query(f"SELECT * FROM trades WHERE {' AND '.join(tw)} ORDER BY close_time DESC LIMIT ?", tp + [limit]):
                items.append({"id": f"t{t['id']}", "kind": "trade", "time": t["close_time"], "mode": t["mode"], "side": t["side"], "setup": t["setup"],
                              "exit_reason": t["exit_reason"], "exit_label": EXIT_LABELS.get(t["exit_reason"], t["exit_reason"]),
                              "r": t["r_multiple"], "pnl": t["pnl_eur"], "volume": t["volume"], "open_price": t["open_price"], "close_price": t["close_price"],
                              "decision_id": t["decision_id"], "open_time": t["open_time"], "agreement": t["signal_agreement"]})
        items.sort(key=lambda x: x["time"], reverse=True)
        return items[:limit]

    @app.get("/api/journal")
    async def journal(filter: str = "all", q: str = "", before: str | None = None, limit: int = 60) -> dict:
        limit = max(1, min(limit, 200))
        items = journal_rows(filter, q.strip(), before, limit)
        return {"items": items, "next": items[-1]["time"] if len(items) == limit else None, "holds": holds_count(mode()), "mode": mode()}

    @app.get("/api/journal/{decision_id}")
    async def journal_detail(decision_id: int) -> dict:
        d = store.one("SELECT * FROM decisions WHERE id=?", (decision_id,))
        if d is None:
            raise HTTPException(404, "No such decision")
        snap = json.loads(d["snapshot_json"]) if d["snapshot_json"] else None
        out = decision_summary(d)
        out.update({
            "skip_reason": d["skip_reason"], "model": d["model"], "llm_status": d["llm_status"], "error": d["error"], "stop_reason": d["stop_reason"],
            "input_tokens": d["input_tokens"], "output_tokens": d["output_tokens"], "cache_read_tokens": d["cache_read_tokens"],
            "cache_write_tokens": d["cache_write_tokens"], "cost_usd": d["cost_usd"], "latency_ms": d["latency_ms"],
            "risk": json.loads(d["risk_json"]) if d["risk_json"] else None,
            "signal": json.loads(d["signal_json"]) if d["signal_json"] else None,
            "orders": store.query("SELECT created_at, purpose, retcode, retcode_name, comment, order_ticket, deal_ticket, price, volume, ok FROM orders WHERE decision_id=? ORDER BY id", (decision_id,)),
            "saw": None,
        })
        if snap:
            i15 = snap["indicators"]["M15"]
            asia = (snap.get("levels") or {}).get("asian_range")
            out["saw"] = {
                "bid": snap["market"]["bid"], "ask": snap["market"]["ask"], "spread_pips": snap["market"]["spread_pips"],
                "atr_pips": i15["atr14_pips"], "rsi": i15["rsi14"], "session": snap["session"],
                "ema_order": ema_order(i15), "asian_range": None if not asia else [asia["low"], asia["high"]],
                "position": snap.get("position"), "pending": snap.get("pending"), "today_pnl": snap["account"]["today_pnl"],
                "currency": snap["account"]["currency"], "events": snap.get("events") or [], "signal": snap.get("signal"),
                "bars_sent": snap.get("bars_sent"), "risk_state": snap.get("risk_state"),
            }
        return out

    @app.get("/api/journal/{decision_id}/raw")
    async def journal_raw(decision_id: int) -> dict:
        d = store.one("SELECT prompt_text, response_json, system_hash FROM decisions WHERE id=?", (decision_id,))
        if d is None:
            raise HTTPException(404, "No such decision")
        p = store.one("SELECT system_text FROM prompts WHERE hash=?", (d["system_hash"],)) if d["system_hash"] else None
        return {"system": p["system_text"] if p else None, "prompt": d["prompt_text"], "response": json.loads(d["response_json"]) if d["response_json"] else None}

    @app.get("/api/journal.csv")
    async def journal_csv(filter: str = "all", q: str = "") -> Response:
        items = journal_rows(filter, q.strip(), None, 100_000)
        buf = io.StringIO()
        cols = ["time_utc", "kind", "mode", "bar_time_utc", "action", "order_type", "entry", "stop_loss", "take_profit", "confidence", "setup",
                "verdict", "verdict_reason", "signal_agreement", "side", "exit_reason", "r_multiple", "pnl_eur", "reasoning", "invalidation"]
        w = csv.writer(buf)
        w.writerow(cols)
        for it in items:
            w.writerow([it.get("time"), it["kind"], it.get("mode"), it.get("bar_time"), it.get("action"), it.get("order_type"), it.get("entry"),
                        it.get("stop_loss"), it.get("take_profit"), it.get("confidence"), it.get("setup"), it.get("verdict"),
                        it.get("verdict_reason"), it.get("agreement"), it.get("side"), it.get("exit_reason"), it.get("r"), it.get("pnl"),
                        it.get("reasoning"), it.get("invalidation")])
        name = f"opus-trader-journal-{engine.clock.now():%Y%m%d-%H%M}.csv"
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/stats")
    async def stats(period: str = "all") -> dict:
        m = mode()
        now = engine.clock.now()
        start = period_start(period, now)
        s_iso = fmt_utc(start)
        rows = store.query("SELECT * FROM trades WHERE mode=? AND status='closed' AND close_time>=? ORDER BY close_time", (m, s_iso))
        bench = store.query("SELECT * FROM bench_trades WHERE status='closed' AND exit_time>=? ORDER BY exit_time", (s_iso,))
        eq = await equity(period)
        _, dd_pct, dd_eur = drawdown_series([(p["time"], p["value"]) for p in eq["opus"]])
        _, bdd_pct, bdd_eur = drawdown_series([(p["time"], p["value"]) for p in eq["bench"]])
        opus = trade_metrics(rows)
        cost = api_cost_since(start, m)
        entries = store.query("SELECT signal_agreement FROM decisions WHERE mode=? AND verdict='APPROVED' AND action IN ('OPEN_LONG','OPEN_SHORT') AND created_at>=?", (m, s_iso))
        opus.update({"max_dd_pct": dd_pct, "max_dd_eur": dd_eur, "api_cost": cost,
                     "cost_per_trade": round(cost / opus["trades"], 2) if opus["trades"] else None,
                     "with_signal": {"agree": sum(1 for e in entries if e["signal_agreement"] == "agree"), "total": len(entries)}})
        b = trade_metrics(bench)
        b.update({"max_dd_pct": bdd_pct, "max_dd_eur": bdd_eur, "api_cost": None, "cost_per_trade": None, "with_signal": None})
        sessions = ["Asia", "London", "London/NY overlap", "New York"]
        return {
            "period": period, "from": s_iso, "mode": m, "currency": engine.rt.account.get("currency", "EUR"),
            "opus": opus, "bench": b, "holds": holds_count(m, start), "bench_start": store.get_state("bench_start"),
            "equity": eq,
            "by_setup": group_r(rows, lambda r: (r["setup"] or "Unlabelled").strip()),
            "by_confidence": group_r(rows, lambda r: r["confidence"] if r["confidence"] else "—", order=[5, 4, 3, 2, 1, "—"]),
            "by_session": group_r(rows, lambda r: r["session"] or "—", order=sessions),
        }

    @app.get("/api/settings")
    async def settings() -> dict:
        cfg = engine.cfg
        nxt = engine.news.next_event(engine.clock.now())
        upcoming = [e for e in engine.news.high_impact() if e.time >= engine.clock.now()]
        changes = store.query("SELECT time, key, old_value, new_value, source FROM settings_log ORDER BY id DESC LIMIT 6")
        specs = {s["key"]: s for s in field_specs(cfg)}
        for c in changes:
            spec = specs.get(c["key"], {})
            c["label"], c["unit"] = spec.get("label", c["key"]), spec.get("unit", "")
            c["old_value"], c["new_value"] = json.loads(c["old_value"]), json.loads(c["new_value"])
        src = getattr(engine.news.source, "path", None)
        return {
            "fields": list(specs.values()),
            "changes": changes,
            "account": engine.rt.account,
            "magic": cfg.app.magic,
            "news": {"file": src.name if src else None, "upcoming": len(upcoming), "next": None if nxt is None else nxt.to_dict(engine.clock.now()),
                     "error": getattr(engine.news.source, "error", None)},
            "can_switch_mode": not engine.has_open_book(engine.mode),
        }

    @app.get("/api/stream")
    async def stream(request: Request):
        q = engine.bus.subscribe()

        async def gen():
            try:
                yield {"event": "status", "data": json.dumps(engine.status())}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        continue
                    yield {"event": event, "data": json.dumps(data, default=str)}
            finally:
                engine.bus.unsubscribe(q)

        return EventSourceResponse(gen(), ping=15)

    # ------------------------------------------------------------ commands
    async def body(request: Request) -> dict:
        try:
            return await request.json()
        except Exception:  # noqa: BLE001
            return {}

    @app.put("/api/settings")
    async def update_settings(request: Request) -> dict:
        data = await body(request)
        changes = data.get("changes") or {}
        if "app.dry_run" in changes:
            raise HTTPException(400, "Use the dry-run switch")
        try:
            diffs = engine.config.update(changes, source="ui")
        except SettingsError as exc:
            return JSONResponse({"error": exc.message, "field": exc.field}, status_code=400)
        if diffs:
            engine.event("settings", "; ".join(f"{k}: {o} → {n}" for k, o, n in diffs))
            engine.bus.publish("settings", {"keys": [k for k, _, _ in diffs]})
        return {"ok": True, "changed": [{"key": k, "old": o, "new": n} for k, o, n in diffs]}

    @app.post("/api/dry-run")
    async def dry_run(request: Request) -> dict:
        data = await body(request)
        err = engine.set_dry_run(bool(data.get("value")))
        if err:
            return JSONResponse({"error": err}, status_code=409)
        return {"ok": True, "dry_run": engine.cfg.app.dry_run}

    @app.post("/api/pause")
    async def pause() -> dict:
        engine.pause()
        return {"ok": True}

    @app.post("/api/resume")
    async def resume() -> dict:
        err = engine.resume()
        if err:
            return JSONResponse({"error": err}, status_code=409)
        return {"ok": True}

    @app.post("/api/stop")
    async def stop() -> dict:
        summary = await engine.kill("Stopped from the app", "button")
        return {"ok": True, **summary}

    @app.post("/api/exit")
    async def exit_app() -> dict:
        if on_exit is None:
            return JSONResponse({"error": "Exit is only available in the app window. Close the console window to stop."}, status_code=400)
        asyncio.get_running_loop().call_later(0.2, on_exit)
        return {"ok": True}

    return app


def ema_order(i: dict) -> str:
    vals = [("20", i.get("ema20")), ("50", i.get("ema50")), ("200", i.get("ema200"))]
    if any(v is None for _, v in vals):
        return "n/a"
    ordered = sorted(vals, key=lambda x: -x[1])
    return " > ".join(n for n, _ in ordered)
