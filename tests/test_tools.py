"""check.bat and flips.bat, run against the fake terminal (installed as the MetaTrader5 module)."""

import sys
from argparse import Namespace

import pytest

from opus_trader import llm as llm_mod
from opus_trader import tools
from opus_trader.fake_mt5 import FakeMT5

from .engine_env import NOW, market
from opus_trader.clock import SimClock

FIXTURE = __import__("pathlib").Path(__file__).parent / "fixtures" / "eurusd_m15_fixture.csv"


@pytest.fixture
def fake_terminal(monkeypatch, tmp_path):
    fake = FakeMT5(SimClock(NOW), market())
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setattr(tools, "ROOT", tmp_path)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr("opus_trader.clock.Clock", lambda: fake.clock)  # the terminal and the app share one clock in real life
    return fake


def test_check_runs_one_dry_cycle_and_sends_nothing(fake_terminal, monkeypatch, capsys, tmp_path):
    scripted = llm_mod.FakeLLM(script=[{"action": "HOLD", "order_type": None, "entry": None, "stop_loss": None, "take_profit": None,
                                        "confidence": 3, "setup": "No clear edge", "reasoning": "Waiting.", "invalidation": "A break."}])
    monkeypatch.setattr(llm_mod, "OpusClient", lambda key=None: scripted)
    code = tools.run_check(Namespace(verbose=False))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "OK Type                        Demo" in out
    assert "OK Margin mode                 Hedging" in out
    assert "IOC → FOK → RETURN" in out
    assert "Verdict                     HOLD" in out
    assert len(scripted.calls) == 1
    assert fake_terminal.sent == []  # nothing was sent to MT5
    assert (tmp_path / "data" / "check.sqlite").exists()


def test_check_refuses_a_real_account(fake_terminal, capsys):
    fake_terminal.trade_mode = fake_terminal.ACCOUNT_TRADE_MODE_REAL
    assert tools.run_check(Namespace(verbose=False)) == 1
    assert "real-money account" in capsys.readouterr().out


def test_flips_from_csv(capsys):
    assert tools.run_flips(Namespace(csv=str(FIXTURE), bars=5000, count=20)) == 0
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.strip().startswith("2026-")]
    assert len(rows) == 20
    assert all(("BUY" in r) or ("SELL" in r) for r in rows)
    assert "bar OPEN times in UTC" in out


def test_check_on_a_closed_market_uses_the_last_bar(fake_terminal, monkeypatch, capsys):
    from datetime import datetime, timezone

    fake_terminal.clock.set(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc))  # Saturday
    scripted = llm_mod.FakeLLM(script=[{"action": "HOLD", "order_type": None, "entry": None, "stop_loss": None, "take_profit": None,
                                        "confidence": 3, "setup": "Weekend", "reasoning": "Closed.", "invalidation": "n/a"}])
    monkeypatch.setattr(llm_mod, "OpusClient", lambda key=None: scripted)
    assert tools.run_check(Namespace(verbose=False)) == 0
    out = capsys.readouterr().out
    assert "A pre-check would normally skip this bar: Weekend window" in out
    bar_lines = [line for line in out.splitlines() if "Bar (UTC)" in line]
    # Index 0 is still Friday's 20:45 bar while the market is closed, so start_pos=1 gives 20:30, as on a real terminal.
    assert bar_lines and "2026-10-16 20:30" in bar_lines[0], bar_lines
