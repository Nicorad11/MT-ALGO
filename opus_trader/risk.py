"""Deterministic risk engine: validates the model's decision and sizes it.

The model never trades. It proposes; `evaluate` either returns a concrete
`Plan` for the executor or a rejection with a plain-English reason that is
shown in the UI and fed back to the model on the next bar.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Config

ACTIONS = ("OPEN_LONG", "OPEN_SHORT", "HOLD", "CLOSE", "MOVE_STOP")


@dataclass
class Decision:
    action: str
    order_type: str | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: int | None = None
    setup: str | None = None
    reasoning: str | None = None
    invalidation: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        def num(v):
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) and f > 0 else None

        action = str(d.get("action", "HOLD")).upper()
        if action not in ACTIONS:
            raise ValueError(f"Unknown action {action!r}")
        order_type = d.get("order_type")
        order_type = str(order_type).upper() if order_type else None
        conf = d.get("confidence")
        try:
            conf = int(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        return cls(action, order_type, num(d.get("entry")), num(d.get("stop_loss")), num(d.get("take_profit")), conf,
                   (d.get("setup") or None), (d.get("reasoning") or None), (d.get("invalidation") or None))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def side(self) -> str | None:
        return {"OPEN_LONG": "long", "OPEN_SHORT": "short"}.get(self.action)


@dataclass
class SymbolSpec:
    digits: int
    point: float
    tick_size: float
    tick_value_loss: float
    tick_value_profit: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int  # points
    freeze_level: int  # points
    specified_expiration: bool = True

    @classmethod
    def from_info(cls, info: Any, specified_expiration: bool = True) -> "SymbolSpec":
        tv = float(info.trade_tick_value)
        tvl = float(getattr(info, "trade_tick_value_loss", 0.0) or 0.0) or tv
        tvp = float(getattr(info, "trade_tick_value_profit", 0.0) or 0.0) or tv
        return cls(int(info.digits), float(info.point), float(info.trade_tick_size) or float(info.point), tvl, tvp,
                   float(info.volume_min), float(info.volume_max), float(info.volume_step), int(info.trade_stops_level),
                   int(info.trade_freeze_level), specified_expiration)

    @property
    def pip(self) -> float:
        return self.point * 10 if self.digits in (3, 5) else self.point

    def norm(self, price: float) -> float:
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, self.digits)

    def pips(self, distance: float) -> float:
        return distance / self.pip


@dataclass
class Market:
    bid: float
    ask: float
    atr: float  # ATR(14) on M15, price units
    now: datetime

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class PositionView:
    ticket: int
    side: str  # long | short
    volume: float
    entry: float
    sl: float
    tp: float


@dataclass
class PendingView:
    ticket: int
    side: str
    volume: float
    price: float
    sl: float
    tp: float


@dataclass
class RiskState:
    equity: float
    position: PositionView | None = None
    pending: PendingView | None = None
    entries_today: int = 0
    cooldown_bars_left: int = 0
    entry_block: str | None = None  # weekend window / news blackout reason
    recent_flip_direction: str | None = None  # long | short | None (Trend Signals, filter mode)

    def blocked_reasons(self, cfg: Config) -> list[str]:
        out = []
        if self.entry_block:
            out.append(self.entry_block)
        if self.position or self.pending:
            out.append("a bot position or order is already open (max 1)")
        if self.entries_today >= cfg.risk.max_trades_per_day:
            out.append(f"max {cfg.risk.max_trades_per_day} entries for today reached")
        if self.cooldown_bars_left > 0:
            out.append(f"cooldown after a loss, {self.cooldown_bars_left} bar{'s' if self.cooldown_bars_left != 1 else ''} left")
        if cfg.signal.mode == "filter" and self.recent_flip_direction is None:
            out.append(f"signal filter: no Trend Signals flip in the last {cfg.signal.filter_bars} bars")
        return out


@dataclass
class Plan:
    kind: str  # OPEN_MARKET | OPEN_LIMIT | CLOSE_POSITION | CANCEL_PENDING | MOVE_STOP
    side: str | None = None
    volume: float | None = None
    price: float | None = None  # limit price, or reference price for market orders
    sl: float | None = None
    tp: float | None = None
    ticket: int | None = None
    expiration: datetime | None = None
    risk_eur: float | None = None
    risk_pct: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiration"] = None if self.expiration is None else self.expiration.strftime("%Y-%m-%dT%H:%M:%SZ")
        return d


@dataclass
class Verdict:
    verdict: str  # APPROVED | REJECTED | HOLD
    reason: str
    plan: Plan | None = None
    calc: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVED"


def reject(reason: str, **calc) -> Verdict:
    return Verdict("REJECTED", reason, None, calc)


def _fmt(x: float, digits: int) -> str:
    return f"{x:.{digits}f}"


def size_lots(risk_eur: float, stop_distance: float, spec: SymbolSpec) -> tuple[float, float]:
    """Lots for a given risk: round down to volume_step, clamp to volume_max.

    Returns (lots, loss_per_lot_eur). lots == 0 means below volume_min.
    """
    loss_per_lot = stop_distance / spec.tick_size * spec.tick_value_loss
    if loss_per_lot <= 0:
        return 0.0, loss_per_lot
    raw = risk_eur / loss_per_lot
    steps = math.floor(raw / spec.volume_step + 1e-9)
    lots = round(steps * spec.volume_step, 8)
    lots = min(lots, spec.volume_max)
    lots = round(math.floor(lots / spec.volume_step + 1e-9) * spec.volume_step, 8)
    if lots < spec.volume_min - 1e-12:
        return 0.0, loss_per_lot
    return lots, loss_per_lot


def evaluate(d: Decision, spec: SymbolSpec, mkt: Market, st: RiskState, cfg: Config) -> Verdict:
    if d.action == "HOLD":
        return Verdict("HOLD", "No action")
    if d.action in ("OPEN_LONG", "OPEN_SHORT"):
        return _evaluate_entry(d, spec, mkt, st, cfg)
    if d.action == "CLOSE":
        if st.position:
            return Verdict("APPROVED", f"Close the {st.position.side} position", Plan("CLOSE_POSITION", st.position.side, st.position.volume, ticket=st.position.ticket))
        if st.pending:
            return Verdict("APPROVED", f"Cancel the pending {st.pending.side} limit", Plan("CANCEL_PENDING", st.pending.side, st.pending.volume, ticket=st.pending.ticket))
        return reject("Nothing to close: no bot position or pending order")
    if d.action == "MOVE_STOP":
        return _evaluate_move_stop(d, spec, mkt, st)
    return reject(f"Unknown action {d.action}")


def _evaluate_entry(d: Decision, spec: SymbolSpec, mkt: Market, st: RiskState, cfg: Config) -> Verdict:
    r = cfg.risk
    side = d.side
    digits = spec.digits
    if st.entry_block:
        return reject(f"No new entries: {st.entry_block}")
    if st.position or st.pending:
        return reject("A bot position or pending order is already open (max 1)")
    if st.entries_today >= r.max_trades_per_day:
        return reject(f"Max {r.max_trades_per_day} entries for today reached")
    if st.cooldown_bars_left > 0:
        n = st.cooldown_bars_left
        return reject(f"Cooldown after a loss: {n} bar{'s' if n != 1 else ''} left")
    if cfg.signal.mode == "filter" and st.recent_flip_direction != side:
        if st.recent_flip_direction is None:
            return reject(f"Signal filter: no Trend Signals flip in the last {cfg.signal.filter_bars} bars")
        return reject(f"Signal filter: the recent flip is {st.recent_flip_direction}, so only {st.recent_flip_direction} entries are allowed")
    spread_pips = spec.pips(mkt.spread)
    if spread_pips > r.max_spread_pips + 1e-9:
        return reject(f"Spread is {spread_pips:.1f} pips; maximum is {r.max_spread_pips:.1f}")
    if d.stop_loss is None:
        return reject("No stop-loss: every entry needs one")
    if d.take_profit is None:
        return reject("No take-profit: every entry needs one")
    order_type = d.order_type or "MARKET"
    if order_type not in ("MARKET", "LIMIT"):
        return reject(f"Unknown order type {order_type}")

    sl, tp = spec.norm(d.stop_loss), spec.norm(d.take_profit)
    stops = spec.stops_level * spec.point
    if order_type == "LIMIT":
        if d.entry is None:
            return reject("A LIMIT order needs an entry price")
        entry = spec.norm(d.entry)
        if side == "long" and not entry < mkt.ask:
            return reject(f"Buy limit {_fmt(entry, digits)} must be below the ask {_fmt(mkt.ask, digits)}")
        if side == "short" and not entry > mkt.bid:
            return reject(f"Sell limit {_fmt(entry, digits)} must be above the bid {_fmt(mkt.bid, digits)}")
        gap = (mkt.ask - entry) if side == "long" else (entry - mkt.bid)
        if gap < stops - 1e-12:
            return reject(f"Limit price is {spec.pips(gap):.1f} pips from the market; the broker's stops level is {spec.pips(stops):.1f}")
        ref_sl = ref_tp = entry  # SL/TP are checked against the order price
    else:
        entry = mkt.ask if side == "long" else mkt.bid
        ref_sl = ref_tp = mkt.bid if side == "long" else mkt.ask  # broker checks against the closing price

    if side == "long" and not (sl < entry < tp):
        return reject(f"For a long, stop-loss must be below and take-profit above the entry {_fmt(entry, digits)}")
    if side == "short" and not (tp < entry < sl):
        return reject(f"For a short, stop-loss must be above and take-profit below the entry {_fmt(entry, digits)}")

    stop_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    atr = mkt.atr
    atr_mult = stop_dist / atr if atr > 0 else float("inf")
    rr = tp_dist / stop_dist if stop_dist > 0 else 0.0
    calc = {
        "entry": entry, "stop_loss": sl, "take_profit": tp, "order_type": order_type,
        "stop_pips": round(spec.pips(stop_dist), 1), "tp_pips": round(spec.pips(tp_dist), 1),
        "atr_pips": round(spec.pips(atr), 1), "stop_atr": round(atr_mult, 2), "rr": round(rr, 2),
        "spread_pips": round(spread_pips, 1),
    }
    if atr <= 0:
        return reject("ATR is not available yet", **calc)
    if atr_mult < r.min_stop_atr - 1e-9:
        return reject(f"Stop is {atr_mult:.1f}× ATR; minimum is {r.min_stop_atr:.1f}×", **calc)
    if atr_mult > r.max_stop_atr + 1e-9:
        return reject(f"Stop is {atr_mult:.1f}× ATR; maximum is {r.max_stop_atr:.1f}×", **calc)
    if rr < r.min_reward_risk - 1e-9:
        return reject(f"Reward:risk is {rr:.2f}; minimum is {r.min_reward_risk:.2f}", **calc)
    sl_gap = (ref_sl - sl) if side == "long" else (sl - ref_sl)
    tp_gap = (tp - ref_tp) if side == "long" else (ref_tp - tp)
    if sl_gap < stops - 1e-12 or tp_gap < stops - 1e-12:
        which = "Stop-loss" if sl_gap < stops - 1e-12 else "Take-profit"
        return reject(f"{which} is closer than the broker's stops level ({spec.pips(stops):.1f} pips)", **calc)

    risk_eur = st.equity * r.risk_per_trade_pct / 100.0
    lots, loss_per_lot = size_lots(risk_eur, stop_dist, spec)
    calc.update({"risk_target_eur": round(risk_eur, 2), "loss_per_lot_eur": round(loss_per_lot, 2)})
    if lots <= 0:
        min_risk_pct = spec.volume_min * loss_per_lot / st.equity * 100 if st.equity > 0 else float("inf")
        return reject(f"Risk too small for the minimum lot: {spec.volume_min:g} lots would risk {min_risk_pct:.2f}%", **calc)
    actual_risk = lots * loss_per_lot
    risk_pct = actual_risk / st.equity * 100 if st.equity > 0 else 0.0
    calc.update({"lots": lots, "risk_eur": round(actual_risk, 2), "risk_pct": round(risk_pct, 3)})

    expiration = None
    if order_type == "LIMIT":
        expiration = mkt.now + timedelta(minutes=15 * r.limit_expiry_bars)
    plan = Plan("OPEN_LIMIT" if order_type == "LIMIT" else "OPEN_MARKET", side, lots, entry, sl, tp, None, expiration, round(actual_risk, 2), round(risk_pct, 3))
    reason = (f"Approved: stop {calc['stop_pips']} pips ({calc['stop_atr']}× ATR), R:R {calc['rr']:.2f}, "
              f"risk {actual_risk:,.2f} ({risk_pct:.2f}%), {lots:g} lots")
    return Verdict("APPROVED", reason, plan, calc)


def _evaluate_move_stop(d: Decision, spec: SymbolSpec, mkt: Market, st: RiskState) -> Verdict:
    p = st.position
    digits = spec.digits
    if p is None:
        return reject("No open position to move the stop on")
    if d.stop_loss is None:
        return reject("MOVE_STOP needs a new stop-loss price")
    new_sl = spec.norm(d.stop_loss)
    calc = {"old_sl": p.sl, "new_sl": new_sl}
    if p.side == "long":
        if p.sl and not new_sl > p.sl:
            return reject(f"Stops only tighten: new stop {_fmt(new_sl, digits)} is not above {_fmt(p.sl, digits)}", **calc)
        price = mkt.bid
        gap = price - new_sl
    else:
        if p.sl and not new_sl < p.sl:
            return reject(f"Stops only tighten: new stop {_fmt(new_sl, digits)} is not below {_fmt(p.sl, digits)}", **calc)
        price = mkt.ask
        gap = new_sl - price
    if gap <= 0:
        return reject(f"New stop {_fmt(new_sl, digits)} is on the wrong side of the price {_fmt(price, digits)}", **calc)
    stops = spec.stops_level * spec.point
    if gap < stops - 1e-12:
        return reject(f"New stop is {spec.pips(gap):.1f} pips from the price; the broker's stops level is {spec.pips(stops):.1f}", **calc)
    freeze = spec.freeze_level * spec.point
    if freeze > 0 and ((p.sl and abs(price - p.sl) <= freeze) or (p.tp and abs(price - p.tp) <= freeze)):
        return reject(f"Price is within the broker's freeze level ({spec.pips(freeze):.1f} pips) of the stop or target, so it can't be changed now", **calc)
    calc["locked_pips"] = round(spec.pips((new_sl - p.entry) if p.side == "long" else (p.entry - new_sl)), 1)
    return Verdict("APPROVED", f"Move stop {_fmt(p.sl, digits)} → {_fmt(new_sl, digits)}",
                   Plan("MOVE_STOP", p.side, p.volume, price, new_sl, p.tp, p.ticket), calc)
