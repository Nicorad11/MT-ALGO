"""Builds what the model sees each bar: structured data (stored) and three text blocks.

The H4 and H1 blocks only change when a new H4/H1 bar closes, so their bytes
stay identical between M15 bars and can be served from the prompt cache.
Everything that changes every bar goes in the last block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from . import indicators as ind
from .clock import ServerTime, session_of, trading_day_start
from .config import RiskConfig, SignalConfig
from .indicators import trend_signals as ts
from .news import Event

UTC = timezone.utc
SEND_BARS = {"M15": 150, "H1": 100, "H4": 60}
FETCH_BARS = 1000  # history used for indicator warm-up (EMA200, ATR convergence)


# ---------------------------------------------------------------- bars

@dataclass
class Bars:
    t: np.ndarray  # UTC epoch seconds of bar open
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    spread: np.ndarray  # points

    @classmethod
    def from_rates(cls, rates, server_time: ServerTime) -> "Bars":
        st = np.asarray(rates["time"], dtype=np.int64)
        return cls(server_to_utc(st, server_time), np.asarray(rates["open"], float), np.asarray(rates["high"], float),
                   np.asarray(rates["low"], float), np.asarray(rates["close"], float), np.asarray(rates["tick_volume"], float),
                   np.asarray(rates["spread"], float))

    def __len__(self) -> int:
        return len(self.c)

    def time(self, i: int) -> datetime:
        return datetime.fromtimestamp(int(self.t[i]), UTC)

    def tail(self, n: int) -> "Bars":
        return Bars(self.t[-n:], self.o[-n:], self.h[-n:], self.l[-n:], self.c[-n:], self.v[-n:], self.spread[-n:])


def server_to_utc(server_epochs: np.ndarray, st: ServerTime) -> np.ndarray:
    if st.model.mode == "fixed":
        return server_epochs - st.model.seconds
    hours = server_epochs // 3600
    uniq, inv = np.unique(hours, return_inverse=True)
    utc_hours = np.array([int(st.to_utc(int(h) * 3600).timestamp()) for h in uniq], dtype=np.int64)
    return utc_hours[inv] + (server_epochs - hours * 3600)


# ---------------------------------------------------------------- formatting helpers

def fmt_price(x: float | None, digits: int = 5) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def fmt_t(epoch_or_dt) -> str:
    if isinstance(epoch_or_dt, datetime):
        return epoch_or_dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return datetime.fromtimestamp(int(epoch_or_dt), UTC).strftime("%Y-%m-%d %H:%M")


def signed(x: float, digits: int = 2, unit: str = "") -> str:
    s = f"{x:+,.{digits}f}"
    return s.replace("-", "−") + unit


def candles_csv(b: Bars, digits: int) -> str:
    rows = ["time_utc (bar open),open,high,low,close,tick_volume"]
    for i in range(len(b)):
        rows.append(f"{fmt_t(b.t[i])},{b.o[i]:.{digits}f},{b.h[i]:.{digits}f},{b.l[i]:.{digits}f},{b.c[i]:.{digits}f},{int(b.v[i])}")
    return "\n".join(rows)


def tf_indicators(b: Bars, pip: float) -> dict:
    c = b.c
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    r = ind.rsi(c, 14)
    a = ind.atr(b.h, b.l, c, 14)
    bu, bm, bl = ind.bollinger(c, 20, 2.0)

    def last(x):
        v = float(x[-1])
        return None if math.isnan(v) else v

    slope = (e20[-1] - e20[-6]) / pip if len(c) > 6 and not math.isnan(e20[-6]) else None
    return {
        "ema20": last(e20), "ema50": last(e50), "ema200": last(e200),
        "ema20_slope_5_pips": None if slope is None else round(slope, 1),
        "rsi14": None if last(r) is None else round(last(r), 1),
        "atr14": last(a), "atr14_pips": None if last(a) is None else round(last(a) / pip, 1),
        "bb_upper": last(bu), "bb_mid": last(bm), "bb_lower": last(bl),
        "bb_width_pips": None if last(bu) is None else round((last(bu) - last(bl)) / pip, 1),
        "close": float(c[-1]),
    }


def indicators_line(name: str, d: dict, digits: int, bar_open: datetime, minutes: int) -> str:
    slope = d["ema20_slope_5_pips"]
    slope_txt = "" if slope is None else f" ({signed(slope, 1)} pips over 5 bars)"
    closed = (bar_open + timedelta(minutes=minutes)).strftime("%H:%M")
    return (f"{name} indicators as of the {fmt_t(bar_open)} bar (closed {closed} UTC): "
            f"EMA20 {fmt_price(d['ema20'], digits)}{slope_txt} · EMA50 {fmt_price(d['ema50'], digits)} · EMA200 {fmt_price(d['ema200'], digits)} · "
            f"RSI14 {d['rsi14']} · ATR14 {d['atr14_pips']} pips · "
            f"Bollinger 20/2 {fmt_price(d['bb_upper'], digits)} / {fmt_price(d['bb_mid'], digits)} / {fmt_price(d['bb_lower'], digits)} (width {d['bb_width_pips']} pips)")


# ---------------------------------------------------------------- levels

def previous_day(h1: Bars, now: datetime) -> dict | None:
    today = trading_day_start(now)
    prev = trading_day_start(today - timedelta(seconds=1))
    sel = (h1.t >= prev.timestamp()) & (h1.t < today.timestamp())
    if not sel.any():
        return None
    idx = np.flatnonzero(sel)
    return {"start": fmt_t(prev), "end": fmt_t(today), "high": float(h1.h[idx].max()), "low": float(h1.l[idx].min()), "close": float(h1.c[idx[-1]])}


def asian_range(m15: Bars, now: datetime) -> dict | None:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = day, day + timedelta(hours=7)
    sel = (m15.t >= start.timestamp()) & (m15.t < end.timestamp())
    if not sel.any():
        start, end = start - timedelta(days=1), end - timedelta(days=1)
        sel = (m15.t >= start.timestamp()) & (m15.t < end.timestamp())
        if not sel.any():
            return None
    idx = np.flatnonzero(sel)
    complete = now >= end
    return {"date": start.strftime("%Y-%m-%d"), "high": float(m15.h[idx].max()), "low": float(m15.l[idx].min()), "complete": complete}


# ---------------------------------------------------------------- trend signals

def signal_summary(m15: Bars, sig: ts.TrendSignals, bid: float, ask: float, pip: float, point: float, cfg: SignalConfig) -> dict:
    n = len(m15)
    last = n - 1
    flips = sig.flip_indices()
    last_flip = int(flips[-1]) if len(flips) else None
    trend = int(sig.trend[last])
    line = sig.line(last)
    price = ask if trend == 1 else bid  # executable entry in the trend's direction
    dist = (price - line) if trend == 1 else (line - price)
    atr_v = float(sig.atr[last])
    targets = {f"{k}R": (price + k * dist) if trend == 1 else (price - k * dist) for k in (1, 2, 3)} if dist > 0 else {}
    spreads = m15.spread * point
    trades = ts.simulate(m15.o, m15.h, m15.l, m15.c, spreads, sig)
    by_entry = {t.entry_i: t for t in trades}
    recent = []
    for fi in flips[-5:][::-1]:
        fi = int(fi)
        t = by_entry.get(fi)
        item = {"time": fmt_t(m15.t[fi]), "direction": "BUY" if sig.buy[fi] else "SELL", "price": float(m15.c[fi])}
        if t is None:
            item["outcome"] = "no trade"
        elif t.exit_reason == "open":
            mark = m15.c[last] if t.side == "long" else m15.c[last] + spreads[last]
            item["outcome"] = "open"
            item["r"] = round(t.r_at(mark), 2)
        else:
            item["outcome"] = t.exit_reason
            item["r"] = round(t.r_at(t.exit_price), 2)
        recent.append(item)
    recent_dir = None
    if last_flip is not None and last - last_flip < cfg.filter_bars:
        recent_dir = "long" if sig.buy[last_flip] else "short"
    return {
        "trend": "up" if trend == 1 else "down",
        "trend_value": trend,
        "bars_since_flip": None if last_flip is None else last - last_flip,
        "last_flip_time": None if last_flip is None else fmt_t(m15.t[last_flip]),
        "flipped_now": bool(sig.buy[last] or sig.sell[last]),
        "flip_now": "BUY" if sig.buy[last] else "SELL" if sig.sell[last] else None,
        "line": line,
        "line_side": "below" if trend == 1 else "above",
        "line_distance_pips": round(dist / pip, 1),
        "line_distance_atr": round(dist / atr_v, 2) if atr_v > 0 else None,
        "atr_pips": round(atr_v / pip, 1),
        "entry_reference": price,
        "targets": targets,
        "cloud": ts.cloud_state(sig, m15.c, last),
        "last_flips": recent,
        "mode": cfg.mode,
        "filter_bars": cfg.filter_bars,
        "recent_flip_direction": recent_dir,
    }


def signal_text(s: dict, digits: int, cfg: SignalConfig) -> str:
    trend_word = "UP" if s["trend"] == "up" else "DOWN"
    since = "" if s["last_flip_time"] is None else f" since the {s['last_flip_time']} UTC flip ({s['bars_since_flip']} bars ago)"
    now_txt = f"The bar that just closed flipped: {s['flip_now']}." if s["flipped_now"] else "The bar that just closed did not flip."
    side = "long" if s["trend"] == "up" else "short"
    ref = "ask" if side == "long" else "bid"
    lines = [
        f"## Trend Signals indicator (M15, ATR {cfg.atr_length} × {cfg.multiplier}, closed bars)",
        f"Trend: {trend_word}{since}. {now_txt}",
    ]
    if s["line_distance_pips"] > 0:
        lines.append(f"Trailing line (the natural stop for a {side}): {fmt_price(s['line'], digits)}, "
                     f"{s['line_distance_pips']} pips / {s['line_distance_atr']}× ATR {s['line_side']} the {ref} {fmt_price(s['entry_reference'], digits)}.")
        t = s["targets"]
        lines.append(f"Targets measured from that stop: 1R {fmt_price(t['1R'], digits)} · 2R {fmt_price(t['2R'], digits)} · 3R {fmt_price(t['3R'], digits)}")
    else:
        lines.append(f"Trailing line: {fmt_price(s['line'], digits)}; price is on the wrong side of it, so it is not a usable stop right now.")
    c = s["cloud"]
    lines.append(f"Cloud: price {c['price_vs_cloud']} the EMA(10) high/low band ({fmt_price(c['cloud_high'], digits)} / {fmt_price(c['cloud_low'], digits)}). "
                 f"MACD(12,26,9) line {signed(c['macd'], 5)}, {c['macd_side']} and {c['macd_direction']}; histogram {signed(c['macd_hist'], 5)}, {c['macd_hist_direction']}.")
    if s["last_flips"]:
        lines.append("Last 5 flips with the indicator-only outcome (enter at the flip close, stop on the line, spread included):")
        for f in s["last_flips"]:
            if f["outcome"] == "open":
                out = f"still open, {signed(f['r'])}R so far"
            elif f["outcome"] == "no trade":
                out = "no trade"
            else:
                out = f"{signed(f['r'])}R ({'stopped on the line' if f['outcome'] == 'stop' else 'closed on the opposite flip'})"
            lines.append(f"  {f['time']}  {f['direction']:<4}  {out}")
    if cfg.mode == "filter":
        allowed = {"long": "long entries only", "short": "short entries only"}.get(s["recent_flip_direction"] or "", "no entries right now")
        lines.append(f"Signal mode: FILTER. You may only open a trade in the direction of a flip within the last {cfg.filter_bars} closed bars "
                     f"(currently: {allowed}). HOLD, CLOSE and MOVE_STOP are always allowed.")
    else:
        lines.append("Signal mode: INPUT. Use this indicator or ignore it, as you judge best.")
    return "\n".join(lines)


# ---------------------------------------------------------------- snapshot

@dataclass
class SnapshotInputs:
    bar_time: datetime
    now: datetime
    symbol: str
    digits: int
    point: float
    m15: Bars
    h1: Bars
    h4: Bars
    bid: float
    ask: float
    mode: str  # dry | live
    currency: str
    balance: float
    equity: float
    today_pnl: float
    sod_equity: float
    position: dict | None = None
    pending: dict | None = None
    recent_trades: list[dict] = field(default_factory=list)
    recent_decisions: list[dict] = field(default_factory=list)
    last_rejection: dict | None = None
    events: list[Event] = field(default_factory=list)
    risk_state: dict = field(default_factory=dict)
    signal_cfg: SignalConfig = field(default_factory=SignalConfig)
    risk_cfg: RiskConfig = field(default_factory=RiskConfig)


@dataclass
class Snapshot:
    data: dict
    h4_text: str
    h1_text: str
    live_text: str
    sig: ts.TrendSignals
    m15_atr: float  # ATR(14) of M15 in price units

    @property
    def prompt_text(self) -> str:
        return "\n\n".join([self.h4_text, self.h1_text, self.live_text])


def pip_size(digits: int, point: float) -> float:
    """0.0001 (0.01 for JPY-style quotes), however many decimals the broker adds: 4, 5 or 6."""
    return 0.01 if digits <= 3 else 0.0001


def build(x: SnapshotInputs) -> Snapshot:
    d = x.digits
    pip = pip_size(d, x.point)
    h4, h1, m15 = x.h4, x.h1, x.m15
    ind_h4, ind_h1, ind_m15 = tf_indicators(h4, pip), tf_indicators(h1, pip), tf_indicators(m15, pip)
    sc = x.signal_cfg
    sig = ts.compute(m15.h, m15.l, m15.c, sc.atr_length, sc.multiplier, sc.atr_method)
    sig_sum = signal_summary(m15, sig, x.bid, x.ask, pip, x.point, sc)
    prev = previous_day(h1, x.now)
    asia = asian_range(m15, x.now)
    session = session_of(x.now)
    spread_pips = round((x.ask - x.bid) / pip, 1)

    s4, s1, s15 = h4.tail(SEND_BARS["H4"]), h1.tail(SEND_BARS["H1"]), m15.tail(SEND_BARS["M15"])
    h4_text = (f"## H4 candles: last {len(s4)} closed bars, UTC, oldest first\n{candles_csv(s4, d)}\n\n"
               + indicators_line("H4", ind_h4, d, s4.time(-1), 240))
    h1_text = (f"## H1 candles: last {len(s1)} closed bars, UTC, oldest first\n{candles_csv(s1, d)}\n\n"
               + indicators_line("H1", ind_h1, d, s1.time(-1), 60))

    live: list[str] = []
    live.append(f"## M15 candles: last {len(s15)} closed bars, UTC, oldest first\n{candles_csv(s15, d)}")
    live.append(indicators_line("M15", ind_m15, d, s15.time(-1), 15))

    lv = ["## Levels"]
    if prev:
        lv.append(f"Previous trading day ({prev['start']} to {prev['end']} UTC, 17:00 New York rollover): "
                  f"high {fmt_price(prev['high'], d)} · low {fmt_price(prev['low'], d)} · close {fmt_price(prev['close'], d)}")
    if asia:
        state = "" if asia["complete"] else " (still forming)"
        lv.append(f"Asian range {asia['date']} 00:00 to 07:00 UTC{state}: high {fmt_price(asia['high'], d)} · low {fmt_price(asia['low'], d)} "
                  f"({round((asia['high'] - asia['low']) / pip, 1)} pips)")
    lv.append(f"Current session: {session}")
    live.append("\n".join(lv))

    live.append(signal_text(sig_sum, d, sc))

    live.append(f"## Market now\nBid {fmt_price(x.bid, d)} · Ask {fmt_price(x.ask, d)} · Spread {spread_pips} pips · {x.now.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    mode_txt = "dry run: simulated fills" if x.mode == "dry" else "demo account: live orders"
    pnl_pct = (x.today_pnl / x.sod_equity * 100) if x.sod_equity else 0.0
    live.append(f"## Account ({mode_txt})\nBalance {x.balance:,.2f} {x.currency} · Equity {x.equity:,.2f} {x.currency} · "
                f"Bot P&L today {signed(x.today_pnl)} {x.currency} ({signed(pnl_pct)}%) since 17:00 New York")

    pos_lines = ["## Open bot position"]
    if x.position:
        p = x.position
        pos_lines.append(f"{p['side'].capitalize()} {p['volume']:.2f} lots @ {fmt_price(p['entry'], d)} since {p['opened']} UTC ({p['bars_open']} bars) · "
                         f"SL {fmt_price(p['sl'], d)} · TP {fmt_price(p['tp'], d)} · unrealized {signed(p['unrealized'])} {x.currency} ({signed(p['unrealized_r'])}R)")
    elif x.pending:
        p = x.pending
        pos_lines.append(f"No position. Pending {p['side']} limit {p['volume']:.2f} lots @ {fmt_price(p['price'], d)} · SL {fmt_price(p['sl'], d)} · "
                         f"TP {fmt_price(p['tp'], d)} · expires {p['expires']} UTC. CLOSE cancels it.")
    else:
        pos_lines.append("None. You are flat.")
    live.append("\n".join(pos_lines))

    tr = ["## Your last 10 closed trades"]
    if x.recent_trades:
        for t in x.recent_trades:
            tr.append(f"{t['close_time']}  {t['side']:<5}  {t.get('setup') or 'n/a'}  {signed(t['r_multiple'])}R  {t.get('exit_reason') or ''}".rstrip())
    else:
        tr.append("None yet.")
    live.append("\n".join(tr))

    dec = ["## Your last 3 decisions"]
    if x.recent_decisions:
        for r in x.recent_decisions:
            dec.append(f"{r['bar_time']}  {r['action']}  ({r.get('verdict') or ''}): {r.get('reasoning') or ''}")
    else:
        dec.append("None yet.")
    live.append("\n".join(dec))

    if x.last_rejection:
        r = x.last_rejection
        live.append(f"## Rejection on the previous bar\nYour {r['action']} at {r['bar_time']} was rejected by the risk engine: {r['reason']}")

    ev = ["## High-impact events in the next 4 hours"]
    if x.events:
        for e in x.events:
            mins = int((e.time - x.now).total_seconds() // 60)
            ev.append(f"{fmt_t(e.time)} UTC  {e.currency}  {e.title} (in {mins} min)")
    else:
        ev.append("None.")
    live.append("\n".join(ev))

    rs = x.risk_state
    allowed = "yes" if rs.get("entries_allowed", True) else "no (" + "; ".join(rs.get("blocked", [])) + ")"
    risk_eur = x.equity * x.risk_cfg.risk_per_trade_pct / 100
    live.append(f"## Risk state\nNew entries allowed: {allowed}. Entries today: {rs.get('entries_today', 0)} of {x.risk_cfg.max_trades_per_day}. "
                f"Risk per trade: {x.risk_cfg.risk_per_trade_pct:.2f}% of equity (≈ {risk_eur:,.2f} {x.currency}); the engine sizes the lots.")

    close_time = x.bar_time + timedelta(minutes=15)
    live.append(f"Decide for the M15 bar that closed at {fmt_t(close_time)} UTC. Call submit_decision exactly once.")

    data = {
        "bar_time": fmt_t(x.bar_time),
        "symbol": x.symbol,
        "digits": d,
        "pip": pip,
        "bars_sent": {"H4": len(s4), "H1": len(s1), "M15": len(s15)},
        "indicators": {"H4": ind_h4, "H1": ind_h1, "M15": ind_m15},
        "levels": {"previous_day": prev, "asian_range": asia},
        "session": session,
        "market": {"bid": x.bid, "ask": x.ask, "spread_pips": spread_pips, "time": x.now.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "account": {"mode": x.mode, "currency": x.currency, "balance": x.balance, "equity": x.equity, "today_pnl": x.today_pnl, "today_pnl_pct": round(pnl_pct, 2)},
        "position": x.position,
        "pending": x.pending,
        "recent_trades": x.recent_trades,
        "recent_decisions": x.recent_decisions,
        "last_rejection": x.last_rejection,
        "events": [e.to_dict(x.now) for e in x.events],
        "risk_state": rs,
        "signal": sig_sum,
    }
    return Snapshot(data, h4_text, h1_text, "\n\n".join(live), sig, float(ind_m15["atr14"] or 0.0))
