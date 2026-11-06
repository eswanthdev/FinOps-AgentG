# Scoped Athena, MCP and AgentCore pilot

Update: the [hosted cloud UI](CLOUD_UI.md) now connects chat, charts, filters, saved dashboards and report
refreshes to this runtime. Additional application-only MCP tools provide dashboard-compatible responses and
source metadata without model calls. The investigator's model tool allow-list stays separate.

This is a synthetic test-account deployment. It is separate from the existing hosted investigator runtime.

```text
AgentCore Runtime (Nova investigator + existing answer number checks)
  -> IAM-signed MCP requests
  -> AgentCore Gateway (AWS_IAM inbound authentication)
  -> scoped Lambda tools
  -> enforced Athena workgroup
  -> registered Glue tables over read-only export/normalized prefixes
```

## Query boundaries

`AthenaScope` is operator configuration: workspace, account allow-list, fixed catalog/database/tables/locations,
workgroup, results prefix, timeout, scan cap and row cap. Model inputs cannot override these settings. Every call
checks workgroup enforcement, table locations and explicit partition locations. IAM also restricts source S3
prefixes, registered Glue tables and the one workgroup. Cross-account production access will need customer grants.

The compiler accepts a validated `CostQuery`, selects dimension expressions from an allow-list and passes filter
values through Athena execution parameters. It does not accept SQL. Monetary aggregation uses decimal arithmetic
and row-level half-even conversion to nano-USD. Empty data, unsupported currency/amortization and excessive result
rows do not produce a guessed total. Timeout/scan overruns trigger cancellation.

The pilot workgroup has an enforced 10 MB scan cap per query, encrypted S3 output with expected bucket owner,
and a distinct `queries/` prefix. Results expire after seven days. This is a scan cap, not a free-tier guarantee
or a total monthly spending budget. The source buckets are never written by the query role.

## Tools

| Tool | Data and limits |
|---|---|
| query_costs | Typed explore/variance, scoped accounts and dates, top N plus Other |
| data_coverage | Observed billing periods/data-through dates; does not infer finality |
| list_opportunities | Common deduplicated source claims and alternatives; not counted savings |
| list_recommendations | Original source recommendations for supporting investigation |
| storage_metrics | Labelled synthetic storage fixture through MCP; no S3 export bucket |

The Gateway API's schema format is narrower than JSON Schema. A projected discovery schema is used there, while
the Lambda handler retains field and scope validation. Gateway tool names are prefixed `finops___`.
The client only permits the fixed configured regional AWS HTTPS endpoint and the compiled tool allow-list.

Storage telemetry in production is not yet connected: the tool returns unavailable when synthetic mode is off.
Realised savings, governance, budgets and anomalies are not yet connected to this cloud tool set. The local demo
retains those modules. The Athena tool response is evidence for the cloud investigator, not a drop-in dashboard
response for every function in the SQLite engine. Variance reports absolute changes and flags unequal windows;
the local engine has richer normalized drivers. Billing finality remains unknown for this pilot.

## Runtime and state

`cloud_runtime.py` implements AgentCore's `/ping` and `/invocations` endpoints. Inbound calls use AgentCore IAM
authorization; public network mode does not make invocation unauthenticated. The runtime can invoke only its
Gateway and the selected Nova inference profile/model resources. It cannot execute Athena directly or mutate cloud
resources. It keeps no durable user history, memory or reports. Those remain application responsibilities.

`{"operation":"health"}` checks authenticated tool discovery without calling a model. A question triggers a bounded
model investigation. Responses retain the original number-presence check; this does not prove the semantic correctness
of every statement. A model/tool failure returns unavailable rather than an unverified answer. No application-wide
monthly token cap is enforced by this standalone runtime yet; restrict who can invoke it during the pilot.

## Deployment and verification

Optional local tooling: boto3, PyArrow and pytest. Live deployment uses the existing `costlens-demo` profile and
`eu-central-1`. Destination/state files stay in ignored `var/`; do not commit credentials or actual deployment state.

```powershell
python deploy/aws/deploy_scoped_pilot.py --profile costlens-demo --region eu-central-1
python deploy/aws/check_scoped_pilot.py --profile costlens-demo
# One explicitly approved paid model smoke check:
python deploy/aws/check_scoped_pilot.py --profile costlens-demo --model
```

The deployment script creates a separate results/artifact bucket, Glue database/tables, workgroup, three scoped
roles, Lambda tool function, authenticated Gateway/target and AgentCore Runtime. It records IDs in
`var/scoped-pilot-deployment.json`. It may update only the roles/function/runtime belonging to this pilot; the
previous hosted runtime is untouched. Source schemas require the tested synthetic fixtures.

Production configuration will point at customer-supplied sources with read-only S3 and scoped Athena access.
Native schema adapters, freshness/finality policies, managed workflow state, per-user authorization and cost budgets
must be validated before treating this test deployment as production-ready. Do not enable production by simply
changing the synthetic label.

## Verified on 5 November 2026

- Direct Athena returned October's exact synthetic total: `18561934352639` nano-USD, displayed as $18,561.93.
- IAM-authenticated MCP returned the same bill and all four recommendation sources (25 COH, 21 Compute Optimizer,
  three Trusted Advisor and one Savings Plans record).
- The common table exposed 29 canonical opportunities from those 50 original records, preserving evidence and alternatives.
- AgentCore discovered the five tools through its own IAM-authenticated Gateway connection.
- A live Nova investigation called the scoped cost tool and answered: "Our total unblended AWS cost in October 2026
  was $18,561.93. This is synthetic data." An invalid top-N value was rejected; the model corrected it before answering.
- The broad non-browser suite passed 533 tests before the final schema/error-reporting refinement. After that change,
  94 targeted common-database, scoped-query and investigator tests passed. Browser tests were not run.

The Nova EU inference profile currently includes Frankfurt, Ireland, Paris and Stockholm. Its pilot role permits
only the exact Nova Lite model ARNs in those four regions and the selected EU inference profile. No wildcard model
or source-data permission was added to resolve model access.

References: [Athena execution parameters](https://docs.aws.amazon.com/athena/latest/ug/querying-with-prepared-statements-querying-using-execution-parameters.html),
[workgroup controls](https://docs.aws.amazon.com/athena/latest/ug/workgroups-manage-queries-control-costs.html),
[Gateway IAM authentication](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html),
[Lambda targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-lambda.html),
[Runtime direct code deployment](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html).
