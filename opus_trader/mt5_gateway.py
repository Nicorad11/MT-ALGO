"""Thread-safe wrapper around the MetaTrader5 module.

This is the only module that talks to the terminal, and `order_send` here is
the only place an order can leave the app. Every send re-checks that the
account is still the same demo account, that algo trading is allowed, that
the engine's gate is open, and that the request carries the bot's magic number.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)
UTC = timezone.utc

# Symbol flag constants (MQL5 SYMBOL_FILLING_* / SYMBOL_EXPIRATION_*); the Python package doesn't export them.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_EXPIRATION_GTC = 1
SYMBOL_EXPIRATION_DAY = 2
SYMBOL_EXPIRATION_SPECIFIED = 4
SYMBOL_EXPIRATION_SPECIFIED_DAY = 8


class NotConnected(Exception):
    """The terminal isn't reachable (closed, IPC failure, not logged in)."""


class SafetyError(Exception):
    """A safety rule blocks trading. `fatal` errors stop the engine for good."""

    def __init__(self, code: str, message: str, fatal: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal


@dataclass
class AccountId:
    login: int
    server: str
    company: str
    name: str
    currency: str
    trade_mode: int
    margin_mode: int
    leverage: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Health:
    ipc_ok: bool
    connected: bool
    trade_allowed: bool
    login: int | None
    is_demo: bool | None


def _to_dict(rec: Any) -> dict:
    if rec is None:
        return {}
    if hasattr(rec, "_asdict"):
        return dict(rec._asdict())
    return dict(rec)


class MT5Gateway:
    def __init__(self, mt5: Any, symbol: str, magic: int, terminal_path: str | None = None):
        self.mt5 = mt5
        self.configured_symbol = symbol
        self.symbol = symbol  # resolved on connect (a broker may name it EURUSD.pro, EURUSDm, ...)
        self.magic = magic
        self.terminal_path = terminal_path or None
        self.lock = threading.RLock()
        self.account_id: AccountId | None = None
        self.retcode_names = {getattr(mt5, n): n.removeprefix("TRADE_RETCODE_") for n in dir(mt5) if n.startswith("TRADE_RETCODE_")}

    # ------------------------------------------------------------ connection
    def connect(self) -> AccountId:
        """Attach to the running, logged-in terminal. Never passes credentials."""
        with self.lock:
            ok = self.mt5.initialize(path=self.terminal_path) if self.terminal_path else self.mt5.initialize()
            if not ok:
                raise NotConnected(f"Could not attach to MT5: {self.mt5.last_error()}")
            acct = self.mt5.account_info()
            if acct is None:
                raise NotConnected(f"MT5 is running but not logged in: {self.mt5.last_error()}")
            expected = self.account_id.login if self.account_id else None
            try:
                self._check_account(acct, expected)
            except SafetyError:
                self.mt5.shutdown()
                raise
            self.symbol = self._resolve_symbol()
            if self.mt5.symbol_info(self.symbol) is None:
                raise NotConnected(f"No symbol info for {self.symbol}")
            self.account_id = AccountId(acct.login, acct.server, acct.company, acct.name, acct.currency, acct.trade_mode, acct.margin_mode, acct.leverage)
            log.info("Attached to MT5: account %s on %s (%s)", acct.login, acct.server, acct.currency)
            return self.account_id

    def _resolve_symbol(self) -> str:
        """The configured symbol, or its only broker variant (EURUSD -> EURUSD.pro)."""
        wanted = self.configured_symbol
        if self.mt5.symbol_select(wanted, True):
            return wanted
        found = self.mt5.symbols_get(group=f"*{wanted}*") or ()
        names = sorted({s.name for s in found if s.name.upper().startswith(wanted.upper())})
        visible = [s.name for s in found if s.name in names and getattr(s, "visible", False)]
        pick = names[0] if len(names) == 1 else visible[0] if len(visible) == 1 else None
        if pick and self.mt5.symbol_select(pick, True):
            if pick != wanted:
                log.info("Symbol %s is called %s at this broker; using %s", wanted, pick, pick)
            return pick
        if names:
            raise SafetyError("symbol", f"{wanted} isn't in this MT5 terminal, but these are: {', '.join(names)}. "
                                        f"Set app.symbol in config.yaml to the one you trade, then restart.", fatal=True)
        raise SafetyError("symbol", f"No symbol like {wanted} in this MT5 terminal. Check app.symbol in config.yaml, then restart.", fatal=True)

    def _check_account(self, acct: Any, expected_login: int | None) -> None:
        mt5 = self.mt5
        if acct.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            kind = {mt5.ACCOUNT_TRADE_MODE_REAL: "a real-money", mt5.ACCOUNT_TRADE_MODE_CONTEST: "a contest"}.get(acct.trade_mode, "not a demo")
            raise SafetyError("not_demo", f"Account {acct.login} on {acct.server} is {kind} account. Opus Trader only runs on demo accounts.", fatal=True)
        if expected_login is not None and acct.login != expected_login:
            raise SafetyError("account_changed", f"The terminal switched from account {expected_login} to {acct.login}. Restart Opus Trader to continue.", fatal=True)
        if acct.margin_mode != mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise SafetyError("not_hedging", f"Account {acct.login} is not a hedging account. Opus Trader expects a hedging account.", fatal=True)

    def verify_demo(self) -> None:
        with self.lock:
            acct = self.mt5.account_info()
            if acct is None:
                raise NotConnected(f"account_info failed: {self.mt5.last_error()}")
            self._check_account(acct, self.account_id.login if self.account_id else None)

    def health(self) -> Health:
        with self.lock:
            ti = self.mt5.terminal_info()
            if ti is None:
                return Health(False, False, False, None, None)
            acct = self.mt5.account_info()
            login = acct.login if acct else None
            is_demo = (acct.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO) if acct else None
            return Health(True, bool(ti.connected), bool(ti.trade_allowed), login, is_demo)

    def shutdown(self) -> None:
        with self.lock:
            try:
                self.mt5.shutdown()
            except Exception:  # noqa: BLE001 - best effort on exit
                pass

    # ------------------------------------------------------------ market data
    def account(self) -> Any:
        with self.lock:
            acct = self.mt5.account_info()
            if acct is None:
                raise NotConnected(f"account_info failed: {self.mt5.last_error()}")
            return acct

    def symbol_info(self) -> Any:
        with self.lock:
            info = self.mt5.symbol_info(self.symbol)
            if info is None:
                raise NotConnected(f"symbol_info failed: {self.mt5.last_error()}")
            return info

    def tick(self) -> Any:
        with self.lock:
            t = self.mt5.symbol_info_tick(self.symbol)
            if t is None:
                raise NotConnected(f"symbol_info_tick failed: {self.mt5.last_error()}")
            return t

    def rates(self, timeframe: int, start_pos: int, count: int):
        with self.lock:
            r = self.mt5.copy_rates_from_pos(self.symbol, timeframe, start_pos, count)
            if r is None:
                raise NotConnected(f"copy_rates_from_pos failed: {self.mt5.last_error()}")
            return r

    def rates_range_server(self, timeframe: int, from_server_epoch: int, to_server_epoch: int):
        """Bars between two *server-time* epochs (MT5 compares range arguments with server time)."""
        with self.lock:
            r = self.mt5.copy_rates_range(self.symbol, timeframe, datetime.fromtimestamp(from_server_epoch, UTC), datetime.fromtimestamp(to_server_epoch, UTC))
            if r is None:
                raise NotConnected(f"copy_rates_range failed: {self.mt5.last_error()}")
            return r

    # ------------------------------------------------------------ bot positions / orders / deals
    def bot_positions(self) -> list[Any]:
        with self.lock:
            ps = self.mt5.positions_get(symbol=self.symbol)
            if ps is None:
                raise NotConnected(f"positions_get failed: {self.mt5.last_error()}")
            return [p for p in ps if p.magic == self.magic and p.symbol == self.symbol]

    def bot_orders(self) -> list[Any]:
        with self.lock:
            os_ = self.mt5.orders_get(symbol=self.symbol)
            if os_ is None:
                raise NotConnected(f"orders_get failed: {self.mt5.last_error()}")
            return [o for o in os_ if o.magic == self.magic and o.symbol == self.symbol]

    def bot_deals(self, from_server_epoch: int, to_server_epoch: int) -> list[Any]:
        with self.lock:
            ds = self.mt5.history_deals_get(datetime.fromtimestamp(from_server_epoch, UTC), datetime.fromtimestamp(to_server_epoch, UTC))
            if ds is None:
                raise NotConnected(f"history_deals_get failed: {self.mt5.last_error()}")
            return [d for d in ds if d.magic == self.magic and d.symbol == self.symbol]

    def position_deals(self, position_ticket: int) -> list[Any]:
        with self.lock:
            ds = self.mt5.history_deals_get(position=position_ticket)
            if ds is None:
                raise NotConnected(f"history_deals_get failed: {self.mt5.last_error()}")
            return [d for d in ds if d.magic == self.magic]

    # ------------------------------------------------------------ trading
    def filling_candidates(self) -> list[int]:
        """Filling modes to try, in order: IOC, then FOK (if the symbol allows them), then RETURN."""
        info = self.symbol_info()
        flags = int(info.filling_mode)
        modes = []
        if flags & SYMBOL_FILLING_IOC:
            modes.append(self.mt5.ORDER_FILLING_IOC)
        if flags & SYMBOL_FILLING_FOK:
            modes.append(self.mt5.ORDER_FILLING_FOK)
        modes.append(self.mt5.ORDER_FILLING_RETURN)
        return modes

    def supports_specified_expiration(self) -> bool:
        return bool(int(self.symbol_info().expiration_mode) & SYMBOL_EXPIRATION_SPECIFIED)

    def order_check(self, request: dict) -> Any:
        with self.lock:
            res = self.mt5.order_check(request)
            if res is None:
                raise NotConnected(f"order_check failed: {self.mt5.last_error()}")
            return res

    def order_send(self, request: dict, gate: Callable[[], str | None] | None = None) -> Any:
        """Send an order after re-checking every safety rule. Raises SafetyError if blocked."""
        with self.lock:
            self.verify_demo()
            ti = self.mt5.terminal_info()
            if ti is None:
                raise NotConnected(f"terminal_info failed: {self.mt5.last_error()}")
            if not ti.trade_allowed:
                raise SafetyError("algo_off", "Algo Trading is off in the MT5 terminal.")
            if gate is not None:
                reason = gate()
                if reason:
                    raise SafetyError("gate", reason)
            if int(request.get("magic", 0)) != self.magic:
                raise SafetyError("magic", "Refusing to send an order without the bot's magic number.")
            if request.get("symbol") not in (None, self.symbol):
                raise SafetyError("symbol", f"Refusing to send an order for {request.get('symbol')}.")
            res = self.mt5.order_send(request)
            if res is None:
                raise NotConnected(f"order_send failed: {self.mt5.last_error()}")
            return res

    def retcode_name(self, code: int) -> str:
        if code == 0:
            return "OK"
        return self.retcode_names.get(code, str(code))


def record(rec: Any) -> dict:
    """namedtuple -> plain dict (for logging and JSON)."""
    d = _to_dict(rec)
    if "request" in d and d["request"] is not None and not isinstance(d["request"], dict):
        d["request"] = _to_dict(d["request"])
    return d
