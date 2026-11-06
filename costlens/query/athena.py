"""Scoped SELECT-only Athena tools. Configuration is supplied by the operator.

No model-supplied SQL, catalog, table, bucket, workgroup or role is accepted.
The existing SQLite engine remains the local dashboard backend.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from ..money import nanos_to_cents_str, pct_change
from .model import CostQuery, QueryError

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_DIMENSIONS = {"service": "line_item_product_code", "account": "line_item_usage_account_id",
               "region": "product_region_code", "usage_type": "line_item_usage_type",
               "line_item_type": "line_item_line_item_type", "resource": "line_item_resource_id"}
SOURCES = {"cur", "cost-optimization-hub", "compute-optimizer", "trusted-advisor", "savings-plans"}


def _literal(value: str) -> str:
    # Athena ExecutionParameters are SQL expressions, not raw strings.
    return "'" + value.replace("'", "''") + "'"


def display(nanos: int) -> str:
    amount = nanos_to_cents_str(abs(nanos))
    whole, fraction = amount.split(".")
    return f"{'-' if nanos < 0 else ''}${int(whole):,}.{fraction}"


@dataclass(frozen=True)
class AthenaScope:
    region: str
    account_id: str
    workspace_id: str
    database: str
    workgroup: str
    results_uri: str
    accounts: tuple[str, ...]
    tables: dict[str, str]
    locations: dict[str, str]
    tag_keys: tuple[str, ...] = ("user:team", "user:env", "user:app")
    synthetic: bool = True
    max_scan_bytes: int = 10_000_000
    timeout_seconds: int = 45
    max_rows: int = 500
    catalog: str = "AwsDataCatalog"
    reference_date: str = "2026-11-03"

    @classmethod
    def from_dict(cls, raw: dict):
        if set(raw) - set(cls.__dataclass_fields__):
            raise ValueError("unknown Athena scope setting")
        scope = cls(**{**raw, "accounts": tuple(raw["accounts"]),
                       "tag_keys": tuple(raw.get("tag_keys", ("user:team", "user:env", "user:app")))})
        if not all(_IDENTIFIER.fullmatch(v) for v in (scope.database, *scope.tables.values())):
            raise ValueError("invalid catalog identifier")
        if (scope.catalog != "AwsDataCatalog" or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", scope.workgroup)
                or not scope.workspace_id or not re.fullmatch(r"\d{12}", scope.account_id)):
            raise ValueError("invalid fixed scope")
        if not scope.accounts or len(scope.accounts) > 50 or not all(re.fullmatch(r"\d{12}", a) for a in scope.accounts):
            raise ValueError("explicit account scope is required")
        if (not SOURCES <= set(scope.tables) or set(scope.tables) - SOURCES - {"opportunities"}
                or set(scope.tables) != set(scope.locations)):
            raise ValueError("all five registered source tables are required")
        if not 10_000_000 <= scope.max_scan_bytes <= 100_000_000 or not 1 <= scope.timeout_seconds <= 90 or not 1 <= scope.max_rows <= 1000:
            raise ValueError("invalid query limits")
        if not isinstance(scope.synthetic, bool):
            raise ValueError("synthetic must be a boolean")
        for uri in (*scope.locations.values(), scope.results_uri):
            if not re.fullmatch(r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/[A-Za-z0-9_=/.-]+/", uri) or "/../" in uri:
                raise ValueError("S3 locations must be explicit bucket prefixes")
        if any(scope.results_uri.startswith(uri) or uri.startswith(scope.results_uri) for uri in scope.locations.values()):
            raise ValueError("query results must be separate from source prefixes")
        if not all(re.fullmatch(r"[A-Za-z0-9_:/.-]{1,128}", key) for key in scope.tag_keys):
            raise ValueError("invalid configured tag key")
        return scope


class AthenaBackend:
    def __init__(self, scope: AthenaScope, athena, glue, *, clock=time.monotonic, sleep=time.sleep):
        self.scope, self.athena, self.glue = scope, athena, glue
        self.clock, self.sleep = clock, sleep

    def _check(self, source: str):
        config = self.athena.get_work_group(WorkGroup=self.scope.workgroup)["WorkGroup"]["Configuration"]
        result = config.get("ResultConfiguration", {})
        if (not config.get("EnforceWorkGroupConfiguration") or not config.get("BytesScannedCutoffPerQuery")
                or config["BytesScannedCutoffPerQuery"] > self.scope.max_scan_bytes
                or result.get("OutputLocation") != self.scope.results_uri
                or result.get("ExpectedBucketOwner") != self.scope.account_id
                or result.get("EncryptionConfiguration", {}).get("EncryptionOption") not in ("SSE_S3", "SSE_KMS")):
            raise PermissionError("Athena workgroup does not enforce the configured limits and destination")
        table = self.glue.get_table(CatalogId=self.scope.account_id, DatabaseName=self.scope.database,
                                    Name=self.scope.tables[source])["Table"]
        if table.get("TableType") != "EXTERNAL_TABLE" or table.get("StorageDescriptor", {}).get("Location") != self.scope.locations[source]:
            raise PermissionError("registered table location changed")
        if table.get("TargetTable") or table.get("ViewOriginalText") or table.get("Parameters", {}).get("projection.enabled") == "true":
            raise PermissionError("views, links and projected locations are not accepted")
        # Explicit partitions may otherwise redirect an allowed table to another bucket.
        token = None
        count = 0
        while True:
            request = dict(CatalogId=self.scope.account_id, DatabaseName=self.scope.database,
                           TableName=self.scope.tables[source], MaxResults=100)
            if token:
                request["NextToken"] = token
            response = self.glue.get_partitions(**request)
            for partition in response.get("Partitions", []):
                count += 1
                location = partition.get("StorageDescriptor", {}).get("Location", "")
                if not location.startswith(self.scope.locations[source]) or "/../" in location or count > 1000:
                    raise PermissionError("partition is outside the registered source scope")
            token = response.get("NextToken")
            if not token:
                break

    def _execute(self, source: str, sql: str, parameters: list[str]) -> tuple[list[dict], dict]:
        self._check(source)
        request = dict(QueryString=sql, ClientRequestToken=str(uuid.uuid4()), WorkGroup=self.scope.workgroup,
                       QueryExecutionContext={"Catalog": self.scope.catalog, "Database": self.scope.database},
                       ResultConfiguration={"OutputLocation": self.scope.results_uri, "ExpectedBucketOwner": self.scope.account_id,
                                            "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"}},
                       ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {"Enabled": False}})
        if parameters:
            request["ExecutionParameters"] = parameters
        query_id = self.athena.start_query_execution(**request)["QueryExecutionId"]
        started = self.clock()
        while True:
            execution = self.athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
            state = execution["Status"]["State"]
            scanned = execution.get("Statistics", {}).get("DataScannedInBytes", 0)
            if scanned > self.scope.max_scan_bytes or self.clock() - started > self.scope.timeout_seconds:
                self.athena.stop_query_execution(QueryExecutionId=query_id)
                raise TimeoutError("Athena query reached its scan or time limit")
            if state == "SUCCEEDED":
                break
            if state in ("FAILED", "CANCELLED"):
                raise RuntimeError("Athena query failed; execution " + query_id)
            self.sleep(0.25)
        rows, token, first = [], None, True
        while True:
            request = {"QueryExecutionId": query_id, "MaxResults": min(self.scope.max_rows + 2, 1000)}
            if token:
                request["NextToken"] = token
            page = self.athena.get_query_results(**request)
            columns = [c["Name"] for c in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
            records = page["ResultSet"]["Rows"]
            for row in records[1:] if first else records:
                rows.append({key: cell.get("VarCharValue") for key, cell in zip(columns, row["Data"])})
                if len(rows) > self.scope.max_rows:
                    raise ValueError("result exceeds row limit; narrow the query")
            first = False
            token = page.get("NextToken")
            if not token:
                break
        return rows, {"query_execution_id": query_id, "bytes_scanned": scanned,
                      "source": source, "location": self.scope.locations[source]}

    def _dimension(self, name):
        if name in _DIMENSIONS:
            return _DIMENSIONS[name]
        if name and name.startswith("tag:") and name[4:] in self.scope.tag_keys:
            return "element_at(try_cast(json_parse(resource_tags) AS map(varchar,varchar)), " + _literal(name[4:]) + ")"
        raise QueryError("dimension is not in the configured allow-list")

    def _where(self, q):
        clauses = ["line_item_usage_start_date >= ?", "line_item_usage_start_date < ?",
                   "line_item_usage_account_id IN (" + ",".join("?" for _ in self.scope.accounts) + ")"]
        parameters = [_literal(q.start.isoformat()), _literal(q.end.isoformat()), *map(_literal, self.scope.accounts)]
        for dimension, values in q.filters.items():
            if dimension == "account" and not set(values) <= set(self.scope.accounts):
                raise PermissionError("requested account is outside this workspace")
            expr = self._dimension(dimension)
            clauses.append("coalesce(nullif(" + expr + ", ''), '(untagged)') IN (" + ",".join("?" for _ in values) + ")")
            parameters.extend(map(_literal, values))
        return " AND ".join(clauses), parameters

    @staticmethod
    def _nanos(expression):
        # CUR has ten decimal places. Convert each row with half-even rounding,
        # before aggregation, matching the existing engine's nano-USD contract.
        x = f"(CAST(({expression}) AS DECIMAL(28,10)) * DECIMAL '1000000000')"
        return (f"(CAST(round({x}, 0) AS DECIMAL(38,0)) - CASE WHEN abs({x} - truncate({x})) = DECIMAL '0.5' "
                f"AND mod(abs(truncate({x})), 2) = 0 THEN CAST(sign({x}) AS DECIMAL(38,0)) ELSE DECIMAL '0' END)")

    def _aggregate(self, q):
        where, parameters = self._where(q)
        dimension = self._dimension(q.group_by) if q.group_by else "'Total'"
        bucket = ("substr(line_item_usage_start_date, 1, 10)" if q.granularity == "daily" else
                  "substr(line_item_usage_start_date, 1, 7)" if q.granularity == "monthly" else "'Total'")
        money = "line_item_unblended_cost"
        if q.metric == "amortized":
            money = ("CASE WHEN line_item_line_item_type IN ('Usage','Tax','Fee','Credit','Refund') THEN line_item_unblended_cost "
                     "WHEN line_item_line_item_type='SavingsPlanCoveredUsage' THEN savings_plan_savings_plan_effective_cost "
                     "WHEN line_item_line_item_type='SavingsPlanNegation' THEN DECIMAL '0' "
                     "WHEN line_item_line_item_type='SavingsPlanRecurringFee' AND savings_plan_total_commitment_to_date >= savings_plan_used_commitment "
                     "THEN savings_plan_total_commitment_to_date-savings_plan_used_commitment ELSE NULL END")
        sql = (f'SELECT {bucket} AS bucket, coalesce({dimension}, \'(untagged)\') AS group_key, '
               f'CAST(SUM({self._nanos(money)}) AS VARCHAR) AS nanos, '
               f'count_if(({money}) IS NULL OR line_item_currency_code IS NULL OR line_item_currency_code <> \'USD\') AS invalid_rows, '
               f"array_join(array_sort(array_agg(DISTINCT substr(line_item_usage_start_date,1,10))), ',') AS observed_days "
               f'FROM "{self.scope.database}"."{self.scope.tables["cur"]}" WHERE {where} '
               f'GROUP BY 1,2 ORDER BY 1,2 LIMIT {self.scope.max_rows + 1}')
        rows, evidence = self._execute("cur", sql, parameters)
        if any(int(r["invalid_rows"]) for r in rows):
            raise QueryError("currency or amortization evidence is unsupported")
        return [{"bucket": r["bucket"], "key": r["group_key"], "nanos": int(r["nanos"] or 0),
                 "days": (r.get("observed_days") or "").split(",") if r.get("observed_days") else []} for r in rows], evidence

    def query(self, raw: dict):
        q = CostQuery.from_dict(raw)
        try:
            rows, evidence = self._aggregate(q)
            total = sum(r["nanos"] for r in rows)
            if not rows:
                return {"status": "insufficient_data", "reason": "No billing evidence in this scope and window", "evidence": [evidence]}
            totals = {}
            for row in rows:
                totals[row["key"]] = totals.get(row["key"], 0) + row["nanos"]
            selected = set(sorted(totals, key=lambda key: (-totals[key], key))[:q.top_n])
            collapsed = {}
            for row in rows:
                key = row["key"] if row["key"] in selected else "Other"
                index = row["bucket"], key
                collapsed[index] = collapsed.get(index, 0) + row["nanos"]
            result = {"status": "ok", "kind": q.kind, "query": q.to_dict(), "synthetic_data": self.scope.synthetic,
                      "total_display": display(total), "total_nanos": str(total), "metric": q.metric, "currency": "USD",
                      "rows": [{"bucket": bucket, "group": key, "cost_display": display(nanos)}
                               for (bucket, key), nanos in sorted(collapsed.items())], "evidence": [evidence],
                      "evidence_query": q.to_dict(),
                      "warnings": ["Billing finality is unknown. Missing days are not assumed to be zero."]}
            result["summary"] = f"{q.metric.capitalize()} cost for {q.start} to {q.end} (exclusive): {display(total)}."
            if q.kind == "variance":
                previous = CostQuery.from_dict({**q.to_dict(), "kind": "explore", "start": q.compare_start.isoformat(),
                                               "end": q.compare_end.isoformat()})
                baseline, before_evidence = self._aggregate(previous)
                if not baseline:
                    return {"status": "insufficient_data", "reason": "No comparison-period evidence", "evidence": [evidence, before_evidence]}
                before = sum(r["nanos"] for r in baseline)
                result.update(baseline_display=display(before), change_display=display(total-before),
                              change_pct=pct_change(total, before))
                result["evidence"].append(before_evidence)
                if (q.end-q.start).days != (q.compare_end-q.compare_start).days:
                    result["warnings"].append("Comparison windows have different lengths; change is not normalized per day.")
            return result
        except (QueryError, ValueError) as exc:
            return {"status": "insufficient_data", "reason": str(exc), "synthetic_data": self.scope.synthetic}

    def coverage(self, start: str, end: str):
        q = CostQuery.from_dict({"start": start, "end": end, "group_by": None})
        where, parameters = self._where(q)
        sql = (f'SELECT substr(line_item_usage_start_date,1,7) AS period, min(substr(line_item_usage_start_date,1,10)) AS first_day, '
               f'max(substr(line_item_usage_start_date,1,10)) AS data_through, count(*) AS rows '
               f'FROM "{self.scope.database}"."{self.scope.tables["cur"]}" WHERE {where} GROUP BY 1 ORDER BY 1 LIMIT 15')
        rows, evidence = self._execute("cur", sql, parameters)
        return {"status": "ok" if rows else "insufficient_data", "periods": rows, "evidence": [evidence],
                "synthetic_data": self.scope.synthetic, "note": "Presence is not final billing or proof of complete coverage."}

    def recommendations(self, source: str, account: str = ""):
        if source not in SOURCES - {"cur"}:
            raise QueryError("unsupported recommendation source")
        if account and account not in self.scope.accounts:
            raise PermissionError("requested account is outside this workspace")
        column = '"accountId"' if source == "compute-optimizer" else "account_id"
        accounts = (account,) if account else self.scope.accounts
        sql = (f'SELECT * FROM "{self.scope.database}"."{self.scope.tables[source]}" '
               f'WHERE {column} IN ({",".join("?" for _ in accounts)}) LIMIT {self.scope.max_rows + 1}')
        rows, evidence = self._execute(source, sql, list(map(_literal, accounts)))
        for row in rows:
            for key in list(row):
                if key.startswith("estimated_monthly_") and row[key] is not None:
                    row[key + "_display"] = display(int((Decimal(row[key]) * 1_000_000_000).to_integral_value()))
        return {"status": "ok", "source": source, "recommendations": rows, "evidence": [evidence],
                "synthetic_data": self.scope.synthetic,
                "note": "Source proposals, not independently validated or additive savings. Cross-source recommendations overlap."}

    def opportunities(self, account: str = ""):
        if "opportunities" not in self.scope.tables:
            return {"status": "unavailable", "reason": "Common opportunity table is not configured"}
        if account and account not in self.scope.accounts:
            raise PermissionError("requested account is outside this workspace")
        accounts = (account,) if account else self.scope.accounts
        sql = (f'SELECT opportunity_id,account_id,resource_arn,action,target,source_claim_display,selected_source,'
               f'claim_sources_json,evidence_json,has_alternatives,validation_state,counted,review_status '
               f'FROM "{self.scope.database}"."{self.scope.tables["opportunities"]}" '
               f'WHERE workspace_id = ? AND state = ? AND account_id IN ({",".join("?" for _ in accounts)}) '
               f'ORDER BY opportunity_id LIMIT {self.scope.max_rows + 1}')
        rows,evidence = self._execute("opportunities",sql,[_literal(self.scope.workspace_id),_literal("active"),*map(_literal,accounts)])
        import json
        for row in rows:
            row["claim_sources"] = json.loads(row.pop("claim_sources_json"))
            row["evidence_links"] = json.loads(row.pop("evidence_json"))
        return {"status":"ok","opportunities":rows,"evidence":[evidence],"synthetic_data":self.scope.synthetic,
                "note":"Deduplicated source proposals. These claims are unvalidated, alternatives overlap, and no savings total is counted here."}
