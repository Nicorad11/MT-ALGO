/* Settings: grouped, inline-edited, validated against the schema's limits. */
(() => {
  "use strict";
  const { $, fmt, api } = window.Opus;
  const Opus = window.Opus;

  const GROUPS = ["risk", "schedule", "news", "signal", "model"];
  const HIDDEN = new Set(["model.max_tokens", "model.price_input_per_mtok", "model.price_output_per_mtok", "model.price_cache_read_per_mtok",
    "model.price_cache_write_1h_per_mtok", "news.currencies", "risk.max_stop_atr"]);
  let data = null;

  function decimals(f) {
    if (f.type === "int") return 0;
    const step = f.step || 0.1;
    return Math.max(0, (String(step).split(".")[1] || "").length);
  }
  function show(f, v) {
    if (f.type === "float" || f.type === "int") return Number(v).toFixed(decimals(f));
    return v;
  }
  function rangeText(f) {
    if (f.min == null || f.max == null) return "";
    return `${show(f, f.min)} – ${show(f, f.max)}`;
  }

  function control(f) {
    const id = `set-${f.key.replace(".", "-")}`;
    if (!f.editable) {
      let v = f.value;
      if (f.key === "risk.max_open_positions") v = `${v} (fixed)`;
      if (f.key === "news.events_file") {
        const n = data.news;
        v = `${v} · ${n.upcoming} upcoming${n.next ? ` · next ${n.next.currency} ${n.next.title}, ${fmt.dayTime(n.next.time)} UTC` : ""}${n.error ? ` · ${n.error}` : ""}`;
      }
      return `<span class="static-value">${fmt.esc(v)}</span>`;
    }
    if (f.type === "choice") {
      return `<select class="field wide" id="${id}" data-key="${f.key}">${f.choices.map((c) => `<option value="${c}"${String(c) === String(f.value) ? " selected" : ""}>${choiceLabel(f, c)}</option>`).join("")}</select>`;
    }
    if (f.type === "time") {
      return `<input class="field" id="${id}" data-key="${f.key}" value="${fmt.esc(f.value)}" inputmode="numeric" maxlength="5" pattern="[0-2][0-9]:[0-5][0-9]" aria-label="${fmt.esc(f.label)} (HH:MM)">`;
    }
    return `<input class="field num" id="${id}" data-key="${f.key}" type="number" value="${show(f, f.value)}" step="${f.type === "int" ? 1 : (f.step || 0.1)}" min="${f.min}" max="${f.max}" aria-label="${fmt.esc(f.label)}">`;
  }

  function choiceLabel(f, c) {
    if (f.key === "signal.mode") return c === "input" ? "Input" : "Filter";
    if (f.key === "signal.atr_method") return c === 1 ? "1 · Wilder (RMA)" : "2 · Simple (SMA)";
    return c;
  }

  function rowHtml(f) {
    const label = f.tip ? `<span data-tip="${fmt.esc(f.tip)}">${fmt.esc(f.label)}</span>` : fmt.esc(f.label);
    if (f.key === "risk.min_stop_atr") {
      const max = data.fields.find((x) => x.key === "risk.max_stop_atr");
      return `<div class="setting-row"><div class="setting-label"><span data-tip="${fmt.esc(f.tip)}">Stop distance</span></div><div class="setting-control">
        ${control(f)}<span class="muted">to</span>${control(max)}<span class="unit">× ATR</span><span class="range num">${rangeText(f)}</span>
        <span class="field-error" data-err="${f.key}"></span><span class="field-error" data-err="${max.key}"></span><span class="saved" data-saved="${f.key}"></span></div></div>`;
    }
    const unit = f.type === "time" ? "" : f.unit;
    const range = f.editable && f.type !== "choice" && f.type !== "time" ? `<span class="range num">${rangeText(f)}</span>` : "";
    const timeUnit = f.type === "time" ? `<span class="unit">${fmt.esc(f.unit)}</span>` : "";
    return `<div class="setting-row"><div class="setting-label">${label}</div><div class="setting-control">${f.type === "time" ? timeUnit.replace(" UTC", "") : ""}${control(f)}
      ${unit && f.type !== "time" ? `<span class="unit">${fmt.esc(unit)}</span>` : f.type === "time" ? '<span class="unit">UTC</span>' : ""}${range}
      <span class="field-error" data-err="${f.key}"></span><span class="saved" data-saved="${f.key}"></span></div></div>`;
  }

  function render() {
    const byGroup = {};
    for (const f of data.fields) {
      if (f.section === "app" || HIDDEN.has(f.key)) continue;
      (byGroup[f.section] = byGroup[f.section] || []).push(f);
    }
    $("#settings-groups").innerHTML = GROUPS.filter((g) => byGroup[g]).map((g) => {
      const title = byGroup[g][0].section_title;
      let rows = byGroup[g].map(rowHtml).join("");
      if (g === "schedule") rows += '<div class="setting-row"><div class="setting-label">Daily-limit halt ends</div><div class="setting-control"><span class="static-value">17:00 New York (fixed)</span></div></div>';
      return `<div class="settings-group"><h2 class="group-title">${fmt.esc(title)}</h2>${rows}</div>`;
    }).join("");

    const dry = !!(Opus.status ? Opus.status.dry_run : data.fields.find((f) => f.key === "app.dry_run").value);
    const sw = $("#dry-switch");
    sw.setAttribute("aria-checked", String(dry));
    sw.setAttribute("aria-label", dry ? "Dry run is on" : "Dry run is off");
    $("#dry-note").textContent = dry ? "Simulated fills; no orders are sent." : "Off: approved trades are sent to the MT5 demo account.";
    $("#dry-note").className = dry ? "muted" : "warn";

    const ch = data.changes || [];
    $("#settings-changes").innerHTML = ch.length ? ch.map((c) => `<li><span class="muted num">${fmt.dayTime(c.time)}</span><span>${fmt.esc(c.label)} ${fmt.esc(fmtVal(c.old_value))} → ${fmt.esc(fmtVal(c.new_value))}${c.unit && !["Fri UTC", "Sun UTC"].includes(c.unit) ? ` ${fmt.esc(c.unit)}` : ""}</span></li>`).join("")
      : '<li><span class="muted">No changes yet.</span><span></span></li>';

    const a = data.account || {};
    $("#settings-foot").innerHTML = `Magic number <span class="num">${data.magic}</span>${a.login ? ` · Account <span class="num">${a.login}</span> · ${fmt.esc(a.server || "")}` : ""} · Times are UTC · ` +
      'Charts by <a href="https://www.tradingview.com/" target="_blank" rel="noopener">TradingView Lightweight Charts™</a>';
  }

  function fmtVal(v) {
    if (v === true) return "on";
    if (v === false) return "off";
    return String(v);
  }

  async function save(input) {
    const key = input.dataset.key;
    const f = data.fields.find((x) => x.key === key);
    const err = $(`[data-err="${key}"]`);
    let value = input.value.trim();
    if (f.type === "float" || f.type === "int") {
      const n = Number(value);
      if (value === "" || Number.isNaN(n) || (f.type === "int" && !Number.isInteger(n)) || n < f.min || n > f.max) {
        input.classList.add("invalid");
        err.textContent = f.type === "int" && !Number.isInteger(n) ? "Must be a whole number" : `Must be between ${show(f, f.min)} and ${show(f, f.max)}`;
        return;
      }
      value = n;
    } else if (f.type === "time") {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) { input.classList.add("invalid"); err.textContent = "Use HH:MM, 24-hour"; return; }
    } else if (f.type === "choice" && key === "signal.atr_method") {
      value = Number(value);
    }
    if (String(value) === String(f.value)) { input.classList.remove("invalid"); err.textContent = ""; return; }
    try {
      await api("/api/settings", { method: "PUT", body: { changes: { [key]: value } } });
      input.classList.remove("invalid");
      err.textContent = "";
      f.value = value;
      const saved = $(`[data-saved="${key === "risk.max_stop_atr" ? "risk.min_stop_atr" : key}"]`);
      if (saved) { saved.textContent = "Saved · applies from the next bar"; setTimeout(() => { saved.textContent = ""; }, 2500); }
      reload(false);
    } catch (e) {
      input.classList.add("invalid");
      const target = $(`[data-err="${(e.data && e.data.field) || key}"]`) || err;
      target.textContent = e.message;
    }
  }

  async function reload(full = true) {
    try {
      data = await api("/api/settings");
      if (full) render();
      else {
        const ch = data.changes || [];
        $("#settings-changes").innerHTML = ch.map((c) => `<li><span class="muted num">${fmt.dayTime(c.time)}</span><span>${fmt.esc(c.label)} ${fmt.esc(fmtVal(c.old_value))} → ${fmt.esc(fmtVal(c.new_value))}${c.unit && !["Fri UTC", "Sun UTC"].includes(c.unit) ? ` ${fmt.esc(c.unit)}` : ""}</span></li>`).join("");
      }
    } catch (e) {
      $("#settings-groups").innerHTML = `<p class="empty">Couldn't load settings: ${fmt.esc(e.message)}</p>`;
    }
  }

  async function setDry(value) {
    try {
      await api("/api/dry-run", { method: "POST", body: { value } });
      Opus.toast(value ? "Dry run on: trades are simulated from the next bar." : "Dry run off: approved trades are sent to the demo account from the next bar.");
      await reload();
      Opus.refreshState();
    } catch (e) { Opus.toast(e.message, 4500); }
  }

  Opus.views.settings = {
    init() {
      const groups = $("#settings-groups");
      groups.addEventListener("change", (e) => { if (e.target.matches("[data-key]")) save(e.target); });
      groups.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches("[data-key]")) { e.preventDefault(); save(e.target); } });
      groups.addEventListener("input", (e) => { if (e.target.matches(".invalid")) { e.target.classList.remove("invalid"); const er = $(`[data-err="${e.target.dataset.key}"]`); if (er) er.textContent = ""; } });
      $("#dry-switch").addEventListener("click", async () => {
        const on = $("#dry-switch").getAttribute("aria-checked") === "true";
        if (!on) { setDry(true); return; }
        await reload();
        if (!data.can_switch_mode) { Opus.toast("Close the open position or pending order first.", 4500); return; }
        const a = data.account || {};
        const type = `${a.trade_mode === 0 ? "Demo" : "Not demo"} · ${a.margin_mode === 2 ? "Hedging" : "Netting"} · ${a.currency || ""}`;
        $("#dlg-dry-account").innerHTML = `<dt>Account</dt><dd class="num">${fmt.esc(a.login ?? "—")}</dd><dt>Server</dt><dd>${fmt.esc(a.server || "—")}</dd><dt>Type</dt><dd>${fmt.esc(type)}</dd>`;
        Opus.openDialog("dlg-dry");
      });
      $("#dlg-dry-ok").addEventListener("click", () => { $("#dlg-dry").close(); setDry(false); });
      Opus.on("settings", () => { if (Opus.view === "settings") reload(); });
      Opus.on("status", (s) => {
        if (Opus.view !== "settings" || !data) return;
        const sw = $("#dry-switch");
        if (sw.getAttribute("aria-checked") !== String(s.dry_run)) render();
      });
    },
    show() { reload(); },
  };
})();
