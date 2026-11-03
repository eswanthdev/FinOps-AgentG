/* CostLens: Anomalies and Budgets & forecasting pages, plus their chat cards. */
(function () {
  "use strict";
  const U = () => window.CostLensUI;
  const C = window.CostCharts;
  const local = { anomalyTab: "open", lastAnomalies: null };

  function el(...a) { return U().h(...a); }
  function pill(st, text) { return el("span", { class: "status-pill st-" + st, text: text || st.replace(/_/g, " ") }); }
  const STATE_TEXT = { on_track: "On track", warning: "Alert triggered", forecast_exceeded: "Forecast over budget",
                       exceeded: "Over budget", no_data: "No data" };
  const DELIVERY_TEXT = { sent: "Email sent", failed: "Email failed", not_sent_no_provider: "Saved to outbox (email not configured)",
                          no_recipients: "No recipients" };

  async function refreshCounts() {
    try {
      const a = await U().api("/api/anomalies");
      document.getElementById("anomalyCount").textContent = a.open_count ? String(a.open_count) : "";
    } catch (e) { /* ignore */ }
    try {
      const b = await U().api("/api/budgets");
      document.getElementById("budgetCount").textContent = b.budgets.length ? String(b.budgets.length) : "";
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ anomalies
  function anomalyCard(a, opts) {
    const chartEl = el("div");
    const card = el("div", { class: "card", "data-anomaly": a.id },
      el("div", { class: "card-head" },
        el("div", {}, el("div", { class: "card-title", text: `${a.service} · ${a.account}` }),
          el("div", { class: "card-range", text: `${U().fmtDate(a.start)} – ${U().fmtDate(a.end)} · ${a.days} day${a.days > 1 ? "s" : ""}` }),
          el("div", { class: "pills" }, pill(a.status), pill(a.type), a.ongoing ? pill("ongoing", "still ongoing") : pill("ended", a.new_normal ? "ended – cost stayed at new level" : "ended"))),
        el("div", { class: "card-actions" })),
      el("div", { class: "kpis" },
        U().kpi("Above expected", a.impact_display, "sum over flagged days", "up"),
        U().kpi("First day", a.first_day_actual_display, `expected ${a.expected_daily_display}`),
        U().kpi("Peak excess", a.peak_excess_display, U().fmtDate(a.peak_day)),
        U().kpi("Likely owner", a.owner, a.owner_source)),
      a.explanation ? el("p", { class: "explain", text: a.explanation }) : null,
      opts.compact ? null : chartEl,
      opts.compact ? null : el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Driver (first day vs baseline average)", "Usage type", "Extra per day"].map((t, i) => el("th", { class: i === 2 ? "num" : "", text: t })))),
        el("tbody", {}, a.drivers.map(d => el("tr", {}, el("td", {}, d.resource_id, d.new ? el("span", { class: "new-tag", text: "new" }) : null),
          el("td", { text: d.usage_type }), el("td", { class: "num up", text: "+" + d.delta_display })))))),
      a.new_normal && !opts.compact ? el("div", { class: "notice neutral", text: "Cost stayed at the higher level after the alert ended. The detector compares with the last 14 days, so a lasting change becomes the new normal. Confirm this was planned." }) : null,
      el("div", { class: "card-foot" },
        opts.compact ? el("button", { class: "btn", type: "button", text: "Open in Anomalies", onclick: () => U().showView("anomalies") }) : null,
        el("button", { class: "btn", type: "button", text: "Investigate in Visualize", onclick: () => investigate(a) }),
        opts.compact ? null : statusButtons(a)));
    if (!opts.compact) requestAnimationFrame(() => C.render(chartEl, {
      kind: "explore", chart: "line", title: "Daily cost vs expected", buckets: a.context.days,
      series: [{ key: "a", name: "Actual daily cost", values_cents: a.context.actual_cents },
               { key: "e", name: "Expected (14-day median)", values_cents: a.context.expected_cents }],
    }));
    return card;
  }

  function statusButtons(a) {
    const set = async st => {
      try { await U().api("/api/anomalies/" + a.id, { method: "POST", body: { status: st } }); U().toast(`Marked as ${st}`); renderAnomalies(); refreshCounts(); }
      catch (e) { U().toast(e.message); }
    };
    const out = [];
    if (a.status !== "acknowledged") out.push(el("button", { class: "btn", type: "button", text: "Acknowledge", onclick: () => set("acknowledged") }));
    if (a.status !== "dismissed") out.push(el("button", { class: "btn", type: "button", text: "Dismiss", onclick: () => set("dismissed") }));
    if (a.status !== "open") out.push(el("button", { class: "btn", type: "button", text: "Reopen", onclick: () => set("open") }));
    return out;
  }

  function investigate(a) {
    const start = U().addDays(a.start, -14);
    const end = U().addDays(a.end, 4);
    U().openQuery({ start, end, kind: "explore", granularity: "daily", group_by: "usage_type", metric: "unblended",
                    filters: { account: [a.account_id], service: [a.service] }, top_n: 8, chart: "stacked" },
                  `Investigate: ${a.service} in ${a.account} by usage type, ${U().fmtDate(start)} – ${U().fmtDate(U().addDays(end, -1))}`);
  }

  async function renderAnomalies() {
    const page = document.getElementById("anomalyPage");
    page.textContent = "";
    page.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Scanning daily costs…"));
    let data;
    try { data = await U().api("/api/anomalies"); }
    catch (e) { page.textContent = ""; page.appendChild(el("div", { class: "notice error", text: e.message })); return; }
    local.lastAnomalies = data;
    page.textContent = "";
    const counts = { open: 0, acknowledged: 0, dismissed: 0 };
    data.anomalies.forEach(a => { counts[a.status] = (counts[a.status] || 0) + 1; });
    const tabs = el("div", { class: "tabs", role: "tablist" }, [["open", "Open"], ["acknowledged", "Acknowledged"], ["dismissed", "Dismissed"], ["all", "All"]].map(([k, t]) =>
      el("button", { type: "button", class: local.anomalyTab === k ? "on" : "", role: "tab", "aria-selected": String(local.anomalyTab === k),
                     text: `${t} (${k === "all" ? data.anomalies.length : counts[k] || 0})`, onclick: () => { local.anomalyTab = k; renderAnomalies(); } })));
    const m = data.method;
    page.append(
      el("div", { class: "page-head" }, el("h2", { text: "Anomalies" }),
        el("p", { class: "muted", text: `${data.open_count} open · ${data.open_impact_display} above expected in total. Checked against ingested CUR data up to the reference date ${U().fmtDate(data.reference_date)}.` })),
      el("div", { class: "summary-row" }, tabs),
      el("details", { class: "calc panel", style: "margin-bottom:14px" }, el("summary", { text: "How anomalies are detected" }),
        el("div", { class: "calc-body" }, el("dl", {}, Object.entries(m).map(([k, v]) => [el("dt", { text: k[0].toUpperCase() + k.slice(1) }), el("dd", { text: v })])))));
    const items = data.anomalies.filter(a => local.anomalyTab === "all" || a.status === local.anomalyTab);
    if (!items.length) {
      page.appendChild(el("div", { class: "empty-pins" }, el("p", { text: local.anomalyTab === "open" ? "No open anomalies. Costs look normal for the detection rules." : "Nothing in this list." })));
      return;
    }
    page.appendChild(el("div", { class: "stack" }, items.map(a => anomalyCard(a, {}))));
  }

  // ------------------------------------------------------------------ budgets
  function fmtScope(scope) {
    if (!scope || !scope.length) return [el("span", { class: "chip", text: "All spend" })];
    return scope.map(s => el("span", { class: "chip", text: `${s.dimension.replace(/^tag:user:/, "")}: ${s.values.join(", ").replace("(untagged)", "untagged")}` }));
  }

  function previewCard(parsed, onDone) {
    const sp = JSON.parse(JSON.stringify(parsed.spec));
    const name = el("input", { type: "text", value: sp.name, maxlength: "80", "aria-label": "Budget name" });
    const amount = el("input", { type: "number", min: "0.01", step: "0.01", value: sp.amount, "aria-label": "Amount in USD" });
    const period = el("select", { "aria-label": "Period" }, [["monthly", "Monthly"], ["quarterly", "Quarterly"], ["yearly", "Yearly"]].map(([v, t]) => el("option", { value: v, text: t })));
    period.value = sp.period;
    const recips = el("input", { type: "text", value: sp.recipients.join(", "), placeholder: "name@company.com, …", "aria-label": "Email recipients" });
    const thText = el("input", { type: "text", value: sp.thresholds.map(t => `${t.pct}% ${t.type}`).join(", "), "aria-label": "Alert thresholds" });
    const err = el("div", { class: "notice error", hidden: true });
    const createBtn = el("button", { class: "btn primary", type: "button", text: "Create budget" });
    const ctl = (label, input, wide) => {
      const id = "b" + Math.random().toString(36).slice(2, 9);
      input.id = id;
      input.removeAttribute("aria-label");
      return el("div", { class: "ctl" + (wide ? " wide" : "") }, el("label", { for: id, text: label }), input);
    };
    const card = el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: "Review budget before creating" }),
        el("div", { class: "card-range", text: "CostLens budget (stored in this app, not in AWS Budgets)" }))),
      el("div", { class: "filters-row" }, "Scope:", fmtScope(parsed.scope || Object.entries(sp.filters).map(([d, v]) => ({ dimension: d, values: v })))),
      el("div", { class: "form-grid" },
        ctl("Name", name, true), ctl("Amount (USD)", amount), ctl("Period", period),
        ctl("Cost type", el("input", { type: "text", value: sp.metric, disabled: true })),
        ctl("Alerts (e.g. 80% actual, 100% forecasted)", thText, true),
        ctl("Email alerts to", recips, true)),
      parsed.assumptions && parsed.assumptions.length ? el("div", { class: "notice" }, el("ul", {}, parsed.assumptions.map(a => el("li", { text: a })))) : null,
      err,
      el("div", { class: "card-foot" }, createBtn, el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => { card.remove(); if (onDone) onDone(null); } })));
    createBtn.addEventListener("click", async () => {
      err.hidden = true;
      const thresholds = [];
      for (const part of thText.value.split(",").map(x => x.trim()).filter(Boolean)) {
        const m = part.match(/^(\d{1,3})\s*%?\s*(actual|forecast(?:ed)?)?$/i);
        if (!m) { err.textContent = `Cannot read alert “${part}”. Use for example “80% actual” or “100% forecasted”.`; err.hidden = false; return; }
        thresholds.push({ pct: +m[1], type: m[2] && m[2].toLowerCase().startsWith("forecast") ? "forecasted" : "actual" });
      }
      const spec = { name: name.value, amount: amount.value, period: period.value, metric: sp.metric, filters: sp.filters, thresholds,
                     recipients: recips.value.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean) };
      createBtn.disabled = true;
      try {
        const r = await U().api("/api/budgets", { method: "POST", body: { spec, source_text: parsed.source_text || "" } });
        const n = r.notifications.length;
        U().toast(n ? `Budget created. ${n} alert${n > 1 ? "s" : ""} triggered now.` : "Budget created.");
        card.replaceWith(el("div", { class: "notice neutral" }, `Budget “${spec.name}” created.`, " ",
          el("button", { class: "btn-ghost", type: "button", text: "View in Budgets & forecasting →", onclick: () => U().showView("budgets") })));
        refreshCounts();
        if (onDone) onDone(r);
      } catch (e) { err.textContent = e.message; err.hidden = false; createBtn.disabled = false; }
    });
    return card;
  }

  function progress(b) {
    if (!b.used_pct) return null;
    const used = Math.min(100, parseFloat(b.used_pct));
    const fc = Math.min(100, parseFloat(b.forecast_pct));
    const wrap = el("div", { class: "progress", role: "img", "aria-label": `${b.used_pct}% used, forecast ${b.forecast_pct}%` },
      el("div", { class: "bar" + (parseFloat(b.used_pct) >= 100 ? " over" : ""), style: `width:${used}%` }),
      el("div", { class: "fc", style: `left:calc(${fc}% - 1px)`, title: `Forecast ${b.forecast_pct}%` }),
      b.alerts.map(a => el("div", { class: "mk", style: `left:${Math.min(100, a.pct)}%`, title: `${a.pct}% ${a.type}` })));
    return [wrap, el("div", { class: "progress-legend" }, el("span", { text: `${b.used_pct}% used` }), el("span", { text: `Forecast ${b.forecast_pct}% │ marks = alert levels` }))];
  }

  function budgetCard(b, opts) {
    const f = b.forecast;
    let confirmDel = false;
    const del = el("button", { class: "btn danger", type: "button", text: "Delete" });
    del.addEventListener("click", async () => {
      if (!confirmDel) { confirmDel = true; del.textContent = "Confirm delete"; return; }
      try { await U().api("/api/budgets/" + b.id, { method: "DELETE", body: {} }); U().toast("Budget deleted"); renderBudgets(); refreshCounts(); }
      catch (e) { U().toast(e.message); }
    });
    return el("div", { class: "card", "data-budget": b.id },
      el("div", { class: "card-head" },
        el("div", {}, el("div", { class: "card-title", text: b.name }),
          el("div", { class: "card-range", text: `${b.period_label} · ${b.amount_display} · ${b.spec.metric} cost` }),
          el("div", { class: "pills" }, pill(b.state, STATE_TEXT[b.state]))),
        el("div", { class: "card-actions" })),
      el("div", { class: "filters-row" }, "Scope:", fmtScope(b.scope)),
      f.status === "ok" ? el("div", { class: "kpis" },
        U().kpi("Spent so far", f.actual_display, `data through ${U().fmtDate(f.data_through)}`),
        U().kpi("Forecast", f.forecast_display, `${f.low_display} – ${f.high_display} · ${f.confidence} confidence`, parseFloat(b.forecast_pct) >= 100 ? "up" : ""),
        U().kpi("Budget", b.amount_display, `${b.remaining_display.startsWith("-") ? "over by " + b.remaining_display.slice(1) : b.remaining_display + " left"}`),
        U().kpi("Days left", String(f.days_left), `${f.days_elapsed} days with data`)) : el("div", { class: "notice neutral", text: f.reason }),
      progress(b),
      opts.compact ? null : el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Alert", "Level", "Triggered", "Email"].map(t => el("th", { text: t })))),
        el("tbody", {}, b.alerts.map(a => el("tr", {},
          el("td", { text: `${a.pct}% of ${a.type === "actual" ? "actual spend" : "forecast"}` }), el("td", { text: a.level_display }),
          el("td", {}, a.triggered ? pill("open", "yes") : pill("on_track", "not yet")),
          el("td", {}, a.notification ? pill(a.notification.delivery_status, DELIVERY_TEXT[a.notification.delivery_status] || a.notification.delivery_status) : el("span", { class: "muted", text: "—" }))))))),
      el("div", { class: "filters-row", style: "padding-bottom:10px" }, "Emails go to:", b.spec.recipients.length ? b.spec.recipients.map(r => el("span", { class: "chip", text: r })) : el("span", { class: "chip", text: "no one (add recipients)" })),
      opts.compact ? null : el("details", { class: "calc" }, el("summary", { text: "How the forecast was calculated" }),
        el("div", { class: "calc-body" }, el("p", { text: f.method || f.reason }),
          f.rate_window ? el("p", { text: `Rate window: ${f.rate_window[0]} to ${f.rate_window[1]}. Period: ${b.period_start} to ${b.period_end} (end exclusive).` }) : null,
          f.notes && f.notes.length ? el("ul", {}, f.notes.map(n => el("li", { text: n }))) : null,
          b.source_text ? el("p", { text: `Created from: “${b.source_text}”` }) : null)),
      opts.compact ? null : el("div", { class: "card-foot" }, del));
  }

  function forecastPanel(fcData) {
    const t = fcData.total;
    if (t.status !== "ok") return el("div", { class: "panel" }, el("h3", { text: `Forecast · ${fcData.period_label}` }), el("p", { text: t.reason }));
    return el("div", { class: "card" },
      el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: `Forecast for ${fcData.period_label} · all accounts` }),
        el("div", { class: "card-range", text: "Run-rate estimate · unblended cost" }))),
      el("div", { class: "kpis" },
        U().kpi("Spent so far", t.actual_display, `${t.days_elapsed} days, through ${U().fmtDate(t.data_through)}`),
        U().kpi("Forecast", t.forecast_display, `${t.low_display} – ${t.high_display}`),
        U().kpi("Daily run-rate", t.daily_rate_display, `last 7 days`),
        U().kpi("Confidence", t.confidence, `${t.days_left} days projected`)),
      el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Account", "Spent so far", "Forecast", "Range"].map((x, i) => el("th", { class: i ? "num" : "", text: x })))),
        el("tbody", {}, fcData.by_account.map(r => el("tr", {}, el("td", { text: r.account }), el("td", { class: "num", text: r.actual_display }),
          el("td", { class: "num", text: r.forecast_display }), el("td", { class: "num", text: `${r.low_display} – ${r.high_display}` })))))),
      t.notes.length ? el("div", { class: "notice" }, el("ul", {}, t.notes.map(n => el("li", { text: n })))) : null,
      el("details", { class: "calc" }, el("summary", { text: "How this was calculated" }), el("div", { class: "calc-body" }, el("p", { text: t.method }))));
  }

  function createBox() {
    const ta = el("textarea", { maxlength: "600", placeholder: "e.g. Set up a monthly budget of $5,000 for prod-data and email ravi@company.com at 80% and 100% of actual, and when the forecast crosses 100%", "aria-label": "Describe a budget" });
    const out = el("div");
    const btn = el("button", { class: "btn primary", type: "button", text: "Preview budget" });
    const go = async () => {
      if (!ta.value.trim()) return;
      btn.disabled = true; out.textContent = "";
      try {
        const r = await U().api("/api/budgets/parse", { method: "POST", body: { text: ta.value } });
        if (r.status !== "ok") out.appendChild(el("div", { class: "notice", text: r.message }));
        else out.appendChild(previewCard(r, res => { if (res) { ta.value = ""; renderBudgets(); } }));
      } catch (e) { out.appendChild(el("div", { class: "notice error", text: e.message })); }
      btn.disabled = false;
    };
    btn.addEventListener("click", go);
    ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) go(); });
    return el("div", { class: "create-box" }, el("h3", { text: "New budget" }),
      el("div", { class: "muted", text: "Describe it in plain English. You will see what I understood before anything is saved." }),
      ta, el("div", { class: "create-row" }, btn), out);
  }

  function outboxPanel(items, email) {
    const body = el("tbody", {});
    items.forEach(n => {
      const detail = el("tr", { hidden: true }, el("td", { colspan: "5" }, el("div", { class: "mono", text: `Subject: ${n.subject}\nTo: ${n.recipients.join(", ") || "(none)"}\n\n${n.body}\nDelivery: ${n.delivery_detail}` })));
      body.append(el("tr", { class: "clickable", title: "Show message", onclick: () => { detail.hidden = !detail.hidden; } },
        el("td", { text: U().fmtTs(n.created_at) }), el("td", { text: n.budget_name || `#${n.budget_id}` }), el("td", { text: n.threshold_key.replace("-", " ") + "%" }),
        el("td", { text: n.recipients.join(", ") || "—" }), el("td", {}, pill(n.delivery_status, DELIVERY_TEXT[n.delivery_status]))), detail);
    });
    return el("div", { class: "panel" }, el("h3", { text: "Alert outbox" }),
      el("p", { text: email.detail }),
      items.length ? el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Time", "Budget", "Alert", "To", "Delivery"].map(t => el("th", { text: t })))), body))
        : el("p", { text: "No alerts yet." }));
  }

  async function renderBudgets() {
    const page = document.getElementById("budgetPage");
    page.textContent = "";
    page.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Loading budgets…"));
    let data;
    try { data = await U().api("/api/budgets"); }
    catch (e) { page.textContent = ""; page.appendChild(el("div", { class: "notice error", text: e.message })); return; }
    page.textContent = "";
    const check = el("button", { class: "btn", type: "button", text: "Check alerts now" });
    check.addEventListener("click", async () => {
      check.disabled = true;
      try { const r = await U().api("/api/budgets/evaluate", { method: "POST", body: {} }); U().toast(r.notifications.length ? `${r.notifications.length} alert(s) processed` : "No new alerts"); renderBudgets(); }
      catch (e) { U().toast(e.message); check.disabled = false; }
    });
    page.append(
      el("div", { class: "page-head" }, el("h2", { text: "Budgets & forecasting" }),
        el("p", { class: "muted", text: "Budgets live in CostLens and are checked against ingested CUR data. Each alert is sent once per budget period. Forecasts are run-rate estimates, not invoices." })),
      el("div", { class: "stack" },
        data.email.configured ? null : el("div", { class: "notice", text: "Email is not configured, so alerts are saved to the outbox below and not sent. An administrator can enable SMTP (for example Amazon SES SMTP) with environment variables; see the README." }),
        createBox(),
        forecastPanel(data.forecast),
        el("div", { class: "summary-row" }, el("strong", { text: `Budgets (${data.budgets.length})` }), check),
        data.budgets.length ? data.budgets.map(b => budgetCard(b, {})) : el("div", { class: "empty-pins" }, el("p", { text: "No budgets yet." }), el("p", { class: "muted", text: "Describe one above in plain English." })),
        outboxPanel(data.outbox, data.email)));
  }

  // ------------------------------------------------------------------ chat cards
  function renderChatSpecial(body, resp) {
    if (resp.worker && resp.worker !== "visualization") body.appendChild(el("div", { class: "module-note", text: resp.worker === "anomalies" ? "Anomalies" : "Budgets & forecasting" }));
    body.appendChild(el("p", { class: "msg-text", text: resp.message }));
    if (resp.kind === "budget_preview") body.appendChild(previewCard(resp.preview));
    if (resp.kind === "budget_list") resp.budgets.slice(0, 5).forEach(b => body.appendChild(el("div", { style: "margin-bottom:10px" }, budgetCard(b, { compact: true }))));
    if (resp.kind === "anomalies") {
      resp.anomalies.forEach(a => body.appendChild(el("div", { style: "margin-bottom:10px" }, anomalyCard(a, { compact: true }))));
      if (resp.total > resp.anomalies.length) body.appendChild(el("button", { class: "followup", type: "button", text: `See all ${resp.total} in Anomalies`, onclick: () => U().showView("anomalies") }));
    }
    if (resp.kind === "forecast") {
      const f = resp.forecast;
      if (f.status === "ok") body.appendChild(el("div", { class: "card" },
        el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: `Forecast · ${f.period_label}` }),
          el("div", { class: "card-range", text: (f.understood.length ? f.understood.join(" · ") : "all spend") + ` · ${f.metric} cost` }))),
        el("div", { class: "kpis" }, U().kpi("Spent so far", f.actual_display, `through ${U().fmtDate(f.data_through)}`),
          U().kpi("Forecast", f.forecast_display, `${f.low_display} – ${f.high_display}`), U().kpi("Confidence", f.confidence, `${f.days_left} days projected`)),
        f.notes.length ? el("div", { class: "notice" }, el("ul", {}, f.notes.map(n => el("li", { text: n })))) : null,
        el("details", { class: "calc" }, el("summary", { text: "How this was calculated" }), el("div", { class: "calc-body" }, el("p", { text: f.method })))));
    }
  }

  window.CostLensFeatures = { renderAnomalies, renderBudgets, renderChatSpecial, refreshCounts };
})();
