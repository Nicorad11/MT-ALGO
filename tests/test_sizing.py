import dataclasses

import pytest

from opus_trader.risk import Decision, evaluate, size_lots


def test_lots_from_tick_value_and_tick_size(spec):
    # 20-pip stop = 200 ticks; one tick per lot is worth tick_value_loss EUR.
    lots, loss_per_lot = size_lots(50.0, 0.0020, spec)
    assert loss_per_lot == pytest.approx(200 * spec.tick_value_loss)
    assert lots == pytest.approx(0.29)  # 50 / 172.4 = 0.290 -> rounded down to the 0.01 step
    assert lots * loss_per_lot <= 50.0


def test_rounds_down_to_volume_step(spec):
    coarse = dataclasses.replace(spec, volume_step=0.1, volume_min=0.1)
    lots, _ = size_lots(50.0, 0.0020, coarse)
    assert lots == pytest.approx(0.2)


def test_clamps_to_volume_max(spec):
    capped = dataclasses.replace(spec, volume_max=0.1)
    lots, _ = size_lots(5_000.0, 0.0020, capped)
    assert lots == pytest.approx(0.1)


def test_below_volume_min_is_rejected_not_clamped_up(spec, market, state, cfg):
    small = dataclasses.replace(state, equity=100.0)  # 0.50 EUR risk -> 0.003 lots
    v = evaluate(Decision("OPEN_LONG", "MARKET", None, 1.15808, 1.16408), spec, market, small, cfg)
    assert v.verdict == "REJECTED"
    assert "minimum lot" in v.reason and "would risk" in v.reason


def test_uses_tick_value_loss(spec):
    worse = dataclasses.replace(spec, tick_value_loss=spec.tick_value_loss * 2)
    lots, _ = size_lots(50.0, 0.0020, worse)
    assert lots == pytest.approx(0.14)


def test_approved_entry_carries_size_and_risk(spec, market, state, cfg):
    v = evaluate(Decision("OPEN_LONG", "MARKET", None, 1.15808, 1.16408), spec, market, state, cfg)
    assert v.verdict == "APPROVED", v.reason
    assert v.plan.kind == "OPEN_MARKET"
    assert v.plan.volume == pytest.approx(0.29)  # 20-pip stop from the ask 1.16008: 50 / 172.4 per lot
    assert v.plan.risk_pct <= cfg.risk.risk_per_trade_pct
    assert v.plan.sl == 1.15808 and v.plan.tp == 1.16408
