# Security: implemented vs designed

## Implemented and tested in this release

| Control | Where |
|---|---|
| No AWS calls, no AWS credentials, no secrets in code or config | whole codebase; `S3ExportSource` raises |
| Server binds to localhost by default; non-local bind refused unless both proxy settings are set | `__main__.py`, `hosting.py` |
| Host header allow-list (DNS-rebinding protection); extra names must be exact hostnames, no wildcards | `server.py`, `hosting.py` |
| `/healthz` is the only path that skips the host check, and it returns no data | `server.py` |
| POST/DELETE need `application/json` and same-origin `Origin` (blocks cross-site posts) | `server.py` |
| Request body limit 64 KB; generic error messages, no stack traces to clients | `server.py` |
| Strict CSP (`default-src 'self'`, no inline script, no eval), nosniff, no-referrer, frame DENY | `server.py` |
| UI inserts all data with `textContent`; tested with an HTML-injection question | `app.js`, `test_ui.py` |
| Only typed, validated `CostQuery`; parameterised SQL; dimension and tag-key allow-lists | `query/` |
| Manifest file keys cannot escape the delivery folder | `ingest/sources.py` |
| Per-worker tool allow-list | `agent/orchestrator.py` |
| Audit log of ingestion outcomes, questions (redacted), tool calls, pins | `audit_events` table |
| Secret redaction (AWS key ids, `secret=`/`token=` pairs, 40-char key-like strings) before audit | `orchestrator.redact` |
| Budget creation needs explicit confirmation; budget, alert and anomaly-status changes are audited | `budgets.py`, `server.py` |
| SMTP credentials read only from environment variables; never stored, logged, returned or shown | `notify.py` |
| Email header injection blocked (CR/LF stripped from subjects); recipients validated, max 10 | `notify.py`, `budgets.py` |
| Optimize is read-only: no AWS calls, plans are text; status changes and pipeline runs are audited | `agent/optimize.py`, `server.py` |
| Optimization workers have fixed tool allow-lists; snapshot rows are validated, bounded and all-or-nothing | `agent/optimize.py`, `ingest/optimization.py` |
| Govern is draft-only: guardrails are text, no AWS client exists, and a test checks the code imports none | `agent/govern.py`, `test_govern.py` |
| Policies are typed with a fixed set of kinds; unknown fields are rejected and bad files change nothing | `ingest/governance.py` |
| Guardrail status changes, exceptions and policy on/off are audited; an approved draft that changes loses its approval | `agent/govern.py`, `server.py` |
| Verification is read-only: recording or undoing a change is audited and touches no AWS resource | `agent/verify.py`, `server.py` |
| Investigator tools are a fixed read-only allow-list; input is validated as a typed `CostQuery` | `agent/investigator.py` |
| Answers are rejected if any amount or percentage is not in a tool result the model saw | `agent/investigator.py` |
| Billing text reaches the model only inside tool results; it cannot widen the tool list. Traces are redacted | `agent/investigator.py` |
| Model answers are inserted with `textContent`; runs and tool calls are audited; each run has hard limits | `web/investigate.js`, `agent/investigator.py` |
| Conversations, memory, reports and run data are scoped by user; behind the proxy, no login means 401 | `agent/session.py`, `server.py` |
| The model cannot write memory; memory refuses amounts and secrets and reaches the model as quoted data | `agent/session.py`, `agent/investigator.py` |
| A monthly token limit stops model calls and falls back to fixed rules | `agent/session.py`, `agent/orchestrator.py` |
| Audit retention purge on server start (`COSTLENS_AUDIT_RETENTION_DAYS`, default 90) | `store.purge_audit` |

## Designed, not implemented

- **Hosted demo**: `deploy/aws/` holds templates for HTTPS, Cognito login, a security group that admits only the load
  balancer, encrypted disk, IMDSv2 and a narrow server role. Text checks exist; none of it has been deployed. The app
  trusts the network to enforce that the proxy is the only way in, and does not verify login tokens itself.

- **Verified identity**: the app trusts the load balancer identity header without checking its signature, so the network
  rule that only the load balancer reaches the server is essential. "Approved" in Govern records a decision only, and anyone
  signed in can record it. Real sign-off needs SSO, roles and separation of duties.

- **Alert recipients**: today any valid address can be added. Production needs a verified-domain allow-list
  and recipient confirmation, so the tool cannot be used to email arbitrary people.

- **Customer-controlled deployment**: runs inside the customer's AWS account (container or Lambda + static UI),
  data never leaves it.
- **Least privilege**: ingestion role with only `s3:ListBucket` on the export prefix, `s3:GetObject` on the export
  objects, `kms:Decrypt` on the bucket key if SSE-KMS; Athena role scoped to one workgroup and one database.
  No write permissions in Visualize or Optimize.
- **Tenant isolation**: one database (or schema) per tenant, separate KMS keys, tenant id in every query,
  isolation tests. Today: one SQLite file = one tenant, nothing more.
- **Encryption**: at rest (KMS / SQLCipher or managed DB), TLS in transit. Today: none (local file, localhost HTTP).
- **Authentication and roles**: SSO (OIDC/SAML), roles per dataset and account. Today: none (local single user).
- **Model safety, still to do**: provider configurable per tenant, full prompt and response logging with redaction, and a
  company review before real billing data reaches a model. Prompt injection can still mislead the written answer, even
  though it cannot change state or numbers. The controls above are tested with fake models and have had only a few live runs on Nova Lite.
- **Remediation**: never in Visualize. Later phases only produce reviewable plans; any change needs explicit
  human approval per action, and automatic remediation is a separately approved future capability.
