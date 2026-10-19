"""An in-memory stand-in for the `MetaTrader5` package.

Same function names, constants, namedtuple fields and quirks as the real
module (times are broker server time, history ranges are server-time epochs,
retcodes as documented), so the gateway code paths are identical. Used by the
unit tests and by `--sim` to preview the app without a terminal.

Broker-side behaviour (SL/TP hits, pending fills, expirations) is processed
lazily whenever the module is called, up to the clock's current time.
"""

from __future__ import annotations

import math
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

import fnmatch

from .clock import Clock, OffsetModel, ServerTime, fx_market_open, ny7_offset_s

UTC = timezone.utc

# ---------------------------------------------------------------- constants (subset of MetaTrader5/__init__.py)
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 1 | 0x4000
TIMEFRAME_H4 = 4 | 0x4000
TIMEFRAME_D1 = 24 | 0x4000
TF_MINUTES = {TIMEFRAME_M1: 1, TIMEFRAME_M5: 5, TIMEFRAME_M15: 15, TIMEFRAME_M30: 30, TIMEFRAME_H1: 60, TIMEFRAME_H4: 240, TIMEFRAME_D1: 1440}

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2
ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1
ORDER_TIME_SPECIFIED = 2
ORDER_TIME_SPECIFIED_DAY = 3
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_REASON_CLIENT = 0
DEAL_REASON_EXPERT = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8
SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_ERROR = 10011
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_INVALID_EXPIRATION = 10022
TRADE_RETCODE_NO_CHANGES = 10025
TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
TRADE_RETCODE_FROZEN = 10029
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_CONNECTION = 10031
TRADE_RETCODE_POSITION_CLOSED = 10036
RES_S_OK = 1
RES_E_INVALID_PARAMS = -2
RES_E_INTERNAL_FAIL_INIT = -10003
RES_E_INTERNAL_FAIL_CONNECT = -10004

# ---------------------------------------------------------------- record types (field names as in the real package)
AccountInfo = namedtuple("AccountInfo", "login trade_mode leverage limit_orders margin_so_mode trade_allowed trade_expert margin_mode currency_digits fifo_close balance credit profit equity margin margin_free margin_level margin_so_call margin_so_so margin_initial margin_maintenance assets liabilities commission_blocked name server currency company")
TerminalInfo = namedtuple("TerminalInfo", "community_account community_connection connected dlls_allowed trade_allowed tradeapi_disabled email_enabled ftp_enabled notifications_enabled mqid build maxbars codepage ping_last community_balance retransmission company name language path data_path commondata_path")
SymbolInfo = namedtuple("SymbolInfo", "name visible select digits spread point trade_tick_value trade_tick_value_profit trade_tick_value_loss trade_tick_size trade_contract_size volume_min volume_max volume_step trade_stops_level trade_freeze_level filling_mode expiration_mode trade_mode bid ask time currency_base currency_profit currency_margin")
Tick = namedtuple("Tick", "time bid ask last volume time_msc flags volume_real")
TradePosition = namedtuple("TradePosition", "ticket time time_msc time_update time_update_msc type magic identifier reason volume price_open sl tp price_current swap profit symbol comment external_id")
TradeOrder = namedtuple("TradeOrder", "ticket time_setup time_setup_msc time_done time_done_msc time_expiration type type_time type_filling state magic position_id position_by_id reason volume_initial volume_current price_open sl tp price_current price_stoplimit symbol comment external_id")
TradeDeal = namedtuple("TradeDeal", "ticket order time time_msc type entry magic position_id reason volume price commission swap profit fee symbol comment external_id")
OrderSendResult = namedtuple("OrderSendResult", "retcode deal order volume price bid ask comment request_id retcode_external request")
OrderCheckResult = namedtuple("OrderCheckResult", "retcode balance equity profit margin margin_free margin_level comment request")
TradeRequest = namedtuple("TradeRequest", "action magic order symbol volume price stoplimit sl tp deviation type type_filling type_time expiration comment position position_by")

RATE_DTYPE = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])

SUBSTEPS = 4  # price samples per minute; ticks move every 15 s


# ---------------------------------------------------------------- markets

market_open = fx_market_open


class MinuteMarket:
    """Holds a minute-resolution bid path: per-minute sub-step samples plus spread.

    `times` are UTC epoch seconds of each open minute (sorted), `sub` is a
    (n, SUBSTEPS+1) array of bid samples (column 0 = open), `spread` in points.
    """

    def __init__(self, times: np.ndarray, sub: np.ndarray, spread: np.ndarray, volume: np.ndarray):
        self.times = times.astype(np.int64)
        self.sub = sub
        self.spread = spread.astype(np.int32)
        self.volume = volume.astype(np.uint64)
        hours = self.times // 3600
        uniq, inv = np.unique(hours, return_inverse=True)
        offs = np.array([ny7_offset_s(datetime.fromtimestamp(int(h) * 3600, UTC)) for h in uniq], dtype=np.int64)
        self.server_times = self.times + offs[inv]

    def index_at(self, epoch: float) -> int:
        """Index of the minute containing epoch, or -1 if the market is closed then."""
        i = int(np.searchsorted(self.times, epoch, side="right")) - 1
        if i < 0 or epoch - self.times[i] >= 60:
            return -1
        return i

    def last_index_before(self, epoch: float) -> int:
        return int(np.searchsorted(self.times, epoch, side="right")) - 1

    def bid_at(self, epoch: float) -> tuple[float, int, int] | None:
        """(bid, spread_points, tick_epoch) at time `epoch`; last known price if closed."""
        i = self.last_index_before(epoch)
        if i < 0:
            return None
        if epoch - self.times[i] >= 60:  # market closed: last close
            return float(self.sub[i, -1]), int(self.spread[i]), int(self.times[i] + 59)
        k = min(SUBSTEPS, int((epoch - self.times[i]) // (60 / SUBSTEPS)))
        return float(self.sub[i, k]), int(self.spread[i]), int(self.times[i] + k * (60 // SUBSTEPS))

    def m1(self, i0: int, i1: int, upto_epoch: float | None = None):
        """OHLC arrays for minutes i0..i1-1; the minute containing upto_epoch is partial."""
        sub = self.sub[i0:i1]
        o = sub[:, 0].copy()
        h = sub.max(axis=1)
        lo = sub.min(axis=1)
        c = sub[:, -1].copy()
        if upto_epoch is not None and i1 - i0 > 0:
            last = i1 - 1 - i0
            elapsed = upto_epoch - self.times[i1 - 1]
            if 0 <= elapsed < 60:
                k = min(SUBSTEPS, int(elapsed // (60 / SUBSTEPS)))
                part = sub[last, : k + 1]
                h[last], lo[last], c[last] = part.max(), part.min(), part[-1]
        return o, h, lo, c


def synthetic_market(start_utc: datetime, end_utc: datetime, seed: int = 7, start_price: float = 1.1600) -> MinuteMarket:
    """Deterministic EUR/USD-like minute path with sessions, trends and ranges."""
    rng = np.random.default_rng(seed)
    t0 = int(start_utc.replace(second=0, microsecond=0).timestamp())
    t1 = int(end_utc.timestamp())
    all_minutes = np.arange(t0, t1, 60, dtype=np.int64)
    # Open-market mask (Sunday 17:00 NY to Friday 17:00 NY), computed per hour for speed.
    hours = np.unique(all_minutes // 3600)
    open_hours = {int(h) for h in hours if market_open(datetime.fromtimestamp(int(h) * 3600, UTC))}
    mask = np.fromiter(((m // 3600) in open_hours for m in all_minutes), dtype=bool, count=len(all_minutes))
    times = all_minutes[mask]
    n = len(times)
    utc_hour = (times // 3600) % 24
    # Per-minute volatility (pips) by session.
    vol_by_hour = np.array([0.55, 0.5, 0.5, 0.5, 0.55, 0.6, 0.8, 1.3, 1.5, 1.45, 1.3, 1.2, 1.5, 1.7, 1.6, 1.45, 1.1, 0.95, 0.8, 0.7, 0.6, 0.45, 0.5, 0.55])
    sigma = vol_by_hour[utc_hour] * 0.00014
    # Regime-switching drift: trends of random length and direction, plus ranges.
    drift = np.zeros(n)
    i = 0
    while i < n:
        length = int(rng.exponential(420)) + 60
        kind = rng.random()
        mu = 0.0 if kind < 0.4 else rng.normal(0, 0.06) * 0.0001
        drift[i : i + length] = mu
        i += length
    # Gentle pull towards start_price keeps the path in a realistic band.
    sub = np.empty((n, SUBSTEPS + 1))
    price = start_price
    shocks = rng.standard_normal((n, SUBSTEPS)) / math.sqrt(SUBSTEPS)
    for j in range(n):
        pull = (start_price - price) * 8e-5
        sub[j, 0] = price
        step_sigma = sigma[j]
        for k in range(SUBSTEPS):
            price = price + (drift[j] + pull) / SUBSTEPS + step_sigma * shocks[j, k]
            sub[j, k + 1] = price
    sub = np.round(sub, 5)
    base_spread = np.where((utc_hour >= 7) & (utc_hour < 17), 7, 11)
    rollover = (utc_hour == 21) | (utc_hour == 22)
    spread = base_spread + rng.integers(0, 3, n) + np.where(rollover, 14, 0)
    volume = rng.poisson(np.maximum(5, vol_by_hour[utc_hour] * 60))
    return MinuteMarket(times, sub, spread, volume)


# ---------------------------------------------------------------- the fake module

@dataclass
class SymbolSpec:
    name: str = "EURUSD"
    digits: int = 5
    point: float = 0.00001
    trade_tick_size: float = 0.00001
    trade_contract_size: float = 100000.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    filling_mode: int = 3  # FOK | IOC flags
    expiration_mode: int = 15  # GTC | DAY | SPECIFIED | SPECIFIED_DAY
    allow_return_filling: bool = False


@dataclass
class _Pos:
    ticket: int
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    time: int
    comment: str = ""


@dataclass
class _Order:
    ticket: int
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    time_setup: int
    type_time: int
    expiration: int
    type_filling: int
    comment: str = ""


class FakeMT5:
    """Instance behaves like the `MetaTrader5` module."""

    def __init__(
        self,
        clock: Clock,
        market: MinuteMarket,
        *,
        login: int = 12345678,
        server: str = "OANDATMS-MT5",
        company: str = "OANDA TMS Brokers S.A.",
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
        margin_mode: int = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        currency: str = "EUR",
        balance: float = 10000.0,
        leverage: int = 30,
        spec: SymbolSpec | None = None,
        server_tz: str = "ny7",
    ):
        g = globals()
        for name, value in g.items():
            if name.isupper() and isinstance(value, int):
                setattr(self, name, value)
        self.clock = clock
        self.market = market
        self.spec = spec or SymbolSpec()
        self.login = login
        self.server = server
        self.company = company
        self.trade_mode = trade_mode
        self.margin_mode = margin_mode
        self.currency = currency
        self.balance = balance
        self.leverage = leverage
        self.initialized = False
        self.ipc_ok = True  # False simulates the terminal being closed
        self.connected = True  # broker connection
        self.terminal_trade_allowed = True  # the "Algo Trading" button
        self.init_ok = True
        self.forced_retcodes: list[int] = []  # popped by order_send, for tests
        self.sent: list[dict] = []
        self.checked: list[dict] = []
        self.positions: dict[int, _Pos] = {}
        self.orders: dict[int, _Order] = {}
        self.deals: list[TradeDeal] = []
        self._next_ticket = 50_000_001
        self._processed_until: float = clock.epoch()
        self._last_error = (RES_S_OK, "Success")
        self.price_override: tuple[float, float] | None = None  # (bid, ask) for tests
        # Broker server clock: "ny7" (New York + 7 h) or an IANA zone such as "Europe/Warsaw".
        self.server_model = OffsetModel("ny7") if server_tz == "ny7" else OffsetModel("tz", tz=server_tz)
        if server_tz == "ny7":
            self._server_times = market.server_times
        else:
            hours = market.times // 3600
            uniq, inv = np.unique(hours, return_inverse=True)
            offs = np.array([self.server_model.offset_at(datetime.fromtimestamp(int(h) * 3600, UTC)) for h in uniq], dtype=np.int64)
            self._server_times = market.times + offs[inv]

    # ------------------------------------------------------------ helpers
    def _server_epoch(self, utc_epoch: float) -> int:
        return int(utc_epoch) + self.server_model.offset_at(datetime.fromtimestamp(utc_epoch, UTC))

    def _utc_epoch_from_server(self, server_epoch: float) -> float:
        return ServerTime(self.server_model).to_utc(server_epoch).timestamp()

    def _alive(self) -> bool:
        if not self.initialized or not self.ipc_ok:
            self._last_error = (RES_E_INTERNAL_FAIL_CONNECT, "No IPC connection")
            return False
        return True

    def _quote(self, epoch: float | None = None) -> tuple[float, float, int, int] | None:
        epoch = self.clock.epoch() if epoch is None else epoch
        if self.price_override is not None:
            bid, ask = self.price_override
            return bid, ask, int(round((ask - bid) / self.spec.point)), int(epoch)
        q = self.market.bid_at(epoch)
        if q is None:
            return None
        bid, spread, tick_epoch = q
        ask = round(bid + spread * self.spec.point, self.spec.digits)
        return bid, ask, spread, tick_epoch

    def _ticket(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def _tick_value(self, price: float) -> float:
        # EUR account, USD quote currency: one tick per lot = tick_size * contract / EURUSD.
        return self.spec.trade_tick_size * self.spec.trade_contract_size / price

    def _profit(self, pos_type: int, volume: float, open_price: float, close_price: float) -> float:
        diff = (close_price - open_price) if pos_type == POSITION_TYPE_BUY else (open_price - close_price)
        return round(diff * volume * self.spec.trade_contract_size / close_price, 2)

    def _market_is_open(self, epoch: float | None = None) -> bool:
        epoch = self.clock.epoch() if epoch is None else epoch
        if self.price_override is not None:
            return True
        return self.market.index_at(epoch) >= 0

    # ------------------------------------------------------------ broker-side processing
    def _process(self) -> None:
        """Apply SL/TP hits, pending fills and expirations up to now (substep resolution)."""
        now = self.clock.epoch()
        start = self._processed_until
        self._processed_until = now
        if now <= start or (not self.positions and not self.orders):
            return
        if self.price_override is not None:
            bid, ask = self.price_override
            self._process_price(bid, ask, int(now))
            return
        i0 = max(0, self.market.last_index_before(start))
        i1 = self.market.last_index_before(now)
        if i1 < 0:
            return
        step = 60 / SUBSTEPS
        for i in range(i0, i1 + 1):
            for k in range(SUBSTEPS + 1):
                t = self.market.times[i] + k * step
                if t <= start or t > now:
                    continue
                bid = float(self.market.sub[i, k])
                ask = round(bid + self.market.spread[i] * self.spec.point, self.spec.digits)
                self._process_price(bid, ask, int(t))
                if not self.positions and not self.orders:
                    return

    def _process_price(self, bid: float, ask: float, t_epoch: int) -> None:
        server_t = self._server_epoch(t_epoch)
        for o in list(self.orders.values()):
            if o.type_time == ORDER_TIME_SPECIFIED and o.expiration and server_t >= o.expiration:
                del self.orders[o.ticket]
                continue
            if (o.type == ORDER_TYPE_BUY_LIMIT and ask <= o.price_open) or (o.type == ORDER_TYPE_SELL_LIMIT and bid >= o.price_open):
                del self.orders[o.ticket]
                ptype = POSITION_TYPE_BUY if o.type == ORDER_TYPE_BUY_LIMIT else POSITION_TYPE_SELL
                self.positions[o.ticket] = _Pos(o.ticket, ptype, o.volume, o.price_open, o.sl, o.tp, o.magic, server_t, o.comment)
                self._deal(o.ticket, ptype, DEAL_ENTRY_IN, o.volume, o.price_open, o.magic, o.ticket, DEAL_REASON_EXPERT, 0.0, server_t, o.comment)
        for p in list(self.positions.values()):
            if p.type == POSITION_TYPE_BUY:
                if p.sl and bid <= p.sl:
                    self._close(p, p.sl if bid >= p.sl - 50 * self.spec.point else bid, DEAL_REASON_SL, server_t, "[sl]")
                elif p.tp and bid >= p.tp:
                    self._close(p, p.tp, DEAL_REASON_TP, server_t, "[tp]")
            else:
                if p.sl and ask >= p.sl:
                    self._close(p, p.sl if ask <= p.sl + 50 * self.spec.point else ask, DEAL_REASON_SL, server_t, "[sl]")
                elif p.tp and ask <= p.tp:
                    self._close(p, p.tp, DEAL_REASON_TP, server_t, "[tp]")

    def _deal(self, order, ptype, entry, volume, price, magic, position_id, reason, profit, server_t, comment) -> TradeDeal:
        dtype = DEAL_TYPE_BUY if ptype == POSITION_TYPE_BUY else DEAL_TYPE_SELL
        if entry == DEAL_ENTRY_OUT:
            dtype = DEAL_TYPE_SELL if ptype == POSITION_TYPE_BUY else DEAL_TYPE_BUY
        d = TradeDeal(self._ticket(), order, server_t, server_t * 1000, dtype, entry, magic, position_id, reason, volume, price, 0.0, 0.0, profit, 0.0, self.spec.name, comment, "")
        self.deals.append(d)
        return d

    def _close(self, p: _Pos, price: float, reason: int, server_t: int, comment: str, order: int = 0) -> TradeDeal:
        profit = self._profit(p.type, p.volume, p.price_open, price)
        self.balance = round(self.balance + profit, 2)
        del self.positions[p.ticket]
        return self._deal(order or p.ticket, p.type, DEAL_ENTRY_OUT, p.volume, price, p.magic, p.ticket, reason, profit, server_t, comment)

    # ------------------------------------------------------------ module API
    def initialize(self, path: str | None = None, **kwargs) -> bool:
        if not self.init_ok or not self.ipc_ok:
            self._last_error = (RES_E_INTERNAL_FAIL_INIT, "IPC initialize failed, MetaTrader 5 x64 not found")
            return False
        self.initialized = True
        self._last_error = (RES_S_OK, "Success")
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def last_error(self):
        return self._last_error

    def version(self):
        return (500, 5000, "01 Oct 2026")

    def terminal_info(self):
        if not self._alive():
            return None
        return TerminalInfo(False, False, self.connected, False, self.terminal_trade_allowed, False, False, False, False, 0, 5000, 100000, 0, 25000,
                            0.0, 0.0, self.company, "MetaTrader 5", "English", "C:/Program Files/MT5", "C:/Users/demo/AppData", "C:/Users/demo/AppData/Common")

    def account_info(self):
        if not self._alive():
            return None
        self._process()
        q = self._quote()
        profit = 0.0
        margin = 0.0
        if q:
            bid, ask, _, _ = q
            for p in self.positions.values():
                profit += self._profit(p.type, p.volume, p.price_open, bid if p.type == POSITION_TYPE_BUY else ask)
                margin += p.volume * self.spec.trade_contract_size / self.leverage
        equity = round(self.balance + profit, 2)
        free = round(equity - margin, 2)
        level = round(equity / margin * 100, 2) if margin else 0.0
        return AccountInfo(self.login, self.trade_mode, self.leverage, 200, 0, True, True, self.margin_mode, 2, False,
                           self.balance, 0.0, round(profit, 2), equity, round(margin, 2), free, level, 50.0, 30.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           "Demo User", self.server, self.currency, self.company)

    def symbols_get(self, group: str | None = None):
        """Symbols matching an MT5 group filter ("*EURUSD*", comma lists, "!" exclusions)."""
        if not self._alive():
            return None
        if group:
            pats = [g.strip() for g in group.split(",") if g.strip()]
            inc = [p for p in pats if not p.startswith("!")] or ["*"]
            exc = [p[1:] for p in pats if p.startswith("!")]
            ok = any(fnmatch.fnmatchcase(self.spec.name, p) for p in inc) and not any(fnmatch.fnmatchcase(self.spec.name, p) for p in exc)
            if not ok:
                return ()
        return (self.symbol_info(self.spec.name),)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        if not self._alive():
            return False
        return symbol == self.spec.name

    def symbol_info(self, symbol: str):
        if not self._alive() or symbol != self.spec.name:
            return None
        s = self.spec
        q = self._quote() or (0.0, 0.0, 0, 0)
        tv = self._tick_value(q[0] or 1.16)
        return SymbolInfo(s.name, True, True, s.digits, q[2], s.point, tv, tv, tv, s.trade_tick_size, s.trade_contract_size,
                          s.volume_min, s.volume_max, s.volume_step, s.trade_stops_level, s.trade_freeze_level,
                          s.filling_mode, s.expiration_mode, SYMBOL_TRADE_MODE_FULL, q[0], q[1], self._server_epoch(q[3]) if q[3] else 0, "EUR", "USD", "EUR")

    def symbol_info_tick(self, symbol: str):
        if not self._alive() or symbol != self.spec.name:
            return None
        q = self._quote()
        if q is None:
            return None
        bid, ask, _, tick_epoch = q
        st = self._server_epoch(tick_epoch)
        return Tick(st, bid, ask, 0.0, 0, st * 1000, 6, 0.0)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int):
        if not self._alive() or symbol != self.spec.name:
            return None
        minutes = TF_MINUTES[timeframe]
        now = self.clock.epoch()
        i_last = self.market.last_index_before(now)
        if i_last < 0:
            return np.zeros(0, dtype=RATE_DTYPE)
        # Bucket boundaries are aligned to server time, like a real terminal.
        need = (start_pos + count + 2) * minutes * 2 + 5000
        i0 = max(0, i_last + 1 - need)
        bars = self._aggregate(i0, i_last + 1, minutes, upto=now)
        if start_pos > 0:
            bars = bars[: len(bars) - start_pos] if start_pos < len(bars) else bars[:0]
        return bars[-count:].copy()

    def copy_rates_range(self, symbol: str, timeframe: int, date_from, date_to):
        if not self._alive() or symbol != self.spec.name:
            return None
        minutes = TF_MINUTES[timeframe]
        f = _as_epoch(date_from)
        t = _as_epoch(date_to)
        # Range arguments are compared with server-time epochs (MT5 quirk).
        uf = self._utc_epoch_from_server(f)
        ut = min(self._utc_epoch_from_server(t), self.clock.epoch())
        i0 = max(0, int(np.searchsorted(self.market.times, uf - minutes * 60, side="left")))
        i1 = int(np.searchsorted(self.market.times, ut, side="right"))
        bars = self._aggregate(i0, i1, minutes, upto=self.clock.epoch())
        sel = (bars["time"] >= f) & (bars["time"] <= t)
        return bars[sel].copy()

    def _aggregate(self, i0: int, i1: int, minutes: int, upto: float):
        mk = self.market
        if i1 <= i0:
            return np.zeros(0, dtype=RATE_DTYPE)
        o, h, lo, c = mk.m1(i0, i1, upto_epoch=upto)
        server_times = self._server_times[i0:i1]
        bucket = server_times // (minutes * 60) * (minutes * 60)
        starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
        ends = np.r_[starts[1:], len(bucket)]
        out = np.zeros(len(starts), dtype=RATE_DTYPE)
        out["time"] = bucket[starts]
        out["open"] = o[starts]
        out["high"] = np.maximum.reduceat(h, starts)
        out["low"] = np.minimum.reduceat(lo, starts)
        out["close"] = c[ends - 1]
        out["tick_volume"] = np.add.reduceat(mk.volume[i0:i1], starts)
        out["spread"] = mk.spread[i0:i1][starts]
        return out

    def positions_get(self, symbol: str | None = None, ticket: int | None = None, group: str | None = None):
        if not self._alive():
            return None
        self._process()
        q = self._quote()
        out = []
        for p in self.positions.values():
            if ticket is not None and p.ticket != ticket:
                continue
            cur = (q[0] if p.type == POSITION_TYPE_BUY else q[1]) if q else p.price_open
            out.append(TradePosition(p.ticket, p.time, p.time * 1000, p.time, p.time * 1000, p.type, p.magic, p.ticket, DEAL_REASON_EXPERT,
                                     p.volume, p.price_open, p.sl, p.tp, cur, 0.0, self._profit(p.type, p.volume, p.price_open, cur), self.spec.name, p.comment, ""))
        return tuple(out)

    def positions_total(self):
        r = self.positions_get()
        return None if r is None else len(r)

    def orders_get(self, symbol: str | None = None, ticket: int | None = None, group: str | None = None):
        if not self._alive():
            return None
        self._process()
        q = self._quote()
        out = []
        for o in self.orders.values():
            if ticket is not None and o.ticket != ticket:
                continue
            cur = (q[1] if o.type == ORDER_TYPE_BUY_LIMIT else q[0]) if q else o.price_open
            out.append(TradeOrder(o.ticket, o.time_setup, o.time_setup * 1000, 0, 0, o.expiration, o.type, o.type_time, o.type_filling, 1, o.magic, 0, 0, 3,
                                  o.volume, o.volume, o.price_open, o.sl, o.tp, cur, 0.0, self.spec.name, o.comment, ""))
        return tuple(out)

    def history_deals_get(self, date_from=None, date_to=None, group: str | None = None, ticket: int | None = None, position: int | None = None):
        if not self._alive():
            return None
        self._process()
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        if ticket is not None:
            return tuple(d for d in self.deals if d.ticket == ticket)
        f = _as_epoch(date_from)
        t = _as_epoch(date_to)
        return tuple(d for d in self.deals if f <= d.time <= t)

    # ------------------------------------------------------------ trading
    def _validate(self, req: dict, for_send: bool) -> tuple[int, str, dict]:
        s = self.spec
        if not self.terminal_trade_allowed:
            return TRADE_RETCODE_CLIENT_DISABLES_AT, "AutoTrading disabled by client", {}
        if not self.connected:
            return TRADE_RETCODE_CONNECTION, "No connection with the trade server", {}
        action = req.get("action")
        q = self._quote()
        if q is None or not self._market_is_open():
            return TRADE_RETCODE_MARKET_CLOSED, "Market closed", {}
        bid, ask, _, _ = q
        stops = s.trade_stops_level * s.point
        if action == TRADE_ACTION_DEAL:
            vol = float(req.get("volume", 0))
            if vol < s.volume_min - 1e-9 or vol > s.volume_max + 1e-9 or abs(round(vol / s.volume_step) * s.volume_step - vol) > 1e-9:
                return TRADE_RETCODE_INVALID_VOLUME, "Invalid volume", {}
            allowed = set()
            if s.filling_mode & 1:
                allowed.add(ORDER_FILLING_FOK)
            if s.filling_mode & 2:
                allowed.add(ORDER_FILLING_IOC)
            if s.allow_return_filling:
                allowed.add(ORDER_FILLING_RETURN)
            if req.get("type_filling", ORDER_FILLING_FOK) not in allowed:
                return TRADE_RETCODE_INVALID_FILL, "Unsupported filling mode", {}
            typ = req.get("type")
            price = ask if typ == ORDER_TYPE_BUY else bid
            if req.get("price") and abs(float(req["price"]) - price) > req.get("deviation", 0) * s.point + 1e-9:
                return TRADE_RETCODE_REQUOTE, "Requote", {"bid": bid, "ask": ask}
            pos_ticket = req.get("position")
            if pos_ticket:
                p = self.positions.get(pos_ticket)
                if p is None:
                    return TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist", {}
                return 0, "Done", {"price": price}
            sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            if typ == ORDER_TYPE_BUY:
                if (sl and (bid - sl) < stops - 1e-9) or (tp and (tp - bid) < stops - 1e-9) or (sl and sl >= bid) or (tp and tp <= bid):
                    return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            else:
                if (sl and (sl - ask) < stops - 1e-9) or (tp and (ask - tp) < stops - 1e-9) or (sl and sl <= ask) or (tp and tp >= ask):
                    return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            margin = vol * s.trade_contract_size / self.leverage
            acct = self.account_info()
            if margin > acct.margin_free:
                return TRADE_RETCODE_NO_MONEY, "No money", {}
            return 0, "Done", {"price": price, "margin": margin}
        if action == TRADE_ACTION_PENDING:
            typ = req.get("type")
            price = float(req.get("price", 0))
            sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            if typ == ORDER_TYPE_BUY_LIMIT:
                if ask - price < stops - 1e-9 or price >= ask:
                    return TRADE_RETCODE_INVALID_PRICE, "Invalid price", {}
                if (sl and price - sl < stops - 1e-9) or (tp and tp - price < stops - 1e-9) or (sl and sl >= price) or (tp and tp <= price):
                    return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            elif typ == ORDER_TYPE_SELL_LIMIT:
                if price - bid < stops - 1e-9 or price <= bid:
                    return TRADE_RETCODE_INVALID_PRICE, "Invalid price", {}
                if (sl and sl - price < stops - 1e-9) or (tp and price - tp < stops - 1e-9) or (sl and sl <= price) or (tp and tp >= price):
                    return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            else:
                return TRADE_RETCODE_INVALID, "Invalid request", {}
            tt = req.get("type_time", ORDER_TIME_GTC)
            if tt == ORDER_TIME_SPECIFIED and not (s.expiration_mode & 4):
                return TRADE_RETCODE_INVALID_EXPIRATION, "Invalid expiration", {}
            return 0, "Done", {"price": price}
        if action == TRADE_ACTION_SLTP:
            p = self.positions.get(req.get("position"))
            if p is None:
                return TRADE_RETCODE_POSITION_CLOSED, "Position doesn't exist", {}
            freeze = s.trade_freeze_level * s.point
            cur = bid if p.type == POSITION_TYPE_BUY else ask
            if freeze and ((p.sl and abs(cur - p.sl) <= freeze) or (p.tp and abs(cur - p.tp) <= freeze)):
                return TRADE_RETCODE_FROZEN, "Order or position frozen", {}
            sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            if abs(sl - p.sl) < 1e-9 and abs(tp - p.tp) < 1e-9:
                return TRADE_RETCODE_NO_CHANGES, "No changes", {}
            if p.type == POSITION_TYPE_BUY and ((sl and bid - sl < stops - 1e-9) or (sl and sl >= bid)):
                return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            if p.type == POSITION_TYPE_SELL and ((sl and sl - ask < stops - 1e-9) or (sl and sl <= ask)):
                return TRADE_RETCODE_INVALID_STOPS, "Invalid stops", {}
            return 0, "Done", {}
        if action == TRADE_ACTION_REMOVE:
            if req.get("order") not in self.orders:
                return TRADE_RETCODE_INVALID, "Invalid request", {}
            return 0, "Done", {}
        return TRADE_RETCODE_INVALID, "Invalid request", {}

    def order_check(self, request: dict):
        if not self._alive():
            return None
        self._process()
        self.checked.append(dict(request))
        code, comment, info = self._validate(request, for_send=False)
        acct = self.account_info()
        margin = info.get("margin", 0.0)
        return OrderCheckResult(code, acct.balance, acct.equity, acct.profit, round(margin, 2), round(acct.margin_free - margin, 2),
                                0.0, comment, _request(request))

    def order_send(self, request: dict):
        if not self._alive():
            return None
        self._process()
        self.sent.append(dict(request))
        now_server = self._server_epoch(self.clock.epoch())
        q = self._quote() or (0.0, 0.0, 0, 0)
        if self.forced_retcodes:
            code = self.forced_retcodes.pop(0)
            return OrderSendResult(code, 0, 0, 0.0, 0.0, q[0], q[1], "Forced by test", 1, 0, _request(request))
        code, comment, info = self._validate(request, for_send=True)
        if code != 0:
            return OrderSendResult(code, 0, 0, 0.0, 0.0, q[0], q[1], comment, 1, 0, _request(request))
        action = request["action"]
        magic = int(request.get("magic", 0))
        if action == TRADE_ACTION_DEAL:
            price = info["price"]
            vol = float(request["volume"])
            pos_ticket = request.get("position")
            if pos_ticket:
                p = self.positions[pos_ticket]
                order = self._ticket()
                d = self._close(p, price, DEAL_REASON_EXPERT, now_server, request.get("comment", ""), order=order)
                return OrderSendResult(TRADE_RETCODE_DONE, d.ticket, order, vol, price, q[0], q[1], "Request executed", 1, 0, _request(request))
            ticket = self._ticket()
            ptype = POSITION_TYPE_BUY if request["type"] == ORDER_TYPE_BUY else POSITION_TYPE_SELL
            self.positions[ticket] = _Pos(ticket, ptype, vol, price, float(request.get("sl", 0) or 0), float(request.get("tp", 0) or 0), magic, now_server, request.get("comment", ""))
            d = self._deal(ticket, ptype, DEAL_ENTRY_IN, vol, price, magic, ticket, DEAL_REASON_EXPERT, 0.0, now_server, request.get("comment", ""))
            return OrderSendResult(TRADE_RETCODE_DONE, d.ticket, ticket, vol, price, q[0], q[1], "Request executed", 1, 0, _request(request))
        if action == TRADE_ACTION_PENDING:
            ticket = self._ticket()
            self.orders[ticket] = _Order(ticket, request["type"], float(request["volume"]), float(request["price"]), float(request.get("sl", 0) or 0),
                                         float(request.get("tp", 0) or 0), magic, now_server, request.get("type_time", ORDER_TIME_GTC),
                                         int(request.get("expiration", 0) or 0), request.get("type_filling", ORDER_FILLING_RETURN), request.get("comment", ""))
            return OrderSendResult(TRADE_RETCODE_PLACED, 0, ticket, float(request["volume"]), 0.0, q[0], q[1], "Request executed", 1, 0, _request(request))
        if action == TRADE_ACTION_SLTP:
            p = self.positions[request["position"]]
            p.sl = float(request.get("sl", 0) or 0)
            p.tp = float(request.get("tp", 0) or 0)
            return OrderSendResult(TRADE_RETCODE_DONE, 0, 0, 0.0, 0.0, q[0], q[1], "Request executed", 1, 0, _request(request))
        if action == TRADE_ACTION_REMOVE:
            del self.orders[request["order"]]
            return OrderSendResult(TRADE_RETCODE_DONE, 0, request["order"], 0.0, 0.0, q[0], q[1], "Request executed", 1, 0, _request(request))
        return OrderSendResult(TRADE_RETCODE_INVALID, 0, 0, 0.0, 0.0, q[0], q[1], "Invalid request", 1, 0, _request(request))

    # ------------------------------------------------------------ test helpers
    def add_manual_position(self, type_: int, volume: float, price: float, sl: float = 0.0, tp: float = 0.0, magic: int = 0) -> int:
        """A trade opened by hand in the terminal (magic 0 by default)."""
        ticket = self._ticket()
        self.positions[ticket] = _Pos(ticket, type_, volume, price, sl, tp, magic, self._server_epoch(self.clock.epoch()), "manual")
        return ticket

    def add_manual_order(self, type_: int, volume: float, price: float, magic: int = 0) -> int:
        ticket = self._ticket()
        self.orders[ticket] = _Order(ticket, type_, volume, price, 0.0, 0.0, magic, self._server_epoch(self.clock.epoch()), ORDER_TIME_GTC, 0, ORDER_FILLING_RETURN, "manual")
        return ticket


def _as_epoch(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.timestamp()
    return float(v)


def _request(req: dict) -> TradeRequest:
    return TradeRequest(req.get("action", 0), req.get("magic", 0), req.get("order", 0), req.get("symbol", ""), req.get("volume", 0.0), req.get("price", 0.0),
                        req.get("stoplimit", 0.0), req.get("sl", 0.0), req.get("tp", 0.0), req.get("deviation", 0), req.get("type", 0),
                        req.get("type_filling", 0), req.get("type_time", 0), req.get("expiration", 0), req.get("comment", ""), req.get("position", 0), req.get("position_by", 0))


def flat_market(start_utc: datetime, days: int = 3, price: float = 1.1600, spread_points: int = 8) -> MinuteMarket:
    """Constant-price market for tests that set prices explicitly."""
    t0 = int(start_utc.replace(second=0, microsecond=0).timestamp())
    times = np.arange(t0, t0 + days * 86400, 60, dtype=np.int64)
    sub = np.full((len(times), SUBSTEPS + 1), price)
    return MinuteMarket(times, sub, np.full(len(times), spread_points), np.full(len(times), 50))


def market_from_m1(times_utc: np.ndarray, o: np.ndarray, h: np.ndarray, lo: np.ndarray, c: np.ndarray, spread_points: int = 8) -> MinuteMarket:
    """Build a market from explicit M1 OHLC (tests). Sub-steps: open, high, low, close, close."""
    sub = np.stack([o, h, lo, c, c], axis=1)
    return MinuteMarket(np.asarray(times_utc, dtype=np.int64), sub, np.full(len(times_utc), spread_points), np.full(len(times_utc), 50))
