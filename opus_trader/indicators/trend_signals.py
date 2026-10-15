"""Port of the Trend Signals TradingView indicator (signal-driving parts only).

ATR trailing-stop trend signal, computed on closed bars:

    src = hl2
    atr = ta.atr(len)                    (method 1, Wilder's RMA of true range)
        | ta.sma(ta.tr, len)             (method 2)
    up  = src - mult * atr;  up1 = nz(up[1], up);  up := close[1] > up1 ? max(up, up1) : up
    dn  = src + mult * atr;  dn1 = nz(dn[1], dn);  dn := close[1] < dn1 ? min(dn, dn1) : dn
    trend = nz(trend[1], 1)
    trend := trend == -1 and close > dn1 ? 1 : trend == 1 and close < up1 ? -1 : trend
    buy  = trend == 1 and trend[1] == -1
    sell = trend == -1 and trend[1] == 1

Plus the cloud: EMA(high, 10) / EMA(low, 10) and MACD(12, 26, 9) above or
below zero, rising or falling. The indicator's RSI(9), CMO, pivot
support/resistance and percent-based SL/TP are deliberately not ported.

Pine `na` semantics are reproduced bar by bar: comparisons with na are false,
and nz() falls back to the current value. Feed at least 1000 bars so the ATR
converges to TradingView's values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import ema, macd, rma, sma, true_range


@dataclass
class TrendSignals:
    atr: np.ndarray
    up: np.ndarray
    dn: np.ndarray
    trend: np.ndarray  # +1 / -1
    buy: np.ndarray  # bool, flip to +1 on this bar
    sell: np.ndarray  # bool, flip to -1 on this bar
    cloud_high: np.ndarray  # EMA(high, 10)
    cloud_low: np.ndarray  # EMA(low, 10)
    macd: np.ndarray
    macd_signal: np.ndarray
    macd_hist: np.ndarray

    def line(self, i: int) -> float:
        """Trailing line in force after bar i: `up` in an uptrend, `dn` in a downtrend."""
        return float(self.up[i] if self.trend[i] == 1 else self.dn[i])

    def flip_indices(self) -> np.ndarray:
        return np.flatnonzero(self.buy | self.sell)


def compute(high, low, close, atr_length: int = 14, multiplier: float = 2.0, atr_method: int = 1,
            cloud_length: int = 10, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9) -> TrendSignals:
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(c)
    src = (h + lo) / 2.0
    if atr_method == 1:
        atr_v = rma(true_range(h, lo, c, handle_na=True), atr_length)
    elif atr_method == 2:
        atr_v = sma(true_range(h, lo, c, handle_na=False), atr_length)
    else:
        raise ValueError("atr_method must be 1 or 2")

    up = np.full(n, np.nan)
    dn = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)

    for i in range(n):
        up_raw = src[i] - multiplier * atr_v[i]
        dn_raw = src[i] + multiplier * atr_v[i]
        up1 = up[i - 1] if i > 0 and not math.isnan(up[i - 1]) else up_raw
        dn1 = dn[i - 1] if i > 0 and not math.isnan(dn[i - 1]) else dn_raw
        prev_close = c[i - 1] if i > 0 else math.nan
        # Comparisons with NaN are False, matching Pine's na handling.
        up[i] = max(up_raw, up1) if prev_close > up1 else up_raw
        dn[i] = min(dn_raw, dn1) if prev_close < dn1 else dn_raw
        t = trend[i - 1] if i > 0 else 1
        if t == -1 and c[i] > dn1:
            t = 1
        elif t == 1 and c[i] < up1:
            t = -1
        trend[i] = t
        if i > 0:
            buy[i] = t == 1 and trend[i - 1] == -1
            sell[i] = t == -1 and trend[i - 1] == 1

    m_line, m_sig, m_hist = macd(c, macd_fast, macd_slow, macd_signal)
    return TrendSignals(atr_v, up, dn, trend, buy, sell, ema(h, cloud_length), ema(lo, cloud_length), m_line, m_sig, m_hist)


def cloud_state(sig: TrendSignals, close: np.ndarray, i: int = -1) -> dict:
    """Cloud and MACD state on bar i.

    NOTE: pending the original .pine file, this reports the raw components
    (price vs the EMA high/low band, MACD line and histogram sign and slope)
    rather than a single colour, so nothing is invented.
    """
    i = i % len(close)
    hi, lo, c = float(sig.cloud_high[i]), float(sig.cloud_low[i]), float(close[i])
    position = "above" if c > hi else "below" if c < lo else "inside"
    line, prev_line = float(sig.macd[i]), float(sig.macd[i - 1]) if i > 0 else math.nan
    hist, prev_hist = float(sig.macd_hist[i]), float(sig.macd_hist[i - 1]) if i > 0 else math.nan
    return {
        "cloud_high": hi,
        "cloud_low": lo,
        "price_vs_cloud": position,
        "macd": line,
        "macd_side": "above zero" if line > 0 else "below zero",
        "macd_direction": "rising" if line > prev_line else "falling",
        "macd_hist": hist,
        "macd_hist_side": "above zero" if hist > 0 else "below zero",
        "macd_hist_direction": "rising" if hist > prev_hist else "falling",
    }


@dataclass
class BenchTrade:
    """One trade of the indicator-only benchmark (prices; R is spread-inclusive)."""

    side: str  # long | short
    entry_i: int
    entry_price: float
    stop_initial: float
    stop: float
    exit_i: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # stop | flip | open

    @property
    def risk(self) -> float:
        return abs(self.entry_price - self.stop_initial)

    def r_at(self, price: float) -> float:
        move = price - self.entry_price if self.side == "long" else self.entry_price - price
        return move / self.risk if self.risk > 0 else 0.0


def simulate(open_, high, low, close, spread, sig: TrendSignals, start_index: int = 0) -> list[BenchTrade]:
    """Indicator-only strategy on bid bars, spread as a cost.

    - Enter at the close of each flip bar (long at close + spread, short at close).
    - Initial stop at the trailing line; trail it with the line every bar.
    - Exit when price touches the stop (longs on the bid low, shorts on the ask
      high; a gap through the stop fills at the open) or on the opposite flip
      (at the flip bar's close), where the opposite trade is entered.
    `spread` is in price units per bar.
    """
    o, h, lo, c, sp = (np.asarray(a, dtype=float) for a in (open_, high, low, close, spread))
    trades: list[BenchTrade] = []
    pos: BenchTrade | None = None
    for i in range(max(1, start_index), len(c)):
        if pos is not None:
            if pos.side == "long" and lo[i] <= pos.stop:
                pos.exit_i, pos.exit_price, pos.exit_reason = i, min(o[i], pos.stop), "stop"
                pos = None
            elif pos is not None and pos.side == "short" and h[i] + sp[i] >= pos.stop:
                pos.exit_i, pos.exit_price, pos.exit_reason = i, max(o[i] + sp[i], pos.stop), "stop"
                pos = None
        flipped = bool(sig.buy[i] or sig.sell[i])
        if flipped:
            if pos is not None:
                pos.exit_i = i
                pos.exit_price = c[i] if pos.side == "long" else c[i] + sp[i]
                pos.exit_reason = "flip"
                pos = None
            side = "long" if sig.buy[i] else "short"
            entry = c[i] + sp[i] if side == "long" else c[i]
            stop = sig.line(i)
            if not math.isnan(stop) and ((side == "long" and stop < entry) or (side == "short" and stop > entry)):
                pos = BenchTrade(side, i, entry, stop, stop)
                trades.append(pos)
        elif pos is not None:
            line = sig.line(i)
            if not math.isnan(line):
                pos.stop = max(pos.stop, line) if pos.side == "long" else min(pos.stop, line)
    for t in trades:
        if t.exit_i is None:
            t.exit_reason = "open"
    return trades


def agreement(action: str | None, trend: int, flipped_now: bool, position_side: str | None) -> str:
    """Did the model's decision agree with the indicator on this bar?

    Entries agree when they follow the trend. HOLD while flat on a fresh flip
    ignores a signal. CLOSE agrees when it exits against the trend.
    """
    if action == "OPEN_LONG":
        return "agree" if trend == 1 else "disagree"
    if action == "OPEN_SHORT":
        return "agree" if trend == -1 else "disagree"
    if action == "HOLD":
        return "disagree" if flipped_now and position_side is None else "n/a"
    if action == "CLOSE" and position_side:
        against = (position_side == "long" and trend == -1) or (position_side == "short" and trend == 1)
        return "agree" if against else "disagree"
    return "n/a"
