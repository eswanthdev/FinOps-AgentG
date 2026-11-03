"""Deterministic cost anomaly detection.

Method (documented so results are reproducible):
  * Series: daily unblended cost per (account, service), from ingested CUR only.
  * Baseline for day D: the 14 days before D that have data. Expected = median.
    Spread = 1.4826 x median absolute deviation (a robust standard deviation).
  * Day D is anomalous when ALL hold:
        actual - expected >= max(3 x spread, 20% of expected, $10)
    i.e. clearly outside normal noise, material in relative terms and in dollars.
  * Consecutive anomalous days form one anomaly. Impact = sum(actual - expected)
    over its days. "Sustained" if it lasts 3+ days, else "spike".
  * Only increases are flagged. Days in billing periods that are missing are
    never used as baseline (no gap filling).
  * Drivers: usage type + resource deltas on the first anomalous day versus the
    baseline median day, so the user can see what changed.
Minimum history: 14 days of baseline. Earlier days are not evaluated.
"""
from __future__ import annotations

import hashlib
import statistics
from datetime import date, timedelta

from .money import NANOS_PER_USD, nanos_to_cents_str
from .query.engine import _fmt_usd, account_names, label_for, reference_date
from .store import utc_now

BASELINE_DAYS = 14
MIN_ABS_NANOS = 10 * NANOS_PER_USD
MIN_REL = 0.20
SIGMA = 3.0
STATUSES = ("open", "acknowledged", "dismissed")


def _c(nanos: int) -> int:
    return int(nanos_to_cents_str(nanos).replace(".", ""))


def _context(all_days: list[str], vals: dict[str, int], ev: list[dict]) -> dict:
    """Baseline days + anomaly days + up to 3 days after, for charting actual vs expected."""
    i0 = all_days.index(ev[0]["day"]) - BASELINE_DAYS
    i1 = min(len(all_days), all_days.index(ev[-1]["day"]) + 4)
    days = all_days[i0:i1]
    exp = {d["day"]: d["expected"] for d in ev}
    first_expected = ev[0]["expected"]
    return {"days": days, "actual_cents": [_c(vals.get(d, 0)) for d in days],
            "expected_cents": [_c(exp.get(d, first_expected)) for d in days],
            "flagged": [d in exp for d in days]}


def _anomaly_id(account: str, service: str, start: str) -> str:
    return hashlib.sha256(f"{account}|{service}|{start}".encode()).hexdigest()[:16]


def detect(conn, since: date | None = None) -> list[dict]:
    names = account_names(conn)
    ref = reference_date(conn)
    rows = conn.execute(
        "SELECT account_id, service, usage_date, SUM(unblended_nanos) AS t FROM line_items "
        "GROUP BY account_id, service, usage_date ORDER BY account_id, service, usage_date").fetchall()
    all_days = sorted({r["usage_date"] for r in conn.execute("SELECT DISTINCT usage_date FROM line_items")})
    series: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        series.setdefault((r["account_id"], r["service"]), {})[r["usage_date"]] = r["t"]

    states = {r["anomaly_id"]: dict(r) for r in conn.execute("SELECT * FROM anomaly_states")}
    found: list[dict] = []
    for (acct, svc), vals in series.items():
        events: list[list[dict]] = []
        current: list[dict] = []
        for i, day in enumerate(all_days):
            if i < BASELINE_DAYS:
                continue
            base_days = all_days[i - BASELINE_DAYS:i]
            base = [vals.get(d, 0) for d in base_days]
            expected = int(statistics.median(base))
            mad = statistics.median([abs(b - expected) for b in base])
            spread = 1.4826 * mad
            actual = vals.get(day, 0)
            excess = actual - expected
            threshold = max(SIGMA * spread, MIN_REL * expected, MIN_ABS_NANOS)
            if excess >= threshold:
                current.append({"day": day, "actual": actual, "expected": expected,
                                "baseline_start": base_days[0], "baseline_end": base_days[-1]})
            elif current:
                events.append(current)
                current = []
        if current:
            events.append(current)
        for ev in events:
            start, end = ev[0]["day"], ev[-1]["day"]
            if since and date.fromisoformat(end) < since:
                continue
            aid = _anomaly_id(acct, svc, start)
            impact = sum(d["actual"] - d["expected"] for d in ev)
            peak = max(ev, key=lambda d: d["actual"] - d["expected"])
            ongoing = end == all_days[-1]
            st = states.get(aid, {})
            # After a sustained rise the baseline adapts. Say so when cost stayed at the new level.
            new_normal = False
            if not ongoing:
                nxt = all_days[all_days.index(end) + 1]
                new_normal = vals.get(nxt, 0) >= 0.8 * ev[0]["actual"]
            found.append({
                "id": aid,
                "account_id": acct,
                "account": label_for("account", acct, names),
                "service": svc,
                "start": start,
                "end": end,
                "days": len(ev),
                "ongoing": ongoing,
                "type": "sustained" if len(ev) >= 3 else "spike",
                "impact": nanos_to_cents_str(impact),
                "impact_display": _fmt_usd(impact),
                "impact_cents": int(nanos_to_cents_str(impact).replace(".", "")),
                "first_day_actual_display": _fmt_usd(ev[0]["actual"]),
                "expected_daily_display": _fmt_usd(ev[0]["expected"]),
                "peak_day": peak["day"],
                "peak_excess_display": _fmt_usd(peak["actual"] - peak["expected"]),
                "baseline_window": [ev[0]["baseline_start"], ev[0]["baseline_end"]],
                "new_normal": new_normal,
                "status": st.get("status", "open"),
                "note": st.get("note", ""),
                "series": [{"day": d["day"], "actual_cents": _c(d["actual"]), "expected_cents": _c(d["expected"])}
                           for d in ev],
                "context": _context(all_days, vals, ev),
            })
    for a in found:
        a.update(_drivers_and_owner(conn, a))
    found.sort(key=lambda a: (a["status"] != "open", -a["impact_cents"]))
    return found


def _drivers_and_owner(conn, a: dict) -> dict:
    names = account_names(conn)
    base_start, base_end = a["baseline_window"]
    n_base = conn.execute(
        "SELECT COUNT(DISTINCT usage_date) FROM line_items WHERE usage_date BETWEEN ? AND ?",
        (base_start, base_end)).fetchone()[0] or 1
    params = (a["account_id"], a["service"])
    day = {r["k"]: (r["t"], r["tags"]) for r in conn.execute(
        "SELECT usage_type || '|' || resource_id AS k, SUM(unblended_nanos) AS t, MAX(resource_tags) AS tags "
        "FROM line_items WHERE account_id=? AND service=? AND usage_date=? GROUP BY k", params + (a["start"],))}
    base = {r["k"]: r["t"] for r in conn.execute(
        "SELECT usage_type || '|' || resource_id AS k, SUM(unblended_nanos) AS t FROM line_items "
        "WHERE account_id=? AND service=? AND usage_date BETWEEN ? AND ? GROUP BY k", params + (base_start, base_end))}
    drivers = []
    for k in set(day) | set(base):
        cur = day.get(k, (0, "{}"))[0]
        avg = base.get(k, 0) // n_base
        if cur - avg <= 0:
            continue
        ut, res = k.split("|", 1)
        drivers.append({"usage_type": ut, "resource_id": res or "(no resource id)", "delta": cur - avg,
                        "delta_display": _fmt_usd(cur - avg), "new": k not in base,
                        "tags": day.get(k, (0, "{}"))[1]})
    drivers.sort(key=lambda d: -d["delta"])
    top = drivers[:6]
    # Owner: team tag on the top driver, else the account.
    import json
    owner, owner_source = None, None
    for d in top:
        team = json.loads(d.get("tags") or "{}").get("user:team")
        if team:
            owner, owner_source = team, "user:team tag on the top driver"
            break
    if owner is None:
        owner, owner_source = names.get(a["account_id"], a["account_id"]), "account (no team tag on drivers)"
    for d in top:
        d.pop("tags", None)
        d.pop("delta", None)
    explanation = ""
    if drivers:
        new = [d for d in drivers if d["new"]]
        if new:
            uts = sorted({d["usage_type"] for d in new})
            explanation = f"{len(new)} new resource(s) appeared ({', '.join(uts)})."
        else:
            explanation = f"Higher {top[0]['usage_type']} cost on {top[0]['resource_id']}."
    return {"drivers": top, "owner": owner, "owner_source": owner_source, "explanation": explanation}


def set_status(conn, anomaly_id: str, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if not (len(anomaly_id) == 16 and all(c in "0123456789abcdef" for c in anomaly_id)):
        raise ValueError("invalid anomaly id")
    conn.execute(
        "INSERT INTO anomaly_states(anomaly_id, status, note, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(anomaly_id) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
        (anomaly_id, status, note[:500], utc_now()))


def summary(conn) -> dict:
    items = detect(conn)
    ref = reference_date(conn)
    open_items = [a for a in items if a["status"] == "open"]
    return {
        "anomalies": items,
        "open_count": len(open_items),
        "open_impact_display": _fmt_usd(sum(a["impact_cents"] for a in open_items) * 10_000_000),
        "reference_date": ref.isoformat(),
        "method": {
            "series": "Daily unblended cost per account and service",
            "baseline": f"Median of the previous {BASELINE_DAYS} days with data",
            "rule": "Flag when actual minus expected is at least the largest of: 3 x robust spread (1.4826 x MAD), "
                    "20% of expected, and $10",
            "grouping": "Consecutive flagged days form one anomaly; impact is the sum of excess over expected",
            "limits": "Only increases are flagged. No gap filling. Needs 14 days of history.",
        },
    }
