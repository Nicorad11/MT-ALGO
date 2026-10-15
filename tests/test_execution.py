from datetime import timedelta

import numpy as np
import pytest

from opus_trader.fake_mt5 import SymbolSpec as FakeSpec
from opus_trader.risk import Plan


def market_long():
    return Plan("OPEN_MARKET", "long", 0.25, 1.16008, 1.15808, 1.16408, risk_eur=50.0)


def test_market_entry_has_sl_tp_in_same_request(live, fake):
    res = live.open(market_long(), 1)
    assert res.ok and res.retcode == fake.TRADE_RETCODE_DONE
    req = fake.sent[0]
    assert req["action"] == fake.TRADE_ACTION_DEAL
    assert req["sl"] == 1.15808 and req["tp"] == 1.16408
    assert req["deviation"] == 10 and req["price"] == 1.16008


def test_filling_mode_order_and_fallback(live, fake):
    assert live.gw.filling_candidates() == [fake.ORDER_FILLING_IOC, fake.ORDER_FILLING_FOK, fake.ORDER_FILLING_RETURN]
    fake.forced_retcodes = [fake.TRADE_RETCODE_INVALID_FILL]  # broker rejects IOC
    res = live.open(market_long(), 1)
    assert res.ok
    assert [r["type_filling"] for r in fake.sent] == [fake.ORDER_FILLING_IOC, fake.ORDER_FILLING_FOK]


def test_fok_only_symbol(gw, fake):
    fake.spec.filling_mode = 1
    assert gw.filling_candidates() == [fake.ORDER_FILLING_FOK, fake.ORDER_FILLING_RETURN]


def test_non_done_retcode_is_a_failure_and_logged(live, fake, store):
    fake.forced_retcodes = [fake.TRADE_RETCODE_REQUOTE]
    res = live.open(market_long(), 3)
    assert not res.ok and res.retcode_name == "REQUOTE"
    row = store.one("SELECT * FROM orders WHERE decision_id=3")
    assert row["ok"] == 0 and row["retcode"] == fake.TRADE_RETCODE_REQUOTE and row["comment"] == "Forced by test"
    assert len(fake.sent) == 1  # no retry on anything but an invalid filling mode


def test_pending_order_placed_with_server_time_expiry(live, fake, clock):
    plan = Plan("OPEN_LIMIT", "long", 0.2, 1.15900, 1.15700, 1.16300, expiration=clock.now() + timedelta(hours=1))
    res = live.open(plan, 4)
    assert res.ok and res.retcode == fake.TRADE_RETCODE_PLACED
    req = fake.sent[0]
    assert req["action"] == fake.TRADE_ACTION_PENDING and req["type"] == fake.ORDER_TYPE_BUY_LIMIT
    assert req["type_time"] == fake.ORDER_TIME_SPECIFIED
    assert req["expiration"] == int((clock.now() + timedelta(hours=1)).timestamp()) + 3 * 3600  # EDT server = UTC+3


def test_close_is_opposite_deal_on_ticket(live, fake):
    ticket = live.open(market_long(), 1).ticket
    res = live.close_position(ticket, "model")
    assert res.ok
    req = fake.sent[-1]
    assert req["position"] == ticket and req["type"] == fake.ORDER_TYPE_SELL and req["volume"] == 0.25
    assert ticket not in fake.positions


def test_modify_uses_sltp(live, fake):
    ticket = live.open(market_long(), 1).ticket
    assert live.modify_stop(ticket, 1.15900, 1.16408).ok
    assert fake.sent[-1]["action"] == fake.TRADE_ACTION_SLTP
    assert fake.positions[ticket].sl == 1.15900


# ---------------------------------------------------------------- paper

def test_paper_never_sends_orders(paper, fake, store):
    res = paper.open(market_long(), 1, {"setup": "Breakout", "confidence": 4})
    assert res.ok and res.price == 1.16008
    assert fake.sent == [] and len(fake.checked) == 1
    t = store.one("SELECT * FROM trades WHERE mode='dry'")
    assert t["status"] == "open" and t["setup"] == "Breakout" and t["risk_eur"] == pytest.approx(paper.risk_eur(0.25, 1.16008, 1.15808))


def test_paper_order_check_failure_blocks(paper, fake):
    fake.terminal_trade_allowed = False
    res = paper.open(market_long(), 1)
    assert not res.ok and "order_check failed" in res.message


def test_paper_stop_loss_hit_books_minus_one_r(paper, store, clock):
    paper.open(market_long(), 1)
    events = paper.on_price(1.15805, 1.15813, clock.now() + timedelta(minutes=5))
    assert events == ["sl"]
    t = store.one("SELECT * FROM trades WHERE mode='dry'")
    assert t["status"] == "closed" and t["exit_reason"] == "sl"
    assert t["r_multiple"] == pytest.approx(-1.0, abs=0.03)
    assert paper.balance() == pytest.approx(10_000 + t["pnl_eur"])


def test_paper_take_profit_hit(paper, store, clock):
    paper.open(market_long(), 1)
    assert paper.on_price(1.16410, 1.16418, clock.now()) == ["tp"]
    t = store.one("SELECT * FROM trades WHERE mode='dry'")
    assert t["close_price"] == 1.16408 and t["r_multiple"] == pytest.approx(2.0, abs=0.05)


def test_paper_backfill_assumes_stop_first(paper, store, clock):
    paper.open(market_long(), 1)
    t0 = int(clock.now().timestamp())
    # One M1 bar touching both the stop and the target.
    paper.backfill(np.array([t0 + 60]), np.array([1.16000]), np.array([1.16500]), np.array([1.15700]), np.array([1.16100]), np.array([0.00008]))
    t = store.one("SELECT * FROM trades WHERE mode='dry'")
    assert t["exit_reason"] == "sl"


def test_paper_limit_fill_and_expiry(paper, store, clock):
    plan = Plan("OPEN_LIMIT", "long", 0.2, 1.15900, 1.15700, 1.16300, expiration=clock.now() + timedelta(hours=1))
    assert paper.open(plan, 9).ok
    assert paper.on_price(1.15950, 1.15958, clock.now()) == []
    assert paper.on_price(1.15890, 1.15898, clock.now() + timedelta(minutes=10)) == ["filled"]
    t = store.one("SELECT * FROM trades WHERE mode='dry'")
    assert t["open_price"] == 1.15900 and t["decision_id"] == 9
    plan2 = Plan("OPEN_LIMIT", "short", 0.2, 1.16500, 1.16700, 1.16000, expiration=clock.now() + timedelta(minutes=30))
    paper.close_all("model")
    assert paper.open(plan2, 10).ok
    assert paper.on_price(1.16000, 1.16008, clock.now() + timedelta(minutes=31)) == ["expired"]
    assert store.one("SELECT status FROM paper_orders WHERE decision_id=10")["status"] == "expired"


def test_paper_check_falls_back_on_filling_mode(paper, fake):
    fake.forced_retcodes = []
    calls = []
    real_check = fake.order_check

    def check(req):
        calls.append(req["type_filling"])
        res = real_check(req)
        return res._replace(retcode=fake.TRADE_RETCODE_INVALID_FILL, comment="Unsupported filling mode") if len(calls) == 1 else res

    fake.order_check = check
    res = paper.open(market_long(), 1)
    assert res.ok
    assert calls == [fake.ORDER_FILLING_IOC, fake.ORDER_FILLING_FOK]
    assert fake.sent == []


def test_paper_limit_uses_gtc_when_symbol_cannot_expire(paper, fake, clock):
    fake.spec.expiration_mode = 1  # GTC only
    plan = Plan("OPEN_LIMIT", "long", 0.2, 1.15900, 1.15700, 1.16300, expiration=clock.now() + timedelta(hours=1))
    assert paper.open(plan, 9).ok
    assert fake.checked[-1]["type_time"] == fake.ORDER_TIME_GTC
