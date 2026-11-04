"""Deterministic question interpreter (no AI model).

Turns a plain-English cost question into a validated CostQuery using keyword
and pattern rules. It reports what it understood and every default it assumed,
and asks for clarification instead of guessing when the question is not about
cost data it can answer.

A future model-backed interpreter must implement the same Interpreter protocol
and return the same Interpretation; its CostQuery is validated identically.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from ..query.model import CostQuery, QueryError, dimension_label

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9

SERVICE_ALIASES = {
    "ec2": "AmazonEC2", "compute": "AmazonEC2", "instances": "AmazonEC2",
    "rds": "AmazonRDS", "database": "AmazonRDS", "databases": "AmazonRDS",
    "s3": "AmazonS3", "storage": "AmazonS3",
    "lambda": "AWSLambda", "cloudwatch": "AmazonCloudWatch", "logs": "AmazonCloudWatch",
    "dynamodb": "AmazonDynamoDB", "dynamo": "AmazonDynamoDB", "glue": "AWSGlue",
    "savings plan": "ComputeSavingsPlans", "savings plans": "ComputeSavingsPlans",
}
REGION_ALIASES = {
    "mumbai": "ap-south-1", "ireland": "eu-west-1", "virginia": "us-east-1", "n. virginia": "us-east-1",
    "oregon": "us-west-2", "frankfurt": "eu-central-1", "singapore": "ap-southeast-1",
}
GROUP_WORDS = [
    (r"usage[ -]?types?", "usage_type"),
    (r"charge[ -]?types?|line[ -]?item[ -]?types?", "line_item_type"),
    (r"services?", "service"),
    (r"accounts?", "account"),
    (r"regions?", "region"),
    (r"resources?|resource[ -]?ids?", "resource"),
    (r"teams?", "tag:user:team"),
    (r"env(?:ironment)?s?", "tag:user:env"),
    (r"apps?|applications?", "tag:user:app"),
]
COST_WORDS = re.compile(r"\b(cost|costs|spend|spent|spending|bill|billing|charges?|expense|money|usd|\$|dollars?|"
                        r"price|paid|pay)\b")
VARIANCE_WORDS = re.compile(r"\b(why|increase[sd]?|spike[sd]?|jump(ed)?|grew|growth|went up|go up|gone up|drove|"
                            r"driv(e|ing|ers?)|changed?|changes|compare[sd]?|comparison|vs\.?|versus|"
                            r"month over month|mom|decrease[sd]?|drop(ped)?|went down|delta|difference)\b")
REFINE_START = re.compile(r"^(now|only|and|also|what about|how about|same|instead|just|by|per|show it|make it|switch|"
                          r"change it|split|break (it )?down|filter|exclude|without)\b")


@dataclass
class Interpretation:
    status: str  # ok | clarify | unsupported
    query: CostQuery | None = None
    understood: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    message: str = ""
    suggestions: list[str] = field(default_factory=list)
    module: str = "visualize"

    def to_dict(self) -> dict:
        return {"status": self.status, "query": self.query.to_dict() if self.query else None,
                "understood": self.understood, "assumptions": self.assumptions, "message": self.message,
                "suggestions": self.suggestions, "module": self.module}


@dataclass
class Vocabulary:
    """Known values, loaded from the ingested data (untrusted text, used only for matching)."""
    services: list[str]
    accounts: dict[str, str]  # id -> name
    regions: list[str]
    usage_types: list[str]
    tags: dict[str, list[str]]


class Interpreter(Protocol):
    mode: str

    def interpret(self, question: str, vocab: Vocabulary, today: date,
                  previous: CostQuery | None = None) -> Interpretation: ...


SUGGESTIONS = [
    "Show cost by service for last month",
    "Why did costs increase in October compared to September?",
    "Daily EC2 cost by account for the last 30 days",
    "Top 5 teams by amortized cost this month",
]


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, n: int) -> date:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def _word(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9-])" + re.escape(phrase) + r"(?![a-z0-9-])", text) is not None


class DeterministicInterpreter:
    mode = "deterministic"

    # -------------------------------------------------------------- time
    def _months_mentioned(self, text: str, today: date) -> list[tuple[date, str]]:
        found = []
        for m in re.finditer(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b(?:\s+(20\d\d))?", text):
            name, year = m.group(1), m.group(2)
            if name == "may" and not year and not re.search(r"\bin may\b|\bmay (vs|and|to)\b", text):
                continue  # "may" is usually the verb
            month = MONTHS[name]
            y = int(year) if year else (today.year if month <= today.month else today.year - 1)
            found.append((date(y, month, 1), m.group(0)))
        found.sort(key=lambda x: x[1] and text.index(x[1]))
        uniq = []
        for d, t in found:
            if d not in [u[0] for u in uniq]:
                uniq.append((d, t))
        return uniq

    def _time(self, text: str, today: date, understood: list[str]) -> tuple[date, date] | None:
        end_today = today  # reference date is exclusive end ("up to yesterday")
        m = re.search(r"\b(?:last|past|previous)\s+(\d{1,3})\s+(day|days|week|weeks|month|months)\b", text)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            if unit.startswith("day"):
                understood.append(f"last {n} days")
                return end_today - timedelta(days=n), end_today
            if unit.startswith("week"):
                understood.append(f"last {n} weeks")
                return end_today - timedelta(days=7 * n), end_today
            understood.append(f"last {n} complete months")
            cur = _month_start(today)
            return _add_months(cur, -n), cur
        if re.search(r"\b(last|past|previous) (week|7 days)\b", text):
            understood.append("last 7 days")
            return end_today - timedelta(days=7), end_today
        if re.search(r"\byesterday\b", text):
            understood.append("yesterday")
            return end_today - timedelta(days=1), end_today
        if re.search(r"\b(this month|month to date|month-to-date|mtd|current month)\b", text):
            understood.append("this month to date")
            return _month_start(today), end_today
        if re.search(r"\b(last|previous|prior) month\b", text):
            understood.append("last month")
            cur = _month_start(today)
            return _add_months(cur, -1), cur
        if re.search(r"\b(this year|year to date|ytd)\b", text):
            understood.append("year to date")
            return date(today.year, 1, 1), end_today
        m = re.search(r"\bq([1-4])(?:\s+(20\d\d))?\b", text)
        if m:
            qn = int(m.group(1))
            y = int(m.group(2)) if m.group(2) else today.year
            start = date(y, 3 * qn - 2, 1)
            understood.append(f"Q{qn} {y}")
            return start, _add_months(start, 3)
        months = self._months_mentioned(text, today)
        if months:
            if re.search(r"\bsince\b", text):
                understood.append(f"since {months[0][0].strftime('%B %Y')}")
                return months[0][0], end_today
            ms = sorted(d for d, _ in months)
            if len(ms) >= 2 and re.search(r"\b(to|through|until|-)\b", text) and not VARIANCE_WORDS.search(text):
                understood.append(f"{ms[0].strftime('%B')} to {ms[-1].strftime('%B %Y')}")
                return ms[0], _add_months(ms[-1], 1)
            d = ms[-1]
            understood.append(d.strftime("%B %Y"))
            return d, _add_months(d, 1)
        return None

    # -------------------------------------------------------------- filters
    def _filters(self, text: str, vocab: Vocabulary, understood: list[str]) -> dict[str, list[str]]:
        filters: dict[str, list[str]] = {}

        def add(dim: str, value: str, label: str):
            vals = filters.setdefault(dim, [])
            if value not in vals:
                vals.append(value)
                understood.append(label)

        known_services = set(vocab.services)
        for alias, svc in SERVICE_ALIASES.items():
            if svc in known_services and _word(text, alias):
                # "storage" also appears in "storage lens" / "EBS storage"; keep it simple and explicit.
                add("service", svc, f"service {svc}")
        for svc in vocab.services:
            if _word(text, svc.lower()):
                add("service", svc, f"service {svc}")
        if _word(text, "nat") or "nat gateway" in text:
            for ut in vocab.usage_types:
                if "NatGateway" in ut:
                    add("usage_type", ut, f"usage type {ut}")
        claimed: set[str] = set()
        for acct_id, name in sorted(vocab.accounts.items(), key=lambda x: -len(x[1])):
            n = name.lower()
            hit = None
            for form in (n, n.replace("-", " "), acct_id):
                if form and _word(text, form):
                    hit = form
                    break
            if hit:
                add("account", acct_id, f"account {name}")
                claimed.add(n)
                # Remove the matched account name so its words are not matched again as tags.
                text = re.sub(r"(?<![a-z0-9-])" + re.escape(hit) + r"(?![a-z0-9-])", " ", text)
        for region in vocab.regions:
            if _word(text, region.lower()):
                add("region", region, f"region {region}")
        for alias, region in REGION_ALIASES.items():
            if region in vocab.regions and _word(text, alias):
                add("region", region, f"region {region} ({alias})")
        env_aliases = {"production": "prod", "prod": "prod", "dev": "dev", "development": "dev", "staging": "staging"}
        env_values = vocab.tags.get("user:env", [])
        for word, val in env_aliases.items():
            if val in env_values and val.lower() not in claimed and _word(text, word):
                add("tag:user:env", val, f"environment {val}")
        for key in ("user:team", "user:app"):
            for val in vocab.tags.get(key, []):
                v = val.lower()
                if v in claimed:
                    continue
                if _word(text, v) or _word(text, v.replace("-", " ")):
                    add("tag:" + key, val, f"{key[5:]} {val}")
        if re.search(r"\buntagged\b|\bwithout (a )?(team )?tags?\b|\bno tags?\b", text):
            add("tag:user:team", "(untagged)", "untagged (no user:team tag)")
        return filters

    # -------------------------------------------------------------- main
    def interpret(self, question: str, vocab: Vocabulary, today: date,
                  previous: CostQuery | None = None) -> Interpretation:
        text = re.sub(r"\s+", " ", (question or "").strip().lower())
        if not text:
            return Interpretation("clarify", message="Ask a question about your AWS costs.", suggestions=SUGGESTIONS)
        if len(text) > 500:
            return Interpretation("clarify", message="Please keep the question under 500 characters.",
                                  suggestions=SUGGESTIONS)

        if re.search(r"\b(azure|gcp|google cloud|oracle cloud|oci|kubernetes|k8s|snowflake|databricks)\b", text):
            return Interpretation(
                "unsupported", message="This release only has AWS billing data (CUR 2.0). Other clouds and "
                "platforms are not connected yet.", suggestions=SUGGESTIONS)
        if re.search(r"\b(forecast|predict|projection|project(ed)?|next month|end of month|eom|run[- ]?rate)\b", text):
            return Interpretation(
                "unsupported", message="Forecasting is not in this release. I can show what has already been "
                "spent, and how it is trending.", suggestions=["Daily cost trend for the last 30 days",
                                                               "Show cost by month for the last 3 months"])
        if re.search(r"\b(optimi[sz]\w*|rightsiz\w*|right-siz\w*|waste|idle|save money|savings opportunit\w*|"
                     r"reduce (my |our |the )?(cost|spend|bill)\w*|cut (cost|spend)\w*)\b", text):
            return Interpretation(
                "unsupported", module="optimize",
                message="Finding and validating savings is the job of the Cost Optimization Engineer, which is the "
                        "next module. For now I can show where the money goes, so you know where to look.",
                suggestions=["Top 10 resources by cost last month", "Why did costs increase in October compared to September?"])
        if re.search(r"\b(scps?|guardrails?|block|enforce|policy|policies|governance|limit spend|budgets?)\b", text):
            return Interpretation(
                "unsupported", module="govern",
                message="Guardrails and policies belong to the Govern page, which drafts them for review and applies "
                        "nothing. For budgets, try “set up a monthly budget of $5,000 for prod-data and alert "
                        "ops@example.com at 80%”.",
                suggestions=["Show cost by account this month", "Daily cost by team for the last 30 days"])

        understood: list[str] = []
        assumptions: list[str] = []
        time = self._time(text, today, understood)
        filters = self._filters(text, vocab, understood)

        group_by = None
        for pattern, dim in GROUP_WORDS:
            if re.search(r"\b(by|per|across|each|which|split by|grouped by|break(?:down)?(?: it)? down by)\s+(?:the\s+)?(?:"
                         + pattern + r")\b", text) or re.search(r"\btop\s+\d*\s*(?:" + pattern + r")\b", text) or \
                    re.search(r"\b(?:" + pattern + r")\s+(?:breakdown|split)\b", text):
                if dim.startswith("tag:") and dim[4:] not in vocab.tags:
                    continue
                group_by = dim
                understood.append(f"grouped by {dimension_label(dim).lower()}")
                break
        m = re.search(r"\b(?:by|per)\s+tag\s+([a-z0-9_:.\-]+)", text)
        if m and not group_by:
            key = m.group(1) if m.group(1) in vocab.tags else f"user:{m.group(1)}"
            if key in vocab.tags:
                group_by = "tag:" + key
                understood.append(f"grouped by tag {key}")

        top_n = None
        m = re.search(r"\btop\s+(\d{1,2})\b", text)
        if m:
            top_n = max(1, min(20, int(m.group(1))))
            understood.append(f"top {top_n}")

        metric = None
        if re.search(r"\b(amorti[sz]ed|effective cost)\b", text):
            metric = "amortized"
            understood.append("amortized cost")
        elif re.search(r"\bunblended\b", text):
            metric = "unblended"
            understood.append("unblended cost")

        granularity = None
        if re.search(r"\b(daily|per day|each day|day by day|by day|day-by-day)\b", text):
            granularity = "daily"
        elif re.search(r"\b(monthly|per month|each month|month by month|by month)\b", text):
            granularity = "monthly"
        elif re.search(r"\b(total|overall|breakdown|split|which|biggest|largest|top)\b", text):
            granularity = "total"
        if granularity:
            understood.append(f"{granularity} view")

        chart = None
        for word, c in (("table", "table"), ("line", "line"), ("trend", "line"), ("bar chart", "bar")):
            if _word(text, word):
                chart = c
        is_variance = bool(VARIANCE_WORDS.search(text))
        refers_back = bool(re.search(r"\b(it|this|that|these|those|same|previous period|prior period)\b", text))
        refine = previous is not None and (bool(REFINE_START.match(text)) or
                                           (time is None and refers_back) or
                                           (time is None and len(text.split()) <= 6 and not COST_WORDS.search(text)))
        if re.search(r"\b(all services|clear filters|remove (the )?filters?|no filters?|everything)\b", text):
            filters = {}
            understood.append("filters cleared")
            refine = previous is not None

        cleared = "filters cleared" in understood
        anything = time or filters or group_by or top_n or metric or granularity or is_variance or chart or cleared
        if not anything and not COST_WORDS.search(text):
            return Interpretation(
                "clarify",
                message="I could not match that to a cost question. Try naming a time range, a service, an account "
                        "or a grouping, for example:", suggestions=SUGGESTIONS)

        # Follow-up questions inherit the previous view.
        base = previous if refine else None
        if base:
            understood.insert(0, "follow-up to the previous view")
            if time is None:
                time = (base.start, base.end)
            merged = {} if "filters cleared" in understood else {k: list(v) for k, v in base.filters.items()}
            for k, v in filters.items():
                merged[k] = v
            filters = merged
            group_by = group_by or base.group_by
            metric = metric or base.metric
            top_n = top_n or base.top_n
            if granularity is None and base.kind == "explore":
                granularity = base.granularity
            if not is_variance and base.kind == "variance" and not (REFINE_START.match(text) and group_by):
                is_variance = True

        if is_variance:
            if time is None:
                cur = _month_start(today)
                start, end = _add_months(cur, -1), cur
                assumptions.append("No period given: compared last complete month with the month before.")
            else:
                start, end = time
            months = self._months_mentioned(text, today)
            if len(months) >= 2 and not (base and len(months) < 2):
                ms = sorted(d for d, _ in months)
                c_start, c_end = ms[0], _add_months(ms[0], 1)
                start, end = ms[-1], _add_months(ms[-1], 1)
                understood.append(f"{start.strftime('%B')} vs {c_start.strftime('%B %Y')}")
            elif base and base.kind == "variance" and base.compare_start and time == (base.start, base.end):
                c_start, c_end = base.compare_start, base.compare_end
            else:
                if start.day == 1 and end.day == 1 and (end.year * 12 + end.month) - (start.year * 12 + start.month) >= 1:
                    n = (end.year * 12 + end.month) - (start.year * 12 + start.month)
                    c_start, c_end = _add_months(start, -n), start
                    assumptions.append(f"Baseline is the preceding {n} calendar month{'s' if n > 1 else ''}.")
                else:
                    length = (end - start).days
                    c_start, c_end = start - timedelta(days=length), start
                    assumptions.append(f"Baseline is the preceding {length} days.")
            if group_by is None:
                group_by = "service"
                assumptions.append("No grouping given: compared by service.")
            q = CostQuery(kind="variance", start=start, end=end, compare_start=c_start, compare_end=c_end,
                          metric=metric or "unblended", group_by=group_by, filters=filters, top_n=top_n or 8,
                          granularity="total")
        else:
            if time is None:
                start, end = today - timedelta(days=30), today
                assumptions.append("No time range given: used the last 30 days.")
            else:
                start, end = time
            if group_by is None and granularity != "total" and not filters:
                group_by = "service"
                assumptions.append("No grouping given: grouped by service.")
            elif group_by is None and granularity == "total":
                group_by = "service"
                assumptions.append("Grouped by service for the breakdown.")
            if granularity is None:
                granularity = "daily" if (end - start).days <= 62 else "monthly"
                assumptions.append(f"Used a {granularity} view for this range.")
            q = CostQuery(kind="explore", start=start, end=end, metric=metric or "unblended",
                          granularity=granularity, group_by=group_by, filters=filters, top_n=top_n or 8,
                          chart=chart or "auto")
        if not metric:
            assumptions.append("Used unblended cost (as billed). Say 'amortized' to spread commitments.")
        try:
            q.validate()
        except QueryError as exc:
            return Interpretation("clarify", message=f"I could not build that view: {exc}.", suggestions=SUGGESTIONS)
        return Interpretation("ok", query=q, understood=understood, assumptions=assumptions)
