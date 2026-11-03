# CostLens (working name) — Visualization module, first slice

An evidence-backed FinOps agent for AWS. The product has three pillars:

| Pillar | Status in this release |
|---|---|
| **Visualize** — ask a cost question, get an adjustable Cost Explorer-style dashboard | **Implemented (local, synthetic data)** |
| **Optimize** — Cost Optimization Engineer: find, validate and plan savings | Planned (next phase). The UI shows it as "Next" and does nothing. |
| **Govern** — budgets, alerts, approved guardrails (e.g. SCP proposals) | Planned (later phase). |

> **Everything you see in the demo is synthetic data.** No AWS account, credential or API is used anywhere.
> The app labels itself "Synthetic data" and "Deterministic mode" on every screen.

## Run it locally

Requires Python 3.11+ (tested on 3.13). No third-party packages are needed to run the app.

```bash
python -m costlens demo            # generates fixtures in ./var, ingests them, serves http://127.0.0.1:8765
python -m costlens demo --reset-db # same, starting from an empty demo database
```

Other commands:

```bash
python -m costlens generate-fixtures --out ./my-fixtures
python -m costlens ingest --source ./my-fixtures --db ./my.sqlite --synthetic
python -m costlens status --db ./my.sqlite
python -m costlens serve --db ./my.sqlite --port 8765
```

## Try these questions

- Show cost by service for last month
- Why did costs increase in October compared to September?
- Daily EC2 cost by account for the last 30 days → then "now by region" → "only prod-data"
- Top 5 teams by amortized cost this month
- Untagged spend by account last month
- Cost by service in July (shows the *missing data* state)
- How can I optimize EC2? (explains that this is the next module)

## What works (implemented and tested)

- **CUR 2.0 ingestion** from a local folder of deliveries (manifest + CSV files), all-or-nothing per delivery,
  with outcomes: active, superseded (revision), duplicate, stale, rejected_incomplete, rejected_invalid,
  rejected_conflict, rejected_access. Revisions *replace* a billing period; nothing is ever double counted.
- **Money**: integer nano-USD everywhere, half-even rounding to cents only for display, no floats.
- **Unblended and amortized cost**, with Savings Plan amortization. Charge types the engine cannot amortize
  (e.g. RI fees) return "insufficient data" instead of a guess.
- **Explore queries**: date range, daily/monthly/total, group by service/account/region/usage type/charge
  type/resource/tag, filters, top-N with "Other", comparison with the previous equal-length period.
- **Variance ("why did it change")**: per-group change, per-day normalisation when window lengths differ,
  and resource-level drivers rolled up by account + service + usage type (e.g. "8 new c6i.4xlarge from 15 Oct").
- **Coverage and freshness** on every answer: missing months, not-final months, rejected newer deliveries,
  data-through date, and which delivery (execution id) each number came from.
- **Deterministic question interpreter**: shows what it understood and every default it assumed; supports
  follow-ups ("now by region"); refuses or asks instead of guessing.
- **Orchestrator + worker** with typed, bounded tools and an audit log (secrets redacted).
- **UI**: Claude-style chat, dashboard cards with live controls, chart/table toggle, CSV export, pinning to
  saved dashboards, data & coverage page, audit log, empty/loading/error/insufficient states, mobile layout,
  light and dark themes.

## What is NOT implemented

- Any real AWS access (S3, Athena, Cost Explorer, Cost Optimization Hub, Compute Optimizer, etc.).
  `S3ExportSource` exists only as a documented interface and raises `NotImplementedError`.
- Any AI model. `COSTLENS_MODEL_PROVIDER` is read only to say that model mode is not implemented.
- Forecasting, anomaly detection, budgets, alerts, optimization findings, action plans, approvals.
- Multi-user login, tenant isolation beyond "one database file per tenant", encryption at rest.
- Real AWS Data Exports manifest format: the fixture manifest is our own documented contract
  (see docs/DATA_CONTRACTS.md); a mapping adapter is needed for the real one.

## Tests

```bash
python -m pytest -q                      # 100 tests: unit, integration (HTTP), browser UI
python -m pytest -q --ignore=tests/test_ui.py   # without a browser
```

UI tests need Playwright and a Chrome/Chromium binary (`COSTLENS_CHROME=/path/to/chrome`); they skip otherwise.

## Positioning note (commercial reality)

AWS Cost Explorer added natural-language questions (via Amazon Q) at no extra charge in April 2026, and
vendors such as Vantage ship FinOps agents that build reports and dashboards. A chat-to-chart feature alone is
not a product. This module is the foundation; the differentiation we are building towards is:
evidence and provenance on every number, honest handling of incomplete or revised billing data,
customer-controlled deployment, and a direct path from "where did the money go" into validated,
approved optimization actions and enforced governance.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, agent design, request flow
- [docs/DATA_CONTRACTS.md](docs/DATA_CONTRACTS.md) — CUR subset, manifest, amortization rules, query contract
- [docs/SECURITY.md](docs/SECURITY.md) — implemented controls vs design
- [docs/ROADMAP.md](docs/ROADMAP.md) — next stages
