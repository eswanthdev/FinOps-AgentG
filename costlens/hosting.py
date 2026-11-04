"""Settings for running behind a login proxy. Defaults keep the app local-only.

COSTLENS_ALLOWED_HOSTS       comma-separated hostnames the app answers to, besides localhost
COSTLENS_BEHIND_AUTH_PROXY   "true" only when a login proxy is the sole way in
COSTLENS_LOG_LEVEL           DEBUG, INFO, WARNING (default) or ERROR

The app has no login of its own. Binding to a non-local address is refused unless both
of the first two settings are present, and the network must really enforce the claim.
"""
from __future__ import annotations

import logging
import os
import re

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


def allowed_hosts() -> frozenset[str]:
    """Valid fully qualified hostnames only. Wildcards and anything else are ignored."""
    names = (h.strip().lower() for h in os.environ.get("COSTLENS_ALLOWED_HOSTS", "").split(","))
    return frozenset(h for h in names if _HOSTNAME_RE.match(h))


def behind_auth_proxy() -> bool:
    return os.environ.get("COSTLENS_BEHIND_AUTH_PROXY", "").strip().lower() == "true"


def bind_allowed(host: str) -> bool:
    return host in LOCAL_HOSTS or (behind_auth_proxy() and bool(allowed_hosts()))


def log_level() -> int:
    return _LEVELS.get(os.environ.get("COSTLENS_LOG_LEVEL", "").strip().upper(), logging.WARNING)
