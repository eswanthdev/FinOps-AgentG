# CostLens (working name) — v0.3: Visualize, Optimize, Anomalies, Budgets & forecasting

An evidence-backed FinOps agent for AWS. The product has three pillars:

| Pillar | Status in this release |
|---|---|
| **Visualize** — ask a cost question, get an adjustable Cost Explorer-style dashboard | **Implemented (local, synthetic data)** |
| **Optimize** — Cost Optimization Engineer: find, validate and plan savings | **First slice implemented (local, synthetic data, read-only)**. Estimates only; realised savings are not measured. |
| **Govern** — approved guardrails and enforcement (e.g. SCP proposals) | Planned (later phase). |
| **Anomalies** — unusual cost increases, with drivers and likely owner | **Implemented (local, synthetic data)** |
| **Budgets & forecasting** — plain-English budgets, run-rate forecasts, email alerts | **Implemented (local, synthetic data)**. Budgets live in CostLens, not in AWS Budgets. |

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
- How can I reduce EC2 cost?
- How much can prod-data save?
- How can I reduce S3 cost? (no recommendations, so no number)
- How much have we saved so far? (realised savings: not measured)
- Any cost anomalies in the last 90 days?
- Forecast prod-data cost for this month
- Set up a monthly budget of $5,000 for prod-data and alert ops@example.com at 80% and 100%, and when the forecast crosses 100%

## Optimize

Reads a synthetic Cost Optimization Hub and Compute Optimizer snapshot and checks every recommendation against
CUR. Workers run in order: data quality → investigation → optimization → risk → action planning. Each finding
shows owner, phase, status, monthly estimate, confidence, evidence, time range, a reproducible calculation, risks,
limitations and, when the evidence allows, a reviewable plan.

| Phase | Meaning | Counted in the total? |
|---|---|---|
| Plan ready | Evidence supports the estimate | Yes, once per resource |
| Conditional | Instance is Savings Plan covered; the commitment is still paid | No, shown as "up to" |
| Proposal only | Commitment purchase, scaled down by counted savings in its account | No, shown apart |
| Alternative | Competes with another recommendation on the same resource | No |
| Needs evidence | Evidence is missing or disagrees; no estimate is given | No |

On the demo data the counted estimate is $3,272.06 a month, from sources that add up to $5,975.22. "Accept for
planning" only records a decision. CostLens changes nothing in AWS.

## Anomalies

Daily cost per account + service is compared with the median of the previous 14 days. A day is flagged when
the excess is at least the largest of 3 × robust spread (1.4826 × MAD), 20% of expected, and $10. Consecutive
days form one anomaly. Each anomaly shows impact, first-day actual vs expected, peak, a chart, resource-level
drivers, a likely owner (team tag, else account), and can be acknowledged, dismissed or reopened. Only increases
are flagged. On the demo data it finds exactly the two planted cases.

## Budgets, forecasts and alert emails

- Describe a budget in plain English (on the Budgets page or in chat). You get an editable preview first;
  nothing is saved until you press **Create budget**.
- Supports monthly, quarterly and yearly budgets in USD; scope by account, service, region, team/env/app tag or
  untagged; alert levels on actual or forecasted spend; up to 10 email recipients.
- Forecast = actual so far + average of the last 7 days × days left, with a low–high range and a confidence
  label. It is an estimate; the method is shown on every forecast.
- Each alert fires at most once per budget period. Unsent or failed alerts are retried on the next check.
- **Email**: by default alerts go to the in-app **Alert outbox** and are *not sent*. To send real email, set:

  ```bash
  export COSTLENS_SMTP_HOST=email-smtp.ap-south-1.amazonaws.com   # any SMTP relay, e.g. Amazon SES SMTP
  export COSTLENS_SMTP_FROM=finops-alerts@yourcompany.com
  export COSTLENS_SMTP_USERNAME=...   # from your SMTP provider; never put these in code or chat
  export COSTLENS_SMTP_PASSWORD=...
  # optional: COSTLENS_SMTP_PORT (587), COSTLENS_SMTP_STARTTLS (true)
  ```

  Real delivery has been tested only against a fake SMTP server in the test suite, not a real one.
- Alerts are checked when budgets are created, after each ingestion, and with **Check alerts now**.
  There is no background scheduler yet.

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
- Real Cost Optimization Hub and Compute Optimizer exports: the fixtures use a subset of their column names and
  have not been tested against real files.
- Any AI model. `COSTLENS_MODEL_PROVIDER` is read only to say that model mode is not implemented.
- Savings verification (realised savings), Compute Optimizer evidence for anything except EC2 (so RDS findings stay
  "needs evidence"), approvals, governance enforcement (SCPs).
- Real AWS Budgets, scheduled alert checks, anomaly email subscriptions, seasonality-aware forecasts.
- Multi-user login, tenant isolation beyond "one database file per tenant", encryption at rest.
- Real AWS Data Exports manifest format: the fixture manifest is our own documented contract
  (see docs/DATA_CONTRACTS.md); a mapping adapter is needed for the real one.

## Tests

```bash
python -m pytest -q                      # 224 tests: unit, integration (HTTP), browser UI
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
