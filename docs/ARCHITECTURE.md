# Architecture

Start with the [documentation index](README.md). The [AWS platform design](AWS_DATA_PLATFORM.md) includes both
implemented synthetic pilot components and proposed production adapters; its implementation notes distinguish them.

Implementation update: the [scoped cloud pilot](SCOPED_ATHENA_AGENTCORE.md) implements a bounded Athena tool
backend, IAM MCP Gateway and separate AgentCore runtime over synthetic data. The [common model](COMMON_DATABASE.md)
preserves source evidence and deduplicates recommendations. The diagram and local request flow below still describe
the existing SQLite application, which has not been replaced by that pilot.

```
 deliveries (folder)          ┌──────────────────────── costlens (Python, stdlib only) ───────────────────────┐
 manifest.json + CSV  ──────▶ │ ingest/sources.py   ExportSource protocol: LocalDirectorySource (done),      │
                              │                     S3ExportSource (interface only)                         │
                              │ ingest/loader.py    validate → checksum → parse → idempotent apply          │
                              │ ingest/cur2.py      CUR 2.0 row contract + amortization rules               │
                              │ store.py            SQLite: deliveries, line_items, accounts, pins, audit   │
                              │ query/model.py      CostQuery: the only query shape (typed, bounded)        │
                              │ query/engine.py     explore + variance + coverage, integer nano-USD         │
                              │ agent/interpreter.py DeterministicInterpreter → Interpretation(CostQuery)   │
                              │ agent/orchestrator.py Orchestrator → VisualizationWorker → tools            │
                              │ anomalies.py        14-day median/MAD daily detection, drivers, owner       │
                              │ budgets.py          budget spec + plain-English parser, forecast, alerts    │
                              │ notify.py           OutboxNotifier (default) / SmtpNotifier (env-configured)│
                              │ server.py           localhost HTTP API + static UI                          │
                              └────────────────────────────────────────────────────────────────────────────┘
                                              ▲ JSON
                              web/index.html, app.js, charts.js, styles.css (no external assets)
```

## Request flow: a question

1. UI posts `{question, previous}` to `POST /api/ask`. `previous` is the last `CostQuery`, used for follow-ups.
2. `Orchestrator.ask` loads a `Vocabulary` (known services, accounts, regions, usage types, tag keys/values)
   from the data and calls the interpreter.
3. `DeterministicInterpreter` returns an `Interpretation`: `ok` with a validated `CostQuery`, or `clarify`,
   or `unsupported` (with the module that would own it: optimize / govern).
4. For `ok`, the orchestrator calls the `VisualizationWorker` tool `query_costs(CostQuery)`. Tools are a fixed
   allow-list per worker; calling anything else raises `PermissionError`. Each call is audited.
5. `engine.run` computes coverage, then the numbers, and returns the result with its calculation, SQL template,
   parameters, source deliveries and warnings.
6. The UI renders the summary and a dashboard card. Controls on the card call `POST /api/query` with a modified
   `CostQuery` directly (no interpreter), so adjusting a view is exact.

## Agent design

- **Orchestrator**: routes a request to a worker. Active workers: `visualization`, `optimize`, `govern`, `anomalies`,
  `budgets` (budget, governance, savings, anomaly and forecast requests are routed before the cost interpreter).
  `PLANNED_WORKERS` is empty today.
- **Investigator** (`agent/investigator.py`): optional. A model drives a bounded tool loop over eight read-only tools that
  reach every module. `ModelClient` is the only model boundary, so tests use a fake. Answers pass a check that every
  amount and percentage came from a tool. On any failure `Orchestrator.ask` falls back to the deterministic path.
- **Governance pipeline** (`agent/govern.py`): four workers with fixed tool allow-lists. Policy check evaluates typed
  policies through a `CostQuery`; impact simulates a guardrail on billing data and links Optimize findings; risk adds
  risks and limits; guardrail drafting writes JSON as text. No code path applies anything.
- **Optimization pipeline** (`agent/optimize.py`): five workers, each with a fixed tool allow-list. Data quality
  checks the snapshot and the evidence; investigation reads CUR through a typed `CostQuery`; optimization estimates,
  deduplicates and sequences commitments; risk sets confidence, risks and limitations; action planning writes
  reviewable plans. The Optimize page and chat share this pipeline, so their numbers match.
- **Workers**: own a small set of typed tools. The visualization worker has `query_costs` and `data_coverage`.
- **Interpreter protocol**: `interpret(question, vocab, today, previous) -> Interpretation`. A model-backed
  interpreter must return the same structure. Its output is validated by `CostQuery.validate()` exactly like
  the deterministic one, so a model can never run free-form SQL or exceed limits.
- **Mode labelling**: every response carries `mode: deterministic` and the UI shows "Deterministic mode".

- **Verification pipeline** (`agent/verify.py`): four workers with fixed tool allow-lists. Change record lists what a
  person recorded; baseline measures windows through a `CostQuery`; attribution splits rate, usage and commitment
  effects; verification sets the result state. Results are recalculated on every run and never stored.

## Why these choices

- **SQLite + stdlib HTTP**: zero install, easy to review, good enough for one tenant's CUR at daily
  granularity in local mode. The engine is isolated behind `CostQuery`, so an Athena/DuckDB backend can replace
  the SQL layer for real exports without changing the UI or the interpreter.
- **Integer nano-USD**: CUR has up to 10 decimals; storing 9 decimals as integers makes every SUM exact.
- **Replace-per-period revisions**: CUR revisions restate a whole billing period, so replacing is the only way
  to never double count.
