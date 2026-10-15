"""A literal, slow transliteration of the Trend Signals Pine code, used only by tests.

Written independently of opus_trader/indicators to cross-check the port:
`None` plays Pine's `na`, every series is evaluated bar by bar exactly as the
Pine runtime does, and ta.sma / ta.rma / ta.tr follow the Pine reference.
"""

from __future__ import annotations


def nz(x, y):
    return y if x is None else x


def gt(a, b):  # Pine: any comparison with na is false
    return a is not None and b is not None and a > b


def lt(a, b):
    return a is not None and b is not None and a < b


def pine_tr(h, lo, c, i, handle_na):
    prev = c[i - 1] if i > 0 else None
    if prev is None:
        return (h[i] - lo[i]) if handle_na else None
    return max(h[i] - lo[i], abs(h[i] - prev), abs(lo[i] - prev))


def pine_sma(series, i, length):
    if i < length - 1:
        return None
    window = series[i - length + 1 : i + 1]
    if any(v is None for v in window):
        return None
    return sum(window) / length


def supertrend(high, low, close, periods=14, multiplier=2.0, change_atr=True):
    n = len(close)
    tr_true = [pine_tr(high, low, close, i, True) for i in range(n)]
    tr_false = [pine_tr(high, low, close, i, False) for i in range(n)]
    atr = [None] * n
    rma_sum = [None] * n
    for i in range(n):
        if change_atr:
            # ta.atr(length) = ta.rma(ta.tr(true), length)
            prev = rma_sum[i - 1] if i > 0 else None
            if prev is None:
                rma_sum[i] = pine_sma(tr_true, i, periods)
            else:
                alpha = 1 / periods
                rma_sum[i] = alpha * tr_true[i] + (1 - alpha) * nz(prev, 0)
            atr[i] = rma_sum[i]
        else:
            atr[i] = pine_sma(tr_false, i, periods)

    up = [None] * n
    dn = [None] * n
    trend = [None] * n
    for i in range(n):
        src = (high[i] + low[i]) / 2
        u = None if atr[i] is None else src - multiplier * atr[i]
        up1 = nz(up[i - 1] if i > 0 else None, u)
        close1 = close[i - 1] if i > 0 else None
        up[i] = max(u, up1) if gt(close1, up1) else u
        d = None if atr[i] is None else src + multiplier * atr[i]
        dn1 = nz(dn[i - 1] if i > 0 else None, d)
        dn[i] = min(d, dn1) if lt(close1, dn1) else d
        t = 1
        t = nz(trend[i - 1] if i > 0 else None, t)
        if t == -1 and gt(close[i], dn1):
            t = 1
        elif t == 1 and lt(close[i], up1):
            t = -1
        trend[i] = t
    buy = [i > 0 and trend[i] == 1 and trend[i - 1] == -1 for i in range(n)]
    sell = [i > 0 and trend[i] == -1 and trend[i - 1] == 1 for i in range(n)]
    return {"atr": atr, "up": up, "dn": dn, "trend": trend, "buy": buy, "sell": sell}
