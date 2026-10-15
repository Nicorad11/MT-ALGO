import pytest

from opus_trader.fake_mt5 import FakeMT5
from opus_trader.mt5_gateway import MT5Gateway, NotConnected, SafetyError
from opus_trader.risk import Plan

from .conftest import MAGIC


def open_plan():
    return Plan("OPEN_MARKET", "long", 0.1, 1.16008, 1.15808, 1.16408)


def test_real_account_refused_at_startup(fake):
    fake.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL
    gw = MT5Gateway(fake, "EURUSD", MAGIC)
    with pytest.raises(SafetyError) as err:
        gw.connect()
    assert err.value.code == "not_demo" and err.value.fatal
    assert "real-money" in err.value.message
    assert not fake.initialized  # mt5.shutdown() was called


def test_contest_account_refused(fake):
    fake.trade_mode = fake.ACCOUNT_TRADE_MODE_CONTEST
    with pytest.raises(SafetyError):
        MT5Gateway(fake, "EURUSD", MAGIC).connect()


def test_netting_account_refused(fake):
    fake.margin_mode = fake.ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    with pytest.raises(SafetyError) as err:
        MT5Gateway(fake, "EURUSD", MAGIC).connect()
    assert err.value.code == "not_hedging"


def test_terminal_not_running(fake):
    fake.init_ok = False
    with pytest.raises(NotConnected):
        MT5Gateway(fake, "EURUSD", MAGIC).connect()


def test_rechecked_before_every_order(live, fake):
    assert live.open(open_plan(), 1).ok
    fake.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL  # someone logs the terminal into a real account
    sent_before = len(fake.sent)
    with pytest.raises(SafetyError) as err:
        live.open(open_plan(), 2)
    assert err.value.code == "not_demo"
    assert len(fake.sent) == sent_before  # nothing reached order_send


def test_account_switch_blocks_orders(live, fake):
    fake.login = 99999999
    with pytest.raises(SafetyError) as err:
        live.open(open_plan(), 1)
    assert err.value.code == "account_changed" and err.value.fatal
    assert fake.sent == []


def test_algo_trading_off_blocks_orders(live, fake):
    fake.terminal_trade_allowed = False
    with pytest.raises(SafetyError) as err:
        live.open(open_plan(), 1)
    assert err.value.code == "algo_off"
    assert fake.sent == []


def test_engine_gate_blocks_orders(live, fake, gate_state):
    gate_state["reason"] = "Halted by the kill switch"
    with pytest.raises(SafetyError) as err:
        live.open(open_plan(), 1)
    assert err.value.message == "Halted by the kill switch"
    assert fake.sent == []


def test_gateway_refuses_requests_without_magic(gw, fake):
    req = {"action": fake.TRADE_ACTION_DEAL, "symbol": "EURUSD", "volume": 0.1, "type": fake.ORDER_TYPE_BUY, "price": 1.16008, "magic": 0}
    with pytest.raises(SafetyError) as err:
        gw.order_send(req)
    assert err.value.code == "magic"
    assert fake.sent == []


def test_blocked_attempt_is_logged(live, fake, store, gate_state):
    gate_state["reason"] = "STOP file present"
    with pytest.raises(SafetyError):
        live.open(open_plan(), 5)
    row = store.one("SELECT * FROM orders WHERE decision_id=5")
    assert row["ok"] == 0 and row["retcode_name"] == "BLOCKED" and row["comment"] == "STOP file present"
