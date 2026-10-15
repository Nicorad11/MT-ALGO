"""Trend Signals port: hand-computed bars, a literal Pine cross-check on a fixed fixture, and the benchmark."""

import csv
import json
import math
from pathlib import Path

import numpy as np
import pytest

from opus_trader.indicators import trend_signals as ts

from .pine_reference import supertrend

FIXTURES = Path(__file__).parent / "fixtures"

# Five bars small enough to compute by hand (ATR length 2, multiplier 1.0):
#   bar  high    low     close   TR      ATR(rma2)  up(final)  dn(final)  trend
#   0    1.0010  0.9990  1.0000  0.0020  na         na         na          1
#   1    1.0030  1.0000  1.0025  0.0030  0.00250    0.99900    1.00400     1
#   2    1.0020  0.9950  0.9960  0.0075  0.00500    0.99900    1.00350    -1  (close < up1 0.9990 -> SELL)
#   3    0.9990  0.9940  0.9985  0.0050  0.00500    0.99150    1.00150    -1
#   4    1.0060  0.9990  1.0050  0.0075  0.00625    0.99625    1.00150     1  (close > dn1 1.0015 -> BUY)
H = [1.0010, 1.0030, 1.0020, 0.9990, 1.0060]
L = [0.9990, 1.0000, 0.9950, 0.9940, 0.9990]
C = [1.0000, 1.0025, 0.9960, 0.9985, 1.0050]
O = [1.0000, 1.0000, 1.0025, 0.9960, 0.9985]


def test_hand_computed_bars_method_1():
    s = ts.compute(H, L, C, atr_length=2, multiplier=1.0, atr_method=1, cloud_length=2, macd_fast=2, macd_slow=3, macd_signal=2)
    assert math.isnan(s.atr[0]) and math.isnan(s.up[0]) and math.isnan(s.dn[0])
    assert s.atr[1:] == pytest.approx([0.0025, 0.0050, 0.0050, 0.00625])
    assert s.up[1:] == pytest.approx([0.9990, 0.9990, 0.9915, 0.99625])
    assert s.dn[1:] == pytest.approx([1.0040, 1.0035, 1.0015, 1.0015])
    assert list(s.trend) == [1, 1, -1, -1, 1]
    assert list(np.flatnonzero(s.sell)) == [2]
    assert list(np.flatnonzero(s.buy)) == [4]
    assert s.line(3) == pytest.approx(1.0015)  # downtrend -> dn
    assert s.line(4) == pytest.approx(0.99625)  # uptrend -> up


def test_hand_computed_atr_method_2_is_sma_of_tr():
    s = ts.compute(H, L, C, atr_length=2, multiplier=1.0, atr_method=2, cloud_length=2, macd_fast=2, macd_slow=3, macd_signal=2)
    # ta.tr (no na handling) is na on bar 0, so the 2-bar SMA starts on bar 2.
    assert math.isnan(s.atr[0]) and math.isnan(s.atr[1])
    assert s.atr[2:] == pytest.approx([0.00525, 0.00625, 0.00625])


def load_fixture():
    with open(FIXTURES / "eurusd_m15_fixture.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    cols = {k: np.array([float(r[k]) for r in rows]) for k in ("open", "high", "low", "close", "spread_points")}
    return [r["time_utc"] for r in rows], cols


@pytest.mark.parametrize("method", [1, 2])
def test_fixture_matches_literal_pine_reference(method):
    times, b = load_fixture()
    assert len(times) >= 1000
    ours = ts.compute(b["high"], b["low"], b["close"], 14, 2.0, method)
    ref = supertrend(list(b["high"]), list(b["low"]), list(b["close"]), 14, 2.0, change_atr=(method == 1))
    for key in ("atr", "up", "dn"):
        theirs = np.array([np.nan if v is None else v for v in ref[key]])
        assert np.array_equal(np.isnan(getattr(ours, key)), np.isnan(theirs)), key
        assert np.allclose(getattr(ours, key)[~np.isnan(theirs)], theirs[~np.isnan(theirs)], rtol=0, atol=1e-12), key
    assert list(ours.trend) == ref["trend"]
    assert list(ours.buy) == ref["buy"]
    assert list(ours.sell) == ref["sell"]


def test_fixture_flips_regression():
    """Flip times on the fixture are pinned so any change to the port is visible."""
    times, b = load_fixture()
    s = ts.compute(b["high"], b["low"], b["close"], 14, 2.0, 1)
    flips = [{"time": times[i], "direction": "BUY" if s.buy[i] else "SELL"} for i in s.flip_indices()]
    expected = json.loads((FIXTURES / "trend_signals_expected.json").read_text())
    assert flips == expected["flips"]
    assert s.up[-1] == pytest.approx(expected["last"]["up"], abs=1e-9)
    assert s.dn[-1] == pytest.approx(expected["last"]["dn"], abs=1e-9)
    assert s.atr[-1] == pytest.approx(expected["last"]["atr"], abs=1e-12)


def test_tradingview_export_if_present():
    """Drop a TradingView 'Export chart data' CSV at tests/fixtures/tradingview_export.csv to compare flips with TradingView."""
    path = FIXTURES / "tradingview_export.csv"
    if not path.exists():
        pytest.skip("No TradingView export provided")
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    keys = {k.lower(): k for k in rows[0]}
    get = lambda name: np.array([float(r[keys[name]]) for r in rows])  # noqa: E731
    s = ts.compute(get("high"), get("low"), get("close"), 14, 2.0, 1)
    buy_col = next((keys[k] for k in keys if "buy" in k), None)
    sell_col = next((keys[k] for k in keys if "sell" in k), None)
    assert buy_col and sell_col, "Export must include the Buy and Sell plots"
    tv_buy = np.array([r[buy_col] not in ("", "NaN", "0") for r in rows])
    tv_sell = np.array([r[sell_col] not in ("", "NaN", "0") for r in rows])
    warm = 1000  # ignore the warm-up region, where TradingView had more history
    assert list(np.flatnonzero(s.buy[warm:])) == list(np.flatnonzero(tv_buy[warm:]))
    assert list(np.flatnonzero(s.sell[warm:])) == list(np.flatnonzero(tv_sell[warm:]))


# ---------------------------------------------------------------- benchmark

def test_benchmark_short_then_reverse_on_hand_bars():
    s = ts.compute(H, L, C, atr_length=2, multiplier=1.0, atr_method=1, cloud_length=2, macd_fast=2, macd_slow=3, macd_signal=2)
    trades = ts.simulate(O, H, L, C, [0.0] * 5, s)
    short, long_ = trades
    # SELL on bar 2: short at the close 0.9960, stop on dn 1.0035; trailed to 1.0015 on bar 3.
    assert (short.side, short.entry_i, short.entry_price, short.stop_initial) == ("short", 2, 0.9960, pytest.approx(1.0035))
    # Bar 4's high 1.0060 touches the trailed stop 1.0015 before the close flips to BUY.
    assert (short.exit_i, short.exit_reason) == (4, "stop")
    assert short.exit_price == pytest.approx(1.0015)
    assert short.r_at(short.exit_price) == pytest.approx((0.9960 - 1.0015) / (1.0035 - 0.9960))
    # BUY on bar 4: long at the close, stop on up.
    assert (long_.side, long_.entry_i, long_.exit_reason) == ("long", 4, "open")
    assert long_.stop_initial == pytest.approx(0.99625)


def test_benchmark_charges_the_spread():
    s = ts.compute(H, L, C, atr_length=2, multiplier=1.0, atr_method=1, cloud_length=2, macd_fast=2, macd_slow=3, macd_signal=2)
    spread = [0.0002] * 5
    trades = ts.simulate(O, H, L, C, spread, s)
    long_ = trades[-1]
    assert long_.entry_price == pytest.approx(1.0050 + 0.0002)  # longs buy at the ask
    short = trades[0]
    assert short.entry_price == pytest.approx(0.9960)  # shorts sell at the bid


def test_benchmark_stop_never_loosens():
    _, b = load_fixture()
    s = ts.compute(b["high"], b["low"], b["close"], 14, 2.0, 1)
    trades = ts.simulate(b["open"], b["high"], b["low"], b["close"], b["spread_points"] * 1e-5, s)
    assert len(trades) > 10
    for t in trades:
        if t.side == "long":
            assert t.stop >= t.stop_initial
        else:
            assert t.stop <= t.stop_initial
        if t.exit_reason == "stop":  # filled at the stop, or worse only when the bar gapped through it
            assert (t.exit_price <= t.stop + 1e-12) if t.side == "long" else (t.exit_price >= t.stop - 1e-12)


@pytest.mark.parametrize("action,trend,flip,pos,expected", [
    ("OPEN_LONG", 1, False, None, "agree"),
    ("OPEN_LONG", -1, False, None, "disagree"),
    ("OPEN_SHORT", -1, True, None, "agree"),
    ("HOLD", 1, True, None, "disagree"),
    ("HOLD", 1, False, None, "n/a"),
    ("HOLD", 1, True, "long", "n/a"),
    ("CLOSE", -1, False, "long", "agree"),
    ("CLOSE", 1, False, "long", "disagree"),
    ("MOVE_STOP", 1, False, "long", "n/a"),
])
def test_agreement(action, trend, flip, pos, expected):
    assert ts.agreement(action, trend, flip, pos) == expected
