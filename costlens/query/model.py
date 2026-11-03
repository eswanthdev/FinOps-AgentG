"""Typed, bounded cost query. This is the only shape the engine accepts.

Both the deterministic interpreter and any future model-backed interpreter must
produce a CostQuery; the engine never runs free-form SQL.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date

DIMENSIONS = {
    "service": "service",
    "account": "account_id",
    "region": "region",
    "usage_type": "usage_type",
    "line_item_type": "line_item_type",
    "resource": "resource_id",
}
DIMENSION_LABELS = {
    "service": "Service",
    "account": "Account",
    "region": "Region",
    "usage_type": "Usage type",
    "line_item_type": "Charge type",
    "resource": "Resource",
}
METRICS = ("unblended", "amortized")
GRANULARITIES = ("daily", "monthly", "total")
KINDS = ("explore", "variance")
CHARTS = ("auto", "stacked", "line", "bar", "hbar", "table")
TAG_RE = re.compile(r"^tag:[A-Za-z0-9_.:/=+\-@ ]{1,128}$")
MAX_RANGE_DAYS = 400
MAX_FILTER_VALUES = 50
MAX_TOP_N = 20


class QueryError(ValueError):
    pass


def is_dimension(name: str | None) -> bool:
    return bool(name) and (name in DIMENSIONS or bool(TAG_RE.match(name)))


def dimension_label(name: str) -> str:
    if name.startswith("tag:"):
        return f"Tag {name[4:]}"
    return DIMENSION_LABELS.get(name, name)


@dataclass
class CostQuery:
    start: date
    end: date  # exclusive
    kind: str = "explore"
    metric: str = "unblended"
    granularity: str = "daily"
    group_by: str | None = "service"
    filters: dict[str, list[str]] = field(default_factory=dict)
    top_n: int = 8
    chart: str = "auto"
    compare_start: date | None = None  # variance baseline window
    compare_end: date | None = None

    def validate(self) -> "CostQuery":
        if self.kind not in KINDS:
            raise QueryError(f"kind must be one of {KINDS}")
        if self.metric not in METRICS:
            raise QueryError(f"metric must be one of {METRICS}")
        if self.granularity not in GRANULARITIES:
            raise QueryError(f"granularity must be one of {GRANULARITIES}")
        if self.chart not in CHARTS:
            raise QueryError(f"chart must be one of {CHARTS}")
        if self.end <= self.start:
            raise QueryError("end date must be after start date")
        if (self.end - self.start).days > MAX_RANGE_DAYS:
            raise QueryError(f"date range is limited to {MAX_RANGE_DAYS} days")
        if self.group_by is not None and not is_dimension(self.group_by):
            raise QueryError(f"unsupported group_by {self.group_by!r}")
        if not 1 <= int(self.top_n) <= MAX_TOP_N:
            raise QueryError(f"top_n must be between 1 and {MAX_TOP_N}")
        if not isinstance(self.filters, dict) or len(self.filters) > 8:
            raise QueryError("filters must be an object with at most 8 dimensions")
        for dim, values in self.filters.items():
            if not is_dimension(dim):
                raise QueryError(f"unsupported filter dimension {dim!r}")
            if not isinstance(values, list) or not values or len(values) > MAX_FILTER_VALUES:
                raise QueryError(f"filter {dim} needs 1 to {MAX_FILTER_VALUES} values")
            if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in values):
                raise QueryError(f"filter {dim} values must be non-empty strings up to 200 characters")
        if self.kind == "variance":
            if self.group_by is None:
                raise QueryError("variance needs a group_by dimension")
            if self.compare_start is None or self.compare_end is None:
                raise QueryError("variance needs compare_start and compare_end")
            if self.compare_end <= self.compare_start:
                raise QueryError("compare_end must be after compare_start")
            if (self.compare_end - self.compare_start).days > MAX_RANGE_DAYS:
                raise QueryError(f"comparison range is limited to {MAX_RANGE_DAYS} days")
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start", "end", "compare_start", "compare_end"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "CostQuery":
        if not isinstance(data, dict):
            raise QueryError("query must be an object")
        allowed = {"start", "end", "kind", "metric", "granularity", "group_by", "filters", "top_n",
                   "chart", "compare_start", "compare_end"}
        unknown = set(data) - allowed
        if unknown:
            raise QueryError(f"unknown query fields: {', '.join(sorted(unknown))}")

        def _date(key: str, required: bool = True) -> date | None:
            v = data.get(key)
            if v in (None, ""):
                if required:
                    raise QueryError(f"{key} is required")
                return None
            try:
                return date.fromisoformat(str(v))
            except ValueError as exc:
                raise QueryError(f"{key} must be YYYY-MM-DD") from exc

        try:
            top_n = int(data.get("top_n", 8))
        except (TypeError, ValueError) as exc:
            raise QueryError("top_n must be an integer") from exc
        group_by = data.get("group_by", "service")
        return cls(
            start=_date("start"),
            end=_date("end"),
            kind=str(data.get("kind", "explore")),
            metric=str(data.get("metric", "unblended")),
            granularity=str(data.get("granularity", "daily")),
            group_by=None if group_by in (None, "", "none") else str(group_by),
            filters=data.get("filters") or {},
            top_n=top_n,
            chart=str(data.get("chart", "auto")),
            compare_start=_date("compare_start", required=False),
            compare_end=_date("compare_end", required=False),
        ).validate()
