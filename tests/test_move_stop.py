import dataclasses

import pytest

from opus_trader.risk import Decision, PositionView, evaluate


@pytest.fixture
def long_pos(state):
    return dataclasses.replace(state, position=PositionView(11, "long", 0.2, 1.15800, 1.15600, 1.16400))


@pytest.fixture
def short_pos(state):
    return dataclasses.replace(state, position=PositionView(12, "short", 0.2, 1.16200, 1.16400, 1.15600))


def test_long_stop_can_tighten(spec, market, cfg, long_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.15800), spec, market, long_pos, cfg)
    assert v.verdict == "APPROVED", v.reason
    assert v.plan.kind == "MOVE_STOP" and v.plan.sl == 1.15800 and v.plan.tp == 1.16400 and v.plan.ticket == 11
    assert v.calc["locked_pips"] == 0.0


def test_long_stop_cannot_loosen(spec, market, cfg, long_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.15500), spec, market, long_pos, cfg)
    assert v.verdict == "REJECTED" and v.reason.startswith("Stops only tighten")


def test_long_stop_cannot_cross_price(spec, market, cfg, long_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.16050), spec, market, long_pos, cfg)
    assert v.verdict == "REJECTED" and "wrong side" in v.reason


def test_short_stop_can_tighten(spec, market, cfg, short_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.16100), spec, market, short_pos, cfg)
    assert v.verdict == "APPROVED", v.reason


def test_short_stop_cannot_loosen(spec, market, cfg, short_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.16500), spec, market, short_pos, cfg)
    assert v.verdict == "REJECTED" and v.reason.startswith("Stops only tighten")


def test_short_stop_cannot_cross_price(spec, market, cfg, short_pos):
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.16004), spec, market, short_pos, cfg)  # below the ask 1.16008
    assert v.verdict == "REJECTED" and "wrong side" in v.reason


def test_move_stop_without_position(spec, market, state, cfg):
    assert evaluate(Decision("MOVE_STOP", stop_loss=1.159), spec, market, state, cfg).reason == "No open position to move the stop on"
