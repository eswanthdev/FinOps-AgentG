"""Deterministic cost query engine over ingested CUR 2.0 line items.

Every number returned is computed here with integer nano-USD arithmetic.
Nothing in this module calls a model or the network.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..ingest.cur2 import period_bounds
from ..money import nanos_to_cents_str, pct_change, share_pct
from ..store import get_meta
from .model import CostQuery, dimension_label

OTHER = "Other"
UNTAGGED = "(untagged)"


# ----------------------------------------------------------------- helpers

def _fmt_usd(nanos: int) -> str:
    s = nanos_to_cents_str(abs(nanos))
    whole, frac = s.split(".")
    whole = f"{int(whole):,}"
    return f"{'-' if nanos < 0 else ''}${whole}.{frac}"


def _cents(nanos: int) -> int:
    return int(nanos_to_cents_str(nanos).replace(".", ""))


def _fmt_date(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


def _range_label(start: date, end_excl: date) -> str:
    last = end_excl - timedelta(days=1)
    return _fmt_date(start) if last == start else f"{_fmt_date(start)} – {_fmt_date(last)}"


def _periods(start: date, end_excl: date) -> list[str]:
    out, y, m = [], start.year, start.month
    last = end_excl - timedelta(days=1)
    while (y, m) <= (last.year, last.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _dim_expr(dim: str) -> tuple[str, list]:
    from .model import DIMENSIONS
    if dim in DIMENSIONS:
        return DIMENSIONS[dim], []
    key = dim[4:]
    return "json_extract(resource_tags, ?)", ['$."' + key + '"']


def _where(q: CostQuery, start: date, end: date) -> tuple[str, list]:
    clauses = ["usage_date >= ?", "usage_date < ?"]
    params: list = [start.isoformat(), end.isoformat()]
    for dim, values in sorted(q.filters.items()):
        expr, p = _dim_expr(dim)
        concrete = [v for v in values if v != UNTAGGED]
        parts = []
        if concrete:
            parts.append(f"{expr} IN ({','.join('?' * len(concrete))})")
            params += p + concrete
        if dim.startswith("tag:") and UNTAGGED in values:
            parts.append(f"{expr} IS NULL")
            params += p
        clauses.append("(" + " OR ".join(parts) + ")")
    return " AND ".join(clauses), params


def reference_date(conn) -> date:
    v = get_meta(conn, "reference_date")
    if v:
        return date.fromisoformat(v)
    return date.today()


def account_names(conn) -> dict[str, str]:
    return {r["account_id"]: r["account_name"] for r in conn.execute("SELECT account_id, account_name FROM accounts")}


def label_for(dim: str | None, value: str | None, names: dict[str, str]) -> str:
    if value is None:
        return UNTAGGED if dim and dim.startswith("tag:") else "(none)"
    if dim == "account":
        return f"{names[value]} ({value})" if value in names else value
    return value


# ----------------------------------------------------------------- coverage

def coverage(conn, start: date, end: date) -> dict:
    ref = reference_date(conn)
    expected_start = get_meta(conn, "expected_start_period")
    periods = []
    notes = []
    has_data = False
    data_through: date | None = None
    for p in _periods(start, end):
        active = conn.execute(
            "SELECT execution_id, export_timestamp, period_complete, data_through, ingested_at "
            "FROM deliveries WHERE billing_period=? AND status='active'", (p,)).fetchone()
        revisions = conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE billing_period=? AND status='superseded'", (p,)).fetchone()["n"]
        rejected = conn.execute(
            "SELECT status, detail, export_timestamp FROM deliveries WHERE billing_period=? AND status LIKE 'rejected_%' "
            "ORDER BY id DESC LIMIT 1", (p,)).fetchone()
        entry = {"period": p, "revisions_applied": revisions}
        if active:
            has_data = True
            dt = date.fromisoformat(active["data_through"]) if active["data_through"] else None
            if dt and (data_through is None or dt > data_through):
                data_through = dt
            entry.update(status="final" if active["period_complete"] else "in_progress",
                         execution_id=active["execution_id"], export_timestamp=active["export_timestamp"],
                         data_through=active["data_through"], ingested_at=active["ingested_at"])
            if rejected and rejected["export_timestamp"] > active["export_timestamp"]:
                entry["newer_delivery_rejected"] = f"{rejected['status']}: {rejected['detail']}"
                notes.append(f"{p}: a newer export was rejected ({rejected['detail']}); showing the previous delivery.")
            if not active["period_complete"]:
                notes.append(f"{p}: billing data is not final yet and may still be revised by AWS.")
        else:
            ps, _ = period_bounds(p)
            if ps > ref:
                entry["status"] = "future"
            elif expected_start and p < expected_start:
                entry["status"] = "before_coverage"
            else:
                entry["status"] = "missing"
                notes.append(f"{p}: no CUR delivery received. Costs for this month are not included.")
        periods.append(entry)
    return {"periods": periods, "has_data": has_data,
            "data_through": data_through.isoformat() if data_through else None, "notes": notes}


def _effective_days(cov: dict, start: date, end: date) -> list[date]:
    """Days inside [start,end) that belong to periods with data, up to data_through."""
    with_data = {p["period"]: p for p in cov["periods"] if p.get("status") in ("final", "in_progress")}
    out = []
    d = start
    while d < end:
        p = with_data.get(f"{d.year:04d}-{d.month:02d}")
        if p and p.get("data_through") and d <= date.fromisoformat(p["data_through"]):
            out.append(d)
        d += timedelta(days=1)
    return out


def _unsupported_amortized(conn, where: str, params: list) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM line_items WHERE {where} AND amortized_nanos IS NULL",
                        params).fetchone()["n"]


def _sources(cov: dict) -> list[dict]:
    return [{"billing_period": p["period"], "execution_id": p["execution_id"], "export_timestamp": p["export_timestamp"],
             "status": p["status"], "data_through": p["data_through"]}
            for p in cov["periods"] if p.get("execution_id")]


def _insufficient(reason: str, cov: dict, q: CostQuery, **extra) -> dict:
    return {"status": "insufficient_data", "reason": reason, "coverage": cov, "query": q.to_dict(), **extra}


# ----------------------------------------------------------------- explore

def _window_total(conn, q: CostQuery, start: date, end: date) -> int:
    where, params = _where(q, start, end)
    col = "unblended_nanos" if q.metric == "unblended" else "amortized_nanos"
    return conn.execute(f"SELECT COALESCE(SUM({col}),0) AS t FROM line_items WHERE {where}", params).fetchone()["t"]


def run_explore(conn, q: CostQuery) -> dict:
    q.validate()
    cov = coverage(conn, q.start, q.end)
    metric_name = "Unblended cost" if q.metric == "unblended" else "Amortized cost"
    if not cov["has_data"]:
        return _insufficient("No billing data has been ingested for this date range.", cov, q)
    days = _effective_days(cov, q.start, q.end)
    if not days:
        return _insufficient("The date range has no days with ingested billing data.", cov, q)
    eff_start, eff_end = days[0], days[-1] + timedelta(days=1)

    where, params = _where(q, q.start, q.end)
    if q.metric == "amortized":
        n = _unsupported_amortized(conn, where, params)
        if n:
            return _insufficient(
                f"{n} line items in this range use charge types that this release cannot amortize "
                "(for example Reserved Instance fees). Use unblended cost, or wait for RI support.", cov, q)
    col = "unblended_nanos" if q.metric == "unblended" else "amortized_nanos"
    names = account_names(conn)

    if q.group_by:
        gexpr, gparams = _dim_expr(q.group_by)
    else:
        gexpr, gparams = "'Total'", []

    totals = [(r["g"], r["t"]) for r in conn.execute(
        f"SELECT {gexpr} AS g, SUM({col}) AS t FROM line_items WHERE {where} GROUP BY g ORDER BY t DESC, g",
        gparams + params)]
    grand = sum(t for _, t in totals)
    row_count = conn.execute(f"SELECT COUNT(*) AS n FROM line_items WHERE {where}", params).fetchone()["n"]

    keep = [g for g, _ in totals[: q.top_n]] if q.group_by else [g for g, _ in totals]
    keep_set = set(keep)
    has_other = q.group_by is not None and len(totals) > q.top_n

    if q.granularity == "daily":
        bexpr = "usage_date"
        buckets = [d.isoformat() for d in days]
    elif q.granularity == "monthly":
        bexpr = "substr(usage_date,1,7)"
        buckets = sorted({d.isoformat()[:7] for d in days})
    else:
        bexpr = "'total'"
        buckets = ["total"]

    cells: dict[tuple[str, str], int] = {}
    for r in conn.execute(
            f"SELECT {bexpr} AS b, {gexpr} AS g, SUM({col}) AS t FROM line_items WHERE {where} GROUP BY b, g",
            gparams + params):
        g = r["g"] if (not q.group_by or r["g"] in keep_set) else OTHER
        cells[(r["b"], g)] = cells.get((r["b"], g), 0) + r["t"]

    series_keys = keep + ([OTHER] if has_other else [])
    series = []
    for g in series_keys:
        vals = [cells.get((b, g), 0) for b in buckets]
        total = sum(vals)
        series.append({
            "key": g,
            "name": OTHER if g == OTHER else label_for(q.group_by, g, names),
            "values_cents": [_cents(v) for v in vals],
            "total": nanos_to_cents_str(total),
            "total_display": _fmt_usd(total),
            "share_pct": share_pct(total, grand),
            "other_count": len(totals) - q.top_n if g == OTHER else None,
        })
    bucket_totals = [sum(cells.get((b, g), 0) for g in series_keys) for b in buckets]
    assert sum(bucket_totals) == grand, "bucketed totals must equal grand total"

    # Comparison with the immediately preceding window of the same length.
    length = (eff_end - eff_start).days
    prev_start, prev_end = eff_start - timedelta(days=length), eff_start
    prev_cov = coverage(conn, prev_start, prev_end)
    prev_days = _effective_days(prev_cov, prev_start, prev_end)
    comparison = None
    prev_total = None
    if len(prev_days) == length:
        prev_total = _window_total(conn, q, prev_start, prev_end)
        comparison = {
            "label": _range_label(prev_start, prev_end), "start": prev_start.isoformat(), "end": prev_end.isoformat(),
            "total": nanos_to_cents_str(prev_total), "total_display": _fmt_usd(prev_total),
            "change_pct": pct_change(grand, prev_total), "change_display": _fmt_usd(grand - prev_total),
        }
    comparison_note = None if comparison else (
        f"No comparison: the previous {length} days ({_range_label(prev_start, prev_end)}) are not fully covered by ingested data.")

    chart = q.chart
    if chart == "auto":
        if q.granularity == "total":
            chart = "hbar" if q.group_by else "table"
        elif q.group_by:
            chart = "stacked"
        else:
            chart = "bar" if q.granularity == "monthly" else "line"

    warnings = list(cov["notes"])
    if eff_end < q.end:
        warnings.append(f"Data is available only up to {_fmt_date(eff_end - timedelta(days=1))}; later days are not shown.")
    if eff_start > q.start:
        warnings.append(f"No ingested data before {_fmt_date(eff_start)} in this range.")
    if q.metric == "unblended" and conn.execute(
            f"SELECT 1 FROM line_items WHERE {where} AND line_item_type LIKE 'SavingsPlan%' LIMIT 1", params).fetchone():
        warnings.append("Savings Plan charges are shown as billed (unblended). Choose amortized cost to spread "
                        "commitment cost over the usage it covered.")
    if conn.execute(f"SELECT 1 FROM line_items WHERE {where} AND line_item_type IN ('Credit','Refund') LIMIT 1",
                    params).fetchone():
        warnings.append("Totals include credits or refunds (negative values).")

    top = series[0] if series and q.group_by else None
    summary = f"{metric_name} for {_range_label(eff_start, eff_end)} was {_fmt_usd(grand)}"
    if comparison and comparison["change_pct"] is not None:
        direction = "up" if grand >= prev_total else "down"
        summary += f", {direction} {comparison['change_pct'].lstrip('-')}% vs the previous {length} days"
    summary += "."
    if top and top["key"] != OTHER and len(series) > 1:
        summary += f" The largest {dimension_label(q.group_by).lower()} is {top['name']} at {top['total_display']} ({top['share_pct']}%)."

    sql_template = (f"SELECT {bexpr} AS bucket, {gexpr} AS grp, SUM({col}) FROM line_items "
                    f"WHERE {where} GROUP BY bucket, grp")
    return {
        "status": "ok",
        "kind": "explore",
        "query": q.to_dict(),
        "title": _title(q, names),
        "summary": summary,
        "metric_label": metric_name,
        "chart": chart,
        "buckets": buckets,
        "series": series,
        "bucket_totals_cents": [_cents(v) for v in bucket_totals],
        "total": nanos_to_cents_str(grand),
        "total_display": _fmt_usd(grand),
        "days_with_data": len(days),
        "daily_average_display": _fmt_usd(grand // len(days)),
        "effective_start": eff_start.isoformat(),
        "effective_end": eff_end.isoformat(),
        "range_label": _range_label(eff_start, eff_end),
        "comparison": comparison,
        "comparison_note": comparison_note,
        "group_label": dimension_label(q.group_by) if q.group_by else None,
        "warnings": warnings,
        "coverage": cov,
        "calculation": {
            "formula": f"Sum of {col.replace('_nanos', '')} over CUR line items in range, grouped by "
                       f"{'date' if q.granularity == 'daily' else q.granularity} and "
                       f"{dimension_label(q.group_by).lower() if q.group_by else 'nothing'}. "
                       f"Groups beyond the top {q.top_n} are summed into 'Other'. Amounts are exact integer "
                       "nano-USD sums, rounded half-even to cents only for display.",
            "sql_template": sql_template,
            "parameters": [str(p) for p in gparams + params],
            "line_items_used": row_count,
            "groups_found": len(totals),
            "sources": _sources(cov),
        },
    }


def _title(q: CostQuery, names: dict[str, str]) -> str:
    metric = "Amortized cost" if q.metric == "amortized" else "Cost"
    parts = [metric]
    if q.kind == "variance":
        parts = [f"{metric} change"]
    if q.group_by:
        parts.append(f"by {dimension_label(q.group_by).lower()}")
    flt = []
    for dim, vals in sorted(q.filters.items()):
        shown = [label_for(dim, v, names) if v != UNTAGGED else v for v in vals[:3]]
        flt.append(", ".join(shown) + (" +" if len(vals) > 3 else ""))
    if flt:
        parts.append("· " + " · ".join(flt))
    return " ".join(parts)


# ----------------------------------------------------------------- variance

def run_variance(conn, q: CostQuery) -> dict:
    q.validate()
    base_cov = coverage(conn, q.compare_start, q.compare_end)
    cur_cov = coverage(conn, q.start, q.end)
    if not base_cov["has_data"] or not cur_cov["has_data"]:
        which = "baseline" if not base_cov["has_data"] else "current"
        return _insufficient(f"The {which} window has no ingested billing data, so no comparison is possible.",
                             cur_cov, q, baseline_coverage=base_cov)
    base_days = _effective_days(base_cov, q.compare_start, q.compare_end)
    cur_days = _effective_days(cur_cov, q.start, q.end)
    if not base_days or not cur_days:
        return _insufficient("One of the windows has no days with data.", cur_cov, q, baseline_coverage=base_cov)
    b_start, b_end = base_days[0], base_days[-1] + timedelta(days=1)
    c_start, c_end = cur_days[0], cur_days[-1] + timedelta(days=1)

    col = "unblended_nanos" if q.metric == "unblended" else "amortized_nanos"
    bw, bp = _where(q, b_start, b_end)
    cw, cp = _where(q, c_start, c_end)
    if q.metric == "amortized" and (_unsupported_amortized(conn, bw, bp) or _unsupported_amortized(conn, cw, cp)):
        return _insufficient("Some line items cannot be amortized in this release. Use unblended cost.",
                             cur_cov, q, baseline_coverage=base_cov)
    names = account_names(conn)
    gexpr, gparams = _dim_expr(q.group_by)

    def _sum_by(expr: str, eparams: list, where: str, params: list) -> dict:
        return {r["g"]: r["t"] for r in conn.execute(
            f"SELECT {expr} AS g, SUM({col}) AS t FROM line_items WHERE {where} GROUP BY g", eparams + params)}

    base = _sum_by(gexpr, gparams, bw, bp)
    cur = _sum_by(gexpr, gparams, cw, cp)
    keys = set(base) | set(cur)
    rows = []
    for k in keys:
        b, c = base.get(k, 0), cur.get(k, 0)
        rows.append({"key": k, "name": label_for(q.group_by, k, names), "base": b, "current": c, "delta": c - b})
    rows.sort(key=lambda r: (-abs(r["delta"]), str(r["name"])))
    shown, rest = rows[: q.top_n], rows[q.top_n:]
    if rest:
        shown.append({"key": OTHER, "name": OTHER, "base": sum(r["base"] for r in rest),
                      "current": sum(r["current"] for r in rest), "delta": sum(r["delta"] for r in rest),
                      "other_count": len(rest)})
    base_total, cur_total = sum(base.values()), sum(cur.values())
    assert sum(r["delta"] for r in shown) == cur_total - base_total

    nb, nc = len(base_days), len(cur_days)
    # Driver analysis at resource level, for evidence.
    dexpr = "account_id || '|' || service || '|' || usage_type || '|' || resource_id"
    dbase = _sum_by(dexpr, [], bw, bp)
    dcur = _sum_by(dexpr, [], cw, cp)
    first_seen = {r["g"]: r["f"] for r in conn.execute(
        f"SELECT {dexpr} AS g, MIN(usage_date) AS f FROM line_items WHERE {cw} GROUP BY g", cp)}
    drivers = []
    for k in set(dbase) | set(dcur):
        b, c = dbase.get(k, 0), dcur.get(k, 0)
        if b == c:
            continue
        acct, svc, ut, res = k.split("|", 3)
        drivers.append({
            "account": label_for("account", acct, names), "service": svc, "usage_type": ut,
            "resource_id": res or "(no resource id)", "base": b, "current": c, "delta": c - b,
            "new_in_window": b == 0 and c != 0, "stopped_in_window": c == 0 and b != 0,
            "first_seen": first_seen.get(k) if b == 0 else None,
        })
    drivers.sort(key=lambda r: (-r["delta"], r["resource_id"]))
    increases = [d for d in drivers if d["delta"] > 0][:10]
    decreases = sorted([d for d in drivers if d["delta"] < 0], key=lambda r: r["delta"])[:5]

    # Roll resource drivers up to account + service + usage type, so a fleet of
    # identical new instances reads as one driver.
    groups: dict[tuple, dict] = {}
    for d in drivers:
        gk = (d["account"], d["service"], d["usage_type"])
        g = groups.setdefault(gk, {"account": d["account"], "service": d["service"], "usage_type": d["usage_type"],
                                   "base": 0, "current": 0, "delta": 0, "resources": 0, "new_resources": 0,
                                   "first_seen": None})
        g["base"] += d["base"]; g["current"] += d["current"]; g["delta"] += d["delta"]
        g["resources"] += 1
        if d["new_in_window"]:
            g["new_resources"] += 1
            if d["first_seen"] and (g["first_seen"] is None or d["first_seen"] < g["first_seen"]):
                g["first_seen"] = d["first_seen"]
    driver_groups = sorted(groups.values(), key=lambda g: (-g["delta"], g["usage_type"]))
    top_groups = [g for g in driver_groups if g["delta"] > 0][:5]

    def _fmt_row(r):
        out = dict(r)
        for f in ("base", "current", "delta"):
            out[f + "_display"] = _fmt_usd(r[f])
            out[f + "_cents"] = _cents(r[f])
        out["change_pct"] = pct_change(r["current"], r["base"])
        for f in ("base", "current", "delta"):
            out[f] = nanos_to_cents_str(r[f])
        return out

    per_day_base = base_total // nb
    per_day_cur = cur_total // nc
    delta = cur_total - base_total
    metric_name = "Unblended cost" if q.metric == "unblended" else "Amortized cost"
    summary = (f"{metric_name} went from {_fmt_usd(base_total)} ({_range_label(b_start, b_end)}, {nb} days) to "
               f"{_fmt_usd(cur_total)} ({_range_label(c_start, c_end)}, {nc} days): "
               f"{'+' if delta >= 0 else ''}{_fmt_usd(delta)}")
    pc = pct_change(cur_total, base_total)
    if pc is not None:
        summary += f" ({'+' if delta >= 0 else ''}{pc}%)"
    summary += f". Per day: {_fmt_usd(per_day_base)} → {_fmt_usd(per_day_cur)}."
    top_inc = next((r for r in shown if r["delta"] > 0 and r["key"] != OTHER), None)
    if top_inc:
        summary += f" Largest increase: {top_inc['name']} ({'+'}{_fmt_usd(top_inc['delta'])})."
    if top_groups:
        bits = []
        for g in top_groups[:2]:
            b = f"{g['usage_type']} in {g['account']} (+{_fmt_usd(g['delta'])}"
            if g["new_resources"]:
                b += f", {g['new_resources']} new resource{'s' if g['new_resources'] > 1 else ''} from {g['first_seen']}"
            bits.append(b + ")")
        summary += " Top drivers: " + "; ".join(bits) + "."

    warnings = list(dict.fromkeys(base_cov["notes"] + cur_cov["notes"]))
    if nb != nc:
        warnings.append(f"The windows have different lengths ({nb} vs {nc} days). Compare the per-day figures "
                        "for a like-for-like view.")
    return {
        "status": "ok",
        "kind": "variance",
        "query": q.to_dict(),
        "title": _title(q, names),
        "summary": summary,
        "metric_label": metric_name,
        "chart": "diverging",
        "group_label": dimension_label(q.group_by),
        "baseline": {"label": _range_label(b_start, b_end), "days": nb, "total_display": _fmt_usd(base_total),
                     "total": nanos_to_cents_str(base_total), "per_day_display": _fmt_usd(per_day_base)},
        "current": {"label": _range_label(c_start, c_end), "days": nc, "total_display": _fmt_usd(cur_total),
                    "total": nanos_to_cents_str(cur_total), "per_day_display": _fmt_usd(per_day_cur)},
        "delta_display": ('+' if delta >= 0 else '') + _fmt_usd(delta),
        "delta_pct": pc,
        "rows": [_fmt_row(r) for r in shown],
        "drivers": {"groups": [_fmt_row(g) for g in top_groups],
                    "increases": [_fmt_row(d) for d in increases],
                    "decreases": [_fmt_row(d) for d in decreases]},
        "warnings": warnings,
        "coverage": cur_cov,
        "baseline_coverage": base_cov,
        "calculation": {
            "formula": (f"For each {dimension_label(q.group_by).lower()}: change = sum({col.replace('_nanos','')}) in "
                        "current window − sum in baseline window. Drivers are the same calculation per "
                        "account + service + usage type + resource. Exact integer nano-USD arithmetic."),
            "sql_template": f"SELECT {gexpr} AS grp, SUM({col}) FROM line_items WHERE <window> GROUP BY grp",
            "parameters": [str(p) for p in gparams],
            "baseline_window": [b_start.isoformat(), b_end.isoformat()],
            "current_window": [c_start.isoformat(), c_end.isoformat()],
            "sources": _sources(base_cov) + _sources(cur_cov),
        },
    }


def run(conn, q: CostQuery) -> dict:
    return run_variance(conn, q) if q.kind == "variance" else run_explore(conn, q)


def dimension_values(conn, limit: int = 50) -> dict:
    names = account_names(conn)
    out = {}
    for dim, col in (("service", "service"), ("account", "account_id"), ("region", "region"),
                     ("usage_type", "usage_type"), ("line_item_type", "line_item_type")):
        out[dim] = [{"value": r["v"], "label": label_for(dim, r["v"], names)} for r in conn.execute(
            f"SELECT {col} AS v, SUM(unblended_nanos) AS t FROM line_items GROUP BY v ORDER BY t DESC LIMIT ?", (limit,))]
    tags = {}
    for r in conn.execute("SELECT DISTINCT j.key AS k FROM line_items, json_each(line_items.resource_tags) AS j ORDER BY k"):
        k = r["k"]
        tags[k] = [x["v"] for x in conn.execute(
            "SELECT DISTINCT json_extract(resource_tags, ?) AS v FROM line_items WHERE v IS NOT NULL ORDER BY v LIMIT ?",
            ('$."' + k + '"', limit))]
    out["tags"] = tags
    return out
