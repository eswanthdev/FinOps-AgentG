# Documentation index

Current implementation audit: 6 November 2026. Start here for the live synthetic pilot.

## What is running

The hosted app at https://costlens.cloudaip.com uses AgentCore chat, authenticated MCP and scoped Athena queries
over synthetic Parquet in S3. Five export buckets hold CUR, COH, Compute Optimizer, Trusted Advisor and Savings Plans.
Storage uses a labelled synthetic MCP fixture, not an export bucket. No real customer billing is connected.

The common model retains 50 source records and presents 29 canonical opportunities. The validated Optimize report
has 26 findings and a $3,272.06 monthly counted estimate. These represent different layers; do not combine their
numbers. October's synthetic unblended bill is $18,561.93.

Optimize has service tabs, phase filters, filtered monetary summaries and expandable evidence, confidence, risks
and plans. Its report is calculated from the verified S3 snapshot and republished with the tools deployment;
it is not recalculated from Athena on every page visit. Planning and implementation mutations remain disabled.
Governance, anomalies and budgets still exist in local mode but are not connected to the cloud UI.

## Guides

| Guide | Covers |
|---|---|
| [Cloud UI](CLOUD_UI.md) | Chat/dashboards, service tabs, report refresh, configuration, releases and rollback |
| [Scoped Athena / MCP / AgentCore](SCOPED_ATHENA_AGENTCORE.md) | Query boundaries, IAM, tools, runtime and live checks |
| [Common database](COMMON_DATABASE.md) | Resource identity, deduplication, alternatives, evidence and refresh rules |
| [Synthetic S3 pilot](SYNTHETIC_S3_PILOT.md) | Parquet schemas, publishing/fetching, source markers and checksums |
| [Local AWS CUR adapter](LOCAL_AWS_CUR.md) | CSV/gzip adapter and unsupported native manifest/Parquet ingestion |
| [AWS platform design](AWS_DATA_PLATFORM.md) | Source contracts, proposed production architecture and acceptance gates |
| [Architecture](ARCHITECTURE.md) | Original local architecture with links to the deployed cloud extension |
| [Data contracts](DATA_CONTRACTS.md) | Implemented fixture and agent-state contracts |
| [Security](SECURITY.md) | Implemented controls and remaining production requirements |
| [Roadmap](ROADMAP.md) | Current implementation versus future native production onboarding |
| [AWS operations runbook](../deploy/aws/RUNBOOK.md) | Original hosting bootstrap; links to current release procedures |

## Operational records

Current deployment identifiers, destinations, release commands, rollback containers and smoke-check references
are saved in ignored `var/` JSON files. They are local operational records, not version-controlled configuration.
The host also retains root-only release metadata and stopped rollback containers; the persistent `/data` database
is preserved. Do not put credentials into docs, deployment state or Git.

Relevant scripts: `publish-synthetic.ps1`, `fetch-synthetic.ps1`, `deploy_scoped_pilot.py`,
`check_scoped_pilot.py`, `release_cloud_ui.py` and `check_cloud_ui.py`, all under `deploy/aws/`.
For an updated Optimize snapshot, fetch registered sources into a new local folder and run
`deploy_scoped_pilot.py --code-only --data-root <that-folder>` after validation and AWS authorization.
UI changes need a new immutable image tag and the reversible release script described in the cloud UI guide.

## Verification ledger and remaining work

The cloud app baseline passed 547 non-browser tests and three browser flows. The Optimize restoration passed
83 focused tests. The later service-tab change passed 17 relevant tests, including browser service switching and
exact-money reconciliation. Hosted checks verified RDS filtering, an empty S3 recommendation state, chart totals,
the Optimize estimate and one chat-to-evidence flow. These are recorded runs, not a claim that the whole latest
suite was rerun after every small UI change.

Production native export mappings, real storage telemetry, per-user resource permissions, managed workflow state,
continuous normalization, cloud planning/implementation workflows and remaining cloud modules are unfinished.
Source buckets in production will be supplied by the customer with read-only access and a scoped Athena workgroup;
derived state and query-result destinations require explicit configuration.

The new code and documentation are currently in the local checkout on `feature/investigator-agent` and deployed
to the synthetic test environment. They have not been committed or pushed to GitHub in this chat.
