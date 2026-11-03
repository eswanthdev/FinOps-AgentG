# Security: implemented vs designed

## Implemented and tested in this release

| Control | Where |
|---|---|
| No AWS calls, no AWS credentials, no secrets in code or config | whole codebase; `S3ExportSource` raises |
| Server binds to localhost only; non-local bind refused by the CLI | `__main__.py` |
| Host header allow-list (DNS-rebinding protection) | `server.py` |
| POST/DELETE need `application/json` and same-origin `Origin` (blocks cross-site posts) | `server.py` |
| Request body limit 64 KB; generic error messages, no stack traces to clients | `server.py` |
| Strict CSP (`default-src 'self'`, no inline script, no eval), nosniff, no-referrer, frame DENY | `server.py` |
| UI inserts all data with `textContent`; tested with an HTML-injection question | `app.js`, `test_ui.py` |
| Only typed, validated `CostQuery`; parameterised SQL; dimension and tag-key allow-lists | `query/` |
| Manifest file keys cannot escape the delivery folder | `ingest/sources.py` |
| Per-worker tool allow-list | `agent/orchestrator.py` |
| Audit log of ingestion outcomes, questions (redacted), tool calls, pins | `audit_events` table |
| Secret redaction (AWS key ids, `secret=`/`token=` pairs, 40-char key-like strings) before audit | `orchestrator.redact` |
| Audit retention purge on server start (`COSTLENS_AUDIT_RETENTION_DAYS`, default 90) | `store.purge_audit` |

## Designed, not implemented

- **Customer-controlled deployment**: runs inside the customer's AWS account (container or Lambda + static UI),
  data never leaves it.
- **Least privilege**: ingestion role with only `s3:ListBucket` on the export prefix, `s3:GetObject` on the export
  objects, `kms:Decrypt` on the bucket key if SSE-KMS; Athena role scoped to one workgroup and one database.
  No write permissions in Visualize or Optimize.
- **Tenant isolation**: one database (or schema) per tenant, separate KMS keys, tenant id in every query,
  isolation tests. Today: one SQLite file = one tenant, nothing more.
- **Encryption**: at rest (KMS / SQLCipher or managed DB), TLS in transit. Today: none (local file, localhost HTTP).
- **Authentication and roles**: SSO (OIDC/SAML), roles per dataset and account. Today: none (local single user).
- **Model safety** (when a model is added): imported data and tool output passed only as quoted data, never as
  instructions; model output accepted only as a validated `CostQuery` or a typed plan; no secrets in prompts;
  prompt and response logging with redaction; provider configurable per tenant, including in-account options.
- **Remediation**: never in Visualize. Later phases only produce reviewable plans; any change needs explicit
  human approval per action, and automatic remediation is a separately approved future capability.
