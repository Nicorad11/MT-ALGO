"""Indicators matching TradingView's Pine definitions (numpy only).

- ema:  ta.ema  (alpha = 2/(n+1), seeded with the first value)
- rma:  ta.rma  (alpha = 1/n, seeded with the SMA of the first n values)
- true_range: ta.tr(handle_na)
- atr:  ta.atr  (rma of ta.tr(true))
- rsi:  ta.rsi  (rma of gains / rma of losses)
- bollinger: ta.bb (sma +/- k * population stdev)
- macd: ta.macd (ema fast - ema slow, signal = ema of macd)
Values before an indicator is defined are NaN.
"""

from __future__ import annotations

import numpy as np


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x, n: int) -> np.ndarray:
    """ta.sma: na when any value in the window is na."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1 :] = np.lib.stride_tricks.sliding_window_view(x, n).mean(axis=1)
    return out


def ema(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    alpha = 2.0 / (n + 1)
    prev = np.nan
    for i, v in enumerate(x):
        if np.isnan(v):
            out[i] = prev
            continue
        prev = v if np.isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(x, n: int) -> np.ndarray:
    """Wilder's moving average. First value = SMA of the first n non-NaN inputs."""
    x = _arr(x)
    out = np.full(len(x), np.nan)
    alpha = 1.0 / n
    prev = np.nan
    window: list[float] = []
    for i, v in enumerate(x):
        if np.isnan(prev):
            if np.isnan(v):
                window = []
                continue
            window.append(v)
            if len(window) == n:
                prev = sum(window) / n
                out[i] = prev
            continue
        if not np.isnan(v):
            prev = alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def true_range(high, low, close, handle_na: bool = True) -> np.ndarray:
    h, lo, c = _arr(high), _arr(low), _arr(close)
    prev_c = np.r_[np.nan, c[:-1]]
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_c), np.abs(lo - prev_c)))
    if len(tr):
        tr[0] = (h[0] - lo[0]) if handle_na else np.nan
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close, handle_na=True), n)


def rsi(close, n: int = 14) -> np.ndarray:
    c = _arr(close)
    change = np.r_[np.nan, np.diff(c)]
    up = rma(np.where(np.isnan(change), np.nan, np.maximum(change, 0.0)), n)
    down = rma(np.where(np.isnan(change), np.nan, np.maximum(-change, 0.0)), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(down == 0, 100.0, np.where(up == 0, 0.0, 100.0 - 100.0 / (1.0 + up / down)))
    out[np.isnan(up) | np.isnan(down)] = np.nan
    return out


def stdev(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, n)
    out[n - 1 :] = windows.std(axis=1)  # population stdev, like ta.stdev (biased=true)
    return out


def bollinger(close, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = sma(close, n)
    sd = stdev(close, n)
    return mid + k * sd, mid, mid - k * sd


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig
