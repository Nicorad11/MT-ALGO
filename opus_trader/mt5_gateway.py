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


# last_error() codes from the MetaTrader5 package that mean the terminal itself can't be reached
# (RES_E_INTERNAL_FAIL*, the same values as the package's constants).
IPC_ERRORS = {-10000, -10001, -10002, -10003, -10004, -10005}
NOT_LOGGED_IN = "MetaTrader 5 is open but not logged in. Log in to your demo account in the terminal"


class NotConnected(Exception):
    """The terminal isn't reachable (closed, IPC failure, not logged in)."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint  # what to do about it, in plain words (the banner and check.bat show it)


class NoData(NotConnected):
    """One symbol's call returned nothing while the terminal itself answers (history still loading, no quote yet).

    A NotConnected, so code that doesn't care treats it like before; the engine catches it per pair."""


def connection_hint(err: Any) -> str | None:
    """Plain-English advice for a MetaTrader5 last_error() tuple, if there is one."""
    code = err[0] if isinstance(err, tuple) and err else None
    if code == -10003:
        return "MetaTrader 5 isn't running or wasn't found. Open the MT5 terminal and log in to your demo account"
    if code == -10005:
        return "MetaTrader 5 didn't answer in time; it may still be starting"
    if code in IPC_ERRORS:
        return "MetaTrader 5 isn't responding. Check that the terminal is open"
    return None


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
    """One terminal connection for every traded symbol.

    Per-symbol methods take a broker symbol name and default to the primary
    symbol (the first configured one that the broker has), so a one-pair
    gateway works as before. `view(symbol)` hands executors the same interface
    bound to another pair.
    """

    def __init__(self, mt5: Any, symbols: str | list[str], magic: int, terminal_path: str | None = None):
        self.mt5 = mt5
        self.configured_symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        if not self.configured_symbols:
            raise ValueError("At least one symbol is needed")
        self.primary = self.configured_symbols[0]
        self.names: dict[str, str] = {}  # configured name -> broker name (EURUSD -> EURUSD.pro), set on connect
        self.unavailable: dict[str, str] = {}  # configured name -> why it can't be traded here
        self.magic = magic
        self.terminal_path = terminal_path or None
        self.lock = threading.RLock()
        self.account_id: AccountId | None = None
        self.retcode_names = {getattr(mt5, n): n.removeprefix("TRADE_RETCODE_") for n in dir(mt5) if n.startswith("TRADE_RETCODE_")}

    @property
    def configured_symbol(self) -> str:
        return self.primary

    @property
    def symbol(self) -> str:
        """Broker name of the primary symbol."""
        return self.names.get(self.primary, self.primary)

    def broker_name(self, configured: str) -> str:
        return self.names.get(configured, configured)

    def view(self, configured: str) -> "SymbolView":
        return SymbolView(self, configured)

    # ------------------------------------------------------------ connection
    def connect(self) -> AccountId:
        """Attach to the running, logged-in terminal. Never passes credentials."""
        with self.lock:
            ok = self.mt5.initialize(path=self.terminal_path) if self.terminal_path else self.mt5.initialize()
            if not ok:
                err = self.mt5.last_error()
                raise NotConnected(f"Could not attach to MT5: {err}", connection_hint(err))
            acct = self.mt5.account_info()
            if acct is None or not acct.login:
                raise NotConnected(f"MT5 is running but not logged in: {self.mt5.last_error()}", NOT_LOGGED_IN)
            expected = self.account_id.login if self.account_id else None
            try:
                self._check_account(acct, expected)
            except SafetyError:
                self.mt5.shutdown()
                raise
            self.resolve_symbols(self.configured_symbols)
            if self.mt5.symbol_info(self.symbol) is None:
                raise NotConnected(f"No symbol info for {self.symbol}")
            self.account_id = AccountId(acct.login, acct.server, acct.company, acct.name, acct.currency, acct.trade_mode, acct.margin_mode, acct.leverage)
            log.info("Attached to MT5: account %s on %s (%s)", acct.login, acct.server, acct.currency)
            return self.account_id

    def resolve_symbols(self, configured: list[str]) -> None:
        """Find each configured symbol at the broker. A pair the broker doesn't have is skipped, not fatal,
        unless no pair at all can be traded."""
        with self.lock:
            self.configured_symbols = list(configured)
            first_error: SafetyError | None = None
            for wanted in self.configured_symbols:
                try:
                    self.names[wanted] = self._resolve_symbol(wanted)
                    self.unavailable.pop(wanted, None)
                except SafetyError as exc:
                    self.names.pop(wanted, None)
                    self.unavailable[wanted] = exc.message
                    first_error = first_error or exc
                    log.warning("%s", exc.message)
            available = [c for c in self.configured_symbols if c in self.names]
            if not available:
                self.mt5.shutdown()
                raise first_error or SafetyError("symbol", "No symbols configured", fatal=True)
            self.primary = available[0]

    def available_symbols(self) -> list[str]:
        return [c for c in self.configured_symbols if c in self.names]

    def _resolve_symbol(self, wanted: str) -> str:
        """The configured symbol, or its only broker variant (EURUSD -> EURUSD.pro)."""
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
                                        f"Put the one you trade in app.symbols in config.yaml, then restart.", fatal=True)
        raise SafetyError("symbol", f"No symbol like {wanted} in this MT5 terminal. Check app.symbols in config.yaml.", fatal=True)

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
                raise self._not_connected("account_info")
            if not acct.login:
                raise NotConnected("MT5 is no longer logged in", NOT_LOGGED_IN)
            self._check_account(acct, self.account_id.login if self.account_id else None)

    def health(self) -> Health:
        with self.lock:
            ti = self.mt5.terminal_info()
            if ti is None:
                return Health(False, False, False, None, None)
            acct = self.mt5.account_info()
            login = (acct.login or None) if acct else None
            is_demo = (acct.trade_mode == self.mt5.ACCOUNT_TRADE_MODE_DEMO) if acct else None
            return Health(True, bool(ti.connected), bool(ti.trade_allowed), login, is_demo)

    def _not_connected(self, what: str) -> NotConnected:
        err = self.mt5.last_error()
        return NotConnected(f"{what} failed: {err}", connection_hint(err))

    def _no_data(self, what: str, symbol: str) -> NotConnected:
        """NotConnected if the terminal is gone, NoData if only this symbol's data is missing."""
        err = self.mt5.last_error()
        if isinstance(err, tuple) and err and err[0] in IPC_ERRORS:
            return NotConnected(f"{what} failed: {err}", connection_hint(err))
        return NoData(f"{what} for {symbol} returned nothing: {err}")

    def _refused(self, what: str) -> Exception:
        """A None result: the terminal is gone (NotConnected) or it refused the request itself."""
        err = self.mt5.last_error()
        if isinstance(err, tuple) and err and err[0] in IPC_ERRORS:
            return NotConnected(f"{what} failed: {err}", connection_hint(err))
        return SafetyError("request", f"MT5 refused the {what} request: {err}")

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
                raise self._not_connected("account_info")
            return acct

    def symbol_info(self, symbol: str | None = None) -> Any:
        with self.lock:
            info = self.mt5.symbol_info(symbol or self.symbol)
            if info is None:
                raise self._not_connected("symbol_info")
            return info

    def tick(self, symbol: str | None = None) -> Any:
        with self.lock:
            sym = symbol or self.symbol
            t = self.mt5.symbol_info_tick(sym)
            if t is None:
                raise self._no_data("symbol_info_tick", sym)
            return t

    def rates(self, timeframe: int, start_pos: int, count: int, symbol: str | None = None):
        with self.lock:
            sym = symbol or self.symbol
            r = self.mt5.copy_rates_from_pos(sym, timeframe, start_pos, count)
            if r is None:
                raise self._no_data("copy_rates_from_pos", sym)
            return r

    def rates_range_server(self, timeframe: int, from_server_epoch: int, to_server_epoch: int, symbol: str | None = None):
        """Bars between two *server-time* epochs (MT5 compares range arguments with server time)."""
        with self.lock:
            sym = symbol or self.symbol
            r = self.mt5.copy_rates_range(sym, timeframe, datetime.fromtimestamp(from_server_epoch, UTC), datetime.fromtimestamp(to_server_epoch, UTC))
            if r is None:
                raise self._no_data("copy_rates_range", sym)
            return r

    # ------------------------------------------------------------ bot positions / orders / deals
    def bot_positions(self, symbol: str | None = None) -> list[Any]:
        sym = symbol or self.symbol
        with self.lock:
            ps = self.mt5.positions_get(symbol=sym)
            if ps is None:
                raise self._not_connected("positions_get")
            return [p for p in ps if p.magic == self.magic and p.symbol == sym]

    def bot_orders(self, symbol: str | None = None) -> list[Any]:
        sym = symbol or self.symbol
        with self.lock:
            os_ = self.mt5.orders_get(symbol=sym)
            if os_ is None:
                raise self._not_connected("orders_get")
            return [o for o in os_ if o.magic == self.magic and o.symbol == sym]

    def all_bot_positions(self) -> list[Any]:
        """Every position carrying the bot's magic number, on any symbol."""
        with self.lock:
            ps = self.mt5.positions_get()
            if ps is None:
                raise self._not_connected("positions_get")
            return [p for p in ps if p.magic == self.magic]

    def all_bot_orders(self) -> list[Any]:
        with self.lock:
            os_ = self.mt5.orders_get()
            if os_ is None:
                raise self._not_connected("orders_get")
            return [o for o in os_ if o.magic == self.magic]

    def bot_deals(self, from_server_epoch: int, to_server_epoch: int, symbol: str | None = None) -> list[Any]:
        sym = symbol or self.symbol
        with self.lock:
            ds = self.mt5.history_deals_get(datetime.fromtimestamp(from_server_epoch, UTC), datetime.fromtimestamp(to_server_epoch, UTC))
            if ds is None:
                raise self._not_connected("history_deals_get")
            return [d for d in ds if d.magic == self.magic and d.symbol == sym]

    def position_deals(self, position_ticket: int) -> list[Any]:
        with self.lock:
            ds = self.mt5.history_deals_get(position=position_ticket)
            if ds is None:
                raise self._not_connected("history_deals_get")
            return [d for d in ds if d.magic == self.magic]

    # ------------------------------------------------------------ trading
    def filling_candidates(self, symbol: str | None = None) -> list[int]:
        """Filling modes to try, in order: IOC, then FOK (if the symbol allows them), then RETURN."""
        info = self.symbol_info(symbol)
        flags = int(info.filling_mode)
        modes = []
        if flags & SYMBOL_FILLING_IOC:
            modes.append(self.mt5.ORDER_FILLING_IOC)
        if flags & SYMBOL_FILLING_FOK:
            modes.append(self.mt5.ORDER_FILLING_FOK)
        modes.append(self.mt5.ORDER_FILLING_RETURN)
        return modes

    def supports_specified_expiration(self, symbol: str | None = None) -> bool:
        return bool(int(self.symbol_info(symbol).expiration_mode) & SYMBOL_EXPIRATION_SPECIFIED)

    def order_check(self, request: dict) -> Any:
        with self.lock:
            res = self.mt5.order_check(request)
            if res is None:
                raise self._refused("order_check")
            return res

    def order_send(self, request: dict, gate: Callable[[], str | None] | None = None, symbol: str | None = None) -> Any:
        """Send an order after re-checking every safety rule. Raises SafetyError if blocked."""
        sym = symbol or self.symbol
        with self.lock:
            self.verify_demo()
            ti = self.mt5.terminal_info()
            if ti is None:
                raise self._not_connected("terminal_info")
            if not ti.trade_allowed:
                raise SafetyError("algo_off", "Algo Trading is off in the MT5 terminal.")
            if gate is not None:
                reason = gate()
                if reason:
                    raise SafetyError("gate", reason)
            if int(request.get("magic", 0)) != self.magic:
                raise SafetyError("magic", "Refusing to send an order without the bot's magic number.")
            if request.get("symbol") not in (None, sym):
                raise SafetyError("symbol", f"Refusing to send an order for {request.get('symbol')}.")
            res = self.mt5.order_send(request)
            if res is None:
                raise self._refused("order_send")
            return res

    def retcode_name(self, code: int) -> str:
        if code == 0:
            return "OK"
        return self.retcode_names.get(code, str(code))


class SymbolView:
    """The gateway seen through one pair: the single-symbol interface the executors use."""

    def __init__(self, gw: MT5Gateway, configured: str):
        self._gw = gw
        self.configured_symbol = configured
        self.mt5 = gw.mt5
        self.magic = gw.magic
        self.lock = gw.lock

    @property
    def symbol(self) -> str:
        return self._gw.broker_name(self.configured_symbol)

    @property
    def account_id(self) -> AccountId | None:
        return self._gw.account_id

    def account(self) -> Any:
        return self._gw.account()

    def verify_demo(self) -> None:
        self._gw.verify_demo()

    def symbol_info(self) -> Any:
        return self._gw.symbol_info(self.symbol)

    def tick(self) -> Any:
        return self._gw.tick(self.symbol)

    def rates(self, timeframe: int, start_pos: int, count: int):
        return self._gw.rates(timeframe, start_pos, count, self.symbol)

    def rates_range_server(self, timeframe: int, from_server_epoch: int, to_server_epoch: int):
        return self._gw.rates_range_server(timeframe, from_server_epoch, to_server_epoch, self.symbol)

    def bot_positions(self) -> list[Any]:
        return self._gw.bot_positions(self.symbol)

    def bot_orders(self) -> list[Any]:
        return self._gw.bot_orders(self.symbol)

    def bot_deals(self, from_server_epoch: int, to_server_epoch: int) -> list[Any]:
        return self._gw.bot_deals(from_server_epoch, to_server_epoch, self.symbol)

    def position_deals(self, position_ticket: int) -> list[Any]:
        return self._gw.position_deals(position_ticket)

    def filling_candidates(self) -> list[int]:
        return self._gw.filling_candidates(self.symbol)

    def supports_specified_expiration(self) -> bool:
        return self._gw.supports_specified_expiration(self.symbol)

    def order_check(self, request: dict) -> Any:
        return self._gw.order_check(request)

    def order_send(self, request: dict, gate: Callable[[], str | None] | None = None) -> Any:
        return self._gw.order_send(request, gate, self.symbol)

    def retcode_name(self, code: int) -> str:
        return self._gw.retcode_name(code)


def record(rec: Any) -> dict:
    """namedtuple -> plain dict (for logging and JSON)."""
    d = _to_dict(rec)
    if "request" in d and d["request"] is not None and not isinstance(d["request"], dict):
        d["request"] = _to_dict(d["request"])
    return d
