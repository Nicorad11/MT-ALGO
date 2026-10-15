"""Configuration schema, config.yaml load/save, and validated setting changes.

The schema is the single source of truth for defaults, min/max limits, labels
and tooltips. The Settings screen is generated from it and config.yaml is
written from it, so the file always carries a comment for every key.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def ui(label: str, unit: str = "", tip: str = "", editable: bool = True, step: float | None = None) -> dict:
    return {"label": label, "unit": unit, "tip": tip, "editable": editable, "step": step}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RiskConfig(_Section):
    risk_per_trade_pct: float = Field(
        0.5, ge=0.1, le=2.0, description="Equity risked per trade, in percent.",
        json_schema_extra=ui("Risk per trade", "%", "Share of equity lost if the stop-loss is hit.", step=0.05),
    )
    daily_loss_limit_pct: float = Field(
        2.0, ge=0.5, le=5.0, description="Bot loss (realized + open) that halts trading until 17:00 New York, in percent of start-of-day equity.",
        json_schema_extra=ui("Daily loss limit", "%", "When the bot's losses today reach this share of start-of-day equity, it goes flat and halts until 17:00 New York.", step=0.1),
    )
    max_open_positions: int = Field(
        1, ge=1, le=1, description="Maximum open bot positions (fixed at 1).",
        json_schema_extra=ui("Max open positions", "", editable=False),
    )
    max_trades_per_day: int = Field(
        6, ge=1, le=20, description="Maximum new entries per trading day (17:00 to 17:00 New York).",
        json_schema_extra=ui("Max trades per day", "", "Counted per trading day, which rolls over at 17:00 New York."),
    )
    min_stop_atr: float = Field(
        0.5, ge=0.2, le=5.0, description="Minimum stop distance as a multiple of ATR(14) on M15.",
        json_schema_extra=ui("Min stop distance", "× ATR", "ATR is the average bar range over the last 14 M15 bars.", step=0.1),
    )
    max_stop_atr: float = Field(
        3.0, ge=0.2, le=5.0, description="Maximum stop distance as a multiple of ATR(14) on M15.",
        json_schema_extra=ui("Max stop distance", "× ATR", "ATR is the average bar range over the last 14 M15 bars.", step=0.1),
    )
    min_reward_risk: float = Field(
        1.5, ge=1.0, le=5.0, description="Minimum take-profit distance divided by stop distance.",
        json_schema_extra=ui("Min reward:risk", "", "Take-profit distance divided by stop-loss distance.", step=0.1),
    )
    max_spread_pips: float = Field(
        1.5, ge=0.5, le=5.0, description="Skip the bar when the spread is wider than this, in pips.",
        json_schema_extra=ui("Max spread", "pips", "A pip is 0.0001 on EUR/USD.", step=0.1),
    )
    cooldown_bars_after_loss: int = Field(
        2, ge=0, le=12, description="Bars to wait before a new entry after a losing trade.",
        json_schema_extra=ui("Cooldown after a loss", "bars"),
    )
    limit_expiry_bars: int = Field(
        4, ge=1, le=16, description="Limit orders expire after this many M15 bars.",
        json_schema_extra=ui("Limit order expiry", "bars"),
    )
    deviation_points: int = Field(
        10, ge=0, le=100, description="Maximum slippage for market orders, in points (10 points = 1 pip).",
        json_schema_extra=ui("Max slippage", "points", "10 points equal 1 pip on EUR/USD."),
    )

    @model_validator(mode="after")
    def _stop_range(self) -> "RiskConfig":
        if self.min_stop_atr >= self.max_stop_atr:
            raise ValueError("Min stop distance must be below max stop distance")
        return self


class ScheduleConfig(_Section):
    friday_no_entries_utc: str = Field(
        "19:00", description="No new entries after this time on Friday (UTC, HH:MM).",
        json_schema_extra=ui("No new entries after", "Fri UTC"),
    )
    friday_flat_utc: str = Field(
        "20:30", description="Close bot positions and cancel bot orders at this time on Friday (UTC, HH:MM).",
        json_schema_extra=ui("Flat by", "Fri UTC"),
    )
    sunday_resume_utc: str = Field(
        "23:00", description="Model calls resume at this time on Sunday (UTC, HH:MM).",
        json_schema_extra=ui("Resume after weekend", "Sun UTC"),
    )

    @field_validator("friday_no_entries_utc", "friday_flat_utc", "sunday_resume_utc")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError("Use HH:MM, 24-hour")
        return v

    @model_validator(mode="after")
    def _order(self) -> "ScheduleConfig":
        if self.friday_flat_utc < self.friday_no_entries_utc:
            raise ValueError("Flat-by time must be after the no-new-entries time")
        return self


class NewsConfig(_Section):
    blackout_before_min: int = Field(
        30, ge=0, le=120, description="Minutes before a high-impact event with no model calls.",
        json_schema_extra=ui("Blackout before event", "min"),
    )
    blackout_after_min: int = Field(
        15, ge=0, le=120, description="Minutes after a high-impact event with no model calls.",
        json_schema_extra=ui("Blackout after event", "min"),
    )
    events_file: str = Field(
        "events.yaml", description="High-impact events file (relative to the project folder).",
        json_schema_extra=ui("Events file", editable=False),
    )
    currencies: list[str] = Field(
        default_factory=lambda: ["USD", "EUR"], description="Currencies whose high-impact events trigger a blackout.",
        json_schema_extra=ui("Currencies", editable=False),
    )


class SignalConfig(_Section):
    mode: Literal["input", "filter"] = Field(
        "input", description="input: the model may use or ignore the Trend Signals indicator. filter: entries only in the direction of a recent flip.",
        json_schema_extra=ui("Signal mode", "", "Input: Opus may use or ignore the indicator. Filter: Opus may only open trades in the direction of a recent flip."),
    )
    filter_bars: int = Field(
        2, ge=1, le=10, description="In filter mode, a flip counts as recent if it happened within this many closed bars.",
        json_schema_extra=ui("Recent flip window", "bars", "In filter mode, entries must follow a flip on one of the last N closed bars."),
    )
    atr_length: int = Field(
        14, ge=2, le=100, description="Trend Signals ATR length.",
        json_schema_extra=ui("ATR length", "bars"),
    )
    multiplier: float = Field(
        2.0, ge=0.5, le=10.0, description="Trend Signals ATR multiplier for the trailing line.",
        json_schema_extra=ui("ATR multiplier", "×", step=0.1),
    )
    atr_method: Literal[1, 2] = Field(
        1, description="1 = Wilder's RMA of true range (Pine ta.atr). 2 = simple moving average of true range.",
        json_schema_extra=ui("ATR method", "", "1 uses Wilder's smoothing like TradingView's ta.atr. 2 uses a simple average."),
    )


class ModelConfig(_Section):
    model: str = Field(
        "claude-opus-5-5", description="Anthropic model ID.",
        json_schema_extra=ui("Model", editable=False),
    )
    effort: Literal["low", "medium", "high", "xhigh", "max"] = Field(
        "high", description="Reasoning effort per decision. Higher is more thorough and costs more.",
        json_schema_extra=ui("Effort", "", "How much reasoning Opus does per bar. Higher effort costs more."),
    )
    timeout_s: int = Field(
        120, ge=30, le=300, description="Seconds to wait for a decision before treating the bar as HOLD.",
        json_schema_extra=ui("Timeout", "s"),
    )
    max_tokens: int = Field(
        16000, ge=4000, le=64000, description="Maximum output tokens per decision (thinking included).",
        json_schema_extra=ui("Max output tokens", "", editable=False),
    )
    price_input_per_mtok: float = Field(4.0, ge=0, description="USD per million uncached input tokens.", json_schema_extra=ui("Input price", "$/MTok", editable=False))
    price_output_per_mtok: float = Field(20.0, ge=0, description="USD per million output tokens.", json_schema_extra=ui("Output price", "$/MTok", editable=False))
    price_cache_read_per_mtok: float = Field(0.20, ge=0, description="USD per million cached input tokens read.", json_schema_extra=ui("Cache read price", "$/MTok", editable=False))
    price_cache_write_1h_per_mtok: float = Field(8.0, ge=0, description="USD per million input tokens written to the 1-hour cache.", json_schema_extra=ui("Cache write price", "$/MTok", editable=False))


class AppConfig(_Section):
    symbol: str = Field("EURUSD", description="Symbol to trade.", json_schema_extra=ui("Symbol", editable=False))
    magic: int = Field(551155, ge=1, description="Magic number on every bot order. Positions without it are never touched.", json_schema_extra=ui("Magic number", editable=False))
    dry_run: bool = Field(True, description="true: simulated fills, no orders are sent to MT5.", json_schema_extra=ui("Dry run", editable=False))
    bar_delay_s: int = Field(5, ge=1, le=60, description="Seconds after the M15 close before building the snapshot.", json_schema_extra=ui("Bar delay", "s", editable=False))
    server_port: int = Field(8765, ge=1024, le=65535, description="Local port for the UI server (127.0.0.1 only).", json_schema_extra=ui("UI port", editable=False))


class Config(_Section):
    app: AppConfig = Field(default_factory=AppConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


SECTION_TITLES = {
    "app": "App",
    "risk": "Risk",
    "schedule": "Schedule",
    "news": "News",
    "signal": "Trend Signals",
    "model": "Model",
}


class SettingsError(ValueError):
    """A settings change failed validation. `field` is the dotted key."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        cfg = Config()
        save_config(cfg, path)
        return cfg
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def _yaml_scalar(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    return text.removesuffix("\n...").strip()


def dump_config(cfg: Config) -> str:
    lines = [
        "# Opus Trader configuration.",
        "# Edited by the Settings screen as well; changes apply from the next bar.",
        "# Every change made in the app is also logged in data/opus_trader.sqlite (settings_log).",
        "",
    ]
    for section, title in SECTION_TITLES.items():
        model: BaseModel = getattr(cfg, section)
        lines.append(f"# --- {title} ---")
        lines.append(f"{section}:")
        for name, field in type(model).model_fields.items():
            if field.description:
                lines.append(f"  # {field.description}")
            lines.append(f"  {name}: {_yaml_scalar(getattr(model, name))}")
        lines.append("")
    return "\n".join(lines)


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(dump_config(cfg), encoding="utf-8")
    tmp.replace(path)


def field_specs(cfg: Config) -> list[dict]:
    """Describe every setting for the Settings screen."""
    specs = []
    for section, title in SECTION_TITLES.items():
        model: BaseModel = getattr(cfg, section)
        for name, field in type(model).model_fields.items():
            extra = dict(field.json_schema_extra or {})
            spec = {
                "key": f"{section}.{name}",
                "section": section,
                "section_title": title,
                "label": extra.get("label", name),
                "unit": extra.get("unit", ""),
                "tip": extra.get("tip", ""),
                "editable": extra.get("editable", True),
                "step": extra.get("step"),
                "value": getattr(model, name),
                "description": field.description or "",
            }
            for meta in field.metadata:
                if hasattr(meta, "ge"):
                    spec["min"] = meta.ge
                if hasattr(meta, "le"):
                    spec["max"] = meta.le
            annotation = field.annotation
            if getattr(annotation, "__origin__", None) is Literal:
                spec["type"] = "choice"
                spec["choices"] = list(annotation.__args__)
            elif annotation is bool:
                spec["type"] = "bool"
            elif annotation is int:
                spec["type"] = "int"
            elif annotation is float:
                spec["type"] = "float"
            elif isinstance(spec["value"], str) and _HHMM.match(spec["value"]):
                spec["type"] = "time"
            else:
                spec["type"] = "text"
            specs.append(spec)
    return specs


def apply_changes(cfg: Config, changes: dict[str, Any], allow_locked: bool = False) -> tuple[Config, list[tuple[str, Any, Any]]]:
    """Return a new validated Config with `changes` ({"risk.max_spread_pips": 1.2}) applied.

    Raises SettingsError naming the offending key. Returns (new_config, [(key, old, new), ...]).
    """
    editable = {s["key"]: s for s in field_specs(cfg)}
    data = cfg.model_dump()
    diffs = []
    for key, value in changes.items():
        spec = editable.get(key)
        if spec is None:
            raise SettingsError(key, "Unknown setting")
        if not spec["editable"] and not allow_locked:
            raise SettingsError(key, "This setting can't be changed here")
        section, name = key.split(".", 1)
        old = data[section][name]
        data[section][name] = value
        if old != value:
            diffs.append((key, old, value))
    try:
        new_cfg = Config.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = [str(p) for p in err.get("loc", ())]
        key = ".".join(loc[:2]) if len(loc) >= 2 else (next(iter(changes)) if changes else "")
        if key not in editable and changes:
            key = next(iter(changes))
        spec = editable.get(key, {})
        if "min" in spec and "max" in spec and err.get("type", "").startswith(("greater", "less")):
            message = f"Must be between {spec['min']} and {spec['max']}"
        else:
            message = err.get("msg", "Invalid value").removeprefix("Value error, ")
        raise SettingsError(key, message) from exc
    # Normalise logged values to what validation produced (e.g. "1.2" -> 1.2).
    new_data = new_cfg.model_dump()
    diffs = [(k, old, new_data[k.split(".")[0]][k.split(".")[1]]) for k, old, _ in diffs]
    return new_cfg, diffs
