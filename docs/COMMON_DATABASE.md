# Common resource and opportunity database

The common model sits between source evidence and the agent. Raw source records stay intact. The agent should
start with canonical opportunities and inspect source records only as supporting evidence.

```text
COH / Compute Optimizer / Trusted Advisor / Savings Plans
  -> immutable evidence records + generation membership
  -> resource identity + explicit source links
  -> canonical opportunities + alternatives
  -> one common Athena table -> MCP -> AgentCore
```

## Identity and deduplication

- Every key includes a trusted workspace identity. ARN/account mismatches and accounts outside scope are rejected.
- A matching resource ARN identifies one resource. Names or empty resource IDs alone never establish a match.
- A canonical opportunity is resource + action + target. Matching rightsizing advice from COH and Compute Optimizer
  becomes one opportunity with both evidence references.
- Different actions or targets on the same resource remain alternatives sharing a conflict group. Stopping an
  instance and resizing it are not two additive savings.
- Account-level commitment proposals have separate identities. An explicit, validated source link can connect an
  exported Savings Plans proposal to its COH counterpart; empty ARNs never merge unrelated proposals.
- Records without an actionable recommendation, such as an optimized Compute Optimizer resource, remain context.

The selected source claim is chosen by documented source priority (COH, Compute Optimizer, TA, Savings Plans),
not by picking the largest number. Different discount bases remain different. Conflicting claims on the same
basis are flagged. The canonical table marks all source claims unvalidated and `counted=false`.
Existing Optimize validation and overlap rules still own the local demo's counted savings total.
The cloud agent must not sum source claims, alternatives, or purchases.

## Storage and refresh

`costlens/common.py` implements a pilot relational store with tables for:

| Table | Purpose |
|---|---|
| common_generations | Complete active and historical snapshot generations |
| common_evidence | Immutable source payloads and provider identities |
| common_generation_evidence | Evidence used by each generation |
| common_resources | Workspace-scoped resource identities |
| common_opportunities | Stable opportunities, source links, alternatives and review status |

The test account uses `var/common-evidence.sqlite` as its normalization state. Production should move these
operational tables to a managed relational store if concurrent writers, users and durable workflow are needed.
SQLite inside an AgentCore process is not the common production database.

The queryable common database is the Glue database `costlens_scoped_pilot`. Its `common_opportunities` external
table reads a normalized Parquet generation in the separate pilot results/artifact bucket. Raw export buckets
remain unchanged. Query services have only read access to normalized evidence; the operator publication process
is responsible for writing it. Athena execution results go to a separate `queries/` prefix.

Only a complete four-source recommendation bundle activates a new generation. Duplicate refreshes are skipped,
older observations do not replace newer data, and conflicting contents at the same observation time are rejected.
Disappearing opportunities retire; source evidence, first-seen time and review status are retained. Refreshes
update evidence without resetting an existing review decision. Cross-database publication is not a distributed
transaction: create and verify a generation before switching its table registration.

## Current limits

These mappings are tested on the synthetic contracts, not arbitrary native exports. Explicit links are provided
by those fixtures; native ingestion will need provider IDs and validated matching rules. The pilot uses snapshots,
not a continuous normalization scheduler. The common cloud view exposes source claims, not the local optimization
pipeline's validated/realised totals. There is no automatic remediation.
