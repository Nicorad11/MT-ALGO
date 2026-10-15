import math

import numpy as np
import pytest

from opus_trader import indicators as ind


def test_ema_seeded_with_first_value():
    assert ind.ema([1, 2, 3, 4], 3) == pytest.approx([1, 1.5, 2.25, 3.125])


def test_rma_seeded_with_sma():
    out = ind.rma([1, 2, 3, 4, 5], 2)
    assert math.isnan(out[0])
    assert out[1:] == pytest.approx([1.5, 2.25, 3.125, 4.0625])


def test_true_range_first_bar():
    h, lo, c = [1.0010, 1.0030], [0.9990, 1.0000], [1.0000, 1.0025]
    assert ind.true_range(h, lo, c, handle_na=True)[0] == pytest.approx(0.0020)
    assert math.isnan(ind.true_range(h, lo, c, handle_na=False)[0])


def test_rsi_extremes_and_midpoint():
    up = ind.rsi(np.arange(1, 40, dtype=float), 14)
    assert up[-1] == pytest.approx(100.0)
    zigzag = np.array([1.0, 2.0] * 30)
    assert 40 < ind.rsi(zigzag, 14)[-1] < 60


def test_rsi_matches_wilder_reference():
    # Wilder's textbook example (14-period RSI on the classic 15-close series).
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert ind.rsi(closes, 14)[-1] == pytest.approx(70.46, abs=0.05)


def test_bollinger_uses_population_stdev():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    upper, mid, lower = ind.bollinger(x, 5, 2.0)
    sd = np.std(x)  # ddof=0
    assert mid[-1] == pytest.approx(3.0)
    assert upper[-1] == pytest.approx(3.0 + 2 * sd)
    assert lower[-1] == pytest.approx(3.0 - 2 * sd)


def test_macd_components():
    x = np.linspace(1.0, 2.0, 60)
    line, sig, hist = ind.macd(x, 12, 26, 9)
    assert line[-1] == pytest.approx(ind.ema(x, 12)[-1] - ind.ema(x, 26)[-1])
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])
