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

## Stage 1c — Hosted synthetic demo (prepared, not deployed)

Local preparation is done: container, host setting, health check, CloudFormation templates and a runbook. Next, with
approval for each step: a private GitHub repo, then an AWS account, domain, certificate and first deployment. See
`deploy/aws/RUNBOOK.md`. Real data is Stage 2 and needs its own plan. Region: Frankfurt (`eu-central-1`).

## Stage 2 — Live read-only AWS pilot (needs your approval for AWS access)

1. With a pilot customer, set up a CUR 2.0 Data Export to their own S3 bucket (customer does this).
2. Implement `S3ExportSource` with a customer-created read-only role; map the real manifest; test on real data.
3. Add an Athena or DuckDB-over-Parquet backend behind `CostQuery` for large exports.
4. Reconcile our monthly totals with the customer's invoice/Cost Explorer; document any differences.
5. Deploy inside the customer account; no data leaves it.

## Stage 3 — Cost Optimization Engineer (MVP)

First slice done on synthetic data (brought ahead of Stage 2, which still needs AWS approval). Everything below works
locally. Remaining: test against real exports, Compute Optimizer evidence beyond EC2, re-measuring commitments.

- Ingest Cost Optimization Hub and Compute Optimizer exports; join recommendations to CUR spend.
- Workers: data quality → investigation → optimization → risk → action planning.
- Findings with evidence, time range, confidence, owner (from tags/accounts), and a reproducible calculation.
- Deduplicate overlapping recommendations (e.g. rightsizing vs Savings Plan on the same instances);
  never add overlapping savings.
- Reviewable action plans: prerequisites, expected benefit, risks, owner, validation, rollback.
  Commitment purchases are proposals only. No AWS changes.
- Chat interface similar to Visualize: "how can I reduce EC2 cost?" → investigation → plan.

## Stage 4 — Savings verification

First slice done on synthetic data: recorded changes, per-action measuring, rate, usage and commitment effects, overlap
handling and result states, in a separate demo scenario. Remaining: commitment purchases, suggesting changes seen in CUR,
testing on real billing.

- Baselines chosen per action (pre-change window, normalised for usage), discount and commitment effects
  separated, overlapping actions attributed once. Estimated vs realised savings always shown separately.

## Stage 5 — Governance

First slice done on synthetic data, brought ahead of Stage 4 by decision: typed policies, violations, impact
simulation, SCP and tag policy drafts, a recorded approval workflow and exceptions. Draft only; nothing is enforced.
Remaining: a screen to write policies, real AWS Organizations data, AWS Budgets actions (need real AWS Budgets),
verified approvals (need SSO, Stage 6).

- Budgets and alerts per account/team/service; anomaly alerts.
- Guardrail proposals: AWS Budgets actions that apply an SCP or IAM policy when a threshold is crossed.
  The agent drafts; a human approves before anything is enforced. Automatic enforcement only after
  separate approval and a trust period.

## Stage 6 — Production hardening and model-backed investigation

The investigator is built and tested with a fake model, ahead of the rest of this stage by decision. Next, with approval:
a first live run on Bedrock in Frankfurt, the 20-question evaluation, and a measured cost per question.

- SSO, roles, tenant isolation, encryption, retention settings, security review.
- Configurable model provider for interpretation and narrative, constrained to typed tools and validated output.
- Integrations: Slack/Teams digests, Jira/ServiceNow tickets for owners.
- Separately approved remediation, per action, with rollback.
