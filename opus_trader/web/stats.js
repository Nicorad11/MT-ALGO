/* Stats: Opus vs the indicator-only benchmark, equity with drawdown, R breakdowns. */
(() => {
  "use strict";
  const { $, $$, fmt, api } = window.Opus;
  const Opus = window.Opus;
  const LC = window.LightweightCharts;

  let period = "all";
  let chart = null, opusLine = null, benchLine = null, opusDD = null, benchDD = null;
  let last = null;

  const ROWS = [
    { label: "Net P&L", tip: null, f: (m, cur) => m.trades == null ? "—" : `<span class="${fmt.cls(m.net_pnl)}">${fmt.money(m.net_pnl, cur, true)}</span>` },
    { label: "Trades", f: (m) => m.trades ?? "—" },
    { label: "Win rate", f: (m) => m.win_rate == null ? "—" : `${fmt.num(m.win_rate, 1)}% <span class="muted sec">(${m.wins} of ${m.trades})</span>` },
    { label: "Avg R (win / loss)", tip: "Average result of winning trades and of losing trades, in R. R is profit or loss divided by the money risked at entry.",
      f: (m) => (m.avg_win_r == null && m.avg_loss_r == null) ? "—" : `<span class="up">${fmt.r(m.avg_win_r)}</span> / <span class="down">${fmt.r(m.avg_loss_r)}</span>` },
    { label: "Profit factor", tip: "Gross profit divided by gross loss. Above 1 means the winners paid for the losers.", f: (m) => m.profit_factor == null ? "—" : fmt.num(m.profit_factor, 2) },
    { label: "Expectancy", tip: "Average R per trade: what one trade made or lost on average, in units of risk.", f: (m) => m.expectancy_r == null ? "—" : `<span class="${fmt.cls(m.expectancy_r)}">${fmt.r(m.expectancy_r)}</span>` },
    { label: "Max drawdown", tip: "Largest fall from a peak in equity during the period.", f: (m, cur) => m.max_dd_pct == null ? "—" : `${fmt.pct(m.max_dd_pct)} <span class="muted sec">${m.max_dd_eur ? fmt.money(m.max_dd_eur, cur, true) : ""}</span>` },
    { label: "API cost", tip: "Claude API cost for the period, in US dollars.", f: (m) => m.api_cost == null ? '<span class="muted">—</span>' : `${fmt.usd(m.api_cost)}${m.cost_per_trade != null ? ` <span class="muted sec">${fmt.usd(m.cost_per_trade)}/trade</span>` : ""}` },
    { label: "With signal", tip: "Opus entries that went the same way as the Trend Signals trend on that bar.", f: (m) => !m.with_signal ? '<span class="muted">—</span>' : m.with_signal.total ? `${m.with_signal.agree} of ${m.with_signal.total} <span class="muted sec">(${fmt.num(m.with_signal.agree / m.with_signal.total * 100, 0)}%)</span>` : "—" },
  ];

  function renderMetrics(s) {
    const cur = s.currency || "EUR";
    $("#metrics tbody").innerHTML = ROWS.map((r) => `<tr><td>${r.tip ? `<span data-tip="${fmt.esc(r.tip)}">${r.label}</span>` : r.label}</td>
      <td class="r num">${r.f(s.opus, cur)}</td><td class="r num">${r.f(s.bench, cur)}</td></tr>`).join("");
    const mode = s.mode === "dry" ? "Showing dry-run results (simulated fills)" : "Showing live demo results";
    $("#stats-mode").textContent = mode;
  }

  function chartOptions() {
    const c = Opus.css.bind(Opus);
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: c("--surface") }, textColor: c("--muted"), fontFamily: "JetBrains Mono, monospace", fontSize: 11, attributionLogo: false,
                panes: { separatorColor: c("--border"), separatorHoverColor: c("--border") } },
      grid: { vertLines: { visible: false }, horzLines: { color: c("--border") } },
      rightPriceScale: { borderColor: c("--border") },
      timeScale: { borderColor: c("--border"), timeVisible: true, secondsVisible: false },
      crosshair: { mode: LC.CrosshairMode.Normal, vertLine: { color: c("--muted"), labelBackgroundColor: c("--border") }, horzLine: { color: c("--muted"), labelBackgroundColor: c("--border") } },
    };
  }

  function ensureChart() {
    if (chart) return;
    chart = LC.createChart($("#stats-chart"), chartOptions());
    const c = Opus.css.bind(Opus);
    const money = { type: "price", precision: 2, minMove: 0.01 };
    const pct = { type: "custom", formatter: (v) => `${fmt.num(v, 2)}%`, minMove: 0.01 };
    opusLine = chart.addSeries(LC.LineSeries, { color: c("--accent"), lineWidth: 2, priceFormat: money, priceLineVisible: false, title: "Opus" }, 0);
    benchLine = chart.addSeries(LC.LineSeries, { color: c("--bench"), lineWidth: 2, lineStyle: LC.LineStyle.Dashed, priceFormat: money, priceLineVisible: false, title: "Indicator" }, 0);
    opusDD = chart.addSeries(LC.AreaSeries, { lineColor: c("--accent"), topColor: "rgba(0,0,0,0)", bottomColor: tint(c("--loss"), 0.35), lineWidth: 1,
      invertFilledArea: true, priceFormat: pct, priceLineVisible: false, lastValueVisible: false, title: "Drawdown" }, 1);
    benchDD = chart.addSeries(LC.LineSeries, { color: c("--bench"), lineWidth: 1, lineStyle: LC.LineStyle.Dashed, priceFormat: pct, priceLineVisible: false, lastValueVisible: false }, 1);
    const panes = chart.panes();
    if (panes[1]) { panes[0].setStretchFactor(0.72); panes[1].setStretchFactor(0.28); }
  }

  function tint(hex, alpha) {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((x) => x + x).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }

  function restyle() {
    if (!chart) return;
    const c = Opus.css.bind(Opus);
    chart.applyOptions(chartOptions());
    opusLine.applyOptions({ color: c("--accent") });
    benchLine.applyOptions({ color: c("--bench") });
    opusDD.applyOptions({ lineColor: c("--accent"), bottomColor: tint(c("--loss"), 0.35) });
    benchDD.applyOptions({ color: c("--bench") });
  }

  function renderChart(s) {
    ensureChart();
    const eq = s.equity;
    const empty = !eq.opus.length || s.opus.trades === 0;
    opusLine.setData(eq.opus);
    benchLine.setData(eq.bench);
    opusDD.setData(eq.opus_dd);
    benchDD.setData(eq.bench_dd);
    const msg = $("#stats-empty");
    msg.hidden = !empty;
    if (empty) msg.textContent = `No closed trades in this period.${s.holds ? ` Opus has held for ${s.holds} bar${s.holds === 1 ? "" : "s"}.` : ""}`;
    if (eq.opus.length) chart.timeScale().fitContent();
  }

  function renderBars(el, items) {
    if (!items.length) { el.innerHTML = '<p class="empty">No closed trades yet.</p>'; return; }
    const max = Math.max(...items.map((i) => Math.abs(i.r)), 0.01);
    el.innerHTML = items.map((i) => {
      const w = (Math.abs(i.r) / max) * 50;
      const cls = i.r >= 0 ? "pos" : "neg";
      const tip = `${i.label}: ${fmt.r(i.r)} total over ${i.n} trade${i.n === 1 ? "" : "s"} (${fmt.r(i.r / i.n)} each on average)`;
      return `<div class="rbar" data-tip="${fmt.esc(tip)}"><span class="lbl">${fmt.esc(i.label)}</span>
        <span class="track"><span class="fill ${cls}" style="width:${w}%"></span></span>
        <span class="val num ${i.r >= 0 ? "up" : "down"}">${fmt.r(i.r)}</span><span class="n num muted">${i.n}</span></div>`;
    }).join("");
  }

  async function load() {
    try {
      const s = await api(`/api/stats?period=${period}`);
      last = s;
      renderMetrics(s);
      renderChart(s);
      renderBars($("#bars-setup"), s.by_setup);
      renderBars($("#bars-conf"), s.by_confidence.map((x) => ({ ...x, label: x.label === "—" ? "Not given" : `${x.label} of 5` })));
      renderBars($("#bars-session"), s.by_session);
      renderBars($("#bars-pair"), s.by_symbol || []);
    } catch (e) {
      $("#metrics tbody").innerHTML = `<tr><td colspan="3" class="muted">Couldn't load stats: ${fmt.esc(e.message)}</td></tr>`;
    }
  }

  Opus.views.stats = {
    init() {
      $$("#view-stats [data-period]").forEach((b) => b.addEventListener("click", () => {
        period = b.dataset.period;
        $$("#view-stats [data-period]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        load();
      }));
      Opus.on("theme", restyle);
      Opus.on("trade", () => { if (Opus.view === "stats") load(); });
    },
    show() { load(); },
  };
})();
