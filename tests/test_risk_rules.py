import dataclasses

import pytest

from opus_trader.config import apply_changes
from opus_trader.risk import Decision, PendingView, PositionView, evaluate

# Market: bid 1.16000 / ask 1.16008, ATR 10 pips.


def long_(sl=1.15808, tp=1.16408, order_type="MARKET", entry=None):
    return Decision("OPEN_LONG", order_type, entry, sl, tp, 3, "test", "r", "i")


def short(sl=1.16200, tp=1.15700, order_type="MARKET", entry=None):
    return Decision("OPEN_SHORT", order_type, entry, sl, tp, 3, "test", "r", "i")


def test_hold_is_not_an_order(spec, market, state, cfg):
    assert evaluate(Decision("HOLD"), spec, market, state, cfg).verdict == "HOLD"


@pytest.mark.parametrize("decision,needle", [
    (long_(sl=None), "No stop-loss"),
    (long_(tp=None), "No take-profit"),
])
def test_entry_requires_stop_and_target(spec, market, state, cfg, decision, needle):
    v = evaluate(decision, spec, market, state, cfg)
    assert v.verdict == "REJECTED" and needle in v.reason


def test_stop_and_target_on_correct_sides(spec, market, state, cfg):
    assert "below and take-profit above" in evaluate(long_(sl=1.16100, tp=1.16500), spec, market, state, cfg).reason
    assert "above and take-profit below" in evaluate(short(sl=1.15900, tp=1.15700), spec, market, state, cfg).reason


def test_stop_too_tight_in_atr(spec, market, state, cfg):
    v = evaluate(long_(sl=1.15978, tp=1.16200), spec, market, state, cfg)  # 3 pips = 0.3x ATR
    assert v.verdict == "REJECTED" and v.reason == "Stop is 0.3× ATR; minimum is 0.5×"


def test_stop_too_wide_in_atr(spec, market, state, cfg):
    v = evaluate(long_(sl=1.15608, tp=1.17000), spec, market, state, cfg)  # 40 pips = 4x ATR
    assert v.verdict == "REJECTED" and "maximum is 3.0×" in v.reason


def test_reward_risk_minimum(spec, market, state, cfg):
    v = evaluate(long_(sl=1.15808, tp=1.16258), spec, market, state, cfg)  # 25/20 = 1.25
    assert v.verdict == "REJECTED" and "Reward:risk is 1.25; minimum is 1.50" == v.reason


def test_rr_measured_from_executable_price(spec, market, state, cfg):
    # Short entries fill at the bid: stop 20 pips above 1.16000, target 30 pips below -> 1.5 exactly.
    v = evaluate(short(sl=1.16200, tp=1.15700), spec, market, state, cfg)
    assert v.verdict == "APPROVED", v.reason
    assert v.calc["rr"] == pytest.approx(1.5)


def test_spread_too_wide(spec, market, state, cfg):
    wide = dataclasses.replace(market, ask=1.16020)  # 2.0 pips
    v = evaluate(long_(), spec, wide, state, cfg)
    assert v.verdict == "REJECTED" and v.reason == "Spread is 2.0 pips; maximum is 1.5"


def test_max_one_position(spec, market, state, cfg):
    st = dataclasses.replace(state, position=PositionView(1, "long", 0.1, 1.159, 1.157, 1.163))
    assert "already open" in evaluate(long_(), spec, market, st, cfg).reason
    st = dataclasses.replace(state, pending=PendingView(2, "short", 0.1, 1.161, 1.163, 1.158))
    assert "already open" in evaluate(short(), spec, market, st, cfg).reason


def test_max_trades_per_day(spec, market, state, cfg):
    st = dataclasses.replace(state, entries_today=6)
    assert evaluate(long_(), spec, market, st, cfg).reason == "Max 6 entries for today reached"


def test_cooldown_after_loss(spec, market, state, cfg):
    st = dataclasses.replace(state, cooldown_bars_left=2)
    assert evaluate(long_(), spec, market, st, cfg).reason == "Cooldown after a loss: 2 bars left"


def test_entry_block_from_schedule(spec, market, state, cfg):
    st = dataclasses.replace(state, entry_block="weekend window")
    assert evaluate(long_(), spec, market, st, cfg).reason == "No new entries: weekend window"


def test_limit_needs_entry_and_correct_side(spec, market, state, cfg):
    assert "needs an entry price" in evaluate(long_(order_type="LIMIT"), spec, market, state, cfg).reason
    assert "must be below the ask" in evaluate(long_(order_type="LIMIT", entry=1.16100), spec, market, state, cfg).reason
    assert "must be above the bid" in evaluate(short(order_type="LIMIT", entry=1.15900), spec, market, state, cfg).reason


def test_limit_order_plan_has_expiry(spec, market, state, cfg):
    v = evaluate(long_(order_type="LIMIT", entry=1.15950, sl=1.15750, tp=1.16300), spec, market, state, cfg)
    assert v.verdict == "APPROVED", v.reason
    assert v.plan.kind == "OPEN_LIMIT" and v.plan.price == 1.15950
    assert (v.plan.expiration - market.now).total_seconds() == 4 * 15 * 60


def test_close_targets_position_then_pending(spec, market, state, cfg):
    assert evaluate(Decision("CLOSE"), spec, market, state, cfg).verdict == "REJECTED"
    st = dataclasses.replace(state, pending=PendingView(7, "long", 0.1, 1.159, 1.157, 1.163))
    v = evaluate(Decision("CLOSE"), spec, market, st, cfg)
    assert v.plan.kind == "CANCEL_PENDING" and v.plan.ticket == 7
    st = dataclasses.replace(state, position=PositionView(8, "short", 0.1, 1.161, 1.163, 1.158))
    v = evaluate(Decision("CLOSE"), spec, market, st, cfg)
    assert v.plan.kind == "CLOSE_POSITION" and v.plan.ticket == 8


def test_signal_filter_mode(spec, market, state, cfg):
    cfg, _ = apply_changes(cfg, {"signal.mode": "filter"})
    assert "no Trend Signals flip" in evaluate(long_(), spec, market, state, cfg).reason
    st = dataclasses.replace(state, recent_flip_direction="short")
    assert "only short entries" in evaluate(long_(), spec, market, st, cfg).reason
    assert evaluate(short(), spec, market, st, cfg).verdict == "APPROVED"
    # HOLD, CLOSE and MOVE_STOP are never filtered.
    assert evaluate(Decision("HOLD"), spec, market, state, cfg).verdict == "HOLD"


def test_input_mode_ignores_signal(spec, market, state, cfg):
    st = dataclasses.replace(state, recent_flip_direction="short")
    assert evaluate(long_(), spec, market, st, cfg).verdict == "APPROVED"


@pytest.mark.parametrize("digits,pip", [(2, 0.01), (3, 0.01), (4, 0.0001), (5, 0.0001), (6, 0.0001)])
def test_pip_is_the_same_however_many_decimals_the_broker_quotes(digits, pip):
    from opus_trader.risk import SymbolSpec
    from opus_trader.snapshot import pip_size

    spec = SymbolSpec(digits, 10.0 ** -digits, 10.0 ** -digits, 1.0, 1.0, 0.01, 100.0, 0.01, 0, 0)
    assert spec.pip == pip and pip_size(digits, 10.0 ** -digits) == pip
    assert spec.pips(0.00024 if pip == 0.0001 else 0.024) == pytest.approx(2.4)  # a 2.4-pip spread
