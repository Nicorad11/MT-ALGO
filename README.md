# Opus Trader

Claude Opus 5.5 trades EUR/USD on a **MetaTrader 5 demo account**, one decision per closed M15 candle, and every input, decision, order and trade is recorded so you can audit how the model trades.

- The model never sends orders. It answers with a decision; a deterministic risk engine validates and sizes it, and is the only code that can place an order.
- Demo only. The app refuses to run on any account that isn't a demo hedging account, re-checks before every order, and only touches positions and orders carrying its magic number (551155). Your manual trades are never touched.
- Dry run is on by default: approved trades are simulated at live prices and nothing is sent to MT5.

---

## 1. Set up MetaTrader 5

1. Install the OANDA TMS MetaTrader 5 terminal and log in to your **demo** account (server `OANDATMS-MT5`, hedging, EUR).
2. In the terminal, open **Tools → Options → Expert Advisors** and tick **Allow algorithmic trading**. Make sure the **Algo Trading** button in the toolbar is green.
3. Add **EURUSD** to Market Watch (Opus Trader also selects it automatically).
4. Keep the terminal open and logged in while Opus Trader runs. Opus Trader attaches to the running terminal; it never asks for or stores your MT5 password.

If you have more than one MT5 installed, set `MT5_TERMINAL_PATH` in `.env` to the `terminal64.exe` you want.

## 2. Add your Anthropic API key

Copy `.env.example` to `.env` and put your key in it:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` holds only the API key (and optionally `MT5_TERMINAL_PATH`). It is ignored by git. If the key is missing or still `sk-ant-...`, an amber banner says so and every bar is a HOLD until you fix it and restart.

## 3. Check everything, then start

You need 64-bit Python 3.11–3.14 from python.org (3.13 recommended), with "Add python.exe to PATH" ticked. The launchers pick a suitable 64-bit Python by themselves and create a `.venv` folder with the dependencies on first use (about a minute, internet needed once).

1. **Double-click `check.bat`.** It attaches to the terminal and prints what Opus Trader relies on: account and server, demo and hedging, currency, Algo Trading, the EURUSD specification (tick value and size, volume min/step/max, stops and freeze level, filling and expiration modes), and the detected broker time. Then it runs **one real dry-run cycle** on the last closed bar: a real snapshot, a real Opus call, the risk engine and `order_check` on the order it would send. Nothing is sent to MT5, and the cycle is stored in `data/check.sqlite`, not your journal.
2. **Double-click `start.bat`** to run the bot. The app opens in its own window. If the window can't open (no WebView2 runtime), the UI opens in your browser instead.

Closing the window asks **"Stop the bot and exit?"** Open positions keep their stop-loss and take-profit at the broker. Only one Opus Trader runs at a time: starting it again while it's open is refused, so two bots never manage the same account.

If your broker names the symbol with a suffix (for example `EURUSD.pro`), Opus Trader finds and uses it; `check.bat` shows the name it picked. The broker's server clock (New York + 7, or a European zone such as Warsaw time) is detected from live ticks, with daylight saving handled.

## 4. Dry run, and turning it off

With dry run on (the default), approved orders are checked with MT5's `order_check` and then filled on paper at the live bid/ask. Paper positions are closed by their stop-loss or take-profit (checked on live ticks, and on M1 candles for any time the app wasn't running; if one candle touches both, the stop is assumed), by Opus, or by the rules. Dry-run trades are tagged **DRY**, and the screens show the results of the mode you are in.

To trade the demo account for real, open **Settings** and switch **Dry run** off. A confirmation shows the account number and server read from MT5. The change applies from the next bar, and only while the bot is flat. You can also set `dry_run: false` under `app:` in `config.yaml` while the app is closed.

## 5. Stopping the bot

- **Stop** (top right) closes the bot's positions, cancels its pending orders and halts. **Resume** starts it again from the next bar.
- **Kill switch file:** create an empty file named `STOP` in the Opus Trader folder. Within a second the bot closes its positions, cancels its orders and halts. It stays halted while the file exists; delete it, then press Resume.
- **Pause** stops asking Opus for decisions without closing anything; open positions keep their stop-loss and take-profit.
- The bot also halts on its own when its losses today reach the daily loss limit (2% of start-of-day equity). It goes flat and resumes at 17:00 New York.

## 6. A short tour

**Top bar.** Status (Running, Paused, Halted with the reason, or MT5 disconnected), the DEMO and DRY RUN badges, a countdown to the next M15 close, Pause/Resume and Stop. Amber banners appear when MT5 is disconnected or Algo Trading is off; a red banner when the bot is halted, with when it resumes.

**Live.** Equity, today's P&L (since 17:00 New York), the open position with its result in R, and the maximum drawdown. The chart shows the last two days of closed M15 candles with the entry, stop-loss and take-profit as labelled lines and the bot's trades as markers; switch to Equity, or show the EMAs. The **Latest call** card shows the action, Opus's confidence (five dots), the setup, the reasoning (Show more for the rest), the invalidation and, when the risk engine said no, a thin amber line with the reason. While Opus is deciding it shows "Opus is thinking… 12s". Below it, the last 8 decisions; click one to open it in the Journal. The footer shows the MT5 connection, the spread, the next high-impact event and today's API cost.

**Journal.** Every decision and trade in one timeline, newest first. Filter by Trades, Rejected or Holds, or search the reasoning. Expand an entry to see the full reasoning, a summary of what Opus saw, the risk engine's verdict and calculation, every order with its MT5 retcode, the tokens, cost and time, and the exact prompt and response. Bars skipped by a pre-check (spread, news, weekend…) show as SKIP; repeats are grouped. **Export CSV** exports what the filter shows.

**Stats.** Today, Week (since Sunday 17:00 New York), Month or All. Opus's results next to the **Indicator only** benchmark (see below): net P&L, trades, win rate, average win and loss in R, profit factor, expectancy, maximum drawdown and API cost. The equity curves of both, with drawdown shaded below, and R by setup, by confidence and by session.

**Settings.** Risk, Schedule, News, Trend Signals and Model, edited in place. Values outside their limits are refused. Changes apply from the next bar, and every change is logged (the last few are listed at the bottom; all are in the database).

Hover the underlined terms (R, expectancy, profit factor, drawdown, ATR…) for a one-line explanation. Times are UTC throughout. The theme toggle is at the right of the navigation bar.

## 7. How a bar works

Every M15 close (+5 s):

1. **Pre-checks**, each of which skips the model call and is logged: kill switch or halt, paused, MT5 not ready (disconnected or Algo Trading off), the weekend window (Friday 19:00 UTC to Sunday 23:00 UTC), the daily loss limit, a news blackout (30 min before to 15 min after a high-impact USD or EUR event in `events.yaml`), and a spread above 1.5 pips.
2. **Snapshot**: the last 150 M15, 100 H1 and 60 H4 closed candles (UTC), EMA 20/50/200, RSI 14, ATR 14 and Bollinger 20/2 on each timeframe, the previous trading day's high/low/close, the Asian range, the session, bid/ask and spread, balance and equity, the open position, today's P&L, the last 10 trades in R, Opus's last 3 decisions, last bar's rejection, the next 4 hours of events, the risk state, and the Trend Signals indicator.
3. **Opus** answers through one tool, `submit_decision` (action, order type, entry, stop-loss, take-profit, confidence, setup, reasoning, invalidation). HOLD is the default. If the call fails, times out or doesn't decide, the bar is a HOLD and the reason is logged.
4. **Risk engine**: stop-loss and take-profit required; stop between 0.5× and 3× ATR(14); reward:risk at least 1.5; spread, stops level and freeze level; one position at most; 6 entries a day; 2 bars of cooldown after a loss; stops only tighten; no entries after Friday 19:00 UTC; positions closed by Friday 20:30 UTC. Size is 0.5% of equity from the stop distance and the symbol's tick value, rounded down to the volume step. If even the minimum lot would risk more than that, the trade is rejected rather than oversized.
5. **Execution and log**: every request and MT5 result (retcode and comment) is stored, and MT5 is reconciled each bar as the source of truth.

All limits live in `config.yaml` and in Settings.

## 8. Trend Signals indicator and the benchmark

`opus_trader/indicators/trend_signals.py` ports the signal-driving part of your TradingView indicator: an ATR trailing stop on `hl2` (ATR 14 × 2.0, Wilder's RMA or SMA), whose trend flips when a close crosses the previous bar's line. The cloud (EMA of highs and lows, 10) and MACD (12, 26, 9) state are passed along as context. RSI(9), CMO, the pivot levels and the percent-based stop/target are not used.

Opus sees the trend, bars since the last flip, the trailing line as a natural stop (with its distance in pips and ATR, and 1R/2R/3R targets), the cloud state, and the last 5 flips with how the indicator alone did. In Settings → Trend Signals, **Signal mode** is `input` (Opus may use or ignore it) or `filter` (entries only in the direction of a flip within the last N bars; HOLD, CLOSE and MOVE_STOP are always allowed).

The **Indicator only** benchmark trades every flip on its own: entry at the flip candle's close, stop on the trailing line, trailed with the line, exit on a touch or the opposite flip, the same risk per trade, the spread included. It lives only in the database and is never sent to MT5. Each decision is marked With signal, Against signal or Ignored signal.

**Compare with TradingView:** double-click `flips.bat` to print the last 20 flips with their UTC bar times, computed on 5000 closed M15 bars from MT5. Set your TradingView chart's timezone to UTC to compare. You can also run it on bars exported from TradingView: `.venv\Scripts\python -m opus_trader flips --csv export.csv`. For an automatic check, put a TradingView "Export chart data" file (with the indicator on the chart) at `tests/fixtures/tradingview_export.csv`; the test suite then compares every flip after the first 1000 bars.

## 9. Preview without MT5

`.venv\Scripts\python -m opus_trader --sim` runs the whole app against a simulated terminal and a rule-based stand-in for Opus, with two days of generated history (`--seed-days`). Nothing is real: the badge reads **SIM**, and it uses its own folder, `data/sim/`, so your journal and `config.yaml` are untouched.

## Files and data

| Path | What |
|---|---|
| `config.yaml` | All settings, with a comment for each. The Settings screen edits it. |
| `events.yaml` | High-impact events for the news blackout, re-read every bar. Pre-filled with the Fed, ECB, payrolls and CPI dates through 17 December 2026 from the official calendars; add later dates before then. |
| `data/opus_trader.sqlite` | The record: decisions (full snapshot, exact prompt, raw response, tokens, cost, verdict), orders with retcodes, trades in EUR and R, equity per bar, the benchmark, engine events and every settings change. |
| `data/logs/opus_trader.log` | Rotating text log. |
| `STOP` | Create it to trigger the kill switch. |

## Development

```
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The tests run without MT5 or an API key: `opus_trader/fake_mt5.py` mimics the `MetaTrader5` module (functions, constants, server-time quirks and retcodes), and the model is scripted or reached through the real Anthropic SDK over a mocked HTTP transport. The UI is plain HTML, CSS and JavaScript in `opus_trader/web/`, with Lightweight Charts and the Inter and JetBrains Mono fonts bundled locally, so it works offline.

Charts by [TradingView Lightweight Charts™](https://www.tradingview.com/) (Apache 2.0). Inter and JetBrains Mono are under the SIL Open Font License.
