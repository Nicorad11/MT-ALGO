import dataclasses

from opus_trader.risk import Decision, PositionView, evaluate


def test_stop_inside_stops_level_rejected(spec, market, state, cfg):
    s = dataclasses.replace(spec, stops_level=150)  # 15 pips
    # Stop 10 pips under the bid is fine for ATR (1.0x) but inside the broker's stops level.
    v = evaluate(Decision("OPEN_LONG", "MARKET", None, 1.15900, 1.16300), s, market, state, cfg)
    assert v.verdict == "REJECTED" and v.reason == "Stop-loss is closer than the broker's stops level (15.0 pips)"


def test_short_stop_checked_against_the_ask(spec, market, state, cfg):
    s = dataclasses.replace(spec, stops_level=150)
    # 12.8 pips from the bid entry (ATR and R:R pass) but only 12 pips above the ask, where shorts are closed.
    v = evaluate(Decision("OPEN_SHORT", "MARKET", None, 1.16128, 1.15800), s, market, state, cfg)
    assert v.verdict == "REJECTED" and v.reason == "Stop-loss is closer than the broker's stops level (15.0 pips)"


def test_limit_too_close_to_market(spec, market, state, cfg):
    s = dataclasses.replace(spec, stops_level=50)  # 5 pips
    v = evaluate(Decision("OPEN_LONG", "LIMIT", 1.15978, 1.15778, 1.16300), s, market, state, cfg)  # 3 pips below the ask
    assert v.verdict == "REJECTED" and "stops level is 5.0" in v.reason


def test_freeze_level_blocks_move_stop(spec, market, state, cfg):
    s = dataclasses.replace(spec, freeze_level=30)  # 3 pips
    pos = PositionView(1, "long", 0.1, 1.15800, 1.15980, 1.16500)  # stop 2 pips under the bid
    st = dataclasses.replace(state, position=pos)
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.15985), s, market, st, cfg)
    assert v.verdict == "REJECTED" and "freeze level" in v.reason


def test_move_stop_respects_stops_level(spec, market, state, cfg):
    s = dataclasses.replace(spec, stops_level=100)  # 10 pips
    st = dataclasses.replace(state, position=PositionView(1, "long", 0.1, 1.15800, 1.15700, 1.16500))
    v = evaluate(Decision("MOVE_STOP", stop_loss=1.15950), s, market, st, cfg)  # 5 pips under the bid
    assert v.verdict == "REJECTED" and "stops level is 10.0" in v.reason
