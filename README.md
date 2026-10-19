# Opus Trader

Claude Opus 5.5 trades the major forex pairs (EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD and NZDUSD) on a **MetaTrader 5 demo account**. After every M15 candle closes, Opus makes one decision per pair, and every input, decision, order and trade is recorded so you can audit how the model trades.

- The model never sends orders. It answers with a decision; a deterministic risk engine validates and sizes it, and is the only code that can place an order.
- Demo only. The app refuses to run on any account that isn't a demo hedging account, re-checks before every order, and only touches positions and orders carrying its magic number (551155). Your manual trades are never touched.
- At most 3 trades are open at once, one per pair, and the bot never doubles up on a currency (long EURUSD plus long GBPUSD would be two bets against the dollar, so the second is refused).
- Dry run is on by default: approved trades are simulated at live prices and nothing is sent to MT5.

---

## 0. Get Opus Trader

On the GitHub page, choose **Code → Download ZIP**, then right-click the ZIP → **Extract All…** and extract it to a short folder such as `C:\OpusTrader`. Very long folder paths (a nested folder in Downloads, for example) can make the first install fail on Windows. If Windows asks whether to run one of the `.bat` files, choose **Run** (or **More info → Run anyway**).

## 1. Set up MetaTrader 5

1. Install the OANDA TMS MetaTrader 5 terminal and log in to your **demo** account (server `OANDATMS-MT5`, hedging, EUR).
2. In the terminal, open **Tools → Options → Expert Advisors** and tick **Allow algorithmic trading**. Make sure the **Algo Trading** button in the toolbar is green.
3. The pairs don't need to be in Market Watch: Opus Trader adds them itself.
4. Keep the terminal open and logged in while Opus Trader runs. Opus Trader attaches to the running terminal; it never asks for or stores your MT5 password.

If you have more than one MT5 installed, set `MT5_TERMINAL_PATH` in `.env` to the `terminal64.exe` you want.

## 2. Your Anthropic API key

The first time you run `check.bat` or `start.bat`, Opus Trader asks for your API key in its console window (create one at console.anthropic.com → Settings → API keys; it starts with `sk-ant-`). Paste it and press Enter: it is saved to `.env` in the Opus Trader folder. To change it later, edit that file:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` holds only the API key (and optionally `MT5_TERMINAL_PATH`). It is ignored by git. If the key is missing or still `sk-ant-...`, an amber banner says so and every bar is a HOLD until you fix it and restart.

## 3. Check everything, then start

You need 64-bit Python 3.11–3.14 from python.org (3.13 or 3.14), with "Add python.exe to PATH" ticked if the installer offers it; the newer Python install manager works too. The launchers pick a suitable 64-bit Python by themselves and create a `.venv` folder with the dependencies on first use (about a minute, internet needed once).

1. **Double-click `check.bat`.** It attaches to the terminal and prints what Opus Trader relies on: account and server, demo and hedging, currency, Algo Trading, then one line per pair (the broker's name for it, bid, spread, digits, trading mode, lot limits, tick value, stops and freeze level, filling and expiration modes), and the detected broker time. Then it runs **one real dry-run cycle** on the first pair's last closed bar: a real snapshot, a real Opus call, the risk engine and `order_check` on the order it would send. Nothing is sent to MT5, and the cycle is stored in `data/check.sqlite`, not your journal.
2. **Double-click `start.bat`** to run the bot. The app opens in its own window. If the window can't open (no WebView2 runtime), the UI opens in your browser instead.

Closing the window asks **"Stop the bot and exit?"** Open positions keep their stop-loss and take-profit at the broker. Only one Opus Trader runs at a time: starting it again while it's open is refused, so two bots never manage the same account.

If your broker names a pair with a suffix (for example `EURUSD.pro`), Opus Trader finds and uses it; `check.bat` shows the name it picked. A pair the terminal doesn't have is skipped (an amber banner names it) and the others trade normally. The broker's server clock (New York + 7, or a European zone such as Warsaw time) is detected from live ticks, with daylight saving handled.

## 4. Pairs, and what they cost

**Settings → Pairs** switches pairs on and off (a pair with an open bot trade can't be switched off until the trade is closed). Each pair is decided separately, so each one is its own Opus call: with all seven pairs that is about 670 calls a day while the market is open, seven times the cost of one pair. The footer shows today's API cost, and the Journal shows the cost of every call. Opus decides up to 3 pairs at the same time (**Settings → Model → Parallel calls**), so a bar with seven pairs takes a minute or two.

Limits shared by all pairs:

- **Max open trades** (3): positions plus pending limit orders, across all pairs. Each pair holds at most one.
- **Max trades per currency** (1): long EURUSD and long GBPUSD are both short USD, so together they count 2 and the second is refused. Opposite directions cancel out: long EURUSD with long USDJPY is fine. Every snapshot tells Opus which directions are still open on that pair.
- **Daily loss limit**, **max trades per day** and the account equity cover all pairs together. The cooldown after a loss applies to the pair that lost.
- **Max spread** is checked per pair. GBPUSD, USDCHF, USDCAD and NZDUSD often run wider than EURUSD, especially outside London and New York hours, so those bars are skipped more often.

## 5. Dry run, and turning it off

With dry run on (the default), approved orders are checked with MT5's `order_check` and then filled on paper at the live bid/ask. Paper positions are closed by their stop-loss or take-profit (checked on live ticks, and on M1 candles for any time the app wasn't running; if one candle touches both, the stop is assumed), by Opus, or by the rules. Dry-run trades are tagged **DRY**, and the screens show the results of the mode you are in.

To trade the demo account for real, open **Settings** and switch **Dry run** off. A confirmation shows the account number and server read from MT5. The change applies from the next bar, and only while the bot is flat. You can also set `dry_run: false` under `app:` in `config.yaml` while the app is closed.

## 6. Stopping the bot

- **Stop** (top right) closes the bot's positions on every pair, cancels its pending orders and halts. **Resume** starts it again from the next bar.
- **Kill switch file:** create an empty file named `STOP` in the Opus Trader folder. Within a second the bot closes its positions, cancels its orders and halts. It stays halted while the file exists; delete it, then press Resume.
- **Pause** stops asking Opus for decisions without closing anything; open positions keep their stop-loss and take-profit.
- The bot also halts on its own when its losses today reach the daily loss limit (2% of start-of-day equity). It goes flat and resumes at 17:00 New York.

## 7. A short tour

**Top bar.** Status (Running, Paused, Halted with the reason, or MT5 disconnected), the DEMO and DRY RUN badges, a countdown to the next M15 close, Pause/Resume and Stop. Amber banners appear when MT5 is disconnected or Algo Trading is off; a red banner when the bot is halted, with when it resumes.

**Live.** Equity, today's P&L (since 17:00 New York), the open trades on all pairs with their results in R, and the maximum drawdown. Pick a pair above the chart: it shows that pair's last two days of closed M15 candles with the entry, stop-loss and take-profit as labelled lines and the bot's trades as markers; switch to Equity, or show the EMAs. The **Latest call** card shows that pair's latest decision: the action, Opus's confidence (five dots), the setup, the reasoning (Show more for the rest), the invalidation and, when the risk engine said no, a thin amber line with the reason (click the time to open it in the Journal). While Opus is deciding it shows which pairs it is thinking about. Below it, **Pairs** lists every pair with its latest decision and open trade; click one to show it. The footer shows the MT5 connection, the selected pair's spread, the next high-impact event and today's API cost.

**Journal.** Every decision and trade in one timeline, newest first, each labelled with its pair. Filter by Trades, Rejected or Holds, pick a pair, or search the reasoning. Expand an entry to see the full reasoning, a summary of how Opus thought it through (from the API's thinking summary), a summary of what Opus saw, the risk engine's verdict and calculation, every order with its MT5 retcode, the tokens, cost and time, and the exact prompt and response. Bars skipped by a pre-check (spread, news, weekend…) show as SKIP, labelled ALL when every pair was skipped; repeats are grouped. **Export CSV** exports what the filter shows.

**Stats.** Today, Week (since Sunday 17:00 New York), Month or All. Opus's results next to the **Indicator only** benchmark (see below): net P&L, trades, win rate, average win and loss in R, profit factor, expectancy, maximum drawdown and API cost. The equity curves of both, with drawdown shaded below, and R by setup, by confidence, by session and by pair.

**Settings.** Pairs, Risk, Schedule, News, Trend Signals and Model, edited in place. Values outside their limits are refused. Changes apply from the next bar, and every change is logged (the last few are listed at the bottom; all are in the database).

Hover the underlined terms (R, expectancy, profit factor, drawdown, ATR…) for a one-line explanation. Times are UTC throughout. The theme toggle is at the right of the navigation bar.

## 8. How a bar works

Every M15 close (+5 s):

1. **Pre-checks for the whole account**, each of which skips every pair and is logged once: kill switch or halt, paused, MT5 not ready (disconnected or Algo Trading off), the weekend window (Friday 19:00 UTC to Sunday 23:00 UTC), and the daily loss limit.
2. **Pre-checks per pair**: a news blackout (30 min before to 15 min after a high-impact event in one of the pair's two currencies in `events.yaml`: a USD event pauses every USD pair, an EUR event only EURUSD), and a spread above the limit.
3. **Snapshot** for each remaining pair: the last 150 M15, 100 H1 and 60 H4 closed candles (UTC), EMA 20/50/200, RSI 14, ATR 14 and Bollinger 20/2 on each timeframe, the previous trading day's high/low/close, the Asian range, the session, bid/ask and spread, balance and equity, the pair's open position, the bot's trades on the other pairs and its net exposure per currency, today's P&L, the pair's last 10 trades in R and Opus's last 3 decisions on it, last bar's rejection, the next 4 hours of events for its currencies, the risk state (including which directions are still allowed), and the Trend Signals indicator.
4. **Opus** answers through one tool, `submit_decision` (action, order type, entry, stop-loss, take-profit, confidence, setup, reasoning, invalidation). HOLD is the default. If the call fails, times out or doesn't decide, the bar is a HOLD for that pair and the reason is logged.
5. **Risk engine**, one answer at a time so two pairs can never both take the last free slot: stop-loss and take-profit required; stop between 0.5× and 3× ATR(14); reward:risk at least 1.5; spread, stops level and freeze level; one trade per pair and 3 across all pairs; one trade per currency direction; 6 entries a day; 2 bars of cooldown on a pair after a loss on it; stops only tighten; no entries after Friday 19:00 UTC; positions closed by Friday 20:30 UTC. Size is 0.5% of equity from the stop distance and the pair's tick value (converted to EUR, so a JPY pair risks the same), rounded down to the volume step. If even the minimum lot would risk more than that, the trade is rejected rather than oversized.
6. **Execution and log**: every request and MT5 result (retcode and comment) is stored, and MT5 is reconciled each bar as the source of truth.

All limits live in `config.yaml` and in Settings.

## 9. Trend Signals indicator and the benchmark

`opus_trader/indicators/trend_signals.py` ports the signal-driving part of your TradingView indicator: an ATR trailing stop on `hl2` (ATR 14 × 2.0, Wilder's RMA or SMA), whose trend flips when a close crosses the previous bar's line. The cloud (EMA of highs and lows, 10) and MACD (12, 26, 9) state are passed along as context. RSI(9), CMO, the pivot levels and the percent-based stop/target are not used.

Opus sees the trend, bars since the last flip, the trailing line as a natural stop (with its distance in pips and ATR, and 1R/2R/3R targets), the cloud state, and the last 5 flips with how the indicator alone did. In Settings → Trend Signals, **Signal mode** is `input` (Opus may use or ignore it) or `filter` (entries only in the direction of a flip within the last N bars; HOLD, CLOSE and MOVE_STOP are always allowed).

The **Indicator only** benchmark trades every flip on every pair on its own: entry at the flip candle's close, stop on the trailing line, trailed with the line, exit on a touch or the opposite flip, the same risk per trade, the spread included. It lives only in the database and is never sent to MT5. Each decision is marked With signal, Against signal or Ignored signal.

**Compare with TradingView:** double-click `flips.bat` to print the last 20 flips with their UTC bar times, computed on 5000 closed M15 bars of the first pair from MT5 (`.venv\Scripts\python -m opus_trader flips --symbol GBPUSD` for another pair). Set your TradingView chart's timezone to UTC to compare. You can also run it on bars exported from TradingView: `.venv\Scripts\python -m opus_trader flips --csv export.csv`. For an automatic check, put a TradingView "Export chart data" file (with the indicator on the chart) at `tests/fixtures/tradingview_export.csv`; the test suite then compares every flip after the first 1000 bars.

## 10. Preview without MT5

`.venv\Scripts\python -m opus_trader --sim` runs the whole app against a simulated terminal quoting all seven pairs and a rule-based stand-in for Opus, with two days of generated history (`--seed-days`). Nothing is real: the badge reads **SIM**, and it uses its own folder, `data/sim/`, so your journal and `config.yaml` are untouched.

## Files and data

| Path | What |
|---|---|
| `config.yaml` | All settings, with a comment for each. The Settings screen edits it. |
| `events.yaml` | High-impact events for the news blackout, re-read every bar. Pre-filled with the Fed, ECB, payrolls and CPI dates through 17 December 2026 from the official calendars, so it covers USD and EUR events only; add Bank of England, BoJ, SNB, RBA, Bank of Canada and RBNZ dates (currency GBP, JPY, CHF, AUD, CAD, NZD) if you want blackouts for those too. |
| `data/opus_trader.sqlite` | The record: decisions per pair (full snapshot, exact prompt, raw response, tokens, cost, verdict), orders with retcodes, trades in EUR and R, equity per bar, the benchmark, engine events and every settings change. A database from the one-pair version is upgraded in place on first start; its rows are labelled EURUSD. |
| `data/logs/opus_trader.log` | Rotating text log. |
| `STOP` | Create it to trigger the kill switch. |
| `wheels/` | A pre-built copy of a small pywebview dependency that PyPI only ships as source, so the first install needs no build step. |

## Development

```
python -m venv .venv && .venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

The tests run without MT5 or an API key: `opus_trader/fake_mt5.py` mimics the `MetaTrader5` module (functions, constants, server-time quirks and retcodes), and the model is scripted or reached through the real Anthropic SDK over a mocked HTTP transport. The UI is plain HTML, CSS and JavaScript in `opus_trader/web/`, with Lightweight Charts and the Inter and JetBrains Mono fonts bundled locally, so it works offline.

Charts by [TradingView Lightweight Charts™](https://www.tradingview.com/) (Apache 2.0). Inter and JetBrains Mono are under the SIL Open Font License.
