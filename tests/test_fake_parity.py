"""The stand-in terminal must look like the real MetaTrader5 package (5.0.6231, captured on Windows)."""

import json
from pathlib import Path

import opus_trader.fake_mt5 as fm
from opus_trader.fake_mt5 import FakeMT5, flat_market
from opus_trader.clock import SimClock

from .conftest import T0

REAL = json.loads((Path(__file__).parent / "fixtures" / "mt5_package_surface.json").read_text())


def test_record_fields_match_the_real_package():
    for name, fields in REAL["records"].items():
        fake = getattr(fm, name, None)
        if fake is None:
            continue  # BookInfo: not used
        if name == "SymbolInfo":  # 96 fields; the fake carries the ones the app reads (code reads them by name)
            assert set(fake._fields) <= set(fields), set(fake._fields) - set(fields)
        else:
            assert list(fake._fields) == fields, name


def test_constants_have_the_real_values():
    fake = FakeMT5(SimClock(T0), flat_market(T0, days=1))
    for name, value in REAL["constants"].items():
        if hasattr(fake, name):
            assert getattr(fake, name) == value, name


def test_every_function_the_app_calls_exists_in_the_real_package():
    import re

    used = set()
    for path in Path(fm.__file__).parent.rglob("*.py"):
        if path.name != "fake_mt5.py":
            used |= set(re.findall(r"\bself\.mt5\.([a-z_]+)\(", path.read_text()))
            used |= set(re.findall(r"\bmt5\.([a-z_]+)\(", path.read_text()))
    assert used and used <= set(REAL["functions"]), used - set(REAL["functions"])
