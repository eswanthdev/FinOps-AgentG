# Cloud-connected application

The hosted app connects chat to AgentCore and dashboards to Athena through authenticated MCP. All pilot data
remains synthetic. The existing application database keeps conversations, memory, reports, feedback and pins.

```text
Browser -> existing proxy-protected HTTP app
  chat -> AgentCore investigator -> MCP -> scoped Athena tools
  chart/filter/status -> AgentCore application operation -> MCP -> Athena (no model call)
  personal/workspace state -> existing persistent application database
```

## Configuration

`COSTLENS_BACKEND=cloud` selects this path. `COSTLENS_AGENTCORE_RUNTIME_ARN` identifies the one registered runtime.
The server uses its AWS role; the browser receives no AWS credentials. Local development may set
`COSTLENS_AWS_PROFILE=costlens-demo`. The default backend remains local.

```powershell
$env:COSTLENS_BACKEND = 'cloud'
$env:COSTLENS_AGENTCORE_RUNTIME_ARN = '<registered-runtime-arn>'
$env:COSTLENS_AWS_PROFILE = 'costlens-demo'
python -m costlens serve --db var/cloud-ui-state.sqlite --port 8765
```

Install the optional `cloud` dependency group for the AWS SDK. Dashboard responses preserve the existing chart,
table and filter contract, with exact money conversion and Athena query IDs. Missing days are not filled with
zero and billing finality remains unknown. Variance charts do not yet include resource-level drivers.

## Behaviour

- The top bar shows `AI-assisted · Cloud` and `Synthetic data`.
- Cost investigations automatically open their first evidence query as an adjustable dashboard.
- Filters, saved dashboards, CSV data and saved-report refreshes use the cloud result path.
- Optimize restores the original summary cards, phase tabs, expandable evidence, confidence, risks and action plans.
  Its validated estimates are calculated from the verified S3 export snapshot and published with source checksums.
  The common deduplicated source view remains available separately. Planning/implementation mutations are still disabled.
- The server passes only the requesting user's bounded recent history and saved preferences to the runtime.
- Governance, anomaly and budget modules are unavailable in cloud mode until connected. Their local mutations
  are blocked; local fixture results are not presented as cloud results.
- Cloud failures show errors. No local billing dataset is silently substituted.

The runtime has no durable personal state. The app checks its existing monthly token limit before model requests.
Direct operator invocation of the runtime bypasses app-level workflow controls and should remain restricted.

## Hosted release and rollback

`Dockerfile.cloud` runs the UI as a non-root user and serves the existing database without generating fixtures at
startup. The server role gains only invocation of the scoped runtime and its DEFAULT endpoint, not raw S3 or
Athena access. Cognito, HTTPS and the load-balancer-only inbound network rule remain in place.

`deploy/aws/release_cloud_ui.py` deploys a previously published immutable image through Systems Manager. It
preserves `/data`, retains the previous stopped container and automatically rolls back if cloud-status checking
fails. Actual release IDs and rollback names are in ignored `var/cloud-ui-release.json`. Original container
metadata is saved in a root-only `/var/lib/costlens-deployments/<release>/` directory on the host.

To roll back, stop and retain the new container, rename the recorded rollback container to `costlens` and start
it. Do not delete the data directory. The previous image and container have not been removed.

## Verification on 6 November 2026

- 547 non-browser tests and three cloud browser flows passed.
- Chrome verified chat/chart rendering, pinning, report refreshes, source labels and disabled navigation using a
  fake transport, with no paid AWS calls.
- Live app status and dimensions returned the scoped S3 source metadata and eight services.
- Hosted `/api/query` returned the synthetic October bill of $18,561.93 with eight chart series.
- Hosted `/api/opportunities` returned 29 common opportunities.
- A paid hosted chat smoke check answered: "Our total unblended AWS cost in October 2026 was $18,561.93. This is
  synthetic data." Opening its evidence through `/api/query` returned the same amount.

The hosted smoke command is recorded in `var/cloud-ui-check.json`. Run read/query checks with
`python deploy/aws/check_cloud_ui.py --profile costlens-demo`. Adding `--model` runs one explicitly approved
paid chat investigation and verifies its evidence chart.

Production native schemas, managed workflow state, per-user resource authorization and the remaining FinOps
modules still need separate work. This release is the live synthetic pilot.

## Optimize restoration

The initial cloud UI reduced Optimize to a bare list. The richer original layout is restored using a deterministic
report built by `costlens/ingest/optimization_report.py` from freshly fetched, checksum-verified S3 Parquet files.
It uses the existing optimization pipeline and overlap rules, yielding 26 findings and the $3,272.06 monthly
counted synthetic estimate. The common source database still contains 29 canonical source opportunities; these
are a different layer and are not added to the validated total.

The application-only `dashboard_optimization` MCP tool serves the published report, checks its account/source
locations against the configured workspace, and refuses it outside synthetic mode. No new model tool or source
write permission is introduced. The page shows the source snapshot time explicitly. Rebuild/redeploy the report
when source snapshots change; it is not recomputed from Athena on each page request.

Planning decisions and implementation recording remain unavailable in cloud mode, and their buttons are not shown.
The restored page was checked in Chrome, including phase filtering, expandable evidence/calculation sections and
the absence of unavailable mutation controls. The focused UI/report/optimization suite passed 83 tests.

## Service tabs

Optimize now offers All services, EC2, EBS, RDS, S3 and any additional services represented in the report, including
Savings Plans. A service selection resets the phase to All, filters recommendations and recalculates the monetary
summary with the existing exact-money rules. Phase tabs then narrow that selected service. EBS recommendations
are separated from EC2 even though their billing product is EC2. Services without recommendations display an
explicit empty state; no findings are generated to fill a tab. Report-wide source-quality checks remain labelled
as report-wide. Read-only cloud workflow controls are unchanged.
