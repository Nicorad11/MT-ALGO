"""Opus Trader command line.

    python -m opus_trader                   app in its own window (start.bat runs this)
    python -m opus_trader --no-window       server only; open http://127.0.0.1:8765 yourself
    python -m opus_trader --sim             preview against a simulated terminal (no MT5, no API key)
    python -m opus_trader --once            one decision cycle on the last closed bar, then exit
    python -m opus_trader check             terminal diagnostics + one dry-run cycle (check.bat)
    python -m opus_trader flips             last Trend Signals flips, to compare with TradingView
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opus_trader", description="Claude Opus 5.5 paper-trades EUR/USD on a MetaTrader 5 demo account.")
    p.add_argument("command", nargs="?", default="run", choices=["run", "check", "flips"])
    p.add_argument("--sim", action="store_true", help="preview against a simulated terminal and a rule-based stand-in for Opus")
    p.add_argument("--seed-days", type=float, default=2.0, help="with --sim: days of simulated history to generate first (default 2)")
    p.add_argument("--keep-sim", action="store_true", help="with --sim: keep the previous simulated journal instead of starting fresh")
    p.add_argument("--once", action="store_true", help="run one decision cycle on the last closed bar and exit")
    p.add_argument("--no-window", action="store_true", help="don't open the app window (server only)")
    p.add_argument("--open-browser", action="store_true", help="with --no-window: open the UI in the default browser")
    p.add_argument("--port", type=int, default=None, help="UI port (default from config.yaml)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--csv", help="flips: read bars from a CSV (time, open, high, low, close) instead of MT5")
    p.add_argument("--bars", type=int, default=5000, help="flips: number of closed M15 bars to load from MT5 (default 5000)")
    p.add_argument("--count", type=int, default=20, help="flips: how many flips to print (default 20)")
    args = p.parse_args(argv)

    if args.command == "check":
        from .tools import run_check

        return run_check(args)
    if args.command == "flips":
        from .tools import run_flips

        return run_flips(args)
    if args.once:
        from .app import run_once

        return run_once(args)
    from .app import run_app

    return run_app(args)


if __name__ == "__main__":
    sys.exit(main())
