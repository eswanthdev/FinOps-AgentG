"""Translate scoped Athena aggregates into the existing dashboard contract."""
from __future__ import annotations

from datetime import date, timedelta

from ..money import nanos_to_cents_str, pct_change, share_pct
from .athena import _literal, display
from .model import CostQuery, QueryError, dimension_label


def cents(value):
    result = int(nanos_to_cents_str(value).replace(".", ""))
    if abs(result) > 9_007_199_254_740_991:
        raise QueryError("chart values exceed the exact browser integer range")
    return result


def account_names(backend):
    from ..fixtures import ACCOUNTS
    return {account: ACCOUNTS.get(account, account) if backend.scope.synthetic else account for account in backend.scope.accounts}


def coverage_from_days(q, days, evidence):
    periods = []
    cursor = q.start.replace(day=1)
    while cursor < q.end:
        period = cursor.isoformat()[:7]
        dates = [day for day in days if day.startswith(period)]
        periods.append({"period": period, "status": "observed" if dates else "missing", "revisions_applied": None,
                        "execution_id": evidence["query_execution_id"] if dates else None, "export_timestamp": None,
                        "data_through": max(dates) if dates else None})
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
    return {"periods": periods, "has_data": bool(days), "data_through": max(days) if days else None,
            "notes": ["Live Athena query over synthetic S3 data." if evidence.get("synthetic", False) else "Live Athena query.",
                      "Billing finality is unknown. Unobserved days are not filled with zero."]}


def dashboard(backend, raw):
    q = CostQuery.from_dict(raw)
    names = account_names(backend)
    label = lambda key: names.get(key, key) if q.group_by == "account" else key
    rows, evidence = backend._aggregate(q)
    evidence["synthetic"] = backend.scope.synthetic
    days = sorted({day for row in rows for day in row["days"]})
    cov = coverage_from_days(q, days, evidence)
    if not rows or not days:
        return {"status": "insufficient_data", "query": q.to_dict(), "reason": "No observed billing data in this scope and range.", "coverage": cov}
    metric = "Amortized cost" if q.metric == "amortized" else "Unblended cost"
    group = dimension_label(q.group_by) if q.group_by else None
    title = metric + (" by " + group.lower() if group else "")
    total = sum(row["nanos"] for row in rows)
    calculation = {"formula": "Athena sums exact row-level half-even nano-USD values. Groups outside top N are combined into Other.",
        "sql_template": "Typed, scoped Athena SELECT (no user SQL).", "parameters": [], "groups_found": len({r["key"] for r in rows}),
        "sources": [{"billing_period": "selected range", "execution_id": evidence["query_execution_id"],
                     "export_timestamp": None, "status": "observed", "data_through": max(days)}]}
    result = {"status": "ok", "query": q.to_dict(), "title": title, "metric_label": metric, "group_label": group,
              "coverage": cov, "warnings": list(cov["notes"]), "calculation": calculation, "backend": "cloud",
              "athena_evidence": [evidence], "synthetic_data": backend.scope.synthetic}
    if q.kind == "variance":
        before_q = CostQuery.from_dict({**q.to_dict(), "kind": "explore", "start": q.compare_start.isoformat(), "end": q.compare_end.isoformat()})
        before, before_evidence = backend._aggregate(before_q)
        before_evidence["synthetic"] = backend.scope.synthetic
        before_days = sorted({day for row in before for day in row["days"]})
        if not before_days:
            return {"status": "insufficient_data", "query": q.to_dict(), "coverage": cov, "reason": "No comparison-period billing evidence."}
        bases, currents = {}, {}
        for row in before:
            bases[row["key"]] = bases.get(row["key"], 0) + row["nanos"]
        for row in rows:
            currents[row["key"]] = currents.get(row["key"], 0) + row["nanos"]
        keys = sorted(set(bases) | set(currents), key=lambda key: (-abs(currents.get(key, 0)-bases.get(key, 0)), key))
        keep = keys[:q.top_n]
        combined = [(key, bases.get(key, 0), currents.get(key, 0)) for key in keep]
        if len(keys) > q.top_n:
            combined.append(("Other", sum(bases.get(key, 0) for key in keys[q.top_n:]), sum(currents.get(key, 0) for key in keys[q.top_n:])))
        shown = []
        for key, base, current in combined:
            shown.append({"key": key, "name": label(key), "base": nanos_to_cents_str(base), "current": nanos_to_cents_str(current),
                          "base_display": display(base), "current_display": display(current), "delta_display": display(current-base),
                          "base_cents": cents(base), "current_cents": cents(current), "delta_cents": cents(current-base),
                          "change_pct": pct_change(current,base), "other_count": len(keys)-q.top_n if key == "Other" else None})
        base = sum(r["nanos"] for r in before)
        period = lambda dates, value: {"label": dates[0]+" to "+dates[-1], "days": len(dates), "total": nanos_to_cents_str(value),
                                       "total_display": display(value), "per_day_display": display(value//len(dates))}
        result.update(kind="variance", chart="diverging", baseline=period(before_days,base), current=period(days,total),
                      delta_display=("+" if total>=base else "")+display(total-base), delta_pct=pct_change(total,base), rows=shown,
                      drivers={"groups":[],"increases":[],"decreases":[]}, baseline_coverage=coverage_from_days(before_q,before_days,before_evidence),
                      summary=f"{metric} changed from {display(base)} to {display(total)}.")
        result["warnings"].append("Resource-level variance drivers are not queried by this chart. Compare per-day values for unequal windows.")
        result["athena_evidence"].append(before_evidence)
        calculation.update(baseline_window=[before_q.start.isoformat(),before_q.end.isoformat()],current_window=[q.start.isoformat(),q.end.isoformat()])
        return result
    totals = {}
    for row in rows:
        totals[row["key"]] = totals.get(row["key"], 0)+row["nanos"]
    keep = sorted(totals,key=lambda key:(-totals[key],key))[:q.top_n]
    extra = len(totals)>q.top_n
    cells = {}
    for row in rows:
        key = row["key"] if row["key"] in keep else "Other"
        bucket = "total" if row["bucket"] == "Total" else row["bucket"]
        cells[(bucket,key)] = cells.get((bucket,key),0)+row["nanos"]
    buckets = sorted({key[0] for key in cells})
    series = []
    for key in keep+(["Other"] if extra else []):
        values = [cells.get((bucket,key),0) for bucket in buckets]
        value = sum(values)
        series.append({"key":key,"name":label(key),"values_cents":[cents(v) for v in values],"total":nanos_to_cents_str(value),
                       "total_display":display(value),"share_pct":share_pct(value,total),"other_count":len(totals)-q.top_n if key=="Other" else None})
    chart = q.chart if q.chart != "auto" else ("hbar" if q.granularity=="total" and q.group_by else "table" if q.granularity=="total" else "stacked" if q.group_by else "line")
    result.update(kind="explore",chart=chart,buckets=buckets,series=series,
                  bucket_totals_cents=[cents(sum(cells.get((bucket,key),0) for key in keep+(["Other"] if extra else []))) for bucket in buckets],
                  total=nanos_to_cents_str(total),total_display=display(total),days_with_data=len(days),daily_average_display=display(total//len(days)),
                  effective_start=days[0],effective_end=(date.fromisoformat(days[-1])+timedelta(days=1)).isoformat(),
                  range_label=days[0]+" to "+days[-1],comparison=None,comparison_note="Previous-period comparison was not requested.",
                  summary=f"{metric} for {days[0]} to {days[-1]}: {display(total)}.")
    return result


def dimensions(backend):
    selections = []
    params = []
    for dimension in ("service","account","region","usage_type","line_item_type",*("tag:"+key for key in backend.scope.tag_keys)):
        expr = backend._dimension(dimension)
        selections.append(f"SELECT DISTINCT {_literal(dimension)} AS dimension, {expr} AS value FROM \"{backend.scope.database}\".\"{backend.scope.tables['cur']}\" WHERE line_item_usage_account_id IN ({','.join('?' for _ in backend.scope.accounts)})")
        params.extend(map(_literal,backend.scope.accounts))
    sql = "SELECT dimension,value FROM ("+" UNION ALL ".join(selections)+f") WHERE value IS NOT NULL AND value <> '' ORDER BY 1,2 LIMIT {backend.scope.max_rows+1}"
    rows,_ = backend._execute("cur",sql,params)
    result = {key:[] for key in ("service","account","region","usage_type","line_item_type")}
    result["tags"] = {}
    names = account_names(backend)
    for row in rows:
        if row["dimension"].startswith("tag:"):
            result["tags"].setdefault(row["dimension"][4:],[]).append(row["value"])
        else:
            result[row["dimension"]].append({"value":row["value"],"label":names.get(row["value"],row["value"]) if row["dimension"]=="account" else row["value"]})
    return result


def status(backend):
    ref = date.fromisoformat(backend.scope.reference_date)
    start = (ref-timedelta(days=120)).replace(day=1)
    raw = backend.coverage(start.isoformat(),ref.isoformat())
    days = sorted({day for p in raw["periods"] for day in (p["first_day"],p["data_through"])})
    evidence = {**raw["evidence"][0],"synthetic":backend.scope.synthetic}
    cov = coverage_from_days(CostQuery(start=start,end=ref),days,evidence)
    by_period = {p["period"]:p for p in raw["periods"]}
    deliveries = []
    for period in cov["periods"]:
        if period["period"] in by_period:
            deliveries.append({"billing_period":period["period"],"execution_id":evidence["query_execution_id"],"export_timestamp":None,
                               "status":"observed","row_count":int(by_period[period["period"]]["rows"]),"detail":"Queried live from S3 through scoped Athena; export finality is unknown."})
    names = account_names(backend)
    mapping = {"cur":"cur2","cost-optimization-hub":"coh","compute-optimizer":"compute_optimizer","trusted-advisor":"trusted_advisor","savings-plans":"savings_plans","opportunities":"opportunities"}
    sources = [{"key":mapping[source],"name":source.replace("-"," ").title(),"purpose":"Scoped S3 evidence","state":"connected",
                "detail":"Registered Athena source: "+backend.scope.tables[source]} for source in backend.scope.tables]
    sources.append({"key":"storage_lens","name":"Storage metrics","purpose":"MCP storage evidence","state":"synthetic_fixture" if backend.scope.synthetic else "not_connected","detail":"Synthetic fixture through MCP" if backend.scope.synthetic else "Telemetry connector not configured"})
    return {"backend":"cloud","data_mode":"synthetic-cloud" if backend.scope.synthetic else "cloud","synthetic":backend.scope.synthetic,
            "reference_date":ref.isoformat(),"data_through":cov["data_through"],"lag_days":(ref-date.fromisoformat(cov["data_through"])).days if days else None,
            "freshness":"snapshot","accounts":[{"id":key,"name":value} for key,value in names.items()],"coverage":cov,"sources":sources,
            "deliveries":deliveries,"delivery_issues":[],"permissions":{"aws_access":"scoped_read","detail":"Charts query S3 through scoped Athena and authenticated MCP. Chat runs in AgentCore. The app cannot write source buckets."}}
