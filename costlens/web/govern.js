/* CostLens: Govern page and its chat card. All data is inserted with textContent. */
(function () {
  "use strict";
  const U = () => window.CostLensUI;
  const local = { tab: "violations" };
  const STARTERS = ["Which spend has no owner?", "Draft a guardrail for GPUs in sandbox", "Show policy violations"];
  const MOVE_TEXT = { in_review: "Send for review", approved: "Record approval", rejected: "Reject", draft: "Back to draft" };
  const RESULT_TEXT = { compliant: "Compliant", violations: "Violations", off: "Off" };

  function el(...a) { return U().h(...a); }
  function pill(cls, text) { return el("span", { class: "status-pill st-" + cls, text }); }
  function list(items) { return el("ul", {}, items.map(t => el("li", { text: t }))); }
  function section(title, ...kids) { return el("div", { class: "opt-section" }, el("h4", { text: title }), kids); }

  async function refreshCount() {
    try {
      const s = await U().api("/api/govern");
      document.getElementById("governCount").textContent = s.totals.open_count ? String(s.totals.open_count) : "";
    } catch (e) { /* ignore */ }
  }

  function exceptionForm(v) {
    const owner = el("input", { type: "text", maxlength: "120", "aria-label": "Exception owner", placeholder: "Owner" });
    const reason = el("input", { type: "text", maxlength: "500", "aria-label": "Exception reason", placeholder: "Reason" });
    const expires = el("input", { type: "date", "aria-label": "Exception expiry" });
    const err = el("div", { class: "notice error", hidden: true });
    const save = el("button", { class: "btn", type: "button", text: "Add exception" });
    save.addEventListener("click", async () => {
      err.hidden = true;
      try {
        await U().api(`/api/govern/violations/${v.id}/exception`, { method: "POST", body: { owner: owner.value, reason: reason.value, expires_on: expires.value } });
        U().toast("Exception recorded");
        render(); refreshCount();
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    });
    return el("details", { class: "gov-exception" }, el("summary", { text: "Add an exception" }),
      el("div", { class: "gov-form" }, owner, reason, expires, save), err);
  }

  function violationCard(v, opts) {
    const related = v.resources.flatMap(r => r.optimize.map(o => `${o.title}: ${o.monthly_savings_display} (${o.phase_label})`));
    return el("div", { class: "card", "data-violation": v.id },
      el("div", { class: "card-head" }, el("div", {},
        el("div", { class: "card-title", text: v.title }),
        el("div", { class: "card-range", text: `${U().fmtDate(v.time_range.start)} – ${U().fmtDate(v.time_range.end)} · ${v.resource_count} resource${v.resource_count === 1 ? "" : "s"}` }),
        el("div", { class: "pills" }, pill("gov_" + v.status, v.status)))),
      el("div", { class: "kpis" },
        U().kpi("Spend touched", v.spend_display, "last 30 days"),
        U().kpi("Owner", v.owner, v.owner_source),
        U().kpi("Account", v.account, v.account_id)),
      el("p", { class: "explain", text: v.detail }),
      opts.compact ? null : el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Resource", "First seen", "Spend"].map((t, i) => el("th", { class: i === 2 ? "num" : "", text: t })))),
        el("tbody", {}, v.resources.slice(0, 8).map(r => el("tr", {}, el("td", { text: r.resource_id }), el("td", { text: U().fmtDate(r.first_seen) }), el("td", { class: "num", text: r.spend_display })))))),
      !opts.compact && v.resources.length > 8 ? el("p", { class: "muted gov-pad", text: `Showing the top 8 of ${v.resources.length}.` }) : null,
      opts.compact ? null : el("div", { class: "gov-pad" },
        related.length ? section("Related Optimize findings", list(related.slice(0, 5))) : null,
        v.suggestion ? section("Next step", el("p", { text: v.suggestion })) : null,
        v.exception ? section(v.exception.active ? "Exception" : "Expired exception", el("p", { text: `${v.exception.owner}: ${v.exception.reason}. Expires ${U().fmtDate(v.exception.expires_on)}.` })) : null,
        section("Limitations", list(v.limitations)),
        v.status === "open" ? exceptionForm(v) : null),
      opts.compact ? null : el("div", { class: "card-foot" },
        el("button", { class: "btn", type: "button", text: "Open evidence in Visualize",
                       onclick: () => U().openQuery(v.evidence_query, `Evidence: ${v.title}`) }),
        related.length ? el("button", { class: "btn", type: "button", text: "Open Optimize", onclick: () => U().showView("optimize") }) : null));
  }

  function move(g, status) {
    return async () => {
      try {
        await U().api("/api/govern/guardrails/" + g.id, { method: "POST", body: { status } });
        U().toast(status === "approved" ? "Approval recorded. Nothing was applied in AWS." : "Status updated");
        render();
      } catch (e) { U().toast(e.message); }
    };
  }

  function guardrailCard(g, opts) {
    const copy = el("button", { class: "btn", type: "button", text: "Copy JSON", onclick: async () => {
      try { await navigator.clipboard.writeText(g.document_text); U().toast("Copied"); }
      catch (e) { U().toast("Could not copy. Select the text instead."); }
    } });
    return el("div", { class: "card", "data-guardrail": g.id },
      el("div", { class: "card-head" }, el("div", {},
        el("div", { class: "card-title", text: g.title }),
        el("div", { class: "card-range", text: g.targets.length ? "Target: " + g.targets.map(t => t.name).join(", ") : "No target" }),
        el("div", { class: "pills" }, pill("gr_" + g.status, g.status_label)))),
      el("p", { class: "explain", text: g.impact.summary }),
      g.blockers.length ? el("div", { class: "notice error" }, list(g.blockers)) : null,
      g.note ? el("div", { class: "notice neutral", text: g.note }) : null,
      el("div", { class: "notice neutral", text: g.execution }),
      opts.compact ? null : el("div", { class: "gov-pad" },
        g.document_text ? section("Draft document", el("pre", { text: g.document_text })) : null,
        section("Checks", el("dl", {}, g.checks.map(c => [el("dt", { text: c.check }), el("dd", {}, pill(c.state === "ok" ? "on_track" : c.state === "warn" ? "acknowledged" : "failed", c.state), " ", c.detail)]))),
        section("Risks", list(g.risks)),
        section("Rollout", list(g.rollout)),
        section("Rollback", list(g.rollback)),
        section("Limitations", list(g.limitations))),
      opts.compact ? null : el("div", { class: "card-foot" },
        g.document_text ? copy : null,
        g.next_statuses.map(s => el("button", { class: s === "in_review" || s === "approved" ? "btn primary" : "btn", type: "button", text: MOVE_TEXT[s], onclick: move(g, s) }))));
  }

  function policyRow(p) {
    const toggle = el("button", { class: "btn", type: "button", text: p.enabled ? "Turn off" : "Turn on", onclick: async () => {
      try { await U().api("/api/govern/policies/" + p.id, { method: "POST", body: { enabled: !p.enabled } }); render(); refreshCount(); }
      catch (e) { U().toast(e.message); }
    } });
    return el("div", { class: "card", "data-policy": p.id },
      el("div", { class: "card-head" }, el("div", {},
        el("div", { class: "card-title", text: p.name }),
        el("div", { class: "card-range", text: p.kind.replace(/_/g, " ") }),
        el("div", { class: "pills" }, pill(p.result === "violations" ? "gov_open" : p.result === "off" ? "gov_excepted" : "on_track", RESULT_TEXT[p.result])))),
      el("div", { class: "card-foot" }, toggle));
  }

  function chatBox() {
    const ta = el("textarea", { maxlength: "500", rows: "1", placeholder: "Which spend has no owner?", "aria-label": "Ask about governance" });
    const out = el("div", { class: "opt-answer" });
    const btn = el("button", { class: "btn primary", type: "button", text: "Ask" });
    const go = async text => {
      const q = (text || ta.value).trim();
      if (!q) return;
      btn.disabled = true; out.textContent = "";
      out.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Checking the policies…"));
      try {
        const r = await U().api("/api/ask", { method: "POST", body: { question: q } });
        out.textContent = "";
        if (r.kind === "govern") renderChat(out, r, { inPage: true });
        else out.append(el("p", { class: "msg-text", text: "That is not a governance question, so it belongs in Visualize." }),
          el("button", { class: "btn", type: "button", text: "Ask in Visualize", onclick: () => U().ask(q) }));
      } catch (e) { out.textContent = ""; out.appendChild(el("div", { class: "notice error", text: e.message })); }
      btn.disabled = false;
    };
    btn.addEventListener("click", () => go());
    ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } });
    return el("div", { class: "create-box opt-chat gov-chat" }, el("h3", { text: "Ask about policies and guardrails" }),
      el("div", { class: "muted", text: "Answers use the same evidence as the lists below. Nothing is applied." }),
      ta, el("div", { class: "create-row" }, btn, STARTERS.map(s => el("button", { class: "followup", type: "button", text: s, onclick: () => go(s) }))), out);
  }

  async function render() {
    const page = document.getElementById("governPage");
    page.textContent = "";
    page.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Checking the policies…"));
    let s;
    try { s = await U().api("/api/govern"); }
    catch (e) { page.textContent = ""; page.appendChild(el("div", { class: "notice error", text: e.message })); return; }
    page.textContent = "";
    page.appendChild(el("div", { class: "page-head" }, el("h2", { text: "Govern" }),
      el("p", { class: "muted", text: "Policy checks on CUR, and guardrail drafts for review. CostLens applies nothing to AWS." })));
    if (s.state !== "ok") {
      page.appendChild(el("div", { class: "empty-pins" }, el("p", { text: s.reason })));
      return;
    }
    const t = s.totals;
    const tabs = [["violations", `Violations (${s.violations.length})`], ["guardrails", `Guardrail drafts (${s.guardrails.length})`], ["policies", `Policies (${s.policies.length})`]];
    const items = local.tab === "violations" ? s.violations.map(v => violationCard(v, {}))
      : local.tab === "guardrails" ? s.guardrails.map(g => guardrailCard(g, {})) : s.policies.map(policyRow);
    page.appendChild(el("div", { class: "stack" },
      chatBox(),
      el("div", { class: "card gov-totals" },
        el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: "Governance status" }),
          el("div", { class: "card-range", text: `Evidence window ${U().fmtDate(s.window.start)} – ${U().fmtDate(s.window.end)}` }))),
        el("div", { class: "kpis" },
          U().kpi("Open violations", String(t.open_count), `${t.excepted_count} excepted`, t.open_count ? "up" : ""),
          U().kpi("Spend touched", t.spend_display, `${t.resources} resources, each counted once`),
          U().kpi("Drafts in review", String(t.in_review), `${t.drafts} drafts in all`),
          U().kpi("Approvals", String(t.approved), "recorded, not verified"))),
      s.organization_loaded ? null : el("div", { class: "notice", text: "No organization data is loaded, so guardrail drafts cannot be written." }),
      el("div", { class: "summary-row" }, el("div", { class: "tabs opt-tabs", role: "tablist" }, tabs.map(([k, label]) =>
        el("button", { type: "button", class: local.tab === k ? "on" : "", role: "tab", "aria-selected": String(local.tab === k), text: label,
                       onclick: () => { local.tab = k; render(); } })))),
      items.length ? items : el("div", { class: "empty-pins" }, el("p", { text: "Nothing in this list." })),
      el("details", { class: "calc panel" }, el("summary", { text: "How this works" }),
        el("div", { class: "calc-body" }, el("dl", {}, Object.entries(s.method).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]))))));
  }

  // Chat card, used in the Visualize thread and in the Govern page.
  function renderChat(body, resp, opts) {
    const inPage = opts && opts.inPage;
    if (!inPage) body.appendChild(el("div", { class: "module-note", text: "Govern" }));
    body.appendChild(el("p", { class: "msg-text", text: resp.message }));
    resp.violations.forEach(v => body.appendChild(el("div", { class: "gov-chat-card" }, violationCard(v, { compact: true }))));
    resp.guardrails.forEach(g => body.appendChild(el("div", { class: "gov-chat-card" }, guardrailCard(g, { compact: true }))));
    if (!inPage && resp.state === "ok") body.appendChild(el("button", { class: "followup", type: "button", text: "Open Govern", onclick: () => U().showView("govern") }));
    if (inPage && resp.suggestions.length) body.appendChild(el("div", { class: "followups" }, resp.suggestions.map(q => el("button", { class: "followup", type: "button", text: q, onclick: () => { const box = document.querySelector(".gov-chat textarea"); box.value = q; box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); } }))));
  }

  window.CostLensGovern = { render, renderChat, refreshCount };
})();
