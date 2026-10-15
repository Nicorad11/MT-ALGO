from datetime import datetime, timedelta, timezone

import pytest

from opus_trader.clock import ServerTime, SimClock
from opus_trader.config import Config
from opus_trader.execution import LiveExecutor, PaperExecutor
from opus_trader.fake_mt5 import FakeMT5, flat_market
from opus_trader.mt5_gateway import MT5Gateway
from opus_trader.risk import Market, RiskState, SymbolSpec
from opus_trader.store import Store

UTC = timezone.utc
# A Wednesday in London hours (EDT in New York, so the server runs at UTC+3).
T0 = datetime(2026, 10, 14, 10, 0, 5, tzinfo=UTC)
MAGIC = 551155


@pytest.fixture
def clock():
    return SimClock(T0)


@pytest.fixture
def fake(clock):
    f = FakeMT5(clock, flat_market(T0 - timedelta(days=2), days=5, price=1.16000))
    f.price_override = (1.16000, 1.16008)  # 0.8 pip spread
    return f


@pytest.fixture
def gw(fake):
    g = MT5Gateway(fake, "EURUSD", MAGIC)
    g.connect()
    return g


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def spec(gw):
    return SymbolSpec.from_info(gw.symbol_info())


@pytest.fixture
def market(clock):
    # ATR(14) of 10 pips.
    return Market(bid=1.16000, ask=1.16008, atr=0.0010, now=clock.now())


@pytest.fixture
def state():
    return RiskState(equity=10_000.0)


@pytest.fixture
def gate_state():
    return {"reason": None}


@pytest.fixture
def live(gw, store, clock, gate_state):
    return LiveExecutor(gw, store, MAGIC, lambda: 10, ServerTime(), gate=lambda purpose: gate_state["reason"] if purpose in ("open", "pending", "modify") else None)


@pytest.fixture
def paper(gw, store, clock):
    ex = PaperExecutor(gw, store, MAGIC, lambda: 10, ServerTime(), lambda: SymbolSpec.from_info(gw.symbol_info()), clock.now)
    ex.ensure_balance(10_000.0)
    return ex
