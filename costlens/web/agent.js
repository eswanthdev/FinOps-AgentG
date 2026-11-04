/* CostLens: conversations, memory, reports and agent runs. All data is inserted with textContent. */
(function () {
  "use strict";
  const U = () => window.CostLensUI;
  function el(...a) { return U().h(...a); }
  function page(id, title, sub) {
    const p = document.getElementById(id);
    p.textContent = "";
    p.appendChild(el("div", { class: "page-head" }, el("h2", { text: title }), el("p", { class: "muted", text: sub })));
    return p;
  }
  function empty(text) { return el("div", { class: "empty-pins" }, el("p", { text })); }
  function failed(p, e) { p.appendChild(el("div", { class: "notice error", text: e.message })); }

  async function renderConversations() {
    const p = page("conversationsPage", "Conversations", "Your saved chats. They are removed after 90 days.");
    let d;
    try { d = await U().api("/api/conversations"); } catch (e) { return failed(p, e); }
    if (!d.conversations.length) return p.appendChild(empty("No conversations yet."));
    p.appendChild(el("div", { class: "stack" }, d.conversations.map(c => el("div", { class: "panel agent-row", "data-conversation": c.id },
      el("div", {}, el("strong", { text: c.title }), el("div", { class: "muted", text: U().fmtTs(c.updated_at) })),
      el("div", { class: "agent-actions" },
        el("button", { class: "btn", type: "button", text: "Continue", onclick: () => U().openConversation(c.id) }),
        el("button", { class: "btn danger", type: "button", text: "Delete", onclick: async () => {
          try { await U().api("/api/conversations/" + c.id, { method: "DELETE", body: {} }); renderConversations(); } catch (e) { U().toast(e.message); } } }))))));
  }

  async function renderMemory() {
    const p = page("memoryPage", "Memory", "Only you can add memory, here or by saying “remember that …” in chat. The agent cannot change it. Amounts are not kept, because they go stale.");
    const input = el("input", { type: "text", maxlength: "300", placeholder: "e.g. I care about prod-data first", "aria-label": "New memory" });
    const add = async () => {
      if (!input.value.trim()) return;
      try { await U().api("/api/memories", { method: "POST", body: { text: input.value } }); renderMemory(); } catch (e) { U().toast(e.message); }
    };
    p.appendChild(el("div", { class: "create-box agent-add" }, input, el("button", { class: "btn primary", type: "button", text: "Remember", onclick: add })));
    let d;
    try { d = await U().api("/api/memories"); } catch (e) { return failed(p, e); }
    if (!d.memories.length) return p.appendChild(empty("Nothing remembered yet."));
    p.appendChild(el("div", { class: "stack agent-list" }, d.memories.map(m => el("div", { class: "panel agent-row", "data-memory": m.id }, el("span", { text: m.text }),
      el("button", { class: "btn danger", type: "button", text: "Forget", onclick: async () => {
        try { await U().api("/api/memories/" + m.id, { method: "DELETE", body: {} }); renderMemory(); } catch (e) { U().toast(e.message); } } })))));
  }

  function markdown(r) {
    const lines = [`# ${r.title}`, "", `Saved: ${r.created_at}`, "", `Question: ${r.question}`, "", r.answer, "", "## Current figures"];
    r.current.forEach(c => lines.push(`- ${c.title}: ${c.summary}`));
    lines.push("", "## What the agent did");
    r.trace.forEach(t => lines.push(`- ${t.tool} (${t.status})`));
    return lines.join("\n");
  }

  async function openReport(id) {
    const p = page("reportsPage", "Report", "Saved answer, with current figures recalculated from CUR.");
    let r;
    try { r = await U().api("/api/reports/" + id); } catch (e) { return failed(p, e); }
    const download = () => { const a = el("a", { href: URL.createObjectURL(new Blob([markdown(r)], { type: "text/markdown" })), download: `costlens-report-${r.id}.md` }); a.click(); URL.revokeObjectURL(a.href); };
    p.appendChild(el("div", { class: "card", "data-report-detail": r.id },
      el("div", { class: "card-head" }, el("div", {}, el("div", { class: "card-title", text: r.title }), el("div", { class: "card-range", text: "Saved " + U().fmtTs(r.created_at) }))),
      el("p", { class: "msg-text inv-answer gov-pad", text: r.answer }),
      el("div", { class: "notice neutral", text: r.note }),
      el("div", { class: "gov-pad" }, r.current.map(c => el("div", { class: "opt-section" }, el("h4", { text: c.title }), el("p", { text: c.summary }),
        el("button", { class: "btn", type: "button", text: "Open in Visualize", onclick: () => U().openQuery(c.query, r.title) })))),
      el("div", { class: "card-foot" },
        el("button", { class: "btn", type: "button", text: "Download Markdown", onclick: download }),
        el("button", { class: "btn", type: "button", text: "All reports", onclick: renderReports }),
        el("button", { class: "btn danger", type: "button", text: "Delete", onclick: async () => {
          try { await U().api("/api/reports/" + r.id, { method: "DELETE", body: {} }); renderReports(); } catch (e) { U().toast(e.message); } } }))));
  }

  async function renderReports() {
    const p = page("reportsPage", "Reports", "Saved investigations. Each one keeps its queries, so it can show current figures.");
    let d;
    try { d = await U().api("/api/reports"); } catch (e) { return failed(p, e); }
    if (!d.reports.length) return p.appendChild(empty("No reports yet. Save one from an agent answer."));
    p.appendChild(el("div", { class: "stack" }, d.reports.map(r => el("div", { class: "panel agent-row", "data-report": r.id },
      el("div", {}, el("strong", { text: r.title }), el("div", { class: "muted", text: U().fmtTs(r.created_at) })),
      el("button", { class: "btn", type: "button", text: "Open", onclick: () => openReport(r.id) })))));
  }

  async function renderRuns() {
    const p = page("runsPage", "Agent runs", "What the agent did, how long it took and how many tokens it used.");
    let d;
    try { d = await U().api("/api/agent/runs"); } catch (e) { return failed(p, e); }
    const t = d.last_30_days;
    p.appendChild(el("div", { class: "card" }, el("div", { class: "kpis" },
      U().kpi("Runs, 30 days", String(t.runs), `${t.failed} failed`, t.failed ? "up" : ""),
      U().kpi("Median time", t.median_ms === null ? "—" : `${(t.median_ms / 1000).toFixed(1)} s`, `${t.repaired} needed a repair`),
      U().kpi("Tokens this month", d.tokens_this_month.toLocaleString(), `limit ${d.monthly_token_cap.toLocaleString()}`),
      U().kpi("Feedback", `${t.thumbs_up} up / ${t.thumbs_down} down`, "from users")),
      el("div", { class: "notice neutral", text: d.cost_note })));
    if (!d.runs.length) return p.appendChild(empty("No agent runs yet."));
    p.appendChild(el("div", { class: "tbl-wrap agent-table" }, el("table", { class: "tbl" },
      el("thead", {}, el("tr", {}, ["Time", "Status", "Tools", "Turns", "Tokens", "Seconds"].map(x => el("th", { text: x })))),
      el("tbody", {}, d.runs.map(r => el("tr", { "data-run": r.id, title: r.note },
        el("td", { text: U().fmtTs(r.started_at) }), el("td", {}, U().statusPill(r.status === "ok" ? "active" : "failed")),
        el("td", { text: r.tools.join(", ") || "none" }), el("td", { text: String(r.turns) }),
        el("td", { text: String(r.input_tokens + r.output_tokens) }), el("td", { text: (r.duration_ms / 1000).toFixed(1) })))))));
  }

  function runActions(resp) {
    const rate = v => async () => {
      try { await U().api(`/api/agent/runs/${resp.run_id}/feedback`, { method: "POST", body: { rating: v } }); U().toast("Thanks for the feedback"); } catch (e) { U().toast(e.message); }
    };
    return el("div", { class: "followups agent-feedback" },
      el("button", { class: "followup", type: "button", text: "Save as report", onclick: async () => {
        try { await U().api("/api/reports", { method: "POST", body: { run_id: resp.run_id } }); U().toast("Saved to Reports"); } catch (e) { U().toast(e.message); } } }),
      el("button", { class: "followup", type: "button", text: "Good answer", onclick: rate(1) }),
      el("button", { class: "followup", type: "button", text: "Bad answer", onclick: rate(-1) }));
  }

  window.CostLensAgent = { renderConversations, renderMemory, renderReports, renderRuns, runActions };
})();
