/* CostLens: Optimize page and its chat card. All data is inserted with textContent. */
(function () {
  "use strict";
  const U = () => window.CostLensUI;
  const local = { tab: "all", service: "all", open: new Set() };
  const TABS = [["all", "All"], ["plan_ready", "Plan ready"], ["conditional", "Conditional"], ["proposal", "Proposal only"],
                ["alternative", "Alternative"], ["needs_evidence", "Needs evidence"], ["dismissed", "Dismissed"], ["realised", "Realised"]];
  const MARKABLE = ["plan_ready", "conditional", "alternative"];
  const STARTERS = ["How can I reduce EC2 cost?", "How much can prod-data save?", "How much have we saved so far?"];

  function el(...a) { return U().h(...a); }
  function pill(cls, text) { return el("span", { class: "status-pill st-" + cls, text }); }
  function list(items) { return el("ul", {}, items.map(t => el("li", { text: t }))); }
  function statusPill(s) { return pill("opt_" + s, s); }
  function effortText(e) { return e.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(); }

  async function refreshCount() {
    try {
      const s = await U().api("/api/optimize");
      const count = s.totals.counted_count;
      document.getElementById("optimizeCount").textContent = count ? String(count) : "";
    } catch (e) { /* ignore */ }
  }

  function totalsKpis(t) {
    return el("div", { class: "kpis" },
      U().kpi("Estimated, counted", t.counted_display, `${t.counted_count} findings a month, no overlaps`, "down"),
      U().kpi("Conditional, not counted", t.conditional_display, "Savings Plan covered"),
      U().kpi("Commitment proposal", t.proposal_display, "shown apart, never added"),
      U().kpi("Realised", t.realised_display, t.realised_sub));
  }

  function markForm(f) {
    const day = el("input", { type: "date", "aria-label": "Date implemented" });
    const note = el("input", { type: "text", maxlength: "500", placeholder: "Note (optional)", "aria-label": "Implementation note" });
    const err = el("div", { class: "notice error", hidden: true });
    const save = el("button", { class: "btn", type: "button", text: "Record as implemented" });
    save.addEventListener("click", async () => {
      err.hidden = true;
      try {
        await U().api(`/api/optimize/${f.id}/implemented`, { method: "POST", body: { implemented_on: day.value, note: note.value } });
        U().toast("Recorded. CostLens changed nothing in AWS.");
        local.tab = "realised"; render(); refreshCount();
      } catch (e) { err.textContent = e.message; err.hidden = false; }
    });
    return el("details", { class: "gov-exception opt-mark" }, el("summary", { text: "Record that this was done" }),
      el("p", { class: "muted", text: "Use this after a person has made the change. CostLens then measures what billing shows." }),
      el("div", { class: "gov-form opt-mark-form" }, day, note, save), err);
  }

  function realisedCard(r) {
    const undo = el("button", { class: "btn", type: "button", text: "Undo record", onclick: async () => {
      try { await U().api(`/api/optimize/${r.finding_id}/implemented`, { method: "DELETE", body: {} }); U().toast("Record removed"); render(); refreshCount(); }
      catch (e) { U().toast(e.message); }
    } });
    return el("div", { class: "card", "data-change": r.finding_id },
      el("div", { class: "card-head" }, el("div", {},
        el("div", { class: "card-title", text: r.title }),
        el("div", { class: "card-range", text: `${r.account} · implemented ${U().fmtDate(r.implemented_on)}${r.note ? " · " + r.note : ""}` }),
        el("div", { class: "pills" }, pill(r.state, r.state_label)))),
      el("div", { class: "kpis" },
        U().kpi("Estimated", r.estimate_display, "a month, before the change"),
        U().kpi("Realised, monthly rate", r.monthly_display, r.share_of_estimate ? `${r.share_of_estimate}% of the estimate` : "not measured"),
        U().kpi("Realised so far", r.realised_display, r.after_window ? `${r.after_window.days} days measured` : "not measured")),
      el("p", { class: "explain", text: r.reason }),
      r.split.length ? el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Part of the change", "Amount", "Counted?"].map((x, i) => el("th", { class: i === 1 ? "num" : "", text: x })))),
        el("tbody", {}, r.split.map(x => el("tr", {}, el("td", { text: x.label }), el("td", { class: "num", text: x.value }), el("td", { text: x.counted ? "Yes" : "No" })))))) : null,
      el("div", { class: "gov-pad" },
        r.steps.length ? section("Calculation", el("p", { class: "muted", text: `Before ${U().fmtDate(r.baseline_window.start)} – ${U().fmtDate(r.baseline_window.end)} · after ${U().fmtDate(r.after_window.start)} – ${U().fmtDate(r.after_window.end)}` }), el("pre", { text: r.steps.join("\n") })) : null,
        section("Limitations", list(r.limitations))),
      el("div", { class: "card-foot" },
        r.before_query ? el("button", { class: "btn", type: "button", text: "Before in Visualize", onclick: () => U().openQuery(r.before_query, `Before: ${r.title}`) }) : null,
        r.after_query ? el("button", { class: "btn", type: "button", text: "After in Visualize", onclick: () => U().openQuery(r.after_query, `After: ${r.title}`) }) : null,
        undo));
  }

  function setStatus(f, status) {
    return async () => {
      try {
        await U().api("/api/optimize/" + f.id, { method: "POST", body: { status } });
        U().toast(status === "accepted" ? "Accepted for planning. Nothing was changed in AWS." : `Marked as ${status}`);
        render(); refreshCount();
      } catch (e) { U().toast(e.message); }
    };
  }

  function section(title, ...kids) { return el("div", { class: "opt-section" }, el("h4", { text: title }), kids); }

  function planBlock(p) {
    return section(p.kind === "commitment_proposal" ? "Proposal (review only)" : "Action plan (review only)",
      el("dl", {},
        el("dt", { text: "Owner" }), el("dd", { text: p.owner }),
        el("dt", { text: "Expected benefit" }), el("dd", { text: p.expected_benefit }),
        el("dt", { text: "Who runs it" }), el("dd", { text: p.execution })),
      el("h5", { text: "Prerequisites" }), list(p.prerequisites),
      el("h5", { text: "Validation" }), list(p.validation),
      el("h5", { text: "Rollback" }), list(p.rollback));
  }

  function detail(f) {
    const buttons = [];
    const readOnly=U().state.status && U().state.status.backend==="cloud";
    if (f.evidence_query) buttons.push(el("button", { class: "btn", type: "button", text: "Open evidence in Visualize",
      onclick: () => U().openQuery(f.evidence_query, `Evidence: ${f.title}, ${U().fmtDate(f.time_range.start)} – ${U().fmtDate(f.time_range.end)}`) }));
    const done = f.status === "implemented";
    if (!readOnly && !done && f.status !== "accepted" && f.action_plan) buttons.push(el("button", { class: "btn", type: "button", text: "Accept for planning", onclick: setStatus(f, "accepted") }));
    if (!readOnly && !done && f.status !== "dismissed") buttons.push(el("button", { class: "btn", type: "button", text: "Dismiss", onclick: setStatus(f, "dismissed") }));
    if (!readOnly && !done && f.status !== "open") buttons.push(el("button", { class: "btn", type: "button", text: "Reopen", onclick: setStatus(f, "open") }));
    return el("div", { class: "opt-detail", "data-detail": f.id },
      el("p", { class: "muted", text: `${f.change} · ${f.account} · ${f.region} · source: ${f.source} · effort: ${effortText(f.effort)}` }),
      f.overlaps.length && f.phase === "alternative" ? el("div", { class: "notice neutral", text: "This competes with another recommendation on the same resource. Only one can be done, so this one is not counted." }) : null,
      section("Evidence", el("dl", {}, f.evidence.map(e => [el("dt", { text: e.label }), el("dd", { text: e.value })])),
        el("p", { class: "muted", text: `Time range: ${U().fmtDate(f.time_range.start)} – ${U().fmtDate(f.time_range.end)}` })),
      section("Calculation", el("p", { class: "muted", text: f.calculation.formula }), el("pre", { text: f.calculation.steps.join("\n") })),
      section("Confidence: " + f.confidence, list(f.confidence_reasons)),
      f.risks.length ? section("Risks", list(f.risks)) : null,
      f.limitations.length ? section("Limitations", list(f.limitations)) : null,
      f.action_plan ? planBlock(f.action_plan) : section("Next step", el("p", { text: f.next_step })),
      el("p", { class: "muted", text: `Realised saving: ${f.realised.display}. ${f.realised.detail}` }),
      !readOnly && !done && f.status !== "dismissed" && MARKABLE.includes(f.phase) ? markForm(f) : null,
      el("div", { class: "card-foot" }, buttons));
  }

  function table(items) {
    const body = el("tbody", {});
    items.forEach(f => {
      const row = el("tr", { class: "clickable", "data-opportunity": f.id, tabindex: "0", "aria-expanded": String(local.open.has(f.id)) },
        el("td", {}, el("div", { class: "opt-title", text: f.title }), el("div", { class: "muted", text: f.account })),
        el("td", { text: f.owner, title: f.owner_source, "data-label": "Owner" }),
        el("td", { "data-label": "Phase" }, pill(f.phase, f.phase_label)),
        el("td", { "data-label": "Status" }, statusPill(f.status)),
        el("td", { class: "num", text: f.monthly_savings_display, "data-label": "Monthly estimate" }),
        el("td", { "data-label": "Confidence" }, pill("conf_" + f.confidence, f.confidence)));
      const open = el("tr", { hidden: !local.open.has(f.id) }, el("td", { colspan: "6" }, detail(f)));
      const toggle = () => {
        open.hidden = !open.hidden;
        row.setAttribute("aria-expanded", String(!open.hidden));
        if (open.hidden) local.open.delete(f.id); else local.open.add(f.id);
      };
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      body.append(row, open);
    });
    return el("div", { class: "tbl-wrap" }, el("table", { class: "tbl opt-table" },
      el("thead", {}, el("tr", {}, ["Opportunity", "Owner", "Phase", "Status", "Monthly estimate", "Confidence"].map((t, i) => el("th", { class: i === 4 ? "num" : "", text: t })))),
      body));
  }

  function chatBox() {
    const ta = el("textarea", { maxlength: "500", rows: "1", placeholder: "How can I reduce EC2 cost?", "aria-label": "Ask about savings" });
    const out = el("div", { class: "opt-answer" });
    const btn = el("button", { class: "btn primary", type: "button", text: "Ask" });
    const go = async text => {
      const q = (text || ta.value).trim();
      if (!q) return;
      btn.disabled = true; out.textContent = "";
      out.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Checking the evidence…"));
      try {
        const r = await U().api("/api/ask", { method: "POST", body: { question: q } });
        out.textContent = "";
        if (r.kind === "optimize") {
          renderChat(out, r, { inPage: true });
          if (r.suggestions) out.appendChild(el("div", { class: "followups" }, r.suggestions.map(s => el("button", { class: "followup", type: "button", text: s, onclick: () => go(s) }))));
        } else {
          out.append(el("p", { class: "msg-text", text: "That is not a savings question, so it belongs in Visualize." }),
            el("button", { class: "btn", type: "button", text: "Ask in Visualize", onclick: () => U().ask(q) }));
        }
      } catch (e) { out.textContent = ""; out.appendChild(el("div", { class: "notice error", text: e.message })); }
      btn.disabled = false;
    };
    btn.addEventListener("click", () => go());
    ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } });
    return el("div", { class: "create-box opt-chat" }, el("h3", { text: "Ask the Cost Optimization Engineer" }),
      el("div", { class: "muted", text: "Answers use the same evidence as the list below. Nothing is changed." }),
      ta, el("div", { class: "create-row" }, btn, STARTERS.map(s => el("button", { class: "followup", type: "button", text: s, onclick: () => go(s) }))), out);
  }

  async function render() {
    const page = document.getElementById("optimizePage");
    page.textContent = "";
    page.appendChild(el("div", { class: "thinking" }, el("span", { class: "dots" }, el("span"), el("span"), el("span")), "Checking the evidence…"));
    let s;
    try { s = await U().api("/api/optimize?service="+encodeURIComponent(local.service)); }
    catch (e) { page.textContent = ""; page.appendChild(el("div", { class: "notice error", text: e.message })); return; }
    page.textContent = "";
    page.appendChild(el("div", { class: "page-head" }, el("h2", { text: "Optimize" }),
      el("p", { class: "muted", text: "Savings estimates checked against CUR. Read-only: CostLens changes nothing in AWS." })));
    if (s.state !== "ok") {
      page.appendChild(el("div", { class: "empty-pins" }, el("p", { text: s.reason }), el("p", { class: "muted", text: "Without evidence there are no findings and no savings numbers." })));
      return;
    }
    const t = s.totals;
    const services=el("div",{class:"opt-service-picker"},el("h3",{text:"Choose a service"}),
      el("div",{class:"tabs opt-service-tabs",role:"tablist","aria-label":"Services"},(s.available_services||[]).map(service=>
        el("button",{type:"button",role:"tab",class:local.service===service.key?"on":"","aria-selected":String(local.service===service.key),
          text:`${service.label} (${service.count})`,onclick:()=>{local.service=service.key;local.tab="all";local.open.clear();render();}}))));
    if (s.source_snapshot_as_of) page.appendChild(el("div",{class:"notice neutral",text:`Synthetic S3 snapshot as of ${U().fmtTs(s.source_snapshot_as_of)}. Estimates are validated by the optimization pipeline; no AWS changes are made.`}));
    const inTab = f => local.tab === "all" ? f.status !== "dismissed" : local.tab === "dismissed" ? f.status === "dismissed" : f.phase === local.tab && f.status !== "dismissed";
    const counts = Object.fromEntries(TABS.map(([k]) => { const was = local.tab; local.tab = k; const n = s.opportunities.filter(inTab).length; local.tab = was; return [k, n]; }));
    const changes = s.verification.results;
    counts.realised = changes.length;
    const items = local.tab === "realised" ? [] : s.opportunities.filter(inTab);
    const tabs = el("div", { class: "tabs opt-tabs", role: "tablist", "aria-label":"Recommendation phases" }, TABS.map(([k, label]) =>
      el("button", { type: "button", class: local.tab === k ? "on" : "", role: "tab", "aria-selected": String(local.tab === k),
                     text: `${label} (${counts[k]})`, onclick: () => { local.tab = k; render(); } })));
    page.appendChild(el("div", { class: "stack" },
      services,
      chatBox(),
      el("div", { class: "card opt-totals" },
        el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: "Monthly savings estimate"+(local.service!=="all"?" · "+s.service_label:"") }),
          el("div", { class: "card-range", text: `Evidence window ${U().fmtDate(s.window.start)} – ${U().fmtDate(s.window.end)} · snapshot ${s.snapshot.execution_id}` }))),
        totalsKpis(t),
        el("div", { class: "notice neutral", text: `The sources add up to ${t.raw_display}. That is not a savings number: it includes ${t.alternative_display} of competing alternatives, ${t.unverified_display} without enough evidence, the conditional amount, and the unadjusted commitment.` })),
      s.snapshot.notes.length ? el("div", { class: "notice" }, list(s.snapshot.notes)) : null,
      el("div", { class: "summary-row" }, tabs),
      local.tab === "realised" ? [
        el("div", { class: "notice neutral", text: "Realised amounts are measured from CUR and are never added to estimates. In the demo, every result is a planted synthetic story, not proof of real savings." }),
        changes.length ? changes.map(realisedCard) : el("div", { class: "empty-pins" }, el("p", { text: "No change has been recorded yet." }), el("p", { class: "muted", text: s.workflow_read_only ? "Implementation recording is not connected for this cloud snapshot yet." : "Open a finding and record it after a person has made the change." })),
        el("details", { class: "calc panel" }, el("summary", { text: "How realised savings are measured" }),
          el("div", { class: "calc-body" }, el("dl", {}, Object.entries(s.verification.method).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]))))]
        : items.length ? table(items) : el("div", { class: "empty-pins" }, el("p", { text: local.service!=="all" && !s.opportunities.length ? `No ${s.service_label} recommendations in this snapshot.` : "No recommendations match this phase." })),
      el("details", { class: "calc panel" }, el("summary", { text: "Data quality checks" }),
        el("div", { class: "calc-body" }, el("dl", {}, s.data_quality.map(c => [el("dt", { text: c.check }), el("dd", {}, pill(c.state === "ok" ? "on_track" : "acknowledged", c.state), " ", c.detail)])))),
      el("details", { class: "calc panel" }, el("summary", { text: "How savings are counted" }),
        el("div", { class: "calc-body" }, el("dl", {}, Object.entries(s.method).map(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])))))
    );
  }

  // Chat card, used in the Visualize thread and in the Optimize page.
  function renderChat(body, resp, opts) {
    if (!(opts && opts.inPage)) body.appendChild(el("div", { class: "module-note", text: "Optimize" }));
    body.appendChild(el("p", { class: "msg-text", text: resp.message }));
    if (resp.state !== "ok" || !resp.opportunities.length) return;
    body.appendChild(el("div", { class: "card" },
      totalsKpis(resp.totals),
      el("div", { class: "tbl-wrap" }, el("table", { class: "tbl" },
        el("thead", {}, el("tr", {}, ["Top findings", "Phase", "Monthly estimate", "Confidence"].map((x, i) => el("th", { class: i === 2 ? "num" : "", text: x })))),
        el("tbody", {}, resp.opportunities.map(f => el("tr", {}, el("td", { text: f.title }), el("td", {}, pill(f.phase, f.phase_label)),
          el("td", { class: "num", text: f.monthly_savings_display }), el("td", {}, pill("conf_" + f.confidence, f.confidence))))))),
      opts && opts.inPage ? null : el("div", { class: "card-foot" }, el("button", { class: "btn", type: "button", text: `See all ${resp.total} in Optimize`, onclick: () => U().showView("optimize") }))));
  }

  window.CostLensOptimize = { render, renderChat, refreshCount };
})();
