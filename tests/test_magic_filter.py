from opus_trader.risk import Plan

from .conftest import MAGIC


def test_gateway_only_sees_bot_positions_and_orders(gw, fake, live):
    manual_pos = fake.add_manual_position(fake.POSITION_TYPE_BUY, 1.0, 1.15000)
    other_ea = fake.add_manual_position(fake.POSITION_TYPE_SELL, 0.5, 1.17000, magic=123)
    manual_order = fake.add_manual_order(fake.ORDER_TYPE_BUY_LIMIT, 0.3, 1.14000)
    assert live.open(Plan("OPEN_MARKET", "long", 0.1, 1.16008, 1.15808, 1.16408), 1).ok
    bot = gw.bot_positions()
    assert [p.magic for p in bot] == [MAGIC]
    assert gw.bot_orders() == []
    assert {manual_pos, other_ea} <= set(fake.positions)
    assert manual_order in fake.orders


def test_close_all_leaves_manual_trades_alone(gw, fake, live):
    manual_pos = fake.add_manual_position(fake.POSITION_TYPE_BUY, 1.0, 1.15000)
    manual_order = fake.add_manual_order(fake.ORDER_TYPE_SELL_LIMIT, 0.3, 1.18000)
    assert live.open(Plan("OPEN_MARKET", "short", 0.1, 1.16000, 1.16200, 1.15700), 1).ok
    assert live.open(Plan("OPEN_LIMIT", "long", 0.1, 1.15900, 1.15700, 1.16300), 2).ok
    results = live.close_all("kill_switch")
    assert len(results) == 2 and all(r.ok for r in results)
    assert set(fake.positions) == {manual_pos}
    assert set(fake.orders) == {manual_order}
    # Every request that went out carried the bot's magic number.
    assert all(r.get("magic") == MAGIC for r in fake.sent)


def test_close_position_refuses_foreign_ticket(fake, live):
    manual = fake.add_manual_position(fake.POSITION_TYPE_BUY, 1.0, 1.15000)
    res = live.close_position(manual, "model")
    assert not res.ok
    assert manual in fake.positions
    assert fake.sent == []


def test_modify_and_cancel_refuse_foreign_tickets(fake, live):
    manual = fake.add_manual_position(fake.POSITION_TYPE_BUY, 1.0, 1.15000, sl=1.14)
    order = fake.add_manual_order(fake.ORDER_TYPE_BUY_LIMIT, 0.3, 1.14000)
    assert not live.modify_stop(manual, 1.145, 0.0).ok
    assert not live.cancel_pending(order, "model").ok
    assert fake.sent == []
