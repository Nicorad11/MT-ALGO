"""Command-line tools: `check` (terminal diagnostics + one dry-run cycle) and `flips` (Trend Signals vs TradingView)."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .clock import ServerTime, last_closed_bar_open
from .config import CONFIG_PATH, ROOT, ConfigManager
from .indicators import trend_signals as ts
from .risk import SymbolSpec
from .store import Store

UTC = timezone.utc
OK, BAD = "OK ", "!! "


def line(label: str, value, good: bool | None = None) -> None:
    mark = "   " if good is None else (OK if good else BAD)
    print(f"  {mark}{label:<28}{value}")


def print_decision(store: Store, decision_id: int | None) -> int:
    if not decision_id:
        print("\nNo decision was made (market closed or no new bar). See data/logs/opus_trader.log.")
        return 1
    d = store.one("SELECT * FROM decisions WHERE id=?", (decision_id,))
    print("\nDecision")
    line("Pair", "all pairs" if d["symbol"] == "ALL" else d["symbol"])
    line("Bar (UTC)", d["bar_time"])
    if d["status"] == "SKIPPED":
        line("Skipped", d["skip_reason"])
        return 0
    line("Action", f"{d['action']} {d['order_type'] or ''}".strip())
    if d["stop_loss"]:
        line("Stop / target", f"{d['stop_loss']:g} / {d['take_profit']:g}" if d["take_profit"] else f"{d['stop_loss']:g}")
    line("Confidence / setup", f"{d['confidence']} · {d['setup']}")
    line("Verdict", f"{d['verdict']}: {d['verdict_reason']}", d["verdict"] not in ("ERROR", "NO_DECISION", "FAILED"))
    line("Model", f"{d['model']} ({d['llm_status']}, {d['stop_reason']})")
    line("Tokens", f"{d['input_tokens']} in, {d['cache_read_tokens']} cache read, {d['cache_write_tokens']} cache write, {d['output_tokens']} out")
    line("Cost / latency", f"${d['cost_usd']:.4f} · {d['latency_ms'] / 1000:.1f} s")
    for o in store.query("SELECT purpose, retcode, retcode_name, comment, ok FROM orders WHERE decision_id=? ORDER BY id", (decision_id,)):
        line(f"Order ({o['purpose']})", f"retcode {o['retcode']} {o['retcode_name']} · {o['comment']}", bool(o["ok"]))
        if o["retcode_name"] == "MARKET_CLOSED":
            print(f"     {'':<28}Expected while the market is closed. Run check again on a weekday to test the order check too.")
    if d["reasoning"]:
        print("\nReasoning\n  " + d["reasoning"].replace("\n", "\n  "))
        print(f"  Invalidation: {d['invalidation']}")
    return 0 if d["verdict"] not in ("ERROR", "NO_DECISION", "FAILED") else 2


# ====================================================================== check

def run_check(args) -> int:
    """Attach to the terminal, print what Opus Trader will rely on, then run one dry-run cycle."""
    from .app import load_env, setup_logging

    load_env()
    setup_logging(ROOT / "data" / "logs", args.verbose)
    print("Opus Trader check (nothing is sent to MT5; dry run is forced)\n")
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("  !! The MetaTrader5 package isn't installed. It only exists for Windows: run check.bat on the PC with MT5.")
        return 1
    from .engine import Engine
    from .llm import OpusClient
    from .mt5_gateway import MT5Gateway, NotConnected, SafetyError
    from .clock import Clock
    from .news import NewsCalendar, YamlEventSource

    cfg_mgr = ConfigManager(CONFIG_PATH, None)
    cfg_mgr.persist = False  # dry run is forced for the check; config.yaml is not changed
    cfg_mgr.update({"app.dry_run": True}, source="check", allow_locked=True)
    cfg = cfg_mgr.cfg
    gw = MT5Gateway(mt5, cfg.app.symbols, cfg.app.magic, os.environ.get("MT5_TERMINAL_PATH"))
    print("Terminal and account")
    try:
        acct_id = gw.connect()
    except SafetyError as exc:
        line("Symbol" if exc.code == "symbol" else "Account", exc.message, False)
        return 1
    except NotConnected as exc:
        line("MT5", f"{exc.hint}." if exc.hint else f"{exc}. Is the terminal open and logged in?", False)
        if exc.hint:
            print(f"     {'':<28}({exc})")
        if os.environ.get("MT5_TERMINAL_PATH"):
            print(f"     {'':<28}MT5_TERMINAL_PATH in .env is {os.environ['MT5_TERMINAL_PATH']}")
        return 1
    ti = mt5.terminal_info()
    acct = mt5.account_info()
    line("Account", f"{acct.login} · {acct.name}", True)
    line("Server", f"{acct.server} ({acct.company})", True)
    line("Type", "Demo", acct.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO)
    line("Margin mode", "Hedging", acct.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
    line("Currency", acct.currency, acct.currency == "EUR")
    line("Balance / equity", f"{acct.balance:,.2f} / {acct.equity:,.2f} {acct.currency}")
    line("Terminal", f"{ti.name} · {ti.path}")
    line("Terminal build", ti.build)
    line("Broker connection", "connected" if ti.connected else "NOT connected", bool(ti.connected))
    line("Algo Trading", "on" if ti.trade_allowed else "OFF (turn it on in the toolbar)", bool(ti.trade_allowed))

    names = {mt5.ORDER_FILLING_IOC: "IOC", mt5.ORDER_FILLING_FOK: "FOK", mt5.ORDER_FILLING_RETURN: "RETURN"}
    modes = {mt5.SYMBOL_TRADE_MODE_FULL: "full", mt5.SYMBOL_TRADE_MODE_DISABLED: "disabled", mt5.SYMBOL_TRADE_MODE_CLOSEONLY: "close only",
             mt5.SYMBOL_TRADE_MODE_LONGONLY: "long only", mt5.SYMBOL_TRADE_MODE_SHORTONLY: "short only"}
    wide = []
    print(f"\nPairs ({len(cfg.app.symbols)} in config.yaml; spread limit {cfg.risk.max_spread_pips:g} pips)")
    for wanted in cfg.app.symbols:
        if wanted in gw.unavailable:
            line(wanted, gw.unavailable[wanted], False)
            continue
        sym = gw.broker_name(wanted)
        info = gw.symbol_info(sym)
        spec = SymbolSpec.from_info(info)
        tick = gw.tick(sym)
        spread = (tick.ask - tick.bid) / spec.pip
        ok = spread <= cfg.risk.max_spread_pips and info.trade_mode == mt5.SYMBOL_TRADE_MODE_FULL
        label = wanted if sym == wanted else f"{wanted} = {sym}"
        line(label, f"bid {tick.bid:.{info.digits}f} · spread {spread:.1f} pips · {info.digits} digits · trading "
                    f"{modes.get(info.trade_mode, str(info.trade_mode))} · lots {info.volume_min:g}-{info.volume_max:g} step {info.volume_step:g}", ok)
        print(f"     {'':<28}tick value {info.trade_tick_value:.4f} {acct.currency} · stops/freeze {info.trade_stops_level}/{info.trade_freeze_level} points · "
              f"filling {' → '.join(names[m] for m in gw.filling_candidates(sym))} · limit expiry "
              f"{'by broker' if gw.supports_specified_expiration(sym) else 'by Opus Trader (GTC)'}")
        if spread > cfg.risk.max_spread_pips:
            wide.append(wanted)
            for o in (mt5.symbols_get(group=f"*{wanted[:6]}*") or ()):
                if o.name != sym and o.name.upper().startswith(wanted[:6].upper()):
                    o_spread = f"spread {(o.ask - o.bid) / SymbolSpec.from_info(o).pip:.1f} pips" if o.bid and o.ask else "no quote (not in Market Watch)"
                    print(f"     {'':<28}also at this broker: {o.name}, {o_spread}, trading {modes.get(o.trade_mode, o.trade_mode)}")
    if wide:
        print(f"     {'':<28}Bars are skipped while a spread is above {cfg.risk.max_spread_pips:g} pips ({', '.join(wide)} right now). Spreads are often wider")
        print(f"     {'':<28}outside London and New York hours. If a variant listed above trades with a normal spread, put its name in")
        print(f"     {'':<28}app.symbols in config.yaml instead; or raise Max spread in Settings.")

    st = ServerTime()
    t1 = gw.tick()
    st.observe_tick(int(t1.time), datetime.now(UTC))
    time.sleep(3)
    t2 = gw.tick()
    st.observe_tick(int(t2.time), datetime.now(UTC))
    m = st.model
    print("\nBroker time")
    if m.detected:
        line("Server time", m.describe(), True)
    else:
        line("Server time", "not detected (market closed?). Assuming New York + 7 h until the first live tick.", None)
    gw.shutdown()

    from .app import ensure_api_key
    from .llm import key_problem

    ensure_api_key()
    problem = key_problem(os.environ.get("ANTHROPIC_API_KEY"))
    if problem:
        print(f"\n  !! {problem}. Run check again and paste your key when asked (or put it in .env).")
        return 1

    first = gw.available_symbols()[0]
    print(f"\nOne dry-run cycle on {first}, last closed M15 bar (real snapshot, real Opus call, risk engine, order_check)...")
    print("  (Only the first pair is checked, to keep the cost of the check to one Opus call.)")
    check_db = ROOT / "data" / "check.sqlite"
    if check_db.exists():
        check_db.unlink()
    store = Store(check_db)
    cfg_mgr.store = store
    news = NewsCalendar(YamlEventSource(ROOT / cfg.news.events_file), cfg.news.currencies)
    gw2 = MT5Gateway(mt5, cfg.app.symbols, cfg.app.magic, os.environ.get("MT5_TERMINAL_PATH"))
    engine = Engine(config=cfg_mgr, gateway=gw2, llm=OpusClient(os.environ.get("ANTHROPIC_API_KEY")), store=store, news=news,
                    clock=Clock(), root=ROOT, read_only=True)

    async def cycle():
        await engine.start(run_tasks=False)
        ids = await engine.process_bar(last_closed_bar_open(engine.clock.now()), wait=False, force=True, symbols=[first])
        await engine.shutdown()
        return ids[0] if ids else None

    did = asyncio.run(cycle())
    if engine.forced_precheck:
        print(f"  (A pre-check would normally skip this bar: {engine.forced_precheck}. The check called Opus anyway.)")
    code = print_decision(store, did)
    print(f"\nFull record: {check_db}")
    return code


# ====================================================================== flips

def _read_csv(path: Path) -> tuple[list[datetime], np.ndarray, np.ndarray, np.ndarray]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    keys = {k.lower().strip(): k for k in rows[0]}
    tkey = next(keys[k] for k in keys if k in ("time", "time_utc", "date", "datetime"))

    def when(v: str) -> datetime:
        v = v.strip()
        if v.replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(v), UTC)
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)

    get = lambda k: np.array([float(r[keys[k]]) for r in rows])  # noqa: E731
    return [when(r[tkey]) for r in rows], get("high"), get("low"), get("close")


def run_flips(args) -> int:
    from .app import load_env

    load_env()
    cfg = ConfigManager(CONFIG_PATH, None).cfg
    sc = cfg.signal
    if args.csv:
        times, h, lo, c = _read_csv(Path(args.csv))
        source = args.csv
    else:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            print("The MetaTrader5 package isn't installed (Windows only). Pass --csv FILE to use exported bars instead.")
            return 1
        from .mt5_gateway import MT5Gateway
        from .snapshot import Bars

        gw = MT5Gateway(mt5, [args.symbol] if args.symbol else cfg.app.symbols, cfg.app.magic, os.environ.get("MT5_TERMINAL_PATH"))
        gw.connect()
        st = ServerTime()
        for _ in range(2):
            t = gw.tick()
            st.observe_tick(int(t.time), datetime.now(UTC))
            time.sleep(2)
        b = Bars.from_rates(gw.rates(mt5.TIMEFRAME_M15, 1, args.bars), st)
        gw.shutdown()
        times = [b.time(i) for i in range(len(b))]
        h, lo, c = b.h, b.l, b.c
        source = f"MT5 {gw.symbol} M15, last {len(b)} closed bars"
    if len(c) < 1000:
        print(f"Warning: only {len(c)} bars; use at least 1000 so the ATR matches TradingView.")
    s = ts.compute(h, lo, c, sc.atr_length, sc.multiplier, sc.atr_method)
    idx = s.flip_indices()[-args.count:]
    digits = 3 if float(np.median(c)) > 20 else 5  # JPY pairs quote around 150 with 3 decimals
    pip = 0.01 if digits == 3 else 0.0001
    print(f"Trend Signals flips ({source}; ATR {sc.atr_length} × {sc.multiplier}, method {sc.atr_method}). Times are bar OPEN times in UTC.\n")
    print(f"  {'bar open (UTC)':<18}{'signal':<8}{'close':>10}{'line after':>12}{'ATR pips':>10}")
    for i in idx:
        print(f"  {times[i]:%Y-%m-%d %H:%M}  {'BUY' if s.buy[i] else 'SELL':<8}{c[i]:>10.{digits}f}{s.line(int(i)):>12.{digits}f}{s.atr[i] / pip:>10.1f}")
    print("\nIn TradingView, set the chart timezone to UTC to compare the bar times directly.")
    return 0
