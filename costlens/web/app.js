/* CostLens UI. All untrusted text is inserted with textContent, never innerHTML. */
(function () {
  "use strict";
  const C = window.CostCharts;
  const state = { previous: null, status: null, dims: null, history: [], view: "chat" };
  const $ = id => document.getElementById(id);

  // ------------------------------------------------------------ helpers
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(n.dataset, v);
      else if (k === "style") n.style.cssText = v; // CSSOM: allowed by the strict CSP
      else n.setAttribute(k, v === true ? "" : v);
    }
    kids.flat(Infinity).forEach(c => { if (c !== null && c !== undefined && c !== false) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  async function api(path, opts) {
    const o = Object.assign({ headers: {} }, opts || {});
    if (o.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(o.body); }
    let res;
    try { res = await fetch(path, o); } catch (e) { throw new Error("Cannot reach the CostLens server. Is it still running?"); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
    return data;
  }
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 2600);
  }
  const icon = {
    logo: () => svgIcon('<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" stroke-width="2.2"/><path d="M15.5 15.5 21 21" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>', "0 0 24 24"),
    pin: () => svgIcon('<path d="M7 2h6l-1 5 3 3H5l3-3-1-5zM10 10v8" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linejoin="round" stroke-linecap="round"/>'),
    dl: () => svgIcon('<path d="M10 3v10M6 9l4 4 4-4M4 16h12" stroke="currentColor" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'),
    table: () => svgIcon('<rect x="3" y="4" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.6" fill="none"/><path d="M3 8h14M3 12h14M8 4v12" stroke="currentColor" stroke-width="1.4"/>'),
    chart: () => svgIcon('<path d="M4 16V9M8 16V5M12 16v-5M16 16V7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'),
    x: () => svgIcon('<path d="M5 5l10 10M15 5 5 15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'),
  };
  function svgIcon(inner, vb) {
    const t = document.createElement("template");
    t.innerHTML = `<svg viewBox="${vb || "0 0 20 20"}" aria-hidden="true">${inner}</svg>`; // static markup only
    return t.content.firstChild;
  }
  function addDays(iso, n) { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
  function monthStart(iso) { return iso.slice(0, 8) + "01"; }
  function addMonths(iso, n) { const d = new Date(monthStart(iso) + "T00:00:00Z"); d.setUTCMonth(d.getUTCMonth() + n); return d.toISOString().slice(0, 10); }
  function fmtDate(iso) { if (!iso) return "—"; const [y, m, d] = iso.slice(0, 10).split("-"); return `${+d} ${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][+m - 1]} ${y}`; }
  function fmtTs(iso) { if (!iso) return "—"; return fmtDate(iso) + " " + iso.slice(11, 16) + " UTC"; }
  function pctClass(p) { if (p === null || p === undefined) return ""; return p.startsWith("-") ? "down" : "up"; }
  function statusPill(st) { return h("span", { class: "status-pill st-" + st, text: st.replace(/_/g, " ") }); }

  // ------------------------------------------------------------ presets
  function presets() {
    const ref = (state.status && state.status.reference_date) || new Date().toISOString().slice(0, 10);
    const ms = monthStart(ref);
    return [
      ["last7", "Last 7 days", addDays(ref, -7), ref],
      ["last30", "Last 30 days", addDays(ref, -30), ref],
      ["last90", "Last 90 days", addDays(ref, -90), ref],
      ["mtd", "This month", ms, ref],
      ["lastm", "Last month", addMonths(ms, -1), ms],
      ["last3m", "Last 3 months", addMonths(ms, -3), ms],
    ];
  }

  // ------------------------------------------------------------ sidebar / status
  function renderDataCard() {
    const s = state.status, card = $("dataCard");
    card.textContent = "";
    if (!s) { card.appendChild(h("div", { class: "muted", text: "Data status unavailable." })); return; }
    const head = h("div", { class: "dc-head" }, "Data", s.synthetic ? h("span", { class: "badge-synth", text: "SYNTHETIC" }) : null);
    const cur = s.sources.find(x => x.key === "cur2");
    const missing = s.coverage.periods.filter(p => p.status === "missing").map(p => p.period);
    card.append(head,
      h("div", { class: "row" }, "Data through", h("b", { text: fmtDate(s.data_through) })),
      h("div", { class: "row" }, "Accounts", h("b", { text: String(s.accounts.length) })),
      h("div", { class: "row" }, "CUR 2.0", h("b", { text: cur && cur.state === "connected" ? "Loaded" : "No data" })),
      h("div", { class: "row" }, "Other sources", h("b", { text: `${s.sources.filter(x => x.state === "not_connected").length} not connected` })),
      missing.length ? h("div", { class: "row" }, "Missing months", h("b", { class: "up", text: missing.join(", ") })) : null,
      s.delivery_issues.length ? h("div", { class: "row" }, "Rejected deliveries", h("b", { class: "up", text: String(s.delivery_issues.length) })) : null,
      h("button", { class: "link", type: "button", text: "View coverage and deliveries →", onclick: () => showView("data") }));
    $("modePill").title = s.model.detail;
    $("modePill").setAttribute("aria-label", s.model.label + ". " + s.model.detail);
    $("dataPill").hidden = !s.synthetic;
    if (s.synthetic) $("composerNote").textContent = "Synthetic demo data. Numbers are computed by code from CUR line items; no AI model is used.";
  }
  async function loadStatus() {
    try { state.status = await api("/api/status"); }
    catch (e) { state.status = null; $("dataCard").textContent = ""; $("dataCard").appendChild(h("div", { class: "notice error", text: e.message })); return; }
    renderDataCard();
  }
  async function refreshPinCount() {
    try { const r = await api("/api/pins"); $("pinCount").textContent = r.pins.length ? String(r.pins.length) : ""; } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------ views
  function showView(v) {
    state.view = v;
    ["chat", "dashboards", "data", "anomalies", "budgets"].forEach(x => { $("view-" + x).hidden = x !== v; });
    document.querySelectorAll(".nav-item[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === v));
    $("topbarTitle").textContent = { chat: "Visualize", dashboards: "Saved dashboards", data: "Data & coverage",
                                     anomalies: "Anomalies", budgets: "Budgets & forecasting" }[v];
    $("app").classList.remove("nav-open");
    C.hideTip();
    if (v === "dashboards") renderDashboards();
    if (v === "data") renderDataPage();
    if (v === "anomalies") window.CostLensFeatures.renderAnomalies();
    if (v === "budgets") window.CostLensFeatures.renderBudgets();
    if (v === "chat") $("question").focus();
  }

  // ------------------------------------------------------------ chat
  const STARTERS = [
    ["Breakdown", "Show cost by service for last month"],
    ["Investigate", "Why did costs increase in October compared to September?"],
    ["Trend", "Daily EC2 cost by account for the last 30 days"],
    ["Ownership", "Top 5 teams by amortized cost this month"],
    ["Anomalies", "Any cost anomalies in the last 90 days?"],
    ["Budget", "Set up a monthly budget of $5,000 for prod-data and alert ops@example.com at 80% and 100%"],
  ];
  function renderStarters() {
    const box = $("starterSuggestions");
    box.textContent = "";
    STARTERS.forEach(([k, q]) => box.appendChild(h("button", { class: "suggestion", type: "button", onclick: () => ask(q) },
      h("small", { text: k }), q)));
  }
  function newConversation() {
    state.previous = null; state.history = [];
    const th = $("thread");
    [...th.children].forEach(c => { if (c.id !== "emptyState") c.remove(); });
    $("emptyState").hidden = false;
    renderHistory();
    showView("chat");
  }
  function renderHistory() {
    const ol = $("history");
    ol.textContent = "";
    $("historyWrap").hidden = state.history.length === 0;
    state.history.forEach(item => ol.appendChild(h("li", {}, h("button", { type: "button", title: item.q, text: item.q,
      onclick: () => { showView("chat"); item.el.scrollIntoView({ behavior: "smooth", block: "start" }); } }))));
  }
  function scrollToBottom() { const th = $("thread"); th.scrollTop = th.scrollHeight; }

  async function ask(question) {
    question = (question || "").trim();
    if (!question) return;
    showView("chat");
    $("emptyState").hidden = true;
    const userMsg = h("div", { class: "msg msg-user" }, h("div", { class: "bubble", text: question }));
    const body = h("div", { class: "msg-body" },
      h("div", { class: "thinking" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), "Reading cost data…"));
    const asst = h("div", { class: "msg msg-assistant" }, h("div", { class: "avatar" }, icon.logo()), body);
    $("thread").append(userMsg, asst);
    state.history.push({ q: question, el: userMsg });
    renderHistory();
    scrollToBottom();
    try {
      const resp = await api("/api/ask", { method: "POST", body: { question, previous: state.previous } });
      body.textContent = "";
      renderAssistant(body, resp);
    } catch (e) {
      body.textContent = "";
      body.appendChild(h("div", { class: "notice error" }, h("div", { text: e.message }),
        h("button", { class: "btn-ghost", type: "button", text: "Retry", onclick: () => { asst.remove(); userMsg.remove(); state.history.pop(); ask(question); } })));
    }
    userMsg.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderAssistant(body, resp) {
    if (resp.kind) { window.CostLensFeatures.renderChatSpecial(body, resp); followups(body, resp.suggestions); return; }
    const it = resp.interpretation;
    if (it.status !== "ok") {
      if (it.module && it.module !== "visualize") body.appendChild(h("div", { class: "module-note", text: (it.module === "optimize" ? "Optimize" : "Govern") + " · not in this release" }));
      body.appendChild(h("p", { class: "msg-text", text: resp.message }));
      followups(body, resp.suggestions);
      return;
    }
    if (it.understood.length) {
      body.appendChild(h("div", { class: "interp" }, "Understood:", it.understood.map(u => h("span", { class: "chip", text: u }))));
    }
    if (it.assumptions.length) body.appendChild(h("div", { class: "assumptions" }, h("ul", {}, it.assumptions.map(a => h("li", { text: a })))));
    body.appendChild(h("p", { class: "msg-text", text: resp.message }));
    const holder = h("div");
    body.appendChild(holder);
    mountCard(holder, resp.result, { onResult: r => { if (r.status === "ok") state.previous = r.query; } });
    if (resp.result && resp.result.status === "ok") state.previous = resp.result.query;
    followups(body, resp.suggestions);
  }
  function followups(body, list) {
    if (!list || !list.length) return;
    body.appendChild(h("div", { class: "followups" }, list.map(q => h("button", { class: "followup", type: "button", text: q, onclick: () => ask(q) }))));
  }

  // ------------------------------------------------------------ cards
  function mountCard(holder, result, opts) {
    holder.textContent = "";
    holder.appendChild(buildCard(result, Object.assign({}, opts, { holder })));
  }
  async function rerun(holder, query, opts) {
    const card = holder.firstChild;
    if (card) card.classList.add("loading");
    try {
      const resp = await api("/api/query", { method: "POST", body: { query } });
      mountCard(holder, resp.result, opts);
      if (opts.onResult) opts.onResult(resp.result);
    } catch (e) {
      if (card) {
        card.classList.remove("loading");
        const old = card.querySelector(".notice.error.rerun");
        if (old) old.remove();
        card.insertBefore(h("div", { class: "notice error rerun", text: e.message }), card.children[1] || null);
      }
    }
  }

  function buildCard(result, opts) {
    if (!result) return h("div", { class: "notice error", text: "No result returned." });
    if (result.status === "insufficient_data") return insufficientCard(result, opts);
    return result.kind === "variance" ? varianceCard(result, opts) : exploreCard(result, opts);
  }

  function insufficientCard(r, opts) {
    const periods = (r.coverage && r.coverage.periods) || [];
    const card = h("div", { class: "card" },
      h("div", { class: "card-head" }, h("div", {}, h("div", { class: "card-title", text: "Not enough data to answer" }),
        h("div", { class: "card-range", text: `${fmtDate(r.query.start)} – ${fmtDate(addDays(r.query.end, -1))}` })),
        opts.pinId ? pinRemoveBtn(opts) : null),
      h("div", { class: "notice neutral", text: r.reason + " CostLens does not estimate or fill in missing billing data." }),
      periods.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", { text: "Billing period" }), h("th", { text: "Status" }), h("th", { text: "Data through" }))),
        h("tbody", {}, periods.map(p => h("tr", {}, h("td", { text: p.period }), h("td", {}, statusPill(p.status)), h("td", { text: fmtDate(p.data_through) })))))) : null);
    return card;
  }

  function header(r, opts, extraActions) {
    const actions = h("div", { class: "card-actions" }, extraActions,
      h("button", { class: "btn-ghost", type: "button", title: "Download CSV", onclick: () => downloadCsv(r) }, icon.dl(), "CSV"),
      opts.pinId ? pinRemoveBtn(opts) : h("button", { class: "btn-ghost", type: "button", title: "Pin to saved dashboards", onclick: () => pin(r) }, icon.pin(), "Pin"));
    return h("div", { class: "card-head" },
      h("div", {}, h("div", { class: "card-title", text: r.title }), h("div", { class: "card-range", text: r.kind === "variance" ? `${r.current.label} vs ${r.baseline.label}` : `${r.range_label} · ${r.metric_label}` })),
      actions);
  }
  function pinRemoveBtn(opts) {
    return h("button", { class: "btn-ghost", type: "button", title: "Remove from dashboards", onclick: async () => {
      try { await api("/api/pins/" + opts.pinId, { method: "DELETE", body: {} }); toast("Removed from saved dashboards"); refreshPinCount(); renderDashboards(); }
      catch (e) { toast(e.message); }
    } }, icon.x(), "Remove");
  }
  function kpi(label, value, sub, valueClass) {
    return h("div", { class: "kpi" }, h("div", { class: "kpi-label", text: label }), h("div", { class: "kpi-value " + (valueClass || ""), text: value }),
      sub ? h("div", { class: "kpi-sub", text: sub }) : null);
  }
  function select(label, value, options, onchange) {
    const id = "c" + Math.random().toString(36).slice(2, 9);
    const sel = h("select", { id, onchange: e => onchange(e.target.value) }, options.map(([v, t]) => h("option", { value: v, text: t })));
    sel.value = value;
    return h("div", { class: "ctl" }, h("label", { for: id, text: label }), sel);
  }
  function groupOptions(includeNone) {
    const o = [["service", "Service"], ["account", "Account"], ["region", "Region"], ["usage_type", "Usage type"], ["line_item_type", "Charge type"], ["resource", "Resource"]];
    const tags = (state.dims && state.dims.tags) ? Object.keys(state.dims.tags) : [];
    tags.forEach(t => o.push(["tag:" + t, "Tag: " + t.replace(/^user:/, "")]));
    if (includeNone) o.unshift(["none", "No grouping"]);
    return o;
  }
  function filterChips(r, opts) {
    const f = r.query.filters || {};
    const keys = Object.keys(f);
    if (!keys.length) return null;
    const names = {};
    if (state.status) state.status.accounts.forEach(a => { names[a.id] = a.name; });
    return h("div", { class: "filters-row" }, "Filters:", keys.flatMap(k => f[k].map(v => h("span", { class: "chip removable" },
      `${k.replace(/^tag:user:/, "")}: ${k === "account" && names[v] ? names[v] : v}`,
      h("button", { type: "button", "aria-label": `Remove filter ${k} ${v}`, text: "×", onclick: () => {
        const q = JSON.parse(JSON.stringify(r.query));
        q.filters[k] = q.filters[k].filter(x => x !== v);
        if (!q.filters[k].length) delete q.filters[k];
        rerun(opts.holder, q, opts);
      } })))));
  }
  function warningsBox(r) {
    if (!r.warnings || !r.warnings.length) return null;
    return h("div", { class: "notice" }, h("ul", {}, r.warnings.map(w => h("li", { text: w }))));
  }
  function calcBox(r) {
    const c = r.calculation;
    const src = c.sources || [];
    return h("details", { class: "calc" }, h("summary", { text: "How this was calculated" }),
      h("div", { class: "calc-body" },
        h("p", { text: c.formula }),
        h("dl", {},
          c.line_items_used !== undefined ? [h("dt", { text: "Line items used" }), h("dd", { text: c.line_items_used.toLocaleString("en-US") })] : null,
          c.groups_found !== undefined ? [h("dt", { text: "Groups found" }), h("dd", { text: String(c.groups_found) })] : null,
          c.baseline_window ? [h("dt", { text: "Baseline window" }), h("dd", { text: `${c.baseline_window[0]} to ${c.baseline_window[1]} (end exclusive)` })] : null,
          c.current_window ? [h("dt", { text: "Current window" }), h("dd", { text: `${c.current_window[0]} to ${c.current_window[1]} (end exclusive)` })] : null,
          h("dt", { text: "Query" }), h("dd", { text: JSON.stringify(r.query) })),
        h("pre", { text: c.sql_template + (c.parameters.length ? "\n-- parameters: " + c.parameters.join(", ") : "") }),
        src.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Billing period", "Delivery (execution id)", "Exported", "Status", "Data through"].map(t => h("th", { text: t })))),
          h("tbody", {}, src.map(s => h("tr", {}, h("td", { text: s.billing_period }), h("td", { text: s.execution_id }),
            h("td", { text: fmtTs(s.export_timestamp) }), h("td", {}, statusPill(s.status)), h("td", { text: fmtDate(s.data_through) }))))))
          : null));
  }

  function exploreCard(r, opts) {
    const q = r.query;
    let view = r.chart === "table" ? "table" : "chart";
    const chartEl = h("div");
    const tableEl = h("div", { class: "tbl-wrap" });
    const toggle = h("button", { class: "btn-ghost", type: "button", title: "Switch chart / table" });
    function paintToggle() { toggle.textContent = ""; toggle.append(view === "chart" ? icon.table() : icon.chart(), view === "chart" ? "Table" : "Chart"); }
    toggle.addEventListener("click", () => { view = view === "chart" ? "table" : "chart"; paint(); });

    const kpis = h("div", { class: "kpis" },
      kpi("Total", r.total_display, `${r.days_with_data} day${r.days_with_data === 1 ? "" : "s"} of data`),
      kpi("Daily average", r.daily_average_display),
      r.comparison ? kpi("vs previous period", (r.comparison.change_pct === null ? "—" : (r.comparison.change_pct.startsWith("-") ? "" : "+") + r.comparison.change_pct + "%"),
        `${r.comparison.total_display} · ${r.comparison.label}`, pctClass(r.comparison.change_pct))
        : kpi("vs previous period", "—", "Not enough data"),
      r.series.length && q.group_by ? kpi("Top " + (r.group_label || "").toLowerCase(), r.series[0].name.length > 22 ? r.series[0].name.slice(0, 21) + "…" : r.series[0].name, `${r.series[0].total_display} · ${r.series[0].share_pct}%`) : null);

    const pr = presets();
    const match = pr.find(p => p[2] === q.start && p[3] === q.end);
    const from = h("input", { type: "date", value: q.start, "aria-label": "From date" });
    const to = h("input", { type: "date", value: addDays(q.end, -1), "aria-label": "To date" });
    const customCtl = h("div", { class: "ctl" }, h("label", { text: "From – to" }), h("div", { style: "display:flex;gap:4px" }, from, to));
    customCtl.hidden = !!match;
    const change = patch => rerun(opts.holder, Object.assign({}, q, patch), opts);
    const onCustom = () => { if (from.value && to.value && from.value <= to.value) change({ start: from.value, end: addDays(to.value, 1) }); };
    from.addEventListener("change", onCustom); to.addEventListener("change", onCustom);

    const controls = h("div", { class: "controls" },
      select("Date range", match ? match[0] : "custom", pr.map(p => [p[0], p[1]]).concat([["custom", "Custom…"]]), v => {
        if (v === "custom") { customCtl.hidden = false; return; }
        const p = pr.find(x => x[0] === v); change({ start: p[2], end: p[3] });
      }),
      customCtl,
      select("Granularity", q.granularity, [["daily", "Daily"], ["monthly", "Monthly"], ["total", "Total"]], v => change({ granularity: v, chart: "auto" })),
      select("Group by", q.group_by || "none", groupOptions(true), v => change({ group_by: v === "none" ? null : v })),
      select("Cost", q.metric, [["unblended", "Unblended"], ["amortized", "Amortized"]], v => change({ metric: v })),
      select("Chart", q.chart, [["auto", "Auto"], ["stacked", "Stacked bar"], ["bar", "Bar (total)"], ["line", "Line"], ["hbar", "Ranked bar"], ["table", "Table only"]], v => change({ chart: v })));

    function paintTable() {
      tableEl.textContent = "";
      tableEl.appendChild(h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", { text: r.group_label || "Group" }), h("th", { class: "num", text: "Cost" }), h("th", { class: "num", text: "Share" }))),
        h("tbody", {}, r.series.map((s, i) => h("tr", {},
          h("td", {}, h("span", { class: "sw", style: "background:" + C.colorFor(i, s.key) }), s.name + (s.other_count ? ` (${s.other_count} more)` : "")),
          h("td", { class: "num", text: s.total_display }), h("td", { class: "num", text: s.share_pct === null ? "—" : s.share_pct + "%" })))),
        h("tfoot", {}, h("tr", {}, h("td", { text: "Total" }), h("td", { class: "num", text: r.total_display }), h("td", { class: "num", text: "100%" })))));
    }
    function paint() {
      paintToggle();
      chartEl.hidden = view !== "chart";
      tableEl.hidden = view !== "table";
      if (view === "chart") C.render(chartEl, r, r.chart === "table" ? "stacked" : r.chart);
      paintTable();
    }
    const card = h("div", { class: "card" }, header(r, opts, toggle), filterChips(r, opts), kpis, opts.compact ? null : controls,
      chartEl, tableEl, warningsBox(r), calcBox(r));
    requestAnimationFrame(paint);
    observeResize(chartEl, () => { if (view === "chart") C.render(chartEl, r, r.chart === "table" ? "stacked" : r.chart); });
    return card;
  }

  function varianceCard(r, opts) {
    const q = r.query;
    const chartEl = h("div");
    const change = patch => rerun(opts.holder, Object.assign({}, q, patch), opts);
    const kpis = h("div", { class: "kpis" },
      kpi("Baseline", r.baseline.total_display, `${r.baseline.label} · ${r.baseline.days} days`),
      kpi("Current", r.current.total_display, `${r.current.label} · ${r.current.days} days`),
      kpi("Change", r.delta_display, r.delta_pct === null ? "" : (r.delta_pct.startsWith("-") ? "" : "+") + r.delta_pct + "%", pctClass(r.delta_pct)),
      kpi("Per day", r.current.per_day_display, `was ${r.baseline.per_day_display}`));
    const controls = h("div", { class: "controls" },
      select("Compare by", q.group_by, groupOptions(false), v => change({ group_by: v })),
      select("Cost", q.metric, [["unblended", "Unblended"], ["amortized", "Amortized"]], v => change({ metric: v })));
    const table = h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", { text: r.group_label }), h("th", { class: "num", text: "Baseline" }), h("th", { class: "num", text: "Current" }), h("th", { class: "num", text: "Change" }), h("th", { class: "num", text: "%" }))),
      h("tbody", {}, r.rows.map(x => h("tr", {}, h("td", { text: x.name + (x.other_count ? ` (${x.other_count} more)` : "") }),
        h("td", { class: "num", text: x.base_display }), h("td", { class: "num", text: x.current_display }),
        h("td", { class: "num " + (x.delta_cents > 0 ? "up" : x.delta_cents < 0 ? "down" : ""), text: (x.delta_cents > 0 ? "+" : "") + x.delta_display }),
        h("td", { class: "num", text: x.change_pct === null ? "new" : x.change_pct + "%" }))))));
    const groups = r.drivers.groups || [];
    const drivers = groups.length ? h("div", { class: "section" }, h("div", { class: "section-title", text: "What drove the increase" }),
      groups.map(g => h("div", { class: "driver" },
        h("div", { class: "d-main" }, `${g.usage_type} · ${g.service}`, g.new_resources ? h("span", { class: "new-tag", text: `${g.new_resources} new` }) : null),
        h("div", { class: "d-meta", text: `${g.account} · ${g.resources} resource${g.resources > 1 ? "s" : ""}` + (g.first_seen ? ` · first seen ${fmtDate(g.first_seen)}` : "") + ` · ${g.base_display} → ${g.current_display}` }),
        h("div", { class: "d-val up", text: "+" + g.delta_display }))),
      h("details", { class: "calc" }, h("summary", { text: "Resource-level evidence" }), h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, ["Resource", "Usage type", "Account", "Baseline", "Current", "Change"].map((t, i) => h("th", { class: i > 2 ? "num" : "", text: t })))),
        h("tbody", {}, r.drivers.increases.concat(r.drivers.decreases).map(d => h("tr", {},
          h("td", {}, d.resource_id, d.new_in_window ? h("span", { class: "new-tag", text: "new " + (d.first_seen ? fmtDate(d.first_seen) : "") }) : null),
          h("td", { text: d.usage_type }), h("td", { text: d.account }), h("td", { class: "num", text: d.base_display }),
          h("td", { class: "num", text: d.current_display }), h("td", { class: "num " + (d.delta_cents > 0 ? "up" : "down"), text: (d.delta_cents > 0 ? "+" : "") + d.delta_display }))))))))
      : null;
    const card = h("div", { class: "card" }, header(r, opts), filterChips(r, opts), kpis, opts.compact ? null : controls, chartEl, table, drivers, warningsBox(r), calcBox(r));
    requestAnimationFrame(() => C.render(chartEl, r));
    observeResize(chartEl, () => C.render(chartEl, r));
    return card;
  }

  function observeResize(el, fn) {
    if (!window.ResizeObserver) return;
    let w = 0, t;
    new ResizeObserver(entries => {
      const nw = Math.round(entries[0].contentRect.width);
      if (Math.abs(nw - w) < 24) return;
      w = nw; clearTimeout(t); t = setTimeout(fn, 120);
    }).observe(el);
  }

  // ------------------------------------------------------------ actions
  function csvCell(v) { const s = String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
  function downloadCsv(r) {
    let rows;
    if (r.kind === "variance") {
      rows = [[r.group_label, "baseline_usd", "current_usd", "change_usd", "change_pct"]].concat(r.rows.map(x => [x.name, x.base, x.current, x.delta, x.change_pct ?? ""]));
    } else {
      rows = [["bucket"].concat(r.series.map(s => s.name), ["total"])];
      r.buckets.forEach((b, i) => rows.push([b].concat(r.series.map(s => (s.values_cents[i] / 100).toFixed(2)), [(r.bucket_totals_cents[i] / 100).toFixed(2)])));
    }
    rows.unshift([`# ${r.title} | ${r.kind === "variance" ? r.current.label + " vs " + r.baseline.label : r.range_label} | ${r.metric_label} USD${state.status && state.status.synthetic ? " | SYNTHETIC DATA" : ""}`]);
    const blob = new Blob([rows.map(x => x.map(csvCell).join(",")).join("\n") + "\n"], { type: "text/csv" });
    const a = h("a", { href: URL.createObjectURL(blob), download: (r.title || "costlens").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") + ".csv" });
    document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }
  async function pin(r) {
    try {
      await api("/api/pins", { method: "POST", body: { title: r.title, query: r.query } });
      toast("Pinned to saved dashboards");
      refreshPinCount();
    } catch (e) { toast(e.message); }
  }

  // ------------------------------------------------------------ dashboards page
  async function renderDashboards() {
    const grid = $("pinGrid");
    grid.textContent = "";
    grid.appendChild(h("div", { class: "empty-pins" }, h("div", { class: "thinking", style: "justify-content:center" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), "Loading saved views…")));
    let pins;
    try { pins = (await api("/api/pins")).pins; }
    catch (e) { grid.textContent = ""; grid.appendChild(h("div", { class: "notice error", text: e.message })); return; }
    grid.textContent = "";
    if (!pins.length) {
      grid.appendChild(h("div", { class: "empty-pins" }, h("p", { text: "No saved dashboards yet." }),
        h("p", { class: "muted", text: "Ask a question in Visualize, then press Pin on any answer to keep it here." }),
        h("button", { class: "followup", type: "button", text: "Go to Visualize", onclick: () => showView("chat") })));
      return;
    }
    pins.forEach(p => {
      const holder = h("div", {}, h("div", { class: "card loading" }, h("div", { class: "card-head" }, h("div", { class: "card-title", text: p.title })), h("div", { class: "notice neutral", text: "Running query…" })));
      grid.appendChild(holder);
      const opts = { holder, pinId: p.id, compact: false };
      api("/api/query", { method: "POST", body: { query: p.query } })
        .then(resp => mountCard(holder, resp.result, opts))
        .catch(e => { holder.textContent = ""; holder.appendChild(h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "card-title", text: p.title }), h("div", { class: "card-actions" }, pinRemoveBtn(opts))), h("div", { class: "notice error", text: e.message }))); });
    });
  }

  // ------------------------------------------------------------ data page
  async function renderDataPage() {
    const page = $("dataPage");
    page.textContent = "";
    await loadStatus();
    const s = state.status;
    if (!s) { page.appendChild(h("div", { class: "notice error", text: "Could not load data status." })); return; }
    let audit = [];
    try { audit = (await api("/api/audit")).events; } catch (e) { /* shown as empty */ }
    page.append(
      h("div", { class: "page-head" }, h("h2", { text: "Data & coverage" }),
        h("p", { class: "muted", text: `Mode: ${s.synthetic ? "synthetic demo data (not real AWS)" : s.data_mode}. Reference date ${fmtDate(s.reference_date)}. Data through ${fmtDate(s.data_through)}${s.lag_days !== null ? ` (${s.lag_days} day lag, ${s.freshness})` : ""}.` })),
      h("div", { class: "data-grid" },
        s.synthetic ? h("div", { class: "notice", text: "All accounts, resources and amounts here are synthetic fixtures generated for local testing. They resemble the AWS CUR 2.0 export format but are not real billing data." }) : null,
        panel("Billing period coverage", h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Period", "Status", "Active delivery", "Exported", "Data through", "Revisions", "Note"].map(t => h("th", { text: t })))),
          h("tbody", {}, s.coverage.periods.map(p => h("tr", {}, h("td", { text: p.period }), h("td", {}, statusPill(p.status)),
            h("td", { text: p.execution_id || "—" }), h("td", { text: fmtTs(p.export_timestamp) }), h("td", { text: fmtDate(p.data_through) }),
            h("td", { text: String(p.revisions_applied) }), h("td", { class: "audit-detail", text: p.newer_delivery_rejected || (p.status === "missing" ? "No delivery received" : p.status === "in_progress" ? "Not final; may be revised" : "") })))))),
          h("p", { text: "A revision replaces the whole billing period. Duplicate deliveries are skipped. Incomplete or invalid deliveries are rejected and the previous data is kept. Nothing is ever added twice." })),
        panel("Data sources", h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Source", "Purpose", "State", "Detail"].map(t => h("th", { text: t })))),
          h("tbody", {}, s.sources.map(x => h("tr", {}, h("td", { text: x.name }), h("td", { text: x.purpose }), h("td", {}, statusPill(x.state)), h("td", { text: x.detail }))))))),
        panel("Access and permissions", h("p", { text: s.permissions.detail }),
          h("p", { text: "Planned for the live pilot: a read-only role in the customer account with s3:GetObject and s3:ListBucket on the export prefix only (plus kms:Decrypt if needed). Not implemented yet." })),
        panel("Delivery log", h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Period", "Execution id", "Exported", "Outcome", "Rows", "Detail"].map(t => h("th", { text: t })))),
          h("tbody", {}, s.deliveries.slice().reverse().map(d => h("tr", {}, h("td", { text: d.billing_period || "—" }), h("td", { text: d.execution_id || "—" }),
            h("td", { text: fmtTs(d.export_timestamp) }), h("td", {}, statusPill(d.status)), h("td", { class: "num", text: String(d.row_count) }), h("td", { class: "audit-detail", text: d.detail }))))))),
        panel("Audit log (latest 100)", audit.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, ["Time", "Event", "Detail"].map(t => h("th", { text: t })))),
          h("tbody", {}, audit.map(a => h("tr", {}, h("td", { text: fmtTs(a.ts) }), h("td", { text: a.event_type }), h("td", { class: "audit-detail", text: a.detail })))))) : h("p", { text: "No audit events yet." }))));
  }
  function panel(title, ...kids) { return h("div", { class: "panel" }, h("h3", { text: title }), kids); }

  // ------------------------------------------------------------ boot
  function wire() {
    const ta = $("question"), send = $("sendBtn");
    const resize = () => { ta.style.height = "auto"; ta.style.height = Math.min(160, ta.scrollHeight) + "px"; send.disabled = !ta.value.trim(); };
    ta.addEventListener("input", resize);
    ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); } });
    $("composer").addEventListener("submit", e => { e.preventDefault(); const q = ta.value; ta.value = ""; resize(); ask(q); });
    document.querySelectorAll(".nav-item[data-view]").forEach(b => b.addEventListener("click", () => showView(b.dataset.view)));
    $("newChat").addEventListener("click", newConversation);
    $("menuBtn").addEventListener("click", () => $("app").classList.toggle("nav-open"));
    $("scrim").addEventListener("click", () => $("app").classList.remove("nav-open"));
    $("thread").addEventListener("scroll", C.hideTip, { passive: true });
  }
  async function boot() {
    wire();
    renderStarters();
    await loadStatus();
    try { state.dims = await api("/api/dimensions"); } catch (e) { state.dims = null; }
    refreshPinCount();
    window.CostLensFeatures.refreshCounts();
    $("question").focus();
  }
  async function openQuery(query, label) {
    showView("chat");
    $("emptyState").hidden = true;
    const userMsg = h("div", { class: "msg msg-user" }, h("div", { class: "bubble", text: label }));
    const holder = h("div");
    const body = h("div", { class: "msg-body" }, h("p", { class: "msg-text", text: "Opened directly from the evidence, with these exact filters:" }), holder);
    $("thread").append(userMsg, h("div", { class: "msg msg-assistant" }, h("div", { class: "avatar" }, icon.logo()), body));
    state.history.push({ q: label, el: userMsg }); renderHistory();
    holder.appendChild(h("div", { class: "thinking" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), "Running query…"));
    try {
      const resp = await api("/api/query", { method: "POST", body: { query } });
      mountCard(holder, resp.result, { onResult: r => { if (r.status === "ok") state.previous = r.query; } });
      if (resp.result.status === "ok") state.previous = resp.result.query;
    } catch (e) { holder.textContent = ""; holder.appendChild(h("div", { class: "notice error", text: e.message })); }
    userMsg.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  document.addEventListener("DOMContentLoaded", boot);
  window.CostLens = { ask, showView };
  window.CostLensUI = { h, api, toast, kpi, statusPill, fmtDate, fmtTs, addDays, mountCard, showView, ask, openQuery,
                        state, icon, loadStatus };
})();
