"""Loads the organization structure and the governance policies from local JSON.

Both files are validated before anything is stored. Policies are typed rules with
a fixed set of kinds and fields: there is no way to express free-form logic. A bad
file is rejected whole and the previous data is kept. See docs/DATA_CONTRACTS.md.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..store import get_meta, set_meta, transaction, utc_now
from .loader import Outcome, _audit

ORG_SCHEMA_VERSION = "org-subset-v1"
POLICY_SCHEMA_VERSION = "policy-subset-v1"
MAX_FILE_BYTES = 256 * 1024
MAX_POLICIES = 50
MAX_VALUES = 20
_ACCOUNT_RE = re.compile(r"^\d{12}$")
_OU_RE = re.compile(r"^ou-[a-z0-9-]{1,60}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_FAMILY_RE = re.compile(r"^[a-z][a-z0-9-]{0,14}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.:/=+\-@]{1,100}$")
# kind -> the fields its params must have, exactly.
POLICY_KINDS = {
    "required_tag": {"key"},
    "allowed_regions": {"regions"},
    "restricted_instance": {"accounts", "families"},
    "budget_coverage": set(),
}


class GovernanceError(ValueError):
    pass


def _strings(value, label: str, pattern: re.Pattern) -> list[str]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_VALUES:
        raise GovernanceError(f"{label} needs 1 to {MAX_VALUES} values")
    for v in value:
        if not isinstance(v, str) or not pattern.match(v):
            raise GovernanceError(f"{label} has an invalid value {v!r}")
    return value


@dataclass(frozen=True)
class PolicySpec:
    id: str
    name: str
    kind: str
    params: dict

    @classmethod
    def from_dict(cls, data: dict) -> "PolicySpec":
        if not isinstance(data, dict):
            raise GovernanceError("a policy must be an object")
        unknown = set(data) - {"id", "name", "kind", "params"}
        if unknown:
            raise GovernanceError(f"unknown policy fields: {', '.join(sorted(unknown))}")
        pid, name, kind, params = data.get("id"), data.get("name"), data.get("kind"), data.get("params", {})
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            raise GovernanceError("policy id must be lower-case letters, digits and hyphens")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= 80:
            raise GovernanceError("policy name must be 1-80 characters")
        if kind not in POLICY_KINDS:
            raise GovernanceError(f"unsupported policy kind {kind!r}")
        if not isinstance(params, dict) or set(params) != POLICY_KINDS[kind]:
            raise GovernanceError(f"{kind} params must be exactly: {', '.join(sorted(POLICY_KINDS[kind])) or 'none'}")
        if kind == "required_tag" and (not isinstance(params["key"], str) or not _TAG_RE.match(params["key"])):
            raise GovernanceError("required_tag key is invalid")
        if kind == "allowed_regions":
            _strings(params["regions"], "regions", _REGION_RE)
        if kind == "restricted_instance":
            _strings(params["accounts"], "accounts", _ACCOUNT_RE)
            _strings(params["families"], "families", _FAMILY_RE)
        return cls(pid, name.strip(), kind, params)


def validate_organization(doc: dict) -> dict:
    if not isinstance(doc, dict) or doc.get("schemaVersion") != ORG_SCHEMA_VERSION:
        raise GovernanceError("unsupported organization schemaVersion")
    if not isinstance(doc.get("managementAccount"), str) or not _ACCOUNT_RE.match(doc["managementAccount"]):
        raise GovernanceError("managementAccount must be 12 digits")
    ous = doc.get("organizationalUnits")
    if not isinstance(ous, list) or not ous:
        raise GovernanceError("organizationalUnits must be a non-empty list")
    seen: set[str] = set()
    ou_ids: set[str] = set()
    for ou in ous:
        if not isinstance(ou, dict) or not isinstance(ou.get("id"), str) or not _OU_RE.match(ou["id"]):
            raise GovernanceError("each organizational unit needs an id like ou-xxxx")
        if not isinstance(ou.get("name"), str) or not 0 < len(ou["name"]) <= 80:
            raise GovernanceError("each organizational unit needs a name")
        ou_ids.add(ou["id"])
        for account in _strings(ou.get("accounts"), "accounts", _ACCOUNT_RE):
            if account in seen:
                raise GovernanceError(f"account {account} appears in two organizational units")
            seen.add(account)
    policies = doc.get("serviceControlPolicies", [])
    if not isinstance(policies, list):
        raise GovernanceError("serviceControlPolicies must be a list")
    for p in policies:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("targets"), list):
            raise GovernanceError("each service control policy needs a name and targets")
        for t in p["targets"]:
            if t != "root" and t not in ou_ids and t not in seen:
                raise GovernanceError(f"service control policy target {t!r} is not in this organization")
    return doc


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE_BYTES:
        raise GovernanceError(f"{path.name} is larger than the limit for local mode")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GovernanceError(f"{path.name} is not valid JSON") from exc


def _store_policies(conn, specs: list[PolicySpec]) -> None:
    keep = [s.id for s in specs]
    conn.execute(f"DELETE FROM gov_policies WHERE id NOT IN ({','.join('?' * len(keep))})", keep)
    for s in specs:
        # A reload keeps the on/off choice made in the app.
        conn.execute(
            "INSERT INTO gov_policies(id, name, kind, params_json, enabled, updated_at) VALUES (?,?,?,?,1,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind, params_json=excluded.params_json, "
            "updated_at=excluded.updated_at",
            (s.id, s.name, s.kind, json.dumps(s.params, sort_keys=True), utc_now()))


def load_governance(conn, root: str | Path) -> list[Outcome]:
    """Load organization.json and policies.json from root. A missing folder means nothing to load."""
    root = Path(root)
    if not root.exists():
        return []
    try:
        org = _read(root / "organization.json")
        if org is not None:
            validate_organization(org)
        policy_doc = _read(root / "policies.json")
        specs: list[PolicySpec] = []
        if policy_doc is not None:
            if not isinstance(policy_doc, dict) or policy_doc.get("schemaVersion") != POLICY_SCHEMA_VERSION:
                raise GovernanceError("unsupported policies schemaVersion")
            raw = policy_doc.get("policies")
            if not isinstance(raw, list) or not 0 < len(raw) <= MAX_POLICIES:
                raise GovernanceError(f"policies must have 1 to {MAX_POLICIES} entries")
            specs = [PolicySpec.from_dict(p) for p in raw]
            if len({s.id for s in specs}) != len(specs):
                raise GovernanceError("policy ids must be unique")
    except GovernanceError as exc:
        with transaction(conn):
            _audit(conn, "ingest.governance.rejected_invalid", {"uri": str(root), "detail": str(exc)})
        return [Outcome("rejected_invalid", "", "governance", str(exc), str(root))]
    payload = json.dumps(org, sort_keys=True) if org is not None else ""
    fingerprint = hashlib.sha256((payload + repr(specs)).encode()).hexdigest()
    if get_meta(conn, "governance_fingerprint") == fingerprint:
        return [Outcome("duplicate", "", "governance", "identical files already loaded; skipped", str(root))]
    with transaction(conn):
        if org is not None:
            set_meta(conn, "org_structure", payload)
        if specs:
            _store_policies(conn, specs)
        set_meta(conn, "governance_fingerprint", fingerprint)
        _audit(conn, "ingest.governance.active", {"uri": str(root), "policies": len(specs)})
    return [Outcome("active", "", "governance", f"organization and {len(specs)} policies loaded", str(root), len(specs))]
