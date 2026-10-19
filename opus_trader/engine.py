"""The trading engine: one decision per pair per closed M15 bar, plus a 1-second watchdog.

Bar task:  wait for close + delay -> reconcile -> account pre-checks -> per pair:
           pair pre-checks -> snapshot -> Opus -> risk engine -> execute or reject
           -> log -> push to the UI.
Watchdog:  STOP file, terminal health and reconnects, demo/account check,
           paper SL/TP, Friday flatten, daily loss limit, status heartbeat.

Every pair has its own `Book` (quote, specification, executors). The account,
the daily loss limit, the entry count and the cross-pair limits (max open
trades, one currency at a time) are shared. Opus is asked about several pairs
at once; the risk check and the order for each answer run one pair at a time,
so two answers can never both take the last open-trade slot.

All MT5 calls run on one worker thread, so they are serialized and never
block the event loop; the model calls are async, so Stop and the UI stay
responsive while Opus thinks. Every step is wrapped so one bad bar or pair
never stops the loop.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from . import snapshot as snap_mod
from .clock import (Clock, OffsetModel, ServerTime, fmt_utc, friday_flat_due, fx_market_open, next_bar_close,
                    next_trading_day_start, parse_utc, session_of, trading_day_start, weekend_window)
from .config import Config, ConfigManager, pair_currencies
from .execution import EXIT_LABELS, LiveExecutor, PaperExecutor
from .indicators import trend_signals as ts
from .llm import TOOL, build_request, prompt_hash, system_prompt
from .mt5_gateway import MT5Gateway, NoData, NotConnected, SafetyError
from .news import NewsCalendar
from .risk import Decision, Market, OpenTrade, RiskState, SymbolSpec, evaluate, exposure, size_lots
from .store import Store, now_iso

log = logging.getLogger(__name__)
UTC = timezone.utc
BAR = timedelta(minutes=15)
BACKOFF = [2, 4, 8, 16, 32, 60]
MIN_HISTORY = 250  # closed M15 bars a pair needs before Opus is asked about it (indicators warm up)
DEAL_REASON_MAP = {0: "manual", 1: "manual", 2: "manual", 4: "sl", 5: "tp", 6: "stop_out"}
ALL = "ALL"  # symbol of a skip that applied to every pair (weekend, halt, disconnect...)


# ====================================================================== event bus

class EventBus:
    """Fan-out of engine events to SSE clients."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: str, data: Any = None) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                pass


# ====================================================================== runtime state

@dataclass
class Runtime:
    started_at: datetime | None = None
    connected: bool = False
    broker_connected: bool = True
    algo_on: bool = True
    blocked: str | None = None  # fatal safety stop (not demo, account changed)
    reconnect_attempt: int = 0
    reconnect_at: datetime | None = None
    last_error: str | None = None
    connect_hint: str | None = None  # plain-English advice while disconnected
    next_bar_at: datetime | None = None
    bid: float | None = None  # primary pair, for one-pair callers
    ask: float | None = None
    tick_time: datetime | None = None
    account: dict = field(default_factory=dict)
    position_count: int = -1
    last_daily_check: datetime | None = None
    last_status_push: datetime | None = None
    model_warning: str | None = None
    unavailable_noted: tuple = ()


class Book:
    """One pair: its view of the terminal, specification, latest quote and executors."""

    def __init__(self, engine: "Engine", symbol: str):
        self.symbol = symbol  # as configured (EURUSD); the broker may call it EURUSD.pro
        self.view = engine.gw.view(symbol)
        self.spec: SymbolSpec | None = None
        self.bid: float | None = None
        self.ask: float | None = None
        self.tick_time: int | None = None  # server epoch of the latest tick
        self.last_bars: dict[str, snap_mod.Bars] = {}
        self.last_snapshot: dict | None = None
        self.thinking_since: datetime | None = None
        magic = engine.cfg.app.magic
        dev = lambda: engine.cfg.risk.deviation_points  # noqa: E731
        self.live = LiveExecutor(self.view, engine.store, magic, dev, engine.server_time, gate=engine.gate)
        self.paper = PaperExecutor(self.view, engine.store, magic, dev, engine.server_time, lambda: self.spec, engine.clock.now)

    @property
    def broker_symbol(self) -> str:
        return self.view.symbol

    @property
    def currencies(self) -> tuple[str, str]:
        return pair_currencies(self.symbol)

    def executor(self, mode: str):
        return self.paper if mode == "dry" else self.live

    def spread_pips(self) -> float | None:
        if self.bid is None or self.ask is None or self.spec is None:
            return None
        return round((self.ask - self.bid) / self.spec.pip, 1)


class Engine:
    def __init__(self, *, config: ConfigManager, gateway: MT5Gateway, llm: Any, store: Store, news: NewsCalendar,
                 clock: Clock, root: Path, sim: bool = False, read_only: bool = False):
        self.config = config
        self.gw = gateway
        self.llm = llm
        self.store = store
        self.news = news
        self.clock = clock
        self.root = Path(root)
        self.sim = sim
        self.read_only = read_only  # `check` mode: never flattens, halts or sends anything
        self.bus = EventBus()
        self.rt = Runtime()
        self.stop_file = self.root / "STOP"
        restored = OffsetModel.from_dict(store.get_state("server_offset"))
        restored.detected = False  # re-confirmed from a live tick every session (DST may have changed meanwhile)
        self.server_time = ServerTime(restored)
        self._mt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self._trade_lock = asyncio.Lock()  # orders and paper fills
        self._risk_lock = asyncio.Lock()  # validate-then-execute, one pair at a time
        self._reconcile_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._spec_cache: dict[str, SymbolSpec] = {}
        self._bench_unreal: dict[str, float] = {}
        self.forced_precheck: str | None = None
        self.loading: set[str] = set()  # pairs whose price history MT5 is still downloading
        self.books: dict[str, Book] = {}
        self._fallback = Book(self, gateway.primary)  # until the first connect resolves the pairs

    # ------------------------------------------------------------ basics
    @property
    def cfg(self) -> Config:
        return self.config.cfg

    @property
    def mode(self) -> str:
        return "dry" if self.cfg.app.dry_run else "live"

    @property
    def primary(self) -> Book:
        return next(iter(self.books.values()), self._fallback)

    # One-pair shortcuts (the primary pair); the paper ledger (balance) is shared by every pair.
    @property
    def paper(self) -> PaperExecutor:
        return self.primary.paper

    @property
    def live(self) -> LiveExecutor:
        return self.primary.live

    @property
    def spec(self) -> SymbolSpec | None:
        return self.primary.spec

    @property
    def executor(self):
        return self.primary.executor(self.mode)

    def trading_books(self, only: list[str] | None = None) -> list[Book]:
        """Books Opus is asked about: configured pairs the broker has, in config order."""
        want = only or self.cfg.app.symbols
        return [self.books[s] for s in want if s in self.books]

    def book_for_broker(self, broker_symbol: str) -> Book | None:
        return next((b for b in self.books.values() if b.broker_symbol == broker_symbol), None)

    def configured_name(self, broker_symbol: str) -> str:
        b = self.book_for_broker(broker_symbol)
        if b:
            return b.symbol
        return next((c for c, n in self.gw.names.items() if n == broker_symbol), broker_symbol)

    async def mt(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._mt_pool, partial(fn, *args, **kwargs))

    def halt_state(self) -> dict | None:
        return self.store.get_state("halt")

    def paused(self) -> bool:
        return bool(self.store.get_state("paused", False))

    def gate(self, purpose: str) -> str | None:
        """Last check before an order leaves the app. Closing is always allowed (it reduces risk)."""
        if purpose in ("close", "remove"):
            return None
        if self.rt.blocked:
            return self.rt.blocked
        if self.stop_file.exists():
            return "STOP file present"
        halt = self.halt_state()
        if halt:
            return f"Halted: {halt['reason']}"
        return None

    def event(self, kind: str, message: str, level: str = "info", data: Any = None) -> None:
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, "%s: %s", kind, message)
        self.store.log_event(kind, message, level, data)

    # ------------------------------------------------------------ lifecycle
    def key_warning(self) -> str | None:
        problem = getattr(self.llm, "key_problem", None)
        return f"{problem}. Put your key in .env and restart Opus Trader; until then every bar is a HOLD." if problem else None

    async def start(self, run_tasks: bool = True) -> None:
        self.rt.started_at = self.clock.now()
        self._running = True
        self.rt.model_warning = self.key_warning()
        if self.rt.model_warning:
            log.warning(self.rt.model_warning)
        await self.ensure_connected(force=True)
        if self.rt.connected:
            await self.safe_reconcile()
            await self.paper_catch_up()
        if run_tasks:
            self._tasks = [asyncio.create_task(self.bar_loop(), name="bar_loop"), asyncio.create_task(self.watchdog(), name="watchdog")]

    async def shutdown(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await self.mt(self.gw.shutdown)
        finally:
            self._mt_pool.shutdown(wait=False, cancel_futures=True)
        self.event("shutdown", "Engine stopped; open positions keep their stop-loss and take-profit at the broker.")

    # ------------------------------------------------------------ connection and pairs
    def open_symbols(self) -> list[str]:
        """Pairs with a bot trade or order recorded as open, in either mode."""
        rows = self.store.query("SELECT DISTINCT symbol FROM trades WHERE status='open' UNION SELECT DISTINCT symbol FROM paper_orders WHERE status='pending'")
        return [r["symbol"] for r in rows if r["symbol"]]

    def wanted_symbols(self) -> list[str]:
        """Configured pairs, plus any pair that still has an open bot trade (it keeps being managed)."""
        want = list(self.cfg.app.symbols)
        return want + [s for s in self.open_symbols() if s not in want]

    async def ensure_connected(self, force: bool = False) -> bool:
        if self.rt.blocked:
            return False
        if self.rt.connected:
            return True
        now = self.clock.now()
        if not force and self.rt.reconnect_at and now < self.rt.reconnect_at:
            return False
        self.gw.configured_symbols = self.wanted_symbols()
        try:
            acct = await self.mt(self.gw.connect)
            await self.sync_books()
            account = await self.mt(self.gw.account)
        except SafetyError as exc:
            if exc.fatal:
                self.block(exc.message)
            return False
        except NotConnected as exc:
            self._schedule_reconnect(str(exc), exc.hint)
            return False
        except Exception as exc:  # noqa: BLE001 - the terminal can fail in odd ways
            self._schedule_reconnect(f"{type(exc).__name__}: {exc}")
            return False
        was_disconnected = self.rt.reconnect_attempt > 0
        self.rt.connected = True
        self.rt.reconnect_attempt = 0
        self.rt.reconnect_at = None
        self.rt.last_error = None
        self.rt.connect_hint = None
        self.rt.account = acct.to_dict()
        if self.mode == "dry" or self.store.get_state("paper_balance") is None:
            self.paper.ensure_balance(account.balance)
        pairs = ", ".join(b.broker_symbol for b in self.books.values())
        self.event("connected", f"{'Reconnected' if was_disconnected else 'Attached'} to MT5: account {acct.login} on {acct.server}; pairs {pairs}")
        self.note_unavailable()
        self.push_banner()
        return True

    async def sync_books(self) -> None:
        """One book per pair the broker has (configured pairs first, then pairs that are only being managed)."""
        available = self.gw.available_symbols()
        books = {}
        for sym in available:
            books[sym] = self.books.get(sym) or (self._fallback if self._fallback.symbol == sym and not self.books else Book(self, sym))
        self.books = books
        for book in self.books.values():
            info = await self.mt(book.view.symbol_info)
            specified = await self.mt(book.view.supports_specified_expiration)
            book.spec = SymbolSpec.from_info(info, specified)
            self._spec_cache[book.broker_symbol] = book.spec

    async def refresh_spec(self, book: Book) -> None:
        try:
            info = await self.mt(book.view.symbol_info)
        except NotConnected:
            return
        book.spec = SymbolSpec.from_info(info, book.spec.specified_expiration if book.spec else True)
        self._spec_cache[book.broker_symbol] = book.spec

    async def resync_symbols(self) -> None:
        """After app.symbols changed in Settings: find the new pairs at the broker and open their books."""
        if not self.rt.connected:
            return  # done on the next connect
        try:
            await self.mt(self.gw.resolve_symbols, self.wanted_symbols())
            await self.sync_books()
        except NotConnected as exc:
            self.lost_connection(str(exc), exc.hint)
            return
        except SafetyError as exc:
            self.event("symbols", exc.message, "warning")
            return
        self.event("symbols", "Trading " + ", ".join(b.broker_symbol for b in self.trading_books()))
        self.note_unavailable()
        self.push_status(force=True)

    def note_unavailable(self) -> None:
        key = tuple(sorted(self.gw.unavailable.items()))
        if key != self.rt.unavailable_noted:
            self.rt.unavailable_noted = key
            for sym, why in self.gw.unavailable.items():
                self.event("symbols", f"{sym} is skipped: {why}", "warning")

    def _schedule_reconnect(self, reason: str, hint: str | None = None) -> None:
        delay = BACKOFF[min(self.rt.reconnect_attempt, len(BACKOFF) - 1)]
        self.rt.reconnect_attempt += 1
        self.rt.reconnect_at = self.clock.now() + timedelta(seconds=delay)
        if self.rt.last_error != reason:
            self.event("disconnected", f"MT5 not reachable: {reason}", "warning")
        self.rt.last_error = reason
        self.rt.connect_hint = hint
        self.push_banner()

    def lost_connection(self, reason: str, hint: str | None = None) -> None:
        if self.rt.connected:
            self.rt.connected = False
            self._schedule_reconnect(reason, hint)

    def block(self, message: str) -> None:
        """Fatal safety stop: no more trading until the app is restarted."""
        if self.rt.blocked != message:
            self.rt.blocked = message
            self.rt.connected = False
            self.event("blocked", message, "error")
            self.push_banner()

    # ------------------------------------------------------------ market data
    async def fetch_bars(self, book: Book | None = None) -> dict[str, snap_mod.Bars]:
        book = book or self.primary
        mt5 = self.gw.mt5
        out = {}
        for name, tf in (("M15", mt5.TIMEFRAME_M15), ("H1", mt5.TIMEFRAME_H1), ("H4", mt5.TIMEFRAME_H4)):
            rates = await self.mt(book.view.rates, tf, 1, snap_mod.FETCH_BARS)
            out[name] = snap_mod.Bars.from_rates(rates, self.server_time)
        book.last_bars = out
        return out

    async def observe_book(self, book: Book) -> Any:
        tick = await self.mt(book.view.tick)
        if not (tick.bid > 0 and tick.ask > 0):  # a pair MT5 has only just added can report an empty quote
            book.bid = book.ask = None
            return tick
        book.bid, book.ask, book.tick_time = float(tick.bid), float(tick.ask), int(tick.time)
        if book is self.primary:
            self.rt.bid, self.rt.ask = book.bid, book.ask
        return tick

    async def observe_tick(self) -> Any:
        """Refresh every pair's quote and confirm the broker's clock from the freshest tick. Returns the primary pair's tick."""
        first = None
        for i, book in enumerate(self.books.values() or [self.primary]):
            try:
                tick = await self.observe_book(book)
            except NoData:
                book.bid = book.ask = None  # one pair without a quote doesn't stop the others
                continue
            except NotConnected:
                if i == 0:
                    raise  # the terminal itself is gone
                book.bid = book.ask = None
                continue
            first = first or tick
        latest = max((b.tick_time for b in self.books.values() if b.tick_time), default=None)
        if latest:
            changed = self.server_time.observe_tick(latest, self.clock.now())
            if changed:
                self.store.set_state("server_offset", changed.to_dict())
                self.event("server_time", f"Broker server time detected: {changed.describe()}")
            self.rt.tick_time = self.server_time.to_utc(latest)
        return first

    def spread_pips(self) -> float | None:
        return self.primary.spread_pips()

    async def spec_for(self, broker_symbol: str) -> SymbolSpec:
        if broker_symbol not in self._spec_cache:
            self._spec_cache[broker_symbol] = SymbolSpec.from_info(await self.mt(self.gw.symbol_info, broker_symbol))
        return self._spec_cache[broker_symbol]

    # ------------------------------------------------------------ book state (all pairs)
    def today_start(self, now: datetime | None = None) -> datetime:
        return trading_day_start(now or self.clock.now())

    async def unrealized(self, mode: str | None = None) -> float:
        mode = mode or self.mode
        if mode == "dry":
            return round(sum(b.paper.unrealized(b.bid, b.ask) for b in self.books.values() if b.bid is not None), 2)
        if not self.rt.connected:
            return 0.0
        ps = await self.mt(self.gw.all_bot_positions)
        return round(sum(p.profit + p.swap for p in ps), 2)

    async def pair_unrealized(self, book: Book, mode: str) -> float:
        if mode == "dry":
            return 0.0 if book.bid is None else book.paper.unrealized(book.bid, book.ask)
        ps = await self.mt(book.view.bot_positions)
        return round(sum(p.profit + p.swap for p in ps), 2)

    def realized_since(self, start: datetime, mode: str | None = None) -> float:
        row = self.store.one("SELECT COALESCE(SUM(pnl_eur),0) AS s FROM trades WHERE mode=? AND status='closed' AND close_time>=?",
                             (mode or self.mode, fmt_utc(start)))
        return float(row["s"])

    async def equity_now(self) -> float:
        if self.mode == "dry":
            return self.paper.balance() + await self.unrealized("dry")
        acct = await self.mt(self.gw.account)
        return float(acct.equity)

    async def day_pnl(self) -> tuple[float, float]:
        """(bot P&L today across all pairs, start-of-day equity) for the current mode."""
        start = self.today_start()
        pnl = self.realized_since(start) + await self.unrealized()
        key = f"sod:{self.mode}"
        sod = self.store.get_state(key)
        if not sod or sod.get("day") != fmt_utc(start):
            equity = await self.equity_now()
            sod = {"day": fmt_utc(start), "equity": round(equity - pnl, 2)}
            self.store.set_state(key, sod)
        return round(pnl, 2), float(sod["equity"])

    def entries_today(self) -> int:
        row = self.store.one("SELECT COUNT(*) AS n FROM decisions WHERE mode=? AND verdict='APPROVED' AND action IN ('OPEN_LONG','OPEN_SHORT') AND created_at>=?",
                             (self.mode, fmt_utc(self.today_start())))
        return int(row["n"])

    def cooldown_left(self, now: datetime, symbol: str | None = None) -> int:
        n = self.cfg.risk.cooldown_bars_after_loss
        if n <= 0:
            return 0
        symbol = symbol or self.primary.symbol
        last = self.store.one("SELECT close_time, pnl_eur FROM trades WHERE mode=? AND symbol=? AND status='closed' ORDER BY close_time DESC LIMIT 1",
                              (self.mode, symbol))
        if not last or (last["pnl_eur"] or 0) >= 0:
            return 0
        bars_since = int((now - parse_utc(last["close_time"])) // BAR)
        return max(0, n - bars_since)

    def entry_block(self, now: datetime, symbol: str | None = None) -> str | None:
        s = self.cfg.schedule
        if weekend_window(now, s.friday_no_entries_utc, s.sunday_resume_utc):
            return f"weekend window (no new entries after Fri {s.friday_no_entries_utc} UTC)"
        n = self.cfg.news
        ev = self.news.blackout(now, n.blackout_before_min, n.blackout_after_min, pair_currencies(symbol or self.primary.symbol))
        if ev:
            return f"news blackout ({ev.currency} {ev.title} at {ev.time:%H:%M} UTC)"
        return None

    async def open_trades(self, mode: str | None = None) -> list[OpenTrade]:
        """Every bot position and pending order in this mode, on any pair."""
        mode = mode or self.mode
        if mode == "dry":
            out = [OpenTrade(r["symbol"], r["side"], "position") for r in self.store.query("SELECT symbol, side FROM trades WHERE mode='dry' AND status='open' ORDER BY id")]
            out += [OpenTrade(r["symbol"], r["side"], "pending") for r in self.store.query("SELECT symbol, side FROM paper_orders WHERE status='pending' ORDER BY ticket")]
            return out
        mt5 = self.gw.mt5
        positions = await self.mt(self.gw.all_bot_positions)
        orders = await self.mt(self.gw.all_bot_orders)
        out = [OpenTrade(self.configured_name(p.symbol), "long" if p.type == mt5.POSITION_TYPE_BUY else "short", "position") for p in positions]
        out += [OpenTrade(self.configured_name(o.symbol), "long" if o.type == mt5.ORDER_TYPE_BUY_LIMIT else "short", "pending")
                for o in orders if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT)]
        return out

    # ------------------------------------------------------------ reconcile (MT5 is the truth)
    async def safe_reconcile(self) -> None:
        try:
            if self.mode == "live" or self.store.one("SELECT 1 FROM trades WHERE mode='live' AND status='open'"):
                await self.reconcile_live()
        except NotConnected as exc:
            self.lost_connection(str(exc), exc.hint)
        except Exception:  # noqa: BLE001
            log.exception("Reconcile failed")

    async def reconcile_live(self) -> list[str]:
        async with self._reconcile_lock:
            return await self._reconcile_live()

    async def _reconcile_live(self) -> list[str]:
        mt5 = self.gw.mt5
        positions = await self.mt(self.gw.all_bot_positions)
        orders = await self.mt(self.gw.all_bot_orders)
        self.rt.position_count = len(positions) + len(orders)
        open_rows = {r["ticket"]: r for r in self.store.query("SELECT * FROM trades WHERE mode='live' AND status='open'")}
        changes = []
        for p in positions:
            side = "long" if p.type == mt5.POSITION_TYPE_BUY else "short"
            if p.ticket in open_rows:
                self.store.execute("UPDATE trades SET sl=?, tp=? WHERE mode='live' AND ticket=?", (p.sl, p.tp, p.ticket))
                continue
            link = self.store.one("SELECT o.decision_id, d.setup, d.confidence, d.signal_agreement FROM orders o LEFT JOIN decisions d ON d.id=o.decision_id "
                                  "WHERE o.mode='live' AND o.order_ticket=? AND o.ok=1 ORDER BY o.id DESC LIMIT 1", (p.identifier,)) or {}
            opened = self.server_time.to_utc(p.time)
            spec = await self.spec_for(p.symbol)
            risk = abs(p.price_open - p.sl) / spec.tick_size * spec.tick_value_loss * p.volume if p.sl else None
            self.store.upsert("trades", {
                "mode": "live", "symbol": self.configured_name(p.symbol), "ticket": p.ticket, "decision_id": link.get("decision_id"), "side": side,
                "setup": link.get("setup"), "confidence": link.get("confidence"), "signal_agreement": link.get("signal_agreement"), "session": session_of(opened),
                "open_time": fmt_utc(opened), "open_price": p.price_open, "volume": p.volume, "sl_initial": p.sl, "tp_initial": p.tp,
                "sl": p.sl, "tp": p.tp, "risk_eur": None if risk is None else round(risk, 2), "status": "open",
            }, ["mode", "ticket"])
            changes.append("opened")
        live_tickets = {p.ticket for p in positions}
        for ticket, row in open_rows.items():
            if ticket in live_tickets:
                continue
            deals = await self.mt(self.gw.position_deals, ticket)
            outs = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", 3))]
            if not outs:
                continue  # history not synced yet; try again next time
            last = max(outs, key=lambda d: d.time_msc)
            pnl = round(sum(d.profit + d.swap + d.commission + getattr(d, "fee", 0.0) for d in deals), 2)
            reason = self.store.get_state(f"close_reason:{ticket}") or DEAL_REASON_MAP.get(last.reason, "broker")
            self.store.del_state(f"close_reason:{ticket}")
            r = round(pnl / row["risk_eur"], 2) if row["risk_eur"] else None
            self.store.update("trades", row["id"], {"close_time": fmt_utc(self.server_time.to_utc(last.time)), "close_price": last.price,
                                                    "exit_reason": reason, "pnl_eur": pnl, "r_multiple": r, "status": "closed"})
            changes.append("closed")
        # Pending orders the symbol couldn't expire by itself (GTC fallback).
        expiries = self.store.get_state("live_pending_expiry", {})
        now = self.clock.now()
        for o in orders:
            exp = expiries.get(str(o.ticket))
            if exp and now >= parse_utc(exp):
                ex = self.live_executor_for(o.symbol)
                async with self._trade_lock:
                    await self.mt(ex.cancel_pending, o.ticket, "expired")
                changes.append("expired")
        if changes:
            self.bus.publish("trade", {"changes": changes})
        return changes

    def live_executor_for(self, broker_symbol: str) -> LiveExecutor:
        book = self.book_for_broker(broker_symbol)
        if book:
            return book.live
        # A bot position on a pair that is no longer traded: still closable by ticket.
        return LiveExecutor(self.gw.view(self.configured_name(broker_symbol)), self.store, self.cfg.app.magic,
                            lambda: self.cfg.risk.deviation_points, self.server_time, gate=self.gate)

    def paper_open(self) -> bool:
        return bool(self.store.one("SELECT 1 FROM trades WHERE mode='dry' AND status='open'")
                    or self.store.one("SELECT 1 FROM paper_orders WHERE status='pending'"))

    async def paper_catch_up(self) -> None:
        """Replay M1 bars for the time the app wasn't watching a paper position or order."""
        if not self.paper_open():
            self.store.set_state("paper_checked_until", fmt_utc(self.clock.now()))
            return
        since = parse_utc(self.store.get_state("paper_checked_until")) or self.clock.now()
        await self.paper_backfill(since, self.clock.now())

    async def paper_backfill(self, since: datetime, until: datetime) -> list[str]:
        if until - since < timedelta(minutes=1) or not self.paper_open():
            self.store.set_state("paper_checked_until", fmt_utc(until))
            return []
        events: list[str] = []
        for book in self.books.values():
            if not (book.paper.open_trade_row() or book.paper.pending_row()) or book.spec is None:
                continue
            try:
                rates = await self.mt(book.view.rates_range_server, self.gw.mt5.TIMEFRAME_M1, self.server_time.to_server_epoch(since),
                                      self.server_time.to_server_epoch(until))
            except NoData:
                continue  # checked again on the next bar
            bars = snap_mod.Bars.from_rates(rates, self.server_time)
            sel = (bars.t >= since.timestamp() - 59) & (bars.t + 60 <= until.timestamp())
            events += book.paper.backfill(bars.t[sel], bars.o[sel], bars.h[sel], bars.l[sel], bars.c[sel], bars.spread[sel] * book.spec.point)
        self.store.set_state("paper_checked_until", fmt_utc(until))
        if events:
            self.bus.publish("trade", {"changes": events})
        return events

    # ------------------------------------------------------------ halts and commands
    def set_halt(self, kind: str, reason: str, until: datetime | None = None) -> None:
        self.store.set_state("halt", {"kind": kind, "reason": reason, "since": now_iso(), "until": fmt_utc(until)})
        self.event("halt", f"Halted: {reason}" + (f" until {until:%Y-%m-%d %H:%M} UTC" if until else ""), "warning")
        self.push_banner()

    def clear_halt(self, why: str) -> None:
        if self.halt_state():
            self.store.del_state("halt")
            self.event("resume", why)
            self.push_banner()

    async def kill(self, reason: str, source: str) -> dict:
        """Kill switch: close the bot's positions, cancel its orders, halt."""
        if self.read_only:
            return {"closed": 0, "cancelled": 0, "failed": 0}
        self.set_halt("kill_switch", reason)
        summary = await self.flatten("kill_switch")
        self.event("kill_switch", f"{reason} ({source}): closed {summary['closed']} position(s), cancelled {summary['cancelled']} order(s)", "warning", summary)
        self.push_status(force=True)
        return summary

    async def flatten(self, reason: str) -> dict:
        """Close every bot position and cancel every bot order, on every pair (magic number only)."""
        closed = cancelled = failed = 0
        if self.read_only:
            return {"closed": 0, "cancelled": 0, "failed": 0}
        async with self._trade_lock:
            for book in list(self.books.values()):
                try:
                    pos, pend = book.paper.position(), book.paper.pending()
                    if pos:
                        r = await self.mt(book.paper.close_position, pos.ticket, reason)
                        closed, failed = closed + r.ok, failed + (not r.ok)
                    if pend:
                        r = await self.mt(book.paper.cancel_pending, pend.ticket, reason)
                        cancelled, failed = cancelled + r.ok, failed + (not r.ok)
                except NotConnected as exc:
                    self.lost_connection(str(exc), exc.hint)
                    failed += 1
            if self.rt.connected:
                try:
                    for p in await self.mt(self.gw.all_bot_positions):
                        r = await self.mt(self.live_executor_for(p.symbol).close_position, p.ticket, reason)
                        closed, failed = closed + r.ok, failed + (not r.ok)
                    for o in await self.mt(self.gw.all_bot_orders):
                        r = await self.mt(self.live_executor_for(o.symbol).cancel_pending, o.ticket, reason)
                        cancelled, failed = cancelled + r.ok, failed + (not r.ok)
                except NotConnected as exc:
                    self.lost_connection(str(exc), exc.hint)
                    failed += 1
                except SafetyError as exc:
                    self.event("flatten_blocked", exc.message, "error")
                    failed += 1
        await self.safe_reconcile()
        self.bus.publish("trade", {"changes": ["flatten"]})
        return {"closed": closed, "cancelled": cancelled, "failed": failed}

    def pause(self) -> None:
        self.store.set_state("paused", True)
        self.event("pause", "Paused from the app: no model calls until resumed")
        self.push_status(force=True)

    def resume(self) -> str | None:
        """Returns an error message if resuming isn't possible."""
        if self.rt.blocked:
            return self.rt.blocked
        if self.stop_file.exists():
            return "Delete the STOP file in the Opus Trader folder to resume."
        halt = self.halt_state()
        if halt and halt["kind"] == "daily_limit":
            return f"The daily loss limit halt ends at 17:00 New York ({halt['until']})."
        self.store.set_state("paused", False)
        self.clear_halt("Resumed from the app")
        self.push_status(force=True)
        return None

    def has_open_book(self, mode: str, symbol: str | None = None) -> bool:
        """Any bot position or order in this mode (on one pair, if given)."""
        sym_sql, params = (" AND symbol=?", (symbol,)) if symbol else ("", ())
        if mode == "dry":
            return bool(self.store.one(f"SELECT 1 FROM trades WHERE mode='dry' AND status='open'{sym_sql}", params)
                        or self.store.one(f"SELECT 1 FROM paper_orders WHERE status='pending'{sym_sql}", params))
        if self.store.one(f"SELECT 1 FROM trades WHERE mode='live' AND status='open'{sym_sql}", params):
            return True
        return self.rt.position_count > 0 if symbol is None else False

    def symbols_change_error(self, new: list[str]) -> str | None:
        """A pair can't be switched off while it holds a bot trade or order."""
        removed = [s for s in self.cfg.app.symbols if s not in new]
        busy = [s for s in removed if self.has_open_book("dry", s) or self.has_open_book("live", s)]
        if busy:
            return f"Close the open {', '.join(busy)} trade{'s' if len(busy) > 1 else ''} first."
        return None

    def set_dry_run(self, value: bool) -> str | None:
        if value == self.cfg.app.dry_run:
            return None
        if self.has_open_book(self.mode):
            return "Close the open positions and pending orders first."
        self.config.update({"app.dry_run": value}, source="ui", allow_locked=True)
        self.event("mode", "Dry run turned on: simulated fills" if value else "Dry run turned off: approved trades are sent to the MT5 demo account", "warning")
        self.bus.publish("settings", {"keys": ["app.dry_run"]})
        self.push_status(force=True)
        return None

    # ------------------------------------------------------------ pre-checks
    async def precheck(self, now: datetime) -> str | None:
        """Account-wide reasons to skip every pair this bar."""
        if self.rt.blocked:
            return self.rt.blocked
        if self.stop_file.exists():
            if not (self.halt_state() or {}).get("kind") == "kill_switch":
                await self.kill("STOP file found", "file")
            return "Kill switch: STOP file present"
        halt = self.halt_state()
        if halt:
            return f"Halted: {halt['reason']}"
        if self.paused():
            return "Paused from the app"
        if not self.rt.connected:
            return "MT5 disconnected"
        if not self.rt.broker_connected:
            return "MT5 has no connection to the broker"
        if not self.rt.algo_on:
            return "Algo Trading is off in MT5"
        s = self.cfg.schedule
        if weekend_window(now, s.friday_no_entries_utc, s.sunday_resume_utc):
            return f"Weekend window (Fri {s.friday_no_entries_utc} to Sun {s.sunday_resume_utc} UTC)"
        if await self.check_daily_loss():
            return f"Halted: {self.halt_state()['reason']}"
        return None

    def precheck_pair(self, book: Book, now: datetime) -> str | None:
        """Reasons to skip one pair this bar: news for its currencies, a wide spread, no quote."""
        n = self.cfg.news
        ev = self.news.blackout(now, n.blackout_before_min, n.blackout_after_min, book.currencies)
        if ev:
            return f"News blackout: {ev.currency} {ev.title} at {ev.time:%H:%M} UTC"
        sp = book.spread_pips()
        if sp is not None and sp > self.cfg.risk.max_spread_pips + 1e-9:
            return f"Spread {sp:.1f} pips > {self.cfg.risk.max_spread_pips:.1f} max"
        return None

    async def check_daily_loss(self) -> bool:
        """Flatten and halt until 17:00 New York when the bot's loss today reaches the limit."""
        if (self.halt_state() or {}).get("kind") == "daily_limit":
            return True
        if self.read_only:
            return False
        pnl, sod = await self.day_pnl()
        limit = sod * self.cfg.risk.daily_loss_limit_pct / 100.0
        if sod > 0 and pnl <= -limit:
            pct = pnl / sod * 100
            until = next_trading_day_start(self.clock.now())
            self.set_halt("daily_limit", f"daily loss limit hit ({pct:+.2f}%)".replace("-", "−"), until)
            await self.flatten("daily_limit")
            return True
        return False

    # ------------------------------------------------------------ the bar
    async def bar_loop(self) -> None:
        while self._running:
            now = self.clock.now()
            target = next_bar_close(now) + timedelta(seconds=self.cfg.app.bar_delay_s)
            if target - now > BAR:
                target -= BAR
            self.rt.next_bar_at = target
            self.push_status(force=True)
            await asyncio.sleep(max(0.0, (target - self.clock.now()).total_seconds()))
            bar_open = next_bar_close(target - timedelta(seconds=self.cfg.app.bar_delay_s)) - 2 * BAR
            await self.safe_process_bar(bar_open)

    async def safe_process_bar(self, bar_open: datetime, wait: bool = True, repeat: bool = False, symbols: list[str] | None = None) -> list[int]:
        """Decide every pair for one bar. Returns the decision ids written (one per pair, or one for a skip of every pair)."""
        try:
            return await self.process_bar(bar_open, wait, repeat=repeat, symbols=symbols)
        except Exception as exc:  # noqa: BLE001 - one bad bar never stops the loop
            log.exception("Bar %s failed", bar_open)
            self.event("bar_error", f"Bar {bar_open:%H:%M} UTC failed: {type(exc).__name__}: {exc}", "error")
            for b in self.books.values():
                b.thinking_since = None
            for pending in self.store.query("SELECT id FROM decisions WHERE verdict='PENDING'"):
                self.store.update("decisions", pending["id"], {"action": "HOLD", "verdict": "ERROR", "verdict_reason": f"{type(exc).__name__}: {exc}; treated as HOLD"})
                self.after_decision(pending["id"])
            self.push_status(force=True)
            return []

    async def wait_for_bars(self, books: list[Book], bar_open: datetime, wait: bool = True) -> dict[str, dict[str, snap_mod.Bars]]:
        """Fetch each pair's bars until the bar that opened at `bar_open` has closed on it (or 30 s pass).

        A pair whose history MT5 is still downloading is left out (`self.loading` names it) and the others go ahead."""
        deadline = self.clock.now() + timedelta(seconds=30 if wait else 0)
        ready: dict[str, dict[str, snap_mod.Bars]] = {}
        self.loading = set()
        while True:
            for book in books:
                if book.symbol in ready:
                    continue
                try:
                    bars = await self.fetch_bars(book)
                except NoData as exc:
                    log.info("%s", exc)
                    self.loading.add(book.symbol)
                    continue
                m15 = bars["M15"]
                if len(m15) < MIN_HISTORY or len(bars["H4"]) < 60:
                    self.loading.add(book.symbol)
                    continue
                self.loading.discard(book.symbol)
                if int(m15.t[-1]) >= int(bar_open.timestamp()):
                    ready[book.symbol] = bars
            if len(ready) == len(books) or self.clock.now() >= deadline:
                return ready
            await asyncio.sleep(2)

    def decided(self, bar_key: str, mode: str, symbol: str) -> bool:
        return bool(self.store.one("SELECT 1 FROM decisions WHERE bar_time=? AND mode=? AND symbol IN (?, ?)", (bar_key, mode, symbol, ALL)))

    async def process_bar(self, bar_open: datetime, wait: bool = True, force: bool = False, repeat: bool = False,
                          symbols: list[str] | None = None) -> list[int]:
        """Run one decision cycle for the M15 bar that opened at `bar_open`, on every pair (or `symbols`).

        `force` (used only by `check`) still reports a failed pre-check but calls the model anyway.
        `repeat` (used only by the --sim preview controls) decides a bar that was already decided.
        """
        mode = self.mode
        now = self.clock.now()
        if self.rt.blocked:
            return []
        if not await self.ensure_connected():
            if fx_market_open(now):
                return self._ids(self.record_skip(bar_open, "MT5 disconnected", mode))
            return []
        books = self.trading_books(symbols)
        if not books:
            return []
        try:
            await self.observe_tick()
            if not self.server_time.model.detected and not force:
                # Bar times depend on the broker's clock: confirm it from a live tick first.
                await asyncio.sleep(1.5)
                await self.observe_tick()
                if not self.server_time.model.detected:
                    return self._ids(self.record_skip(bar_open, "Waiting for a live tick to confirm the broker's server time", mode)) if fx_market_open(now) else []
            bars_by = await self.wait_for_bars(books, bar_open, wait)
        except NotConnected as exc:
            self.lost_connection(str(exc), exc.hint)
            return self._ids(self.record_skip(bar_open, "MT5 disconnected", mode))
        if not bars_by and force:
            bars_by = {b.symbol: b.last_bars for b in books if len(b.last_bars.get("M15", []))}  # `check` on a closed market: the last closed bar there is
        if not bars_by:
            log.info("No closed bar for %s: market closed", bar_open)
            return []
        bar_open = max(bars["M15"].time(-1) for bars in bars_by.values())
        key = snap_mod.fmt_t(bar_open)
        ready = [b for b in books if b.symbol in bars_by and bars_by[b.symbol]["M15"].time(-1) == bar_open]
        for book in books:
            if book.symbol in self.loading and not self.decided(key, mode, book.symbol):
                self.record_skip(bar_open, f"MT5 is still loading the {book.broker_symbol} price history", mode, book.symbol)
        if not (repeat and self.sim):
            ready = [b for b in ready if not self.decided(key, mode, b.symbol)]
            if not ready:
                return []  # already decided (e.g. after a restart)
        if mode == "dry":
            await self.paper_backfill(parse_utc(self.store.get_state("paper_checked_until")) or now, now)
        await self.safe_reconcile()
        await self.record_equity(bar_open)
        for book in books:
            if book.symbol in bars_by:
                try:
                    await self.update_benchmark(book, bars_by[book.symbol]["M15"])
                except Exception:  # noqa: BLE001 - the benchmark must never block trading
                    log.exception("Benchmark update failed for %s", book.symbol)

        reason = await self.precheck(now)
        if reason and not force:
            return self._ids(self.record_skip(bar_open, reason, mode))
        self.forced_precheck = reason
        ids: dict[str, int | None] = {}
        to_decide = []
        for book in ready:
            await self.refresh_spec(book)  # tick values move with exchange rates (USDJPY in EUR)
            if book.bid is None:
                ids[book.symbol] = self.record_skip(bar_open, f"No price for {book.broker_symbol}", mode, book.symbol)
                continue
            why = self.precheck_pair(book, now)
            if why and not force:
                ids[book.symbol] = self.record_skip(bar_open, why, mode, book.symbol)
                continue
            self.forced_precheck = self.forced_precheck or why
            to_decide.append(book)
        sem = asyncio.Semaphore(max(1, self.cfg.model.max_concurrent_calls))
        results = await asyncio.gather(*(self.decide_pair(book, bars_by[book.symbol], bar_open, now, mode, sem) for book in to_decide))
        ids.update({book.symbol: did for book, did in zip(to_decide, results)})
        return [ids[b.symbol] for b in ready if ids.get(b.symbol)]

    @staticmethod
    def _ids(did: int | None) -> list[int]:
        return [did] if did else []

    async def decide_pair(self, book: Book, bars: dict[str, snap_mod.Bars], bar_open: datetime, now: datetime, mode: str, sem: asyncio.Semaphore) -> int | None:
        """Snapshot -> Opus -> risk engine -> order, for one pair. Never raises."""
        decision_id = None
        try:
            async with sem:
                await self.observe_book(book)
                snapshot, _ = await self.build_snapshot(book, bars, now, mode)
                book.last_snapshot = snapshot.data
                cfg = self.cfg
                system_text = system_prompt(cfg)
                digest = prompt_hash(system_text)
                self.store.save_prompt(digest, system_text, TOOL)
                request = build_request(cfg, system_text, snapshot.h4_text, snapshot.h1_text, snapshot.live_text)
                sig = snapshot.data["signal"]
                decision_id = self.store.insert("decisions", {
                    "created_at": now_iso(), "bar_time": snap_mod.fmt_t(bar_open), "mode": mode, "symbol": book.symbol, "status": "CALLED", "verdict": "PENDING",
                    "snapshot_json": Store.dumps(snapshot.data), "prompt_text": snapshot.prompt_text, "system_hash": digest,
                    "signal_json": Store.dumps({k: sig[k] for k in ("trend", "bars_since_flip", "flipped_now", "flip_now", "line", "line_distance_pips", "line_distance_atr", "cloud")}),
                })
                book.thinking_since = self.clock.now()
                self.bus.publish("thinking", {"symbol": book.symbol, "since": fmt_utc(book.thinking_since), "decision_id": decision_id})
                self.push_status(force=True)
                try:
                    result = await self.llm.decide(request, cfg, context=snapshot.data)
                finally:
                    book.thinking_since = None
                self.bus.publish("thinking", {"symbol": book.symbol, "since": None, "decision_id": decision_id})
            self.update_model_warning(result)
            row: dict[str, Any] = {
                "model": result.model, "stop_reason": result.stop_reason, "llm_status": result.status, "error": result.error,
                "response_json": None if result.raw is None else Store.dumps(result.raw), "latency_ms": result.latency_ms, "cost_usd": result.cost_usd,
                "input_tokens": result.usage.get("input_tokens"), "output_tokens": result.usage.get("output_tokens"),
                "cache_read_tokens": result.usage.get("cache_read_tokens"), "cache_write_tokens": result.usage.get("cache_write_tokens"),
            }
            position_side = (snapshot.data.get("position") or {}).get("side")
            rej_key = f"last_rejection:{mode}:{book.symbol}"
            if not result.ok:
                row.update({"action": "HOLD", "verdict": "NO_DECISION", "verdict_reason": result.hold_reason() + "; treated as HOLD",
                            "signal_agreement": ts.agreement("HOLD", sig["trend_value"], sig["flipped_now"], position_side)})
                self.store.update("decisions", decision_id, row)
                self.store.del_state(rej_key)
                self.after_decision(decision_id)
                return decision_id

            d = result.decision
            row.update({"action": d.action, "order_type": d.order_type, "entry": d.entry, "stop_loss": d.stop_loss, "take_profit": d.take_profit,
                        "confidence": d.confidence, "setup": d.setup, "reasoning": d.reasoning, "invalidation": d.invalidation,
                        "signal_agreement": ts.agreement(d.action, sig["trend_value"], sig["flipped_now"], position_side)})

            async with self._risk_lock:  # one pair at a time from here: the cross-pair limits see every earlier order
                if self.rt.blocked or self.halt_state() or self.stop_file.exists() or self.paused():
                    why = self.rt.blocked or ("STOP file present" if self.stop_file.exists() else (self.halt_state() or {}).get("reason") or "paused")
                    row.update({"verdict": "DISCARDED", "verdict_reason": f"Engine stopped while Opus was thinking ({why})"})
                    self.store.update("decisions", decision_id, row)
                    self.after_decision(decision_id)
                    return decision_id
                # Re-read the market and the book right before validating: Opus may have taken a while.
                await self.observe_book(book)
                await self.safe_reconcile()
                state = await self.risk_state(book, now, sig)
                mkt = Market(book.bid, book.ask, snapshot.m15_atr, self.clock.now())
                verdict = evaluate(d, book.spec, mkt, state, cfg)
                row["risk_json"] = Store.dumps({"calc": verdict.calc, "plan": None if verdict.plan is None else verdict.plan.to_dict()})
                if verdict.verdict == "HOLD":
                    row.update({"verdict": "HOLD", "verdict_reason": "Opus chose to hold"})
                    self.store.del_state(rej_key)
                elif verdict.verdict == "REJECTED":
                    row.update({"verdict": "REJECTED", "verdict_reason": verdict.reason})
                    self.store.set_state(rej_key, {"bar_time": snap_mod.fmt_t(bar_open + BAR), "action": d.action, "reason": verdict.reason})
                else:
                    self.store.del_state(rej_key)
                    ok, message = await self.execute(book, verdict.plan, decision_id, d, row["signal_agreement"])
                    row.update({"verdict": "APPROVED" if ok else "FAILED", "verdict_reason": verdict.reason if ok else f"{verdict.reason}. Order failed: {message}"})
                self.store.update("decisions", decision_id, row)
            await self.safe_reconcile()
            self.after_decision(decision_id)
            return decision_id
        except Exception as exc:  # noqa: BLE001 - one bad pair never stops the others
            log.exception("Decision for %s failed", book.symbol)
            book.thinking_since = None
            self.event("bar_error", f"{book.symbol} at {bar_open:%H:%M} UTC failed: {type(exc).__name__}: {exc}", "error")
            if isinstance(exc, NotConnected):
                self.lost_connection(str(exc), exc.hint)
            if decision_id:
                self.store.update("decisions", decision_id, {"action": "HOLD", "verdict": "ERROR", "verdict_reason": f"{type(exc).__name__}: {exc}; treated as HOLD"})
                self.after_decision(decision_id)
            return decision_id

    def update_model_warning(self, result) -> None:
        warning = self.key_warning()
        if not warning and result.status == "error" and any(k in (result.error or "").lower() for k in ("api_key", "api key", "authentication", "401", "403", "credit")):
            warning = f"Opus can't be reached ({result.error}). Check ANTHROPIC_API_KEY in .env; bars are treated as HOLD."
        if warning != self.rt.model_warning:
            self.rt.model_warning = warning
            self.push_banner()

    async def execute(self, book: Book, plan, decision_id: int, d: Decision, agreement: str) -> tuple[bool, str]:
        ex = book.executor(self.mode)
        async with self._trade_lock:
            reason = self.gate("open" if plan.kind.startswith("OPEN") else "modify" if plan.kind == "MOVE_STOP" else "close")
            if reason:
                return False, reason
            try:
                if plan.kind in ("OPEN_MARKET", "OPEN_LIMIT"):
                    if ex is book.paper:
                        res = await self.mt(book.paper.open, plan, decision_id, {"setup": d.setup, "confidence": d.confidence, "signal_agreement": agreement})
                    else:
                        res = await self.mt(book.live.open, plan, decision_id)
                elif plan.kind == "CLOSE_POSITION":
                    res = await self.mt(ex.close_position, plan.ticket, "model", decision_id)
                elif plan.kind == "CANCEL_PENDING":
                    res = await self.mt(ex.cancel_pending, plan.ticket, "model", decision_id)
                elif plan.kind == "MOVE_STOP":
                    res = await self.mt(ex.modify_stop, plan.ticket, plan.sl, plan.tp, decision_id)
                else:
                    return False, f"Unknown plan {plan.kind}"
            except SafetyError as exc:
                if exc.fatal:
                    self.block(exc.message)
                return False, exc.message
            except NotConnected as exc:
                self.lost_connection(str(exc), exc.hint)
                return False, "MT5 disconnected"
        if res.ok:
            self.bus.publish("trade", {"changes": [plan.kind.lower()], "symbol": book.symbol})
        return res.ok, res.message

    def record_skip(self, bar_open: datetime, reason: str, mode: str, symbol: str = ALL) -> int | None:
        key = snap_mod.fmt_t(bar_open)
        if self.decided(key, mode, symbol) if symbol != ALL else self.store.one("SELECT 1 FROM decisions WHERE bar_time=? AND mode=?", (key, mode)):
            return None
        decision_id = self.store.insert("decisions", {"created_at": now_iso(), "bar_time": key, "mode": mode, "symbol": symbol, "status": "SKIPPED",
                                                      "skip_reason": reason, "verdict": "SKIPPED", "verdict_reason": reason})
        self.after_decision(decision_id)
        return decision_id

    def after_decision(self, decision_id: int) -> None:
        self.bus.publish("decision", {"id": decision_id})
        self.push_status(force=True)

    async def risk_state(self, book: Book, now: datetime, signal: dict) -> RiskState:
        if self.mode == "dry":
            pos, pend = book.paper.position(), book.paper.pending()
        else:
            pos, pend = await self.mt(book.live.position), await self.mt(book.live.pending)
        others = [t for t in await self.open_trades() if t.symbol != book.symbol]
        return RiskState(equity=await self.equity_now(), position=pos, pending=pend, entries_today=self.entries_today(),
                         cooldown_bars_left=self.cooldown_left(now, book.symbol), entry_block=self.entry_block(now, book.symbol),
                         recent_flip_direction=signal.get("recent_flip_direction"), currency=self.rt.account.get("currency", "EUR"),
                         symbol=book.symbol, others=others)

    async def other_trades_detail(self, book: Book, mode: str) -> list[dict]:
        """The bot's trades on the other pairs, as the snapshot lists them."""
        out = []
        positions = {p.ticket: p for p in await self.mt(self.gw.all_bot_positions)} if mode == "live" else {}
        for t in self.store.query("SELECT * FROM trades WHERE mode=? AND status='open' AND symbol!=? ORDER BY id", (mode, book.symbol)):
            other = self.books.get(t["symbol"])
            digits = other.spec.digits if other and other.spec else 5
            if mode == "dry":
                unreal = other.paper.unrealized(other.bid, other.ask) if other and other.bid is not None else None
            else:
                p = positions.get(t["ticket"])
                unreal = None if p is None else p.profit + p.swap
            out.append({"symbol": t["symbol"], "side": t["side"], "kind": "position", "price": f"{t['open_price']:.{digits}f}",
                        "opened": (t["open_time"] or "").replace("T", " ")[:16],
                        "unrealized_r": round(unreal / t["risk_eur"], 2) if unreal is not None and t["risk_eur"] else None})
        for t in await self.open_trades(mode):
            if t.kind == "pending" and t.symbol != book.symbol:
                other = self.books.get(t.symbol)
                price = None
                if mode == "dry" and other:
                    row = other.paper.pending_row()
                    price = row["price"] if row else None
                elif other:
                    pend = await self.mt(other.live.pending)
                    price = pend.price if pend else None
                digits = other.spec.digits if other and other.spec else 5
                out.append({"symbol": t.symbol, "side": t.side, "kind": "pending", "price": "n/a" if price is None else f"{price:.{digits}f}"})
        return out

    async def build_snapshot(self, book: Book, bars: dict[str, snap_mod.Bars], now: datetime, mode: str) -> tuple[snap_mod.Snapshot, RiskState]:
        cfg = self.cfg
        acct = await self.mt(self.gw.account)
        pnl, sod = await self.day_pnl()
        if mode == "dry":
            balance, equity = self.paper.balance(), self.paper.balance() + await self.unrealized("dry")
        else:
            balance, equity = float(acct.balance), float(acct.equity)
        pre_sig = ts.compute(bars["M15"].h, bars["M15"].l, bars["M15"].c, cfg.signal.atr_length, cfg.signal.multiplier, cfg.signal.atr_method)
        flips = pre_sig.flip_indices()
        last = len(bars["M15"]) - 1
        recent_dir = None
        if len(flips) and last - int(flips[-1]) < cfg.signal.filter_bars:
            recent_dir = "long" if pre_sig.buy[int(flips[-1])] else "short"
        state = await self.risk_state(book, now, {"recent_flip_direction": recent_dir})
        blocked = state.blocked_reasons(cfg)
        position = pending = None
        if state.position:
            p = state.position
            unreal = await self.pair_unrealized(book, mode)
            row = self.store.one("SELECT open_time, risk_eur FROM trades WHERE mode=? AND ticket=?", (mode, p.ticket)) or {}
            opened = parse_utc(row.get("open_time")) or now
            risk = row.get("risk_eur") or 0
            position = {"side": p.side, "volume": p.volume, "entry": p.entry, "sl": p.sl, "tp": p.tp, "opened": snap_mod.fmt_t(opened),
                        "bars_open": int((now - opened) // BAR), "unrealized": unreal, "unrealized_r": round(unreal / risk, 2) if risk else 0.0}
        elif state.pending:
            pd = state.pending
            if mode == "dry":
                exp = (book.paper.pending_row() or {}).get("expires_at")
            else:
                exp = self.store.get_state("live_pending_expiry", {}).get(str(pd.ticket))
            pending = {"side": pd.side, "volume": pd.volume, "price": pd.price, "sl": pd.sl, "tp": pd.tp,
                       "expires": snap_mod.fmt_t(parse_utc(exp)) if exp else "n/a"}
        trades = self.store.query("SELECT close_time, side, setup, r_multiple, exit_reason FROM trades WHERE mode=? AND symbol=? AND status='closed' "
                                  "ORDER BY close_time DESC LIMIT 10", (mode, book.symbol))
        recent_trades = [{"close_time": t["close_time"].replace("T", " ")[:16], "side": t["side"], "setup": t["setup"], "r_multiple": t["r_multiple"] or 0.0,
                          "exit_reason": EXIT_LABELS.get(t["exit_reason"], t["exit_reason"])} for t in trades]
        decs = self.store.query("SELECT bar_time, action, verdict, reasoning FROM decisions WHERE mode=? AND symbol=? AND status='CALLED' AND reasoning IS NOT NULL "
                                "ORDER BY id DESC LIMIT 3", (mode, book.symbol))
        last_rej = self.store.get_state(f"last_rejection:{mode}:{book.symbol}")
        everything = list(state.others) + ([OpenTrade(book.symbol, state.position.side)] if state.position else []) + \
            ([OpenTrade(book.symbol, state.pending.side, "pending")] if state.pending else [])
        x = snap_mod.SnapshotInputs(
            bar_time=bars["M15"].time(-1), now=now, symbol=book.symbol, digits=book.spec.digits, point=book.spec.point,
            m15=bars["M15"], h1=bars["H1"], h4=bars["H4"], bid=book.bid, ask=book.ask, mode=mode, currency=self.rt.account.get("currency", "EUR"),
            balance=balance, equity=equity, today_pnl=pnl, sod_equity=sod, position=position, pending=pending, recent_trades=recent_trades,
            recent_decisions=decs, last_rejection=last_rej, events=self.news.upcoming(now, 4, book.currencies),
            risk_state={"entries_allowed": not blocked, "blocked": blocked, "long_blocked": state.side_block("long", cfg),
                        "short_blocked": state.side_block("short", cfg), "entries_today": state.entries_today,
                        "cooldown_bars_left": state.cooldown_bars_left, "open_trades": len(everything)},
            others=await self.other_trades_detail(book, mode), exposure=exposure(everything),
            signal_cfg=cfg.signal, risk_cfg=cfg.risk,
        )
        return snap_mod.build(x), state

    # ------------------------------------------------------------ equity and benchmark
    async def record_equity(self, bar_open: datetime) -> None:
        mode = self.mode
        t = snap_mod.fmt_t(bar_open + BAR)
        unreal = await self.unrealized(mode)
        if mode == "dry":
            balance = self.paper.balance()
            equity = balance + unreal
            start = float(self.store.get_state("paper_start_balance", balance))
            realized = self.realized_since(datetime(1970, 1, 1, tzinfo=UTC), "dry")
            bot_equity = start + realized + unreal
        else:
            acct = await self.mt(self.gw.account)
            balance, equity = float(acct.balance), float(acct.equity)
            start = self.store.get_state("live_start_equity")
            if start is None:
                start = equity - unreal
                self.store.set_state("live_start_equity", round(start, 2))
            bot_equity = float(start) + self.realized_since(datetime(1970, 1, 1, tzinfo=UTC), "live") + unreal
        self.store.upsert("equity", {"time": t, "mode": mode, "balance": round(balance, 2), "equity": round(equity, 2),
                                     "bot_equity": round(bot_equity, 2), "bot_unrealized": round(unreal, 2)}, ["time", "mode"])

    async def update_benchmark(self, book: Book, m15: snap_mod.Bars) -> None:
        """Shadow 'indicator only' strategy on every pair. Stored in the database, never sent to MT5."""
        cfg = self.cfg
        spec = book.spec
        if spec is None or len(m15) < 50:
            return
        start = self.store.get_state("bench_start")
        if start is None:
            start = snap_mod.fmt_t(m15.t[-1])
            self.store.set_state("bench_start", start)
            self.store.set_state("bench_start_equity", round(await self.equity_now(), 2))
        start_t = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=UTC).timestamp()
        start_equity = float(self.store.get_state("bench_start_equity"))
        sc = cfg.signal
        sig = ts.compute(m15.h, m15.l, m15.c, sc.atr_length, sc.multiplier, sc.atr_method)
        spreads = m15.spread * spec.point
        trades = ts.simulate(m15.o, m15.h, m15.l, m15.c, spreads, sig)
        sym = book.symbol
        for t in trades:
            if m15.t[t.entry_i] < start_t:
                continue
            entry_time = fmt_utc(m15.time(t.entry_i) + BAR)  # entered at the flip bar's close
            existing = self.store.one("SELECT * FROM bench_trades WHERE symbol=? AND entry_time=?", (sym, entry_time))
            if existing and existing["status"] == "closed":
                continue
            if existing:
                volume, risk_eur = existing["volume"], existing["risk_eur"]
            else:
                realized = self.store.one("SELECT COALESCE(SUM(pnl_eur),0) AS s FROM bench_trades WHERE status='closed' AND exit_time<=?", (entry_time,))["s"]
                equity_before = start_equity + float(realized)
                volume, loss_per_lot = size_lots(equity_before * cfg.risk.risk_per_trade_pct / 100, t.risk, spec)
                risk_eur = round(volume * loss_per_lot, 2)
            row = {"symbol": sym, "side": t.side, "entry_time": entry_time, "entry_price": t.entry_price, "stop_initial": t.stop_initial, "stop": t.stop,
                   "volume": volume, "risk_eur": risk_eur, "status": "open", "exit_time": None, "exit_price": None, "exit_reason": None,
                   "pnl_eur": None, "r_multiple": None}
            if t.exit_reason != "open":
                diff = (t.exit_price - t.entry_price) if t.side == "long" else (t.entry_price - t.exit_price)
                tv = spec.tick_value_profit if diff >= 0 else spec.tick_value_loss
                exit_close = m15.time(t.exit_i) + (BAR if t.exit_reason == "flip" else timedelta(0))
                row.update({"status": "closed", "exit_time": fmt_utc(exit_close), "exit_price": t.exit_price, "exit_reason": t.exit_reason,
                            "pnl_eur": round(diff / spec.tick_size * tv * volume, 2), "r_multiple": round(t.r_at(t.exit_price), 2)})
            self.store.upsert("bench_trades", row, ["symbol", "entry_time"])
        # Mark-to-market for the benchmark equity curve: realized on every pair plus each pair's open trade.
        unreal = 0.0
        op = self.store.one("SELECT * FROM bench_trades WHERE symbol=? AND status='open' ORDER BY entry_time DESC LIMIT 1", (sym,))
        if op:
            mark = m15.c[-1] if op["side"] == "long" else m15.c[-1] + spreads[-1]
            diff = (mark - op["entry_price"]) if op["side"] == "long" else (op["entry_price"] - mark)
            tv = spec.tick_value_profit if diff >= 0 else spec.tick_value_loss
            unreal = diff / spec.tick_size * tv * (op["volume"] or 0)
        self._bench_unreal[sym] = unreal
        realized = float(self.store.one("SELECT COALESCE(SUM(pnl_eur),0) AS s FROM bench_trades WHERE status='closed'")["s"])
        self.store.upsert("bench_equity", {"time": snap_mod.fmt_t(m15.t[-1] + 900), "equity": round(start_equity + realized + sum(self._bench_unreal.values()), 2)},
                          ["time"])

    # ------------------------------------------------------------ watchdog
    async def watchdog(self) -> None:
        while self._running:
            try:
                await self.watch_once()
            except Exception:  # noqa: BLE001
                log.exception("Watchdog step failed")
            await asyncio.sleep(1.0)

    async def watch_once(self) -> None:
        now = self.clock.now()
        if self.stop_file.exists() and (self.halt_state() or {}).get("kind") != "kill_switch":
            await self.kill("STOP file found", "file")
        halt = self.halt_state()
        if halt and halt["kind"] == "daily_limit" and halt.get("until") and now >= parse_utc(halt["until"]):
            self.clear_halt("Daily loss limit halt ended at 17:00 New York")
        if not await self.ensure_connected():
            self.push_status()
            return
        try:
            health = await self.mt(self.gw.health)
            if not health.ipc_ok:
                self.lost_connection("terminal closed or not responding", "MetaTrader 5 closed or isn't responding. Open the terminal and log in to your demo account")
                self.push_status(force=True)
                return
            if health.login != self.gw.account_id.login or health.is_demo is False:
                await self.mt(self.gw.verify_demo)  # raises a fatal SafetyError
            changed = (health.connected, health.trade_allowed) != (self.rt.broker_connected, self.rt.algo_on)
            self.rt.broker_connected, self.rt.algo_on = health.connected, health.trade_allowed
            if changed:
                self.event("terminal", "Terminal state: " + ("broker connected" if health.connected else "no broker connection") + ", "
                           + ("Algo Trading on" if health.trade_allowed else "Algo Trading off"), "info" if health.trade_allowed and health.connected else "warning")
                self.push_banner()
            await self.observe_tick()
            if self.mode == "dry":
                events: list[str] = []
                async with self._trade_lock:
                    for book in self.books.values():
                        if book.bid is not None:
                            events += book.paper.on_price(book.bid, book.ask, now)
                self.store.set_state("paper_checked_until", fmt_utc(now))
                if events:
                    self.bus.publish("trade", {"changes": events})
            else:
                count = len(await self.mt(self.gw.all_bot_positions)) + len(await self.mt(self.gw.all_bot_orders))
                if count != self.rt.position_count:
                    await self.safe_reconcile()
            s = self.cfg.schedule
            if friday_flat_due(now, s.friday_flat_utc, s.sunday_resume_utc) and self.has_open_book(self.mode):
                self.event("weekend", f"Friday {s.friday_flat_utc} UTC: closing the bot's positions for the weekend")
                await self.flatten("weekend")
            if self.rt.last_daily_check is None or (now - self.rt.last_daily_check).total_seconds() >= 5:
                self.rt.last_daily_check = now
                await self.check_daily_loss()
        except SafetyError as exc:
            if exc.fatal:
                self.block(exc.message)
        except NotConnected as exc:
            self.lost_connection(str(exc), exc.hint)
        self.push_status()

    # ------------------------------------------------------------ UI payloads
    def banners(self) -> list[dict]:
        out = []
        if self.rt.blocked:
            out.append({"level": "danger", "text": self.rt.blocked})
            return out
        halt = self.halt_state()
        if halt:
            if halt["kind"] == "daily_limit":
                out.append({"level": "danger", "text": f"Halted: {halt['reason']}. Trading resumes at 17:00 New York.", "until": halt.get("until")})
            elif self.stop_file.exists():
                out.append({"level": "danger", "text": "Halted by the kill switch: STOP file present. Delete it to resume."})
            else:
                out.append({"level": "danger", "text": f"Halted: {halt['reason']}. Positions closed; press Resume to trade again."})
        if not self.rt.connected:
            when = ""
            if self.rt.reconnect_at:
                secs = max(0, int((self.rt.reconnect_at - self.clock.now()).total_seconds()))
                when = f" Reconnecting in {secs} s (attempt {self.rt.reconnect_attempt})."
            lead = f"MT5 disconnected: {self.rt.connect_hint}." if self.rt.connect_hint else "MT5 disconnected."
            out.append({"level": "warning", "text": f"{lead}{when} Trading is paused.", "reconnect_at": fmt_utc(self.rt.reconnect_at)})
        elif not self.rt.broker_connected:
            out.append({"level": "warning", "text": "MT5 has no connection to the broker. Trading is paused."})
        elif not self.rt.algo_on:
            out.append({"level": "warning", "text": "Algo Trading is off in MT5. Turn it on in the terminal toolbar to resume."})
        else:
            limit = max(10.0, 5 * self.cfg.risk.max_spread_pips)
            wide = [(b.broker_symbol, sp) for b in self.trading_books() if (sp := b.spread_pips()) is not None and sp > limit]
            if wide:
                names = ", ".join(f"{s} {sp:.1f} pips" for s, sp in wide)
                one = len(wide) == 1
                out.append({"level": "warning", "text": f"Unusually wide spread ({names}), so {'its' if one else 'their'} bars are skipped. If MT5 shows a normal "
                                                        f"spread, {'this symbol' if one else 'these symbols'} may not be the one{'' if one else 's'} your account trades: run check.bat."})
        missing = [s for s in self.cfg.app.symbols if s in self.gw.unavailable]
        if missing and self.rt.connected:
            out.append({"level": "warning", "text": f"Not available in this MT5 terminal, so not traded: {', '.join(missing)}. "
                                                    f"Turn {'it' if len(missing) == 1 else 'them'} off in Settings, or run check.bat to see the broker's names."})
        if self.rt.model_warning:
            out.append({"level": "warning", "text": self.rt.model_warning})
        return out

    def status(self) -> dict:
        halt = self.halt_state()
        if self.rt.blocked:
            pill, reason = "blocked", self.rt.blocked
        elif not self.rt.connected:
            pill, reason = "disconnected", "MT5 disconnected"
        elif halt:
            pill, reason = "halted", halt["reason"]
        elif self.paused():
            pill, reason = "paused", "Paused"
        elif not self.rt.algo_on or not self.rt.broker_connected:
            pill, reason = "paused", "Algo Trading off" if not self.rt.algo_on else "No broker connection"
        else:
            pill, reason = "running", None
        now = self.clock.now()
        nxt = self.news.next_event(now)
        traded = self.trading_books()
        pairs = [{"symbol": b.symbol, "broker": b.broker_symbol, "bid": b.bid, "ask": b.ask, "spread_pips": b.spread_pips(),
                  "digits": b.spec.digits if b.spec else 5, "thinking_since": fmt_utc(b.thinking_since), "traded": b in traded}
                 for b in self.books.values()]
        thinking = [b.thinking_since for b in self.books.values() if b.thinking_since]
        return {
            "status": pill, "reason": reason, "halt": halt, "paused": self.paused(), "blocked": self.rt.blocked,
            "mode": self.mode, "dry_run": self.cfg.app.dry_run, "sim": self.sim,
            "now": fmt_utc(now), "next_bar_at": fmt_utc(self.rt.next_bar_at),
            "thinking_since": fmt_utc(min(thinking)) if thinking else None,
            "thinking": [b.symbol for b in self.books.values() if b.thinking_since],
            "connected": self.rt.connected, "broker_connected": self.rt.broker_connected, "algo_on": self.rt.algo_on,
            "bid": self.rt.bid, "ask": self.rt.ask, "spread_pips": self.spread_pips(),
            "stop_file": self.stop_file.exists(), "account": self.rt.account, "symbol": self.primary.broker_symbol,
            "symbols": [b.symbol for b in traded] or list(self.cfg.app.symbols), "pairs": pairs,
            "unavailable": [s for s in self.cfg.app.symbols if s in self.gw.unavailable],
            "next_event": None if nxt is None else nxt.to_dict(now),
            "banners": self.banners(),
        }

    def push_status(self, force: bool = False) -> None:
        now = self.clock.now()
        if not force and self.rt.last_status_push and (now - self.rt.last_status_push).total_seconds() < 0.9:
            return
        self.rt.last_status_push = now
        self.bus.publish("status", self.status())

    def push_banner(self) -> None:
        self.push_status(force=True)
