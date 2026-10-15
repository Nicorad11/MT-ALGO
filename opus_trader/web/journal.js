/* Journal: one chronological timeline of decisions and trades. */
(() => {
  "use strict";
  const { $, $$, fmt, api } = window.Opus;
  const Opus = window.Opus;

  const S = { filter: "all", q: "", items: [], next: null, loading: false, open: new Set(), details: new Map(), holds: 0, focus: null };
  let searchTimer = null;

  const EXIT = { sl: "Stop-loss", tp: "Take-profit", model: "Closed by Opus", kill_switch: "Kill switch", weekend: "Weekend flat", daily_limit: "Daily loss limit", manual: "Closed in MT5", stop_out: "Stop out", broker: "Closed by broker" };

  async function load(reset) {
    if (S.loading) return;
    S.loading = true;
    if (reset) { S.items = []; S.next = null; }
    const params = new URLSearchParams({ filter: S.filter, q: S.q, limit: "60" });
    if (!reset && S.next) params.set("before", S.next);
    try {
      const data = await api(`/api/journal?${params}`);
      S.items = reset ? data.items : S.items.concat(data.items);
      S.next = data.next;
      S.holds = data.holds;
      render();
    } catch (e) {
      $("#journal-list").innerHTML = `<p class="empty">Couldn't load the journal: ${fmt.esc(e.message)}</p>`;
    } finally {
      S.loading = false;
      $("#journal-more").hidden = !S.next;
    }
    $("#journal-export").href = `/api/journal.csv?${new URLSearchParams({ filter: S.filter, q: S.q })}`;
  }

  // Collapse runs of identical skips ("SKIP ×8 Weekend window").
  function collapse(items) {
    const out = [];
    for (const it of items) {
      const prev = out[out.length - 1];
      if (prev && it.kind === "decision" && it.status === "SKIPPED" && prev.kind === "decision" && prev.status === "SKIPPED" && prev.summary === it.summary) {
        prev.count = (prev.count || 1) + 1;
        prev.first_time = it.time;
        prev.first_bar = it.bar_time;
        continue;
      }
      out.push({ ...it });
    }
    return out;
  }

  function emptyText() {
    if (S.q) return `Nothing matches “${fmt.esc(S.q)}”.`;
    if (S.filter === "trades") return `No trades yet.${S.holds ? ` Opus has held for ${S.holds} bar${S.holds === 1 ? "" : "s"}.` : ""}`;
    if (S.filter === "rejected") return "No rejections. The risk engine has approved every order Opus proposed.";
    if (S.filter === "holds") return "No holds yet.";
    return "Nothing logged yet. Decisions appear here after each closed M15 bar.";
  }

  function render() {
    const box = $("#journal-list");
    if (!S.items.length) {
      box.innerHTML = `<p class="empty">${emptyText()}</p>`;
      return;
    }
    let html = "";
    let day = null;
    for (const it of collapse(S.items)) {
      const d = fmt.day(it.time);
      if (d !== day) { html += `<div class="day eyebrow">${d}</div>`; day = d; }
      html += row(it);
    }
    box.innerHTML = html;
    for (const id of S.open) {
      const el = box.querySelector(`.jrow[data-id="${id}"]`);
      if (el) fillDetail(el, id);
    }
  }

  function barClose(bt) {
    const d = fmt.date(bt);
    return d ? new Date(d.getTime() + 15 * 60000).toISOString() : null;
  }

  function row(it) {
    const id = String(it.id);
    const open = S.open.has(id);
    const dry = it.mode === "dry" ? '<span class="tag" title="Dry run: simulated">DRY</span>' : "";
    if (it.kind === "trade") {
      const side = it.side === "long" ? '<span class="act-long">▲ Long</span>' : '<span class="act-short">▼ Short</span>';
      return `<div class="jrow${open ? " open" : ""}" data-id="${id}" data-kind="trade">
        <div class="jhead" tabindex="0" role="button" aria-expanded="${open}">
          <span class="t num">${fmt.hm(it.time)}</span><span class="a">TRADE</span>
          <span class="s">${side} closed · ${fmt.esc(EXIT[it.exit_reason] || it.exit_reason || "")}${it.setup ? ` · <span class="muted">${fmt.esc(it.setup)}</span>` : ""}</span>
          <span class="end num"><span class="${fmt.cls(it.r)}">${fmt.r(it.r)}</span><span class="${fmt.cls(it.pnl)}">${fmt.money(it.pnl, "EUR", true)}</span>${dry}</span>
          <span class="chev" aria-hidden="true">›</span>
        </div>${open ? '<div class="jdetail"></div>' : ""}</div>`;
    }
    const a = Opus.actionLabel(it);
    const time = fmt.hm(barClose(it.bar_time));
    if (it.status === "SKIPPED") {
      const label = it.count ? `SKIP ×${it.count}` : "SKIP";
      const range = it.count ? ` (${fmt.hm(barClose(it.first_bar))} – ${time})` : "";
      return `<div class="jrow static" data-id="${id}"><div class="jhead"><span class="t num">${time}</span><span class="a act-skip">${label}</span>
        <span class="s muted">${fmt.esc(it.summary)}${range} · model not called</span><span class="end">${dry}</span><span></span></div></div>`;
    }
    const conf = it.confidence ? `<span class="muted num" title="Confidence ${it.confidence} of 5">${"●".repeat(it.confidence)}${"○".repeat(5 - it.confidence)}</span> ` : "";
    let summary = "";
    if (it.verdict === "APPROVED" && it.action !== "HOLD") summary = `${fmt.esc(it.setup || "")} · Approved`;
    else if (it.verdict === "REJECTED" || it.verdict === "FAILED") summary = `${it.action === "OPEN_LONG" ? "▲ Long · " : it.action === "OPEN_SHORT" ? "▼ Short · " : ""}${fmt.esc(it.verdict_reason || "")}`;
    else if (["NO_DECISION", "ERROR", "DISCARDED"].includes(it.verdict)) summary = fmt.esc(it.verdict_reason || "");
    else summary = fmt.esc(it.setup ? `${it.setup}: ${firstSentence(it.reasoning)}` : firstSentence(it.reasoning));
    const ag = Opus.agreeLabel(it);
    return `<div class="jrow${open ? " open" : ""}" data-id="${id}" data-kind="decision">
      <div class="jhead" tabindex="0" role="button" aria-expanded="${open}">
        <span class="t num">${time}</span><span class="a ${a.cls}">${a.text}</span>
        <span class="s">${conf}${summary}</span>
        <span class="end">${ag ? `<span class="tag">${ag}</span>` : ""}${dry}</span>
        <span class="chev" aria-hidden="true">›</span>
      </div>${open ? '<div class="jdetail"></div>' : ""}</div>`;
  }

  function firstSentence(t) {
    if (!t) return "";
    const m = t.match(/^.*?[.!?](\s|$)/);
    return (m ? m[0] : t).trim();
  }

  async function toggle(el) {
    const id = el.dataset.id;
    if (S.open.has(id)) { S.open.delete(id); } else { S.open.add(id); }
    const it = S.items.find((x) => String(x.id) === id);
    if (!it) return;
    const wrap = document.createElement("div");
    wrap.innerHTML = row(it);
    const fresh = wrap.firstElementChild;
    el.replaceWith(fresh);
    if (S.open.has(id)) fillDetail(fresh, id);
  }

  async function fillDetail(el, id) {
    const box = el.querySelector(".jdetail");
    if (!box) return;
    const it = S.items.find((x) => String(x.id) === id) || S.focus;
    if (it && it.kind === "trade") {
      box.innerHTML = tradeDetail(it);
      return;
    }
    let d = S.details.get(id);
    if (!d) {
      box.innerHTML = '<p class="muted">Loading…</p>';
      try { d = await api(`/api/journal/${id}`); S.details.set(id, d); } catch (e) { box.innerHTML = `<p class="muted">${fmt.esc(e.message)}</p>`; return; }
    }
    box.innerHTML = decisionDetail(d);
  }

  function fact(label, value) { return `<span><b>${label}</b>${value}</span>`; }

  function tradeDetail(t) {
    return `<div><h3>Trade</h3><div class="facts num">
      ${fact("Side", t.side === "long" ? "▲ Long" : "▼ Short")}${fact("Lots", fmt.num(t.volume, 2))}
      ${fact("Open", `${fmt.price(t.open_price)} at ${fmt.dayTime(t.open_time)} UTC`)}${fact("Close", `${fmt.price(t.close_price)} at ${fmt.dayTime(t.time)} UTC`)}
      ${fact("Result", `<span class="${fmt.cls(t.r)}">${fmt.r(t.r)}</span> · <span class="${fmt.cls(t.pnl)}">${fmt.money(t.pnl, "EUR", true)}</span>`)}
      ${fact("Exit", fmt.esc(EXIT[t.exit_reason] || t.exit_reason || ""))}</div></div>
      ${t.decision_id ? `<div><button class="link" data-open-decision="${t.decision_id}" type="button">Show the decision that opened it</button></div>` : ""}`;
  }

  function decisionDetail(d) {
    const parts = [];
    if (d.reasoning) {
      parts.push(`<div><h3>Reasoning</h3><p>${fmt.esc(d.reasoning)}</p>${d.invalidation ? `<p class="muted" style="margin-top:4px"><b style="color:var(--text);font-weight:600">Invalidation:</b> ${fmt.esc(d.invalidation)}</p>` : ""}</div>`);
    }
    const w = d.saw;
    if (w) {
      const cur = w.currency || "EUR";
      const pos = w.position ? `${w.position.side === "long" ? "▲ Long" : "▼ Short"} ${fmt.num(w.position.volume, 2)} @ ${fmt.price(w.position.entry)} (${fmt.r(w.position.unrealized_r)})`
        : w.pending ? `Pending ${w.pending.side} limit @ ${fmt.price(w.pending.price)}` : "Flat";
      const sig = w.signal ? `${w.signal.trend === "up" ? "▲ Up" : "▼ Down"}${w.signal.bars_since_flip != null ? `, ${w.signal.bars_since_flip} bars since flip` : ""}${w.signal.flipped_now ? `, flipped ${w.signal.flip_now} this bar` : ""} · line ${fmt.price(w.signal.line)} (${fmt.num(w.signal.line_distance_pips, 1)} pips)` : "—";
      const ev = (w.events || []).length ? w.events.map((e) => `${e.currency} ${fmt.esc(e.title)} ${fmt.hm(e.time)}`).join(", ") : "None";
      parts.push(`<div><h3>What Opus saw</h3><div class="facts num">
        ${fact("Bid/Ask", `${fmt.price(w.bid)} / ${fmt.price(w.ask)}`)}${fact("Spread", fmt.pips(w.spread_pips))}${fact("ATR", fmt.pips(w.atr_pips))}
        ${fact("RSI", fmt.num(w.rsi, 1))}${fact("Session", fmt.esc(w.session))}${fact("M15 EMAs", w.ema_order)}
        ${w.asian_range ? fact("Asian range", `${fmt.price(w.asian_range[0])}–${fmt.price(w.asian_range[1])}`) : ""}
        ${fact("Book", pos)}${fact("Today", fmt.money(w.today_pnl, cur, true))}${fact("Trend Signals", sig)}${fact("Events (4 h)", ev)}
        ${w.bars_sent ? fact("Candles", `${w.bars_sent.M15} M15 · ${w.bars_sent.H1} H1 · ${w.bars_sent.H4} H4`) : ""}
      </div></div>`);
    }
    const c = (d.risk && d.risk.calc) || {};
    const verdictText = { APPROVED: "Approved", REJECTED: "Rejected", HOLD: "Hold (no order)", NO_DECISION: "No decision", FAILED: "Order failed", ERROR: "Error", DISCARDED: "Discarded" }[d.verdict] || d.verdict;
    const calc = c.stop_pips != null ? `${fact("Stop", `${fmt.num(c.stop_pips, 1)} pips (${fmt.num(c.stop_atr, 2)}× ATR)`)}${fact("R:R", fmt.num(c.rr, 2))}${c.lots ? fact("Size", `${fmt.num(c.lots, 2)} lots`) + fact("Risk", `${fmt.money(c.risk_eur, "EUR")} (${fmt.num(c.risk_pct, 2)}%)`) : ""}` : "";
    parts.push(`<div><h3>Risk engine</h3><p><span class="${d.verdict === "APPROVED" ? "" : d.verdict === "HOLD" ? "muted" : "warn"}">${verdictText}</span>${d.verdict_reason && d.verdict !== "HOLD" ? `: ${fmt.esc(d.verdict_reason)}` : ""}</p>${calc ? `<div class="facts num" style="margin-top:4px">${calc}</div>` : ""}</div>`);
    if ((d.orders || []).length) {
      parts.push(`<div><h3>Orders</h3>${d.orders.map((o) => `<p class="num" style="font-size:12px"><span class="${o.ok ? "" : "warn"}">${fmt.esc(o.purpose)}</span> · retcode ${o.retcode ?? "—"} ${fmt.esc(o.retcode_name || "")}${o.order_ticket ? ` · #${o.order_ticket}` : ""}${o.price ? ` · ${fmt.price(o.price)}` : ""}${o.volume ? ` · ${fmt.num(o.volume, 2)} lots` : ""} · ${fmt.esc(o.comment || "")}</p>`).join("")}</div>`);
    }
    if (d.status === "CALLED") {
      const tokens = d.input_tokens != null ? `${(d.input_tokens + (d.cache_read_tokens || 0) + (d.cache_write_tokens || 0)).toLocaleString("en-US")} in (${(d.cache_read_tokens || 0).toLocaleString("en-US")} cached) · ${(d.output_tokens || 0).toLocaleString("en-US")} out` : "—";
      parts.push(`<div><h3>Model</h3><div class="facts num">${fact("Model", fmt.esc(d.model || "—"))}${fact("Tokens", tokens)}${fact("Cost", fmt.usd(d.cost_usd, 4))}${fact("Time", d.latency_ms != null ? `${fmt.num(d.latency_ms / 1000, 1)} s` : "—")}${d.llm_status && d.llm_status !== "ok" ? fact("Status", fmt.esc(`${d.llm_status}${d.error ? `: ${d.error}` : ""}`)) : ""}</div>
        <p style="margin-top:4px"><button class="link" type="button" data-raw="${d.id}">View the exact prompt and response</button></p></div>`);
    }
    return parts.join("");
  }

  async function showRaw(id) {
    const pre = $("#dlg-raw-text");
    pre.textContent = "Loading…";
    Opus.openDialog("dlg-raw");
    try {
      const r = await api(`/api/journal/${id}/raw`);
      pre.textContent = `=== SYSTEM ===\n${r.system || "(not stored)"}\n\n=== USER (snapshot) ===\n${r.prompt || ""}\n\n=== RESPONSE ===\n${JSON.stringify(r.response, null, 2)}`;
    } catch (e) { pre.textContent = e.message; }
  }

  async function focusDecision(id) {
    id = String(id);
    if (!S.items.find((x) => String(x.id) === id)) {
      // Not in the loaded page: fetch it and show it at the top.
      try {
        const d = await api(`/api/journal/${id}`);
        S.details.set(id, d);
        S.items.unshift(d);
      } catch (e) { return; }
    }
    S.open.add(id);
    render();
    const el = $(`#journal-list .jrow[data-id="${id}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  Opus.views.journal = {
    init() {
      $$("#view-journal [data-filter]").forEach((b) => b.addEventListener("click", () => {
        S.filter = b.dataset.filter;
        $$("#view-journal [data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        load(true);
      }));
      $("#journal-search").addEventListener("input", (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { S.q = e.target.value.trim(); load(true); }, 250);
      });
      $("#journal-list").addEventListener("click", (e) => {
        const raw = e.target.closest("[data-raw]");
        if (raw) { showRaw(raw.dataset.raw); return; }
        const od = e.target.closest("[data-open-decision]");
        if (od) { focusDecision(od.dataset.openDecision); return; }
        const head = e.target.closest(".jhead");
        if (head && !head.closest(".jrow.static") && !e.target.closest(".jdetail")) toggle(head.closest(".jrow"));
      });
      $("#journal-list").addEventListener("keydown", (e) => {
        const head = e.target.closest(".jhead");
        if (head && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(head.closest(".jrow")); }
      });
      $("#dlg-raw-copy").addEventListener("click", async () => {
        try { await navigator.clipboard.writeText($("#dlg-raw-text").textContent); Opus.toast("Copied"); } catch (e) { Opus.toast("Copy isn't available here"); }
      });
      const more = $("#journal-more");
      new IntersectionObserver((entries) => { if (entries.some((x) => x.isIntersecting) && S.next) load(false); }, { root: $("#view-journal") }).observe(more);
      Opus.on("decision", () => { if (Opus.view === "journal") load(true); });
      Opus.on("trade", () => { if (Opus.view === "journal") load(true); });
    },
    async show(arg, changed) {
      if (changed || !S.items.length) await load(true);
      if (arg) focusDecision(arg);
    },
  };
})();
