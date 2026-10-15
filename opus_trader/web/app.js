/* Opus Trader UI core: API, live updates, routing, top bar, banners, dialogs, tooltips, formatting. */
(() => {
  "use strict";

  const TOKEN = document.querySelector('meta[name="opus-token"]').content;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const MINUS = "−";

  // ------------------------------------------------------------ formatting
  const fmt = {
    num(x, d = 2) {
      if (x === null || x === undefined || Number.isNaN(x)) return "—";
      const s = Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
      return (x < 0 && Number(s.replace(/,/g, "")) !== 0 ? MINUS : "") + s;
    },
    signed(x, d = 2) {
      if (x === null || x === undefined || Number.isNaN(x)) return "—";
      const s = fmt.num(Math.abs(x), d);
      if (Number(s.replace(/,/g, "")) === 0) return s;
      return (x > 0 ? "+" : MINUS) + s;
    },
    money(x, cur = "EUR", signed = false) {
      if (x === null || x === undefined) return "—";
      return `${signed ? fmt.signed(x, 2) : fmt.num(x, 2)} ${cur}`;
    },
    pct(x, signed = true, d = 2) { return x === null || x === undefined ? "—" : `${signed ? fmt.signed(x, d) : fmt.num(x, d)}%`; },
    r(x, d = 2) { return x === null || x === undefined ? "—" : `${fmt.signed(x, d)}R`; },
    price(x, digits = 5) { return x === null || x === undefined ? "—" : Number(x).toFixed(digits); },
    pips(x) { return x === null || x === undefined ? "—" : `${fmt.num(x, 1)} pips`; },
    usd(x, d = 2) { return x === null || x === undefined ? "—" : `$${fmt.num(x, d)}`; },
    cls(x) { return x > 0 ? "up" : x < 0 ? "down" : ""; },
    date(iso) { return iso ? new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso.replace(" ", "T") + "Z") : null; },
    hm(iso) { const d = fmt.date(iso); return d ? d.toISOString().slice(11, 16) : ""; },
    day(iso) {
      const d = fmt.date(iso);
      if (!d) return "";
      return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }).toUpperCase().replace(",", "");
    },
    dayTime(iso) {
      const d = fmt.date(iso);
      return d ? `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })} ${d.toISOString().slice(11, 16)}` : "";
    },
    duration(sec) {
      sec = Math.max(0, Math.round(sec));
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
      if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m`;
      return `${sec}s`;
    },
    mmss(sec) {
      sec = Math.max(0, Math.round(sec));
      return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
    },
    esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },
  };

  // How each action is shown. Never colour alone: words and arrows carry the meaning.
  function actionLabel(item) {
    if (!item) return { text: "—", cls: "" };
    if (item.kind === "trade") return { text: "TRADE", cls: "" };
    if (item.status === "SKIPPED") return { text: "SKIP", cls: "act-skip" };
    if (item.verdict === "REJECTED" || item.verdict === "FAILED") return { text: "REJECT", cls: "act-reject" };
    if (item.verdict === "ERROR") return { text: "ERROR", cls: "act-reject" };
    switch (item.action) {
      case "OPEN_LONG": return { text: "LONG ▲", cls: "act-long" };
      case "OPEN_SHORT": return { text: "SHORT ▼", cls: "act-short" };
      case "CLOSE": return { text: "CLOSE", cls: "" };
      case "MOVE_STOP": return { text: "MOVE STOP", cls: "" };
      default: return { text: "HOLD", cls: "act-hold" };
    }
  }

  const AGREE = { agree: "With signal", disagree: "Against signal" };
  function agreeLabel(item) {
    if (!item || !item.agreement || item.agreement === "n/a") return null;
    if (item.action === "HOLD" && item.agreement === "disagree") return "Ignored signal";
    return AGREE[item.agreement] || null;
  }

  // ------------------------------------------------------------ API
  async function api(path, { method = "GET", body } = {}) {
    const opts = { method, headers: {} };
    if (method !== "GET") {
      opts.headers["X-Opus-Token"] = TOKEN;
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body || {});
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------ app state + events
  const bus = {};
  const Opus = (window.Opus = {
    $, $$, fmt, api, actionLabel, agreeLabel,
    status: null, state: null, skewMs: 0, view: null, views: {},
    on(evt, fn) { (bus[evt] = bus[evt] || []).push(fn); },
    emit(evt, data) { (bus[evt] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); },
    serverNow() { return Date.now() + Opus.skewMs; },
    css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); },
    toast,
    openDialog,
  });

  async function refreshState() {
    try {
      Opus.state = await api("/api/state");
      Opus.emit("state", Opus.state);
      if (Opus.state.status) applyStatus(Opus.state.status);
    } catch (e) {
      console.warn("state", e);
    }
  }
  Opus.refreshState = refreshState;

  // ------------------------------------------------------------ live updates (SSE)
  let es = null;
  let refreshTimer = null;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshState, 150);
  }
  function connect() {
    es = new EventSource("/api/stream");
    es.addEventListener("status", (e) => applyStatus(JSON.parse(e.data)));
    es.addEventListener("decision", (e) => { scheduleRefresh(); Opus.emit("decision", JSON.parse(e.data)); });
    es.addEventListener("trade", (e) => { scheduleRefresh(); Opus.emit("trade", JSON.parse(e.data)); });
    es.addEventListener("thinking", (e) => Opus.emit("thinking", JSON.parse(e.data)));
    es.addEventListener("settings", (e) => { scheduleRefresh(); Opus.emit("settings", JSON.parse(e.data)); });
    es.onerror = () => { setPill("disconnected", "App server unreachable"); };
    es.onopen = () => scheduleRefresh();
  }

  // ------------------------------------------------------------ top bar
  function setPill(state, text) {
    const pill = $("#status-pill");
    pill.dataset.state = state;
    $("#status-text").textContent = text;
    pill.title = text;
  }

  function applyStatus(s) {
    const prev = Opus.status;
    Opus.status = s;
    if (s.now) Opus.skewMs = Date.parse(s.now) - Date.now();
    const labels = { running: "Running", paused: "Paused", halted: "Halted", disconnected: "MT5 disconnected", blocked: "Stopped" };
    let text = labels[s.status] || s.status;
    if (s.status === "halted" && s.reason) text = `Halted: ${s.reason}`;
    if (s.status === "paused" && s.reason && s.reason !== "Paused") text = `Paused: ${s.reason}`;
    if (s.status === "blocked") text = "Stopped: not allowed";
    setPill(s.status, text);

    $("#symbol-label").textContent = `${s.symbol || "EURUSD"} · M15`;
    const acct = $("#badge-account");
    acct.textContent = s.sim ? "SIM" : "DEMO";
    acct.dataset.tip = s.sim ? "Preview mode: a simulated terminal and a rule-based stand-in for Opus. Nothing is real." : "Opus Trader only runs on MT5 demo accounts.";
    $("#badge-dry").hidden = !s.dry_run;

    const pause = $("#btn-pause");
    const canResume = s.status === "halted" || s.paused;
    pause.textContent = canResume ? "Resume" : "Pause";
    pause.disabled = s.status === "blocked" || (canResume && (s.stop_file || (s.halt && s.halt.kind === "daily_limit")));
    pause.dataset.tip = s.stop_file ? "Delete the STOP file in the Opus Trader folder to resume." :
      (s.halt && s.halt.kind === "daily_limit") ? "Trading resumes automatically at 17:00 New York." :
      canResume ? "Resume model calls from the next bar." : "Stop asking Opus for decisions. Open positions keep their stop-loss and take-profit.";
    $("#btn-stop").disabled = s.status === "blocked";

    renderBanners(s.banners || []);
    Opus.emit("status", s);
    if (prev && prev.dry_run !== s.dry_run) scheduleRefresh();
  }

  function renderBanners(list) {
    const box = $("#banners");
    const html = list.map((b) => {
      let text = fmt.esc(b.text);
      if (b.until) text += ` <span class="num">(in ${fmt.duration((Date.parse(b.until) - Opus.serverNow()) / 1000)})</span>`;
      return `<div class="banner ${b.level === "danger" ? "danger" : "warning"}" role="${b.level === "danger" ? "alert" : "status"}">${text}</div>`;
    }).join("");
    if (box.innerHTML !== html) box.innerHTML = html;
  }

  function tick() {
    const s = Opus.status;
    const el = $("#countdown");
    if (s && s.next_bar_at && s.status !== "blocked") {
      const secs = (Date.parse(s.next_bar_at) - Opus.serverNow()) / 1000;
      el.textContent = `Next bar ${fmt.mmss(secs)}`;
    } else {
      el.textContent = "Next bar --:--";
    }
    Opus.emit("tick", s);
  }

  // ------------------------------------------------------------ routing
  function route() {
    const h = location.hash.replace(/^#\/?/, "");
    const [view, arg] = h.split("/");
    const name = ["live", "journal", "stats", "settings"].includes(view) ? view : "live";
    $$(".view").forEach((v) => { v.hidden = v.dataset.view !== name; });
    $$(".navrow [data-view]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === name)));
    const changed = Opus.view !== name;
    Opus.view = name;
    const v = Opus.views[name];
    if (v && v.show) v.show(arg, changed);
  }
  Opus.go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };

  // ------------------------------------------------------------ tooltips (one floating element, never clipped)
  const tip = $("#tooltip");
  let tipTarget = null;
  function showTip(el) {
    const text = el.dataset.tip;
    if (!text) return;
    tipTarget = el;
    tip.textContent = text;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = Math.min(Math.max(8, r.left + r.width / 2 - tw / 2), window.innerWidth - tw - 8);
    let y = r.bottom + 8;
    if (y + th > window.innerHeight - 8) y = r.top - th - 8;
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
  }
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (el && el !== tipTarget) showTip(el);
    else if (!el && tipTarget) { tip.hidden = true; tipTarget = null; }
  });
  document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) showTip(el); });
  document.addEventListener("focusout", () => { tip.hidden = true; tipTarget = null; });
  document.addEventListener("scroll", () => { tip.hidden = true; tipTarget = null; }, true);

  // ------------------------------------------------------------ dialogs + toast
  function openDialog(id) {
    const d = document.getElementById(id);
    if (!d.open) d.showModal();
    return d;
  }
  $$("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  let toastTimer = null;
  function toast(text, ms = 3200) {
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ms);
  }

  // Stop: always confirm.
  $("#btn-stop").addEventListener("click", async () => {
    await refreshState();
    const st = Opus.state || {};
    const p = st.position;
    const pos = p && p.kind === "position" ? 1 : 0;
    const pend = p && p.kind === "pending" ? 1 : 0;
    $("#dlg-stop-text").textContent =
      `Closes ${pos} position${pos === 1 ? "" : "s"} and cancels ${pend} pending order${pend === 1 ? "" : "s"} with magic ${st.magic ?? ""}. ` +
      "Your manual trades are not touched. The bot then stays halted until you press Resume.";
    openDialog("dlg-stop");
  });
  $("#dlg-stop-ok").addEventListener("click", async () => {
    $("#dlg-stop").close();
    try {
      const r = await api("/api/stop", { method: "POST" });
      toast(`Halted. Closed ${r.closed} position${r.closed === 1 ? "" : "s"}, cancelled ${r.cancelled} order${r.cancelled === 1 ? "" : "s"}.`);
    } catch (e) { toast(e.message); }
    refreshState();
  });

  $("#btn-pause").addEventListener("click", async () => {
    const s = Opus.status || {};
    const resume = s.status === "halted" || s.paused;
    try {
      await api(resume ? "/api/resume" : "/api/pause", { method: "POST" });
      toast(resume ? "Resumed. Opus is asked again from the next bar." : "Paused. Open positions keep their stop-loss and take-profit.");
    } catch (e) { toast(e.message); }
  });

  // Exit (called by the native window when you close it).
  window.opusAskExit = () => openDialog("dlg-exit");
  $("#dlg-exit-ok").addEventListener("click", async () => {
    $("#dlg-exit-ok").disabled = true;
    try {
      await api("/api/exit", { method: "POST" });
      document.body.innerHTML = '<p style="padding:24px;color:var(--muted)">Opus Trader stopped. Open positions keep their stop-loss and take-profit at the broker.</p>';
    } catch (e) {
      $("#dlg-exit").close();
      $("#dlg-exit-ok").disabled = false;
      toast(e.message);
    }
  });

  // Theme
  function applyThemeButton() {
    const light = document.documentElement.dataset.theme === "light";
    const b = $("#btn-theme");
    const label = light ? "Switch to dark theme" : "Switch to light theme";
    b.setAttribute("aria-label", label);
    b.dataset.tip = label;
    $("#theme-icon").setAttribute("d", light
      ? "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"
      : "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zm0-5v3m0 14v3M4.2 4.2l2.1 2.1m11.4 11.4 2.1 2.1M2 12h3m14 0h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1");
    $("#theme-icon").setAttribute("stroke", light ? "none" : "currentColor");
    $("#theme-icon").setAttribute("stroke-width", light ? "0" : "2");
    $("#theme-icon").setAttribute("fill", light ? "currentColor" : "none");
  }
  $("#btn-theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("opus-theme", next); } catch (e) { /* storage unavailable */ }
    applyThemeButton();
    tip.hidden = true;
    Opus.emit("theme", next);
  });

  // Navigation
  $$(".navrow [data-view]").forEach((b) => b.addEventListener("click", () => Opus.go(`#/${b.dataset.view}`)));
  window.addEventListener("hashchange", route);

  // Boot once every view script has registered.
  window.addEventListener("DOMContentLoaded", () => {
    applyThemeButton();
    Object.values(Opus.views).forEach((v) => v.init && v.init());
    route();
    refreshState();
    connect();
    setInterval(tick, 1000);
    tick();
  });
})();
