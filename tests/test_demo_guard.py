import pytest

from opus_trader.fake_mt5 import FakeMT5
from opus_trader.mt5_gateway import NOT_LOGGED_IN, MT5Gateway, NotConnected, SafetyError
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
    with pytest.raises(NotConnected) as err:
        MT5Gateway(fake, "EURUSD", MAGIC).connect()
    assert "-10003" in str(err.value)  # what the real package reports when no terminal is found
    assert err.value.hint.startswith("MetaTrader 5 isn't running or wasn't found")


def test_terminal_open_but_not_logged_in(fake):
    fake.login = 0  # an empty account record, before anyone logs in
    with pytest.raises(NotConnected) as err:
        MT5Gateway(fake, "EURUSD", MAGIC).connect()
    assert err.value.hint == NOT_LOGGED_IN


def test_refused_request_is_logged_not_a_disconnect(live, fake, store):
    def refuse(request):
        fake._last_error = (fake.RES_E_INVALID_PARAMS, 'Invalid "comment" argument')
        return None

    fake.order_send = refuse
    with pytest.raises(SafetyError) as err:
        live.open(open_plan(), 7)
    assert err.value.code == "request" and not err.value.fatal
    row = store.one("SELECT * FROM orders WHERE decision_id=7")
    assert row["ok"] == 0 and row["retcode_name"] == "REFUSED" and 'Invalid "comment" argument' in row["comment"]


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


def test_gate_still_allows_closing_while_halted(live, fake, gate_state):
    ticket = live.open(open_plan(), 1).ticket
    gate_state["reason"] = "Halted by the kill switch"
    assert live.close_position(ticket, "kill_switch").ok
    assert ticket not in fake.positions
