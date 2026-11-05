"""A policy's fingerprint: what the ledger binds every run to (layer L2).

An auditor holding a ledger and a policy file needs to know the file is the one
the decisions were taken under. The fingerprint is a SHA-256 of the *parsed*
policy in canonical form, so comments and key order in the YAML do not change
it and a rewritten rule does.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from enum import Enum
from typing import Any

from runner.policy.models import Policy

__all__ = ["policy_fingerprint"]


def _plain(value: Any) -> Any:
    if isinstance(value, re.Pattern):
        return value.pattern
    if isinstance(value, Enum):
        return value.name
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        items = [_plain(v) for v in value]
        return sorted(items, key=json.dumps) if isinstance(value, set | frozenset) else items
    return value


_NOT_POLICY = ("source",)
"""Fields that describe where a policy was read from, not what it says."""


def policy_fingerprint(policy: Policy) -> str:
    """SHA-256 of the policy's canonical JSON."""
    plain = {k: v for k, v in _plain(policy).items() if k not in _NOT_POLICY}
    canonical = json.dumps(plain, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
