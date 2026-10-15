/* Live screen: KPIs, M15 chart, latest call, recent decisions, status footer. */
(() => {
  "use strict";
  const { $, $$, fmt, api } = window.Opus;
  const Opus = window.Opus;
  const LC = window.LightweightCharts;

  let chart = null, candles = null, equityLine = null, emaSeries = [], markersApi = null, bidLine = null;
  let priceLines = [];
  let mode = "price";
  let lastCandles = null;
  let thinkingSince = null;
  let candleTimer = null;

  // ------------------------------------------------------------ chart
  function chartOptions() {
    const c = Opus.css.bind(Opus);
    return {
      autoSize: true,
      layout: { background: { type: "solid", color: c("--surface") }, textColor: c("--muted"), fontFamily: "JetBrains Mono, monospace", fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: c("--border") } },
      rightPriceScale: { borderColor: c("--border") },
      timeScale: { borderColor: c("--border"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: LC.CrosshairMode.Normal, vertLine: { color: c("--muted"), labelBackgroundColor: c("--border") }, horzLine: { color: c("--muted"), labelBackgroundColor: c("--border") } },
      localization: { priceFormatter: (p) => p.toFixed(5) },
    };
  }

  function ensureChart() {
    if (chart) return;
    const el = $("#live-chart");
    chart = LC.createChart(el, chartOptions());
    const c = Opus.css.bind(Opus);
    candles = chart.addSeries(LC.CandlestickSeries, {
      upColor: c("--profit"), downColor: c("--loss"), borderVisible: false, wickUpColor: c("--profit"), wickDownColor: c("--loss"),
      priceFormat: { type: "price", precision: 5, minMove: 0.00001 }, lastValueVisible: false, priceLineVisible: false,
    });
    markersApi = LC.createSeriesMarkers(candles, []);
    const emaColors = [c("--text"), c("--accent"), c("--muted")];
    emaSeries = [0, 1, 2].map((i) => chart.addSeries(LC.LineSeries, { color: emaColors[i], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: false }));
    document.documentElement.style.setProperty("--ema20", emaColors[0]);
    document.documentElement.style.setProperty("--ema50", emaColors[1]);
    document.documentElement.style.setProperty("--ema200", emaColors[2]);
    equityLine = chart.addSeries(LC.LineSeries, { color: c("--accent"), lineWidth: 2, visible: false, priceLineVisible: false,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
    chart.subscribeCrosshairMove(onCrosshair);
  }

  function onCrosshair(param) {
    const box = $("#ohlc");
    if (!box) return;
    if (!param || !param.time || mode !== "price") { box.textContent = ""; return; }
    const d = param.seriesData.get(candles);
    if (!d) { box.textContent = ""; return; }
    box.textContent = `O ${d.open.toFixed(5)}  H ${d.high.toFixed(5)}  L ${d.low.toFixed(5)}  C ${d.close.toFixed(5)}`;
  }

  function restyle() {
    if (!chart) return;
    chart.applyOptions(chartOptions());
    const c = Opus.css.bind(Opus);
    candles.applyOptions({ upColor: c("--profit"), downColor: c("--loss"), wickUpColor: c("--profit"), wickDownColor: c("--loss") });
    const emaColors = [c("--text"), c("--accent"), c("--muted")];
    emaSeries.forEach((s, i) => s.applyOptions({ color: emaColors[i] }));
    ["--ema20", "--ema50", "--ema200"].forEach((v, i) => document.documentElement.style.setProperty(v, emaColors[i]));
    equityLine.applyOptions({ color: c("--accent") });
    if (lastCandles) drawOverlays(lastCandles);
  }

  function drawOverlays(data) {
    const c = Opus.css.bind(Opus);
    priceLines.forEach((pl) => candles.removePriceLine(pl));
    priceLines = [];
    const styles = {
      entry: { color: c("--text"), title: "Entry", lineStyle: LC.LineStyle.Dashed },
      sl: { color: c("--loss"), title: "SL", lineStyle: LC.LineStyle.Solid },
      tp: { color: c("--profit"), title: "TP", lineStyle: LC.LineStyle.Solid },
      limit: { color: c("--accent"), title: "Limit", lineStyle: LC.LineStyle.Dashed },
    };
    for (const l of data.lines || []) {
      const s = styles[l.kind];
      if (!s) continue;
      priceLines.push(candles.createPriceLine({ price: l.price, color: s.color, lineWidth: 1, lineStyle: s.lineStyle, axisLabelVisible: true, title: s.title }));
    }
    const markers = (data.markers || []).map((m) => {
      if (m.kind === "entry") {
        // The arrow's shape and side say long or short; only exits carry a label (the result in R).
        return m.side === "long"
          ? { time: m.time, position: "belowBar", shape: "arrowUp", color: c("--profit"), size: 1 }
          : { time: m.time, position: "aboveBar", shape: "arrowDown", color: c("--loss"), size: 1 };
      }
      return { time: m.time, position: "aboveBar", shape: "circle", color: (m.r ?? 0) >= 0 ? c("--profit") : c("--loss"), text: m.text, size: 1 };
    });
    markersApi.setMarkers(markers);
    updateBid();
  }

  function updateBid() {
    if (!candles) return;
    const s = Opus.status;
    if (!s || s.bid == null) return;
    const opts = { price: s.bid, color: Opus.css("--muted"), lineWidth: 1, lineStyle: LC.LineStyle.Dotted, axisLabelVisible: true, title: "Bid" };
    if (bidLine) bidLine.applyOptions(opts);
    else bidLine = candles.createPriceLine(opts);
  }

  async function loadCandles() {
    ensureChart();
    if (mode !== "price") return;
    try {
      const data = await api("/api/candles?bars=192");
      $("#live-chart-empty").hidden = true;
      const first = !lastCandles;
      lastCandles = data;
      candles.setData(data.candles);
      emaSeries[0].setData(data.ema20);
      emaSeries[1].setData(data.ema50);
      emaSeries[2].setData(data.ema200);
      drawOverlays(data);
      if (first) chart.timeScale().fitContent();
    } catch (e) {
      $("#live-chart-empty").hidden = false;
      $("#live-chart-empty").textContent = e.status === 503 ? "Waiting for MT5…" : `Chart unavailable: ${e.message}`;
    }
  }

  async function loadEquity() {
    ensureChart();
    try {
      const data = await api("/api/equity?period=all");
      const empty = !data.opus.length;
      $("#live-chart-empty").hidden = !empty;
      if (empty) $("#live-chart-empty").textContent = "The equity curve starts with the first closed bar.";
      equityLine.setData(data.opus);
      chart.timeScale().fitContent();
    } catch (e) {
      $("#live-chart-empty").hidden = false;
      $("#live-chart-empty").textContent = `Equity unavailable: ${e.message}`;
    }
  }

  function setMode(m) {
    mode = m;
    $$("[data-chart]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.chart === m)));
    ensureChart();
    const price = m === "price";
    candles.applyOptions({ visible: price });
    const showEma = price && $("#toggle-emas").checked;
    emaSeries.forEach((s) => s.applyOptions({ visible: showEma }));
    equityLine.applyOptions({ visible: !price });
    markersApi.setMarkers(price && lastCandles ? markersApi.markers() : []);
    $("#toggle-emas").closest("label").hidden = !price;
    $("#ema-legend").hidden = !showEma;
    candles.priceScale().applyOptions({ autoScale: true });
    if (price) { loadCandles(); } else { loadEquity(); }
  }

  // ------------------------------------------------------------ KPIs, card, list
  function renderKpis(st) {
    const k = st.kpis || {};
    const cur = k.currency || "EUR";
    const dry = st.status && st.status.dry_run;
    $("#kpi-equity-label").textContent = dry ? "Paper equity" : "Equity";
    $("#kpi-equity-label").dataset.tip = dry
      ? "Dry run: the starting balance plus the simulated profit or loss of the bot's paper trades."
      : "Account equity: balance plus the floating profit or loss of open positions.";
    $("#kpi-equity").textContent = fmt.money(k.equity, cur);
    $("#kpi-equity-sub").textContent = dry ? "Simulated fills" : "";

    const today = $("#kpi-today");
    today.textContent = k.today_pnl == null ? "—" : fmt.money(k.today_pnl, cur, true);
    today.className = `kpi-value num ${fmt.cls(k.today_pnl)}`;
    const pct = $("#kpi-today-pct");
    pct.textContent = k.today_pct == null ? "" : fmt.pct(k.today_pct);
    pct.className = `kpi-sub num ${fmt.cls(k.today_pct)}`;

    const p = st.position;
    const pv = $("#kpi-position"), ps = $("#kpi-position-sub");
    if (!p) {
      pv.innerHTML = '<span class="muted">Flat</span>';
      ps.textContent = "";
    } else if (p.kind === "pending") {
      pv.innerHTML = `<span class="${p.side === "long" ? "up" : "down"}">${p.side === "long" ? "▲ Buy limit" : "▼ Sell limit"}</span> <span class="num">${fmt.num(p.volume, 2)}</span> lots`;
      ps.innerHTML = `@ ${fmt.price(p.entry)} · expires ${fmt.esc(fmt.hm(p.expires))} UTC`;
      ps.className = "kpi-sub num muted";
    } else {
      pv.innerHTML = `<span class="${p.side === "long" ? "up" : "down"}">${p.side === "long" ? "▲ Long" : "▼ Short"}</span> <span class="num">${fmt.num(p.volume, 2)}</span> lots`;
      const r = p.r == null ? "" : ` · <span class="${fmt.cls(p.r)}">${fmt.r(p.r)}</span>`;
      const u = p.unrealized == null ? "" : ` · <span class="${fmt.cls(p.unrealized)}">${fmt.money(p.unrealized, cur, true)}</span>`;
      ps.innerHTML = `@ ${fmt.price(p.entry)}${r}${u}`;
      ps.className = "kpi-sub num";
    }
    $("#kpi-dd").textContent = fmt.pct(k.max_dd_pct, true);
    $("#kpi-dd-sub").textContent = k.max_dd_eur ? fmt.money(k.max_dd_eur, cur, true) : "";
  }

  function renderLatest(st) {
    const d = st.latest;
    const has = !!d;
    $("#latest-body").hidden = !has;
    $("#latest-empty").hidden = has;
    if (!has) {
      const s = st.status || {};
      const secs = s.next_bar_at ? (Date.parse(s.next_bar_at) - Opus.serverNow()) / 1000 : null;
      $("#latest-empty").textContent = secs != null
        ? `No calls yet. The first one comes after the next M15 close, in ${fmt.mmss(secs)}.`
        : "No calls yet. The first one comes after the next M15 close.";
      $("#latest-time").textContent = "";
      $("#latest-reject").hidden = true;
      return;
    }
    $("#latest-time").textContent = `${fmt.hm(barClose(d.bar_time))} UTC`;
    const word = $("#latest-action");
    const kind = { OPEN_LONG: ["LONG ▲", "long"], OPEN_SHORT: ["SHORT ▼", "short"], CLOSE: ["CLOSE", ""], MOVE_STOP: ["MOVE STOP", ""] }[d.action] || ["HOLD", "hold"];
    word.textContent = kind[0];
    word.className = `action-word ${kind[1]}`;
    const conf = $("#latest-conf");
    if (d.confidence) {
      conf.innerHTML = [1, 2, 3, 4, 5].map((i) => `<i class="${i <= d.confidence ? "on" : ""}"></i>`).join("") + `<span class="conf-n num">${d.confidence}/5</span>`;
      conf.setAttribute("aria-label", `Confidence ${d.confidence} of 5`);
    } else {
      conf.innerHTML = "";
    }
    const setup = $("#latest-setup");
    setup.hidden = !d.setup;
    setup.textContent = d.setup || "";
    const ag = Opus.agreeLabel(d);
    const agEl = $("#latest-agree");
    agEl.hidden = !ag;
    agEl.textContent = ag || "";
    agEl.dataset.tip = "Whether this call agreed with the Trend Signals indicator on that bar.";
    const rej = $("#latest-reject");
    const showRej = ["REJECTED", "FAILED", "NO_DECISION", "ERROR", "DISCARDED"].includes(d.verdict);
    rej.hidden = !showRej;
    if (showRej) rej.textContent = (d.verdict === "REJECTED" ? "Rejected: " : "") + (d.verdict_reason || "");
    const reasoning = $("#latest-reasoning");
    reasoning.textContent = d.reasoning || "";
    reasoning.classList.add("clamp");
    const more = $("#latest-more");
    more.textContent = "Show more";
    requestAnimationFrame(() => { more.hidden = reasoning.scrollHeight <= reasoning.clientHeight + 1; });
    const inv = $("#latest-invalidation");
    inv.hidden = !d.invalidation;
    inv.innerHTML = d.invalidation ? `<b>Invalidation:</b> ${fmt.esc(d.invalidation)}` : "";
  }

  function barClose(barTime) {
    const d = fmt.date(barTime);
    return d ? new Date(d.getTime() + 15 * 60 * 1000).toISOString() : null;
  }

  function renderRecent(st) {
    const list = $("#recent-list");
    const items = st.recent || [];
    if (!items.length) {
      list.innerHTML = '<li class="muted" style="cursor:default;display:block">Decisions appear here after each closed bar.</li>';
      return;
    }
    list.innerHTML = items.map((d) => {
      const a = Opus.actionLabel(d);
      return `<li data-id="${d.id}" tabindex="0" title="Open in Journal"><span class="t num">${fmt.hm(barClose(d.bar_time))}</span><span class="a ${a.cls}">${a.text}</span><span class="s">${fmt.esc(d.summary || "")}</span></li>`;
    }).join("");
  }

  function renderFooter() {
    const s = Opus.status;
    const st = Opus.state || {};
    if (!s) return;
    const conn = $("#foot-conn");
    const ok = s.connected && s.broker_connected;
    conn.className = ok ? "ok" : "bad";
    conn.innerHTML = `<span class="dot" aria-hidden="true"></span>${ok ? "MT5 connected" : s.connected ? "MT5: no broker connection" : "MT5 disconnected"}`;
    const sp = $("#foot-spread");
    sp.textContent = s.spread_pips == null ? "Spread —" : `Spread ${fmt.pips(s.spread_pips)}`;
    sp.className = "num" + (s.spread_pips != null && Opus.maxSpread && s.spread_pips > Opus.maxSpread ? " warn" : "");
    const ev = s.next_event;
    $("#foot-event").textContent = ev
      ? `${ev.currency} ${ev.title} in ${fmt.duration((Date.parse(ev.time) - Opus.serverNow()) / 1000)} (${fmt.hm(ev.time)} UTC)`
      : "No high-impact events in events.yaml";
    if (st.api_cost_today != null) $("#foot-cost").textContent = `API today ${fmt.usd(st.api_cost_today)}`;
  }

  function renderThinking() {
    const s = Opus.status;
    const since = s && s.thinking_since;
    thinkingSince = since ? Date.parse(since) : null;
    $("#latest-thinking").hidden = !since;
    if (since) {
      $("#latest-body").hidden = !(Opus.state && Opus.state.latest);
      $("#latest-empty").hidden = true;
    }
    updateThinkingClock();
  }
  function updateThinkingClock() {
    if (thinkingSince) $("#thinking-secs").textContent = fmt.duration((Opus.serverNow() - thinkingSince) / 1000);
  }

  // ------------------------------------------------------------ wiring
  Opus.views.live = {
    init() {
      $$("[data-chart]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.chart)));
      $("#toggle-emas").addEventListener("change", () => setMode(mode));
      $("#latest-more").addEventListener("click", () => {
        const r = $("#latest-reasoning");
        const clamped = r.classList.toggle("clamp");
        $("#latest-more").textContent = clamped ? "Show more" : "Show less";
      });
      $("#recent-list").addEventListener("click", (e) => {
        const li = e.target.closest("li[data-id]");
        if (li) Opus.go(`#/journal/${li.dataset.id}`);
      });
      $("#recent-list").addEventListener("keydown", (e) => {
        const li = e.target.closest("li[data-id]");
        if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); Opus.go(`#/journal/${li.dataset.id}`); }
      });
      const tb = $(".chart-toolbar");
      const ohlc = document.createElement("span");
      ohlc.id = "ohlc";
      ohlc.className = "num muted grow r";
      ohlc.style.fontSize = "12px";
      tb.appendChild(ohlc);

      Opus.on("state", (st) => { renderKpis(st); renderLatest(st); renderRecent(st); renderFooter(); renderThinking(); });
      Opus.on("status", () => { renderFooter(); renderThinking(); updateBid(); });
      Opus.on("tick", () => { updateThinkingClock(); if (!(Opus.state && Opus.state.latest) && Opus.state) renderLatest(Opus.state); });
      Opus.on("thinking", () => Opus.refreshState());
      Opus.on("decision", () => { if (Opus.view === "live") (mode === "price" ? loadCandles() : loadEquity()); });
      Opus.on("trade", () => { if (Opus.view === "live") (mode === "price" ? loadCandles() : loadEquity()); });
      Opus.on("theme", restyle);
      Opus.on("settings", async () => { try { const s = await api("/api/settings"); Opus.maxSpread = (s.fields.find((f) => f.key === "risk.max_spread_pips") || {}).value; } catch (e) { /* ignore */ } });
      api("/api/settings").then((s) => { Opus.maxSpread = (s.fields.find((f) => f.key === "risk.max_spread_pips") || {}).value; }).catch(() => {});
    },
    show(arg, changed) {
      ensureChart();
      if (changed) (mode === "price" ? loadCandles() : loadEquity());
      clearInterval(candleTimer);
      candleTimer = setInterval(() => { if (Opus.view === "live" && mode === "price") loadCandles(); }, 60_000);
    },
  };
})();
