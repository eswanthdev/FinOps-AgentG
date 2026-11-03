# Roadmap

Order agreed: **Visualize → Cost Optimization Engineer → Governance**. Each stage ends with tests and an honest
"what works / what does not" note.

## Stage 1 — Visualize (this release, local and synthetic) ✅

Done: see README. Remaining polish for Stage 1:
- Saved dashboards with relative dates ("last 30 days" that rolls forward).
- More chart types users expect from Cost Explorer (area, cumulative), and a usage-quantity metric.
- RI amortization (RIFee, DiscountedUsage) so amortized cost works for RI customers.

## Stage 1b — Anomalies and Budgets & forecasting (this release, local and synthetic) ✅

Next for this stage: background scheduler for alert checks, anomaly email subscriptions, Slack/Teams alerts,
seasonality-aware forecast (weekday pattern, month-start charges), optional sync to real AWS Budgets
(needs AWS write access and separate approval).

## Stage 2 — Live read-only AWS pilot (needs your approval for AWS access)

1. With a pilot customer, set up a CUR 2.0 Data Export to their own S3 bucket (customer does this).
2. Implement `S3ExportSource` with a customer-created read-only role; map the real manifest; test on real data.
3. Add an Athena or DuckDB-over-Parquet backend behind `CostQuery` for large exports.
4. Reconcile our monthly totals with the customer's invoice/Cost Explorer; document any differences.
5. Deploy inside the customer account; no data leaves it.

## Stage 3 — Cost Optimization Engineer (MVP)

- Ingest Cost Optimization Hub and Compute Optimizer exports; join recommendations to CUR spend.
- Workers: data quality → investigation → optimization → risk → action planning.
- Findings with evidence, time range, confidence, owner (from tags/accounts), and a reproducible calculation.
- Deduplicate overlapping recommendations (e.g. rightsizing vs Savings Plan on the same instances);
  never add overlapping savings.
- Reviewable action plans: prerequisites, expected benefit, risks, owner, validation, rollback.
  Commitment purchases are proposals only. No AWS changes.
- Chat interface similar to Visualize: "how can I reduce EC2 cost?" → investigation → plan.

## Stage 4 — Savings verification

- Baselines chosen per action (pre-change window, normalised for usage), discount and commitment effects
  separated, overlapping actions attributed once. Estimated vs realised savings always shown separately.

## Stage 5 — Governance

- Budgets and alerts per account/team/service; anomaly alerts.
- Guardrail proposals: AWS Budgets actions that apply an SCP or IAM policy when a threshold is crossed.
  The agent drafts; a human approves before anything is enforced. Automatic enforcement only after
  separate approval and a trust period.

## Stage 6 — Production hardening and model-backed investigation

- SSO, roles, tenant isolation, encryption, retention settings, security review.
- Configurable model provider for interpretation and narrative, constrained to typed tools and validated output.
- Integrations: Slack/Teams digests, Jira/ServiceNow tickets for owners.
- Separately approved remediation, per action, with rollback.
