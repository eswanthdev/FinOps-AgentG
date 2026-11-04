/* CostLens: the investigator's chat card. Model text is inserted with textContent only. */
(function () {
  "use strict";
  const U = () => window.CostLensUI;
  function el(...a) { return U().h(...a); }

  function render(body, resp) {
    body.appendChild(el("div", { class: "module-note", text: "Investigation · AI-assisted" }));
    body.appendChild(el("p", { class: "msg-text inv-answer", text: resp.message }));
    body.appendChild(el("p", { class: "muted", text: "Written by a model. Every amount was checked against tool results. Nothing was changed." }));
    if (resp.evidence_queries.length) body.appendChild(el("div", { class: "followups" }, resp.evidence_queries.map(e =>
      el("button", { class: "followup", type: "button", text: "Open evidence: " + e.title, onclick: () => U().openQuery(e.query, "Evidence: " + e.title) }))));
    body.appendChild(el("details", { class: "calc panel inv-trace" },
      el("summary", { text: `What the agent did (${resp.trace.length} tool call${resp.trace.length === 1 ? "" : "s"})` }),
      el("div", { class: "calc-body" }, el("ol", {}, resp.trace.map(t => el("li", {},
        el("strong", { text: t.tool }), t.status === "ok" ? "" : " (failed)", el("div", { class: "mono inv-input", text: t.input })))))));
  }

  window.CostLensInvestigate = { render };
})();
