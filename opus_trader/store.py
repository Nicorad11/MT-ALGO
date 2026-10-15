"""SQLite storage: the auditable record of every decision, order, trade and setting change."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,          -- UTC ISO time the row was written
    bar_time TEXT NOT NULL,            -- open time (UTC) of the closed M15 bar this decision is for
    mode TEXT NOT NULL,                -- dry | live
    status TEXT NOT NULL,              -- CALLED | SKIPPED
    skip_reason TEXT,
    action TEXT, order_type TEXT, entry REAL, stop_loss REAL, take_profit REAL,
    confidence INTEGER, setup TEXT, reasoning TEXT, invalidation TEXT,
    verdict TEXT,                      -- PENDING | APPROVED | REJECTED | HOLD | NO_DECISION | ERROR | DISCARDED | SKIPPED | FAILED
    verdict_reason TEXT,
    signal_agreement TEXT,             -- agree | disagree | n/a
    signal_json TEXT,                  -- Trend Signals state at this bar
    risk_json TEXT,                    -- risk engine calculation
    snapshot_json TEXT,                -- everything the model saw (structured)
    prompt_text TEXT,                  -- the exact text sent (system prompt stored once per hash in prompts)
    system_hash TEXT,
    response_json TEXT,                -- raw API response
    model TEXT, stop_reason TEXT, llm_status TEXT, error TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL, latency_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS ix_decisions_mode ON decisions(mode, id);

CREATE TABLE IF NOT EXISTS prompts (
    hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    system_text TEXT NOT NULL,
    tool_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    decision_id INTEGER,
    mode TEXT NOT NULL,
    purpose TEXT NOT NULL,             -- open | close | pending | modify | remove | check
    request_json TEXT,
    retcode INTEGER, retcode_name TEXT, comment TEXT,
    order_ticket INTEGER, deal_ticket INTEGER, price REAL, volume REAL,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_decision ON orders(decision_id);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    ticket INTEGER NOT NULL,           -- MT5 position ticket (live) or paper ticket (dry)
    decision_id INTEGER,
    side TEXT NOT NULL,                -- long | short
    setup TEXT, confidence INTEGER, signal_agreement TEXT, session TEXT,
    open_time TEXT NOT NULL, open_price REAL NOT NULL, volume REAL NOT NULL,
    sl_initial REAL, tp_initial REAL, sl REAL, tp REAL,
    risk_eur REAL,
    close_time TEXT, close_price REAL, exit_reason TEXT,
    pnl_eur REAL, r_multiple REAL,
    status TEXT NOT NULL,              -- open | closed
    UNIQUE(mode, ticket)
);
CREATE INDEX IF NOT EXISTS ix_trades_close ON trades(mode, close_time);

CREATE TABLE IF NOT EXISTS paper_orders (
    ticket INTEGER PRIMARY KEY,
    decision_id INTEGER,
    side TEXT NOT NULL, volume REAL NOT NULL, price REAL NOT NULL, sl REAL NOT NULL, tp REAL NOT NULL,
    risk_eur REAL, setup TEXT, confidence INTEGER, signal_agreement TEXT,
    created_at TEXT NOT NULL, expires_at TEXT,
    status TEXT NOT NULL               -- pending | filled | cancelled | expired
);

CREATE TABLE IF NOT EXISTS equity (
    time TEXT NOT NULL,                -- UTC bar time
    mode TEXT NOT NULL,
    balance REAL, equity REAL, bot_equity REAL, bot_unrealized REAL,
    PRIMARY KEY (time, mode)
);

CREATE TABLE IF NOT EXISTS bench_trades (
    id INTEGER PRIMARY KEY,
    side TEXT NOT NULL,
    entry_time TEXT NOT NULL UNIQUE,
    entry_price REAL NOT NULL, stop_initial REAL NOT NULL, stop REAL,
    volume REAL, risk_eur REAL,
    exit_time TEXT, exit_price REAL, exit_reason TEXT,
    pnl_eur REAL, r_multiple REAL,
    status TEXT NOT NULL               -- open | closed
);

CREATE TABLE IF NOT EXISTS bench_equity (
    time TEXT PRIMARY KEY,
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS engine_events (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT
);

CREATE TABLE IF NOT EXISTS settings_log (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,
    key TEXT NOT NULL,
    old_value TEXT, new_value TEXT,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(v: Any) -> str:
    return json.dumps(v, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------ generic
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, row: dict) -> int:
        cols = list(row)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self.lock:
            return self.conn.execute(sql, [row[c] for c in cols]).lastrowid

    def upsert(self, table: str, row: dict, conflict: Iterable[str]) -> None:
        cols = list(row)
        keys = list(conflict)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
        sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
               f"ON CONFLICT({','.join(keys)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING"))
        with self.lock:
            self.conn.execute(sql, [row[c] for c in cols])

    def update(self, table: str, row_id: Any, values: dict, key: str = "id") -> None:
        if not values:
            return
        sets = ",".join(f"{c}=?" for c in values)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*values.values(), row_id])

    # ------------------------------------------------------------ state
    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value_json FROM state WHERE key=?", (key,))
        return default if row is None else json.loads(row["value_json"])

    def set_state(self, key: str, value: Any) -> None:
        self.upsert("state", {"key": key, "value_json": _json(value)}, ["key"])

    def del_state(self, key: str) -> None:
        self.execute("DELETE FROM state WHERE key=?", (key,))

    # ------------------------------------------------------------ logs
    def log_event(self, kind: str, message: str, level: str = "info", data: Any = None) -> None:
        self.insert("engine_events", {"time": now_iso(), "level": level, "kind": kind, "message": message, "data_json": None if data is None else _json(data)})

    def log_setting(self, key: str, old: Any, new: Any, source: str) -> None:
        self.insert("settings_log", {"time": now_iso(), "key": key, "old_value": _json(old), "new_value": _json(new), "source": source})

    def save_prompt(self, digest: str, system_text: str, tool: dict) -> None:
        self.upsert("prompts", {"hash": digest, "created_at": now_iso(), "system_text": system_text, "tool_json": _json(tool)}, ["hash"])

    # ------------------------------------------------------------ JSON helper for callers
    @staticmethod
    def dumps(v: Any) -> str:
        return _json(v)
