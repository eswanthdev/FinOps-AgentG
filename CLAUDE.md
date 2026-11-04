# CostLens (working name)

Evidence-backed FinOps agent for AWS. Original product; do not copy any other vendor's code, branding, wording or screens.
Everything runs locally on synthetic CUR 2.0 fixtures. No real AWS access. No AI model ("Deterministic mode").

## Run and test

Needs Python 3.11+. Standard library only at runtime.

```bash
python -m costlens demo --reset-db   # fixtures -> ./var, ingest, serve http://127.0.0.1:8765
python -m costlens status --db ./var/<db>.sqlite
python -m pytest -q                              # expected: 316 passed
python -m pytest -q --ignore=tests/test_ui.py    # without a browser
```

UI tests need Playwright and Chrome/Chromium (`COSTLENS_CHROME=/path/to/chrome`); they skip otherwise.
On Windows the variable is always needed, because the tests cannot find Chrome by themselves.
Do not edit tests just to make them pass.

## Architecture (see docs/ARCHITECTURE.md)

- `costlens/ingest/` - delivery folder -> validate -> checksum -> parse -> idempotent apply (CUR 2.0 subset).
- `costlens/store.py` - SQLite (deliveries, line_items, accounts, pins, audit).
- `costlens/query/model.py` - `CostQuery`, the only query shape. `query/engine.py` runs it (explore, variance, coverage).
- `costlens/agent/` - `interpreter.py` (deterministic text -> CostQuery), `orchestrator.py` (routes to workers; per-worker tool allow-list; audit).
- `costlens/agent/optimize.py` - Cost Optimization Engineer workers and savings rules. `costlens/ingest/optimization.py` - COH and Compute Optimizer snapshots.
- `costlens/agent/govern.py` - Governance workers, guardrail drafts, workflow, exceptions. `costlens/ingest/governance.py` - organization file and typed policies.
- `costlens/anomalies.py`, `budgets.py`, `notify.py` (outbox by default, SMTP via env vars).
- `costlens/server.py` - localhost HTTP API + static UI in `costlens/web/` (plain JS/HTML/CSS).
- Docs: `docs/ARCHITECTURE.md`, `DATA_CONTRACTS.md`, `SECURITY.md`, `ROADMAP.md`. Read them before changing anything.

## Build order

Visualize (done) -> Cost Optimization Engineer (first slice done) -> Governance (first slice done). Savings verification is still to build.
Also done: Anomalies, Budgets & forecasting.
Sidebar order: Visualize, Optimize, Govern, Anomalies, Budgets & forecasting.

## Coding rules

- Money is integer nano-USD. No floats for money. Round half-even to cents only for display.
- Only a typed, validated `CostQuery` reaches the engine. Never free-form SQL. Parameterised SQL only.
- UI inserts data with `textContent` only. No `innerHTML` with data.
- Strict CSP: no inline script, no eval, no external assets.
- Workers get a fixed allow-list of typed, bounded tools. Any other call raises `PermissionError`.
- Never add up overlapping savings. Keep estimated savings separate from realised savings.
- If evidence is not enough, say so. Do not invent findings or numbers.
- Guardrails are drafts only. "Approved" records a decision; no code path may apply anything to AWS.
- Match surrounding code style. Add tests with every change.

## Safety rules (always)

- Do not create or change AWS resources, IAM roles, billing exports or paid services. No AWS API call without separate, explicit user approval.
- Never ask the user to paste credentials, secrets or tokens into chat or code. SMTP and AWS settings come only from environment variables.
- Do not push, open or merge a PR, deploy, or touch `main` without asking. Commit locally on a feature branch (current: `feature/governance-slice`, built on `feature/optimize-slice`).
- Do not delete or overwrite existing work. Ask before any destructive action.
- Do not install new third-party packages without asking.
- Be honest about what is implemented, tested, simulated, or only planned. Never present estimates as real savings.

## Reporting style

Simple, clear Indian English. Short sentences, plain words. Give exact commands run and their results.
