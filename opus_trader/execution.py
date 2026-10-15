"""Order execution: live orders on the MT5 demo, or simulated (paper) fills in dry run.

Both executors build the identical MT5 request. The live one sends it through
the gateway's gated `order_send`; the paper one runs `order_check` on it and
then simulates the fill at the live bid/ask. Every request and result is
written to the `orders` table.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .clock import ServerTime, fmt_utc, parse_utc, session_of
from .mt5_gateway import MT5Gateway, NotConnected, SafetyError, record
from .risk import PendingView, Plan, PositionView, SymbolSpec
from .store import Store, now_iso

log = logging.getLogger(__name__)
UTC = timezone.utc

EXIT_LABELS = {
    "sl": "Stop-loss",
    "tp": "Take-profit",
    "model": "Closed by Opus",
    "kill_switch": "Kill switch",
    "weekend": "Weekend flat",
    "daily_limit": "Daily loss limit",
    "manual": "Closed in MT5",
    "stop_out": "Stop out",
    "broker": "Closed by broker",
}


@dataclass
class ExecResult:
    ok: bool
    message: str
    retcode: int | None = None
    retcode_name: str | None = None
    ticket: int | None = None
    price: float | None = None


class BaseExecutor:
    mode = "base"

    def __init__(self, gw: MT5Gateway, store: Store, magic: int, deviation: Callable[[], int], server_time: ServerTime):
        self.gw = gw
        self.store = store
        self.magic = magic
        self.deviation = deviation
        self.server_time = server_time
        self.mt5 = gw.mt5

    # ------------------------------------------------------------ request builders
    def _comment(self, decision_id: int | None) -> str:
        return f"opus #{decision_id}" if decision_id else "opus"

    def market_request(self, plan: Plan, tick: Any, decision_id: int | None, filling: int) -> dict:
        long_ = plan.side == "long"
        return {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.gw.symbol,
            "volume": float(plan.volume),
            "type": self.mt5.ORDER_TYPE_BUY if long_ else self.mt5.ORDER_TYPE_SELL,
            "price": float(tick.ask if long_ else tick.bid),
            "sl": float(plan.sl),
            "tp": float(plan.tp),
            "deviation": int(self.deviation()),
            "magic": self.magic,
            "comment": self._comment(decision_id),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

    def limit_request(self, plan: Plan, decision_id: int | None, filling: int, specified_expiration: bool) -> dict:
        req = {
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": self.gw.symbol,
            "volume": float(plan.volume),
            "type": self.mt5.ORDER_TYPE_BUY_LIMIT if plan.side == "long" else self.mt5.ORDER_TYPE_SELL_LIMIT,
            "price": float(plan.price),
            "sl": float(plan.sl),
            "tp": float(plan.tp),
            "magic": self.magic,
            "comment": self._comment(decision_id),
            "type_filling": filling,
        }
        if specified_expiration and plan.expiration:
            req["type_time"] = self.mt5.ORDER_TIME_SPECIFIED
            req["expiration"] = self.server_time.to_server_epoch(plan.expiration)  # MT5 expects server time
        else:
            req["type_time"] = self.mt5.ORDER_TIME_GTC  # the engine removes it at expiry
        return req

    def _log_order(self, purpose: str, decision_id: int | None, request: dict | None, res: Any, ok: bool, comment: str | None = None, **overrides) -> None:
        d = record(res) if res is not None else {}
        code = d.get("retcode")
        row = {
            "created_at": now_iso(), "decision_id": decision_id, "mode": self.mode, "purpose": purpose,
            "request_json": None if request is None else Store.dumps(request), "retcode": code,
            "retcode_name": None if code is None else self.gw.retcode_name(code),
            "comment": comment if comment is not None else d.get("comment"),
            "order_ticket": d.get("order"), "deal_ticket": d.get("deal"), "price": d.get("price"), "volume": d.get("volume"),
            "ok": 1 if ok else 0,
        }
        row.update(overrides)
        self.store.insert("orders", row)


# ====================================================================== live

class LiveExecutor(BaseExecutor):
    mode = "live"

    def __init__(self, gw, store, magic, deviation, server_time, gate: Callable[[str], str | None]):
        super().__init__(gw, store, magic, deviation, server_time)
        self.gate = gate  # gate(purpose) -> reason to block, or None. Closing is allowed while halted.

    def _send(self, purpose: str, decision_id: int | None, request: dict, success: set[int]) -> tuple[bool, Any]:
        try:
            res = self.gw.order_send(request, lambda: self.gate(purpose))
        except SafetyError as exc:
            self._log_order(purpose, decision_id, request, None, False, comment=exc.message, retcode_name="BLOCKED")
            raise
        ok = res.retcode in success
        self._log_order(purpose, decision_id, request, res, ok)
        if not ok:
            log.warning("%s failed: retcode %s (%s) %s", purpose, res.retcode, self.gw.retcode_name(res.retcode), res.comment)
        return ok, res

    def _with_filling(self, purpose: str, decision_id: int | None, build: Callable[[int], dict], success: set[int]) -> ExecResult:
        res = None
        for filling in self.gw.filling_candidates():
            ok, res = self._send(purpose, decision_id, build(filling), success)
            if ok:
                return ExecResult(True, res.comment or "Done", res.retcode, self.gw.retcode_name(res.retcode), res.order or None, res.price or None)
            if res.retcode != self.mt5.TRADE_RETCODE_INVALID_FILL:
                break
        code = None if res is None else res.retcode
        return ExecResult(False, f"{self.gw.retcode_name(code)}: {getattr(res, 'comment', '')}", code, self.gw.retcode_name(code) if code else None)

    def open(self, plan: Plan, decision_id: int | None) -> ExecResult:
        if plan.kind == "OPEN_MARKET":
            tick = self.gw.tick()
            return self._with_filling("open", decision_id, lambda f: self.market_request(plan, tick, decision_id, f), {self.mt5.TRADE_RETCODE_DONE})
        specified = self.gw.supports_specified_expiration()
        result = self._with_filling("pending", decision_id, lambda f: self.limit_request(plan, decision_id, f, specified),
                                    {self.mt5.TRADE_RETCODE_PLACED, self.mt5.TRADE_RETCODE_DONE})
        if result.ok and result.ticket and plan.expiration:
            expiries = self.store.get_state("live_pending_expiry", {})
            expiries[str(result.ticket)] = fmt_utc(plan.expiration)
            self.store.set_state("live_pending_expiry", expiries)
        return result

    def close_position(self, ticket: int, reason: str, decision_id: int | None = None) -> ExecResult:
        pos = next((p for p in self.gw.bot_positions() if p.ticket == ticket), None)
        if pos is None:
            return ExecResult(False, "Position not found among the bot's positions")
        self.store.set_state(f"close_reason:{ticket}", reason)
        tick = self.gw.tick()
        long_ = pos.type == self.mt5.POSITION_TYPE_BUY

        def build(filling: int) -> dict:
            return {
                "action": self.mt5.TRADE_ACTION_DEAL, "symbol": self.gw.symbol, "volume": float(pos.volume),
                "type": self.mt5.ORDER_TYPE_SELL if long_ else self.mt5.ORDER_TYPE_BUY,
                "position": int(ticket), "price": float(tick.bid if long_ else tick.ask),
                "deviation": int(self.deviation()), "magic": self.magic, "comment": self._comment(decision_id),
                "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": filling,
            }

        return self._with_filling("close", decision_id, build, {self.mt5.TRADE_RETCODE_DONE})

    def cancel_pending(self, ticket: int, reason: str, decision_id: int | None = None) -> ExecResult:
        order = next((o for o in self.gw.bot_orders() if o.ticket == ticket), None)
        if order is None:
            return ExecResult(False, "Order not found among the bot's orders")
        req = {"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(ticket), "symbol": self.gw.symbol, "magic": self.magic, "comment": self._comment(decision_id)}
        ok, res = self._send("remove", decision_id, req, {self.mt5.TRADE_RETCODE_DONE})
        if ok:
            expiries = self.store.get_state("live_pending_expiry", {})
            expiries.pop(str(ticket), None)
            self.store.set_state("live_pending_expiry", expiries)
        return ExecResult(ok, res.comment, res.retcode, self.gw.retcode_name(res.retcode), ticket)

    def modify_stop(self, ticket: int, sl: float, tp: float, decision_id: int | None = None) -> ExecResult:
        pos = next((p for p in self.gw.bot_positions() if p.ticket == ticket), None)
        if pos is None:
            return ExecResult(False, "Position not found among the bot's positions")
        req = {"action": self.mt5.TRADE_ACTION_SLTP, "symbol": self.gw.symbol, "position": int(ticket), "sl": float(sl), "tp": float(tp), "magic": self.magic}
        ok, res = self._send("modify", decision_id, req, {self.mt5.TRADE_RETCODE_DONE})
        if ok:
            self.store.execute("UPDATE trades SET sl=? WHERE mode='live' AND ticket=?", (sl, ticket))
        return ExecResult(ok, res.comment, res.retcode, self.gw.retcode_name(res.retcode), ticket)

    def position(self) -> PositionView | None:
        ps = self.gw.bot_positions()
        if not ps:
            return None
        p = ps[0]
        return PositionView(p.ticket, "long" if p.type == self.mt5.POSITION_TYPE_BUY else "short", p.volume, p.price_open, p.sl, p.tp)

    def pending(self) -> PendingView | None:
        os_ = [o for o in self.gw.bot_orders() if o.type in (self.mt5.ORDER_TYPE_BUY_LIMIT, self.mt5.ORDER_TYPE_SELL_LIMIT)]
        if not os_:
            return None
        o = os_[0]
        return PendingView(o.ticket, "long" if o.type == self.mt5.ORDER_TYPE_BUY_LIMIT else "short", o.volume_current, o.price_open, o.sl, o.tp)

    def close_all(self, reason: str) -> list[ExecResult]:
        """Close every bot position and cancel every bot order (magic number only)."""
        results = []
        for p in self.gw.bot_positions():
            results.append(self.close_position(p.ticket, reason))
        for o in self.gw.bot_orders():
            results.append(self.cancel_pending(o.ticket, reason))
        return results


# ====================================================================== paper

class PaperExecutor(BaseExecutor):
    """Dry run: order_check on the real request, then a simulated fill. Nothing is sent."""

    mode = "dry"
    FIRST_TICKET = 9_000_000_001

    def __init__(self, gw, store, magic, deviation, server_time, spec_provider: Callable[[], SymbolSpec], clock_now: Callable[[], datetime]):
        super().__init__(gw, store, magic, deviation, server_time)
        self.spec = spec_provider
        self.now = clock_now

    # ------------------------------------------------------------ ledger
    def balance(self) -> float:
        return float(self.store.get_state("paper_balance", 0.0))

    def ensure_balance(self, start_balance: float) -> None:
        if self.store.get_state("paper_balance") is None:
            self.store.set_state("paper_balance", round(start_balance, 2))
            self.store.set_state("paper_start_balance", round(start_balance, 2))

    def _next_ticket(self) -> int:
        t = int(self.store.get_state("paper_next_ticket", self.FIRST_TICKET))
        self.store.set_state("paper_next_ticket", t + 1)
        return t

    def pnl(self, side: str, volume: float, entry: float, exit_: float) -> float:
        spec = self.spec()
        diff = (exit_ - entry) if side == "long" else (entry - exit_)
        tv = spec.tick_value_profit if diff >= 0 else spec.tick_value_loss
        return round(diff / spec.tick_size * tv * volume, 2)

    def risk_eur(self, volume: float, entry: float, sl: float) -> float:
        spec = self.spec()
        return round(abs(entry - sl) / spec.tick_size * spec.tick_value_loss * volume, 2)

    def open_trade_row(self) -> dict | None:
        return self.store.one("SELECT * FROM trades WHERE mode='dry' AND status='open' ORDER BY id DESC LIMIT 1")

    def pending_row(self) -> dict | None:
        return self.store.one("SELECT * FROM paper_orders WHERE status='pending' ORDER BY ticket DESC LIMIT 1")

    def unrealized(self, bid: float, ask: float) -> float:
        t = self.open_trade_row()
        if not t:
            return 0.0
        return self.pnl(t["side"], t["volume"], t["open_price"], bid if t["side"] == "long" else ask)

    def equity(self, bid: float, ask: float) -> float:
        return round(self.balance() + self.unrealized(bid, ask), 2)

    # ------------------------------------------------------------ actions
    def _check(self, purpose: str, decision_id: int | None, request: dict) -> tuple[bool, Any]:
        res = self.gw.order_check(request)
        ok = res.retcode == 0
        self._log_order("check", decision_id, request, res, ok, comment=f"order_check for {purpose}: {res.comment}")
        return ok, res

    def open(self, plan: Plan, decision_id: int | None, meta: dict | None = None) -> ExecResult:
        meta = meta or {}
        filling = self.gw.filling_candidates()[0]
        if plan.kind == "OPEN_MARKET":
            tick = self.gw.tick()
            req = self.market_request(plan, tick, decision_id, filling)
            ok, res = self._check("open", decision_id, req)
            if not ok:
                return ExecResult(False, f"order_check failed: {self.gw.retcode_name(res.retcode)} {res.comment}", res.retcode, self.gw.retcode_name(res.retcode))
            price = float(tick.ask if plan.side == "long" else tick.bid)
            ticket = self._next_ticket()
            self._create_trade(ticket, decision_id, plan.side, plan.volume, price, plan.sl, plan.tp, self.now(), meta)
            self._log_order("open", decision_id, req, None, True, comment=f"Simulated fill at {price:.5f}",
                            retcode=0, retcode_name="PAPER", order_ticket=ticket, price=price, volume=plan.volume)
            return ExecResult(True, f"Simulated fill at {price:.5f}", 0, "PAPER", ticket, price)
        req = self.limit_request(plan, decision_id, filling, specified_expiration=True)
        ok, res = self._check("pending", decision_id, req)
        if not ok:
            return ExecResult(False, f"order_check failed: {self.gw.retcode_name(res.retcode)} {res.comment}", res.retcode, self.gw.retcode_name(res.retcode))
        ticket = self._next_ticket()
        self.store.insert("paper_orders", {
            "ticket": ticket, "decision_id": decision_id, "side": plan.side, "volume": plan.volume, "price": plan.price, "sl": plan.sl, "tp": plan.tp,
            "risk_eur": plan.risk_eur, "setup": meta.get("setup"), "confidence": meta.get("confidence"), "signal_agreement": meta.get("signal_agreement"),
            "created_at": fmt_utc(self.now()), "expires_at": fmt_utc(plan.expiration), "status": "pending",
        })
        self._log_order("pending", decision_id, req, None, True, comment=f"Simulated limit order placed at {plan.price:.5f}",
                        retcode=0, retcode_name="PAPER", order_ticket=ticket, price=plan.price, volume=plan.volume)
        return ExecResult(True, f"Simulated limit order at {plan.price:.5f}", 0, "PAPER", ticket, plan.price)

    def _create_trade(self, ticket, decision_id, side, volume, price, sl, tp, when: datetime, meta: dict) -> None:
        self.store.insert("trades", {
            "mode": "dry", "ticket": ticket, "decision_id": decision_id, "side": side,
            "setup": meta.get("setup"), "confidence": meta.get("confidence"), "signal_agreement": meta.get("signal_agreement"),
            "session": session_of(when), "open_time": fmt_utc(when), "open_price": price, "volume": volume,
            "sl_initial": sl, "tp_initial": tp, "sl": sl, "tp": tp, "risk_eur": self.risk_eur(volume, price, sl), "status": "open",
        })

    def close_position(self, ticket: int, reason: str, decision_id: int | None = None, price: float | None = None, when: datetime | None = None) -> ExecResult:
        t = self.store.one("SELECT * FROM trades WHERE mode='dry' AND ticket=? AND status='open'", (ticket,))
        if t is None:
            return ExecResult(False, "Paper position not found")
        if price is None:
            tick = self.gw.tick()
            price = float(tick.bid if t["side"] == "long" else tick.ask)
        when = when or self.now()
        pnl = self.pnl(t["side"], t["volume"], t["open_price"], price)
        r = round(pnl / t["risk_eur"], 2) if t["risk_eur"] else None
        self.store.update("trades", t["id"], {"close_time": fmt_utc(when), "close_price": price, "exit_reason": reason, "pnl_eur": pnl, "r_multiple": r, "status": "closed"})
        self.store.set_state("paper_balance", round(self.balance() + pnl, 2))
        self._log_order("close", decision_id, None, None, True, comment=f"Simulated close at {price:.5f} ({EXIT_LABELS.get(reason, reason)})",
                        retcode=0, retcode_name="PAPER", order_ticket=ticket, price=price, volume=t["volume"])
        return ExecResult(True, f"Simulated close at {price:.5f}", 0, "PAPER", ticket, price)

    def cancel_pending(self, ticket: int, reason: str, decision_id: int | None = None, status: str = "cancelled") -> ExecResult:
        o = self.store.one("SELECT * FROM paper_orders WHERE ticket=? AND status='pending'", (ticket,))
        if o is None:
            return ExecResult(False, "Paper order not found")
        self.store.update("paper_orders", ticket, {"status": status}, key="ticket")
        self._log_order("remove", decision_id, None, None, True, comment=f"Simulated limit order {status}",
                        retcode=0, retcode_name="PAPER", order_ticket=ticket, price=o["price"], volume=o["volume"])
        return ExecResult(True, f"Limit order {status}", 0, "PAPER", ticket)

    def modify_stop(self, ticket: int, sl: float, tp: float, decision_id: int | None = None) -> ExecResult:
        t = self.store.one("SELECT * FROM trades WHERE mode='dry' AND ticket=? AND status='open'", (ticket,))
        if t is None:
            return ExecResult(False, "Paper position not found")
        req = {"action": self.mt5.TRADE_ACTION_SLTP, "symbol": self.gw.symbol, "position": int(ticket), "sl": float(sl), "tp": float(tp), "magic": self.magic}
        self.store.update("trades", t["id"], {"sl": sl, "tp": tp})
        self._log_order("modify", decision_id, req, None, True, comment=f"Simulated stop move to {sl:.5f}", retcode=0, retcode_name="PAPER", order_ticket=ticket)
        return ExecResult(True, f"Stop moved to {sl:.5f}", 0, "PAPER", ticket)

    def position(self) -> PositionView | None:
        t = self.open_trade_row()
        if not t:
            return None
        return PositionView(t["ticket"], t["side"], t["volume"], t["open_price"], t["sl"] or 0.0, t["tp"] or 0.0)

    def pending(self) -> PendingView | None:
        o = self.pending_row()
        if not o:
            return None
        return PendingView(o["ticket"], o["side"], o["volume"], o["price"], o["sl"], o["tp"])

    def close_all(self, reason: str) -> list[ExecResult]:
        out = []
        t = self.open_trade_row()
        if t:
            out.append(self.close_position(t["ticket"], reason))
        o = self.pending_row()
        if o:
            out.append(self.cancel_pending(o["ticket"], reason))
        return out

    # ------------------------------------------------------------ simulated broker
    def on_price(self, bid: float, ask: float, when: datetime) -> list[str]:
        """Check the paper position and limit order against the live quote. Returns event strings."""
        events = []
        o = self.pending_row()
        if o:
            if o["expires_at"] and when >= parse_utc(o["expires_at"]):
                self.cancel_pending(o["ticket"], "expired", status="expired")
                events.append("expired")
            elif (o["side"] == "long" and ask <= o["price"]) or (o["side"] == "short" and bid >= o["price"]):
                self._fill_pending(o, o["price"], when)
                events.append("filled")
        t = self.open_trade_row()
        if t:
            if t["side"] == "long":
                if t["sl"] and bid <= t["sl"]:
                    self.close_position(t["ticket"], "sl", price=min(bid, t["sl"]), when=when)
                    events.append("sl")
                elif t["tp"] and bid >= t["tp"]:
                    self.close_position(t["ticket"], "tp", price=t["tp"], when=when)
                    events.append("tp")
            else:
                if t["sl"] and ask >= t["sl"]:
                    self.close_position(t["ticket"], "sl", price=max(ask, t["sl"]), when=when)
                    events.append("sl")
                elif t["tp"] and ask <= t["tp"]:
                    self.close_position(t["ticket"], "tp", price=t["tp"], when=when)
                    events.append("tp")
        return events

    def _fill_pending(self, o: dict, price: float, when: datetime) -> None:
        self.store.update("paper_orders", o["ticket"], {"status": "filled"}, key="ticket")
        meta = {"setup": o["setup"], "confidence": o["confidence"], "signal_agreement": o["signal_agreement"]}
        self._create_trade(o["ticket"], o["decision_id"], o["side"], o["volume"], price, o["sl"], o["tp"], when, meta)

    def backfill(self, m1_times_utc, o, h, lo, c, spread_price) -> list[str]:
        """Replay M1 bars (bid) for a period the app didn't watch tick by tick.

        Conservative: if one bar touches both the stop and the target, the stop wins.
        """
        events: list[str] = []
        for i in range(len(m1_times_utc)):
            when = datetime.fromtimestamp(int(m1_times_utc[i]), UTC)
            sp = float(spread_price[i])
            p = self.pending_row()
            if p:
                if p["expires_at"] and when >= parse_utc(p["expires_at"]):
                    self.cancel_pending(p["ticket"], "expired", status="expired")
                    events.append("expired")
                elif p["side"] == "long" and lo[i] + sp <= p["price"]:
                    self._fill_pending(p, min(o[i] + sp, p["price"]), when)
                    events.append("filled")
                elif p["side"] == "short" and h[i] >= p["price"]:
                    self._fill_pending(p, max(o[i], p["price"]), when)
                    events.append("filled")
            t = self.open_trade_row()
            if not t:
                continue
            close_at = when + timedelta(seconds=59)
            if t["side"] == "long":
                if t["sl"] and lo[i] <= t["sl"]:
                    self.close_position(t["ticket"], "sl", price=min(o[i], t["sl"]), when=close_at)
                    events.append("sl")
                elif t["tp"] and h[i] >= t["tp"]:
                    self.close_position(t["ticket"], "tp", price=max(o[i], t["tp"]) if o[i] > t["tp"] else t["tp"], when=close_at)
                    events.append("tp")
            else:
                if t["sl"] and h[i] + sp >= t["sl"]:
                    self.close_position(t["ticket"], "sl", price=max(o[i] + sp, t["sl"]), when=close_at)
                    events.append("sl")
                elif t["tp"] and lo[i] + sp <= t["tp"]:
                    self.close_position(t["ticket"], "tp", price=min(o[i] + sp, t["tp"]) if o[i] + sp < t["tp"] else t["tp"], when=close_at)
                    events.append("tp")
        return events
