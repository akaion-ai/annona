"""Policy model: what may run, where, and what may cross (layer L2).

A policy is a file the customer owns and a reviewer can read in one sitting.
Everything the perimeter decides is a function of this document plus the state
of the substrates, which is what makes a decision reproducible six months later
from the ledger alone.

Three rules govern the schema, and every design choice below follows from one of
them:

**Fail closed.** Missing means deny. An unknown class name, an unparseable
regex, a rule pointing at a substrate that does not exist — all are errors at
load time, not surprises at decision time. A perimeter that starts with a policy
it could not parse is worse than one that refuses to start, because it looks
like it is working.

**Order is meaning.** Rules are evaluated in file order and the first match
wins; substrates are ranked in the order the rule lists them when a preference
ties. Nothing is decided by dictionary iteration order, so the same policy and
the same facts always produce the same placement.

**Say it once.** A substrate declares its own jurisdiction, ceiling and cost.
Rules refer to substrates by id and never restate their properties, so a
substrate cannot be described two ways in one file.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from runner.kernel.types import SensitivityClass
from runner.policy.redaction import RedactionPolicy

__all__ = [
    "ClassSpec",
    "EgressPolicy",
    "Policy",
    "Prefer",
    "Rule",
    "SkillPolicy",
    "Substrate",
    "ToolPolicy",
    "Unavailable",
    "glob_matches",
    "normalise_path",
]

Unavailable = Literal["hold", "queue", "brief", "redact"]
Prefer = Literal["privacy", "cost", "latency", "quality"]

_JURISDICTION_DISTANCE: Mapping[str, int] = {
    "on-prem": 0,
    "local": 0,
    "eu": 1,
    "eea": 1,
    "uk": 2,
    "us": 3,
    "world": 4,
}
"""How far from the perimeter a jurisdiction is, for the ``privacy`` preference.

Unknown jurisdictions sort last rather than first: an unrecognised country is
treated as the furthest away, not the nearest.
"""


def normalise_path(path: str) -> tuple[str, str]:
    """Return ``(literal, resolved)`` absolute forms of a path.

    Both are returned, and callers must consider both, because a symlink is the
    cheapest way to walk material out of a protected directory: the literal path
    may sit in an innocuous folder while the target is a client file. Resolution
    never raises here — a path that cannot be resolved yields itself, and the
    caller still has the literal form to match on.
    """
    literal = os.path.abspath(os.path.expanduser(str(path)))
    try:
        resolved = str(Path(literal).resolve())
    except (OSError, RuntimeError):  # pragma: no cover - defensive
        resolved = literal
    return literal, resolved


def glob_matches(path: str, pattern: str) -> bool:
    """Match a path against a policy glob.

    ``fnmatch`` semantics, with one addition: a pattern ending in ``/**`` also
    matches the directory itself, so ``/mnt/pratiche/**`` covers
    ``/mnt/pratiche``. Without it, every policy would need two lines per
    directory and one of them would eventually be forgotten.
    """
    expanded = os.path.abspath(os.path.expanduser(pattern))
    if fnmatch.fnmatch(path, expanded):
        return True
    if expanded.endswith("/**"):
        return path == expanded[:-3] or path.startswith(expanded[:-2])
    return False


@dataclass(frozen=True, slots=True)
class ClassSpec:
    """How material earns a class: by where it lives, or by what it contains."""

    paths: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()
    default: bool = False

    def matches_path(self, literal: str, resolved: str) -> bool:
        return any(glob_matches(literal, p) or glob_matches(resolved, p) for p in self.paths)

    def matches_content(self, content: str) -> bool:
        return any(p.search(content) for p in self.patterns)


@dataclass(frozen=True, slots=True)
class Substrate:
    """Somewhere a step can run, and everything placement needs to know about it.

    ``max_class`` is the ceiling, and it is the field the whole product turns on:
    a substrate may serve a step only when the step's class is at or below it.
    It is declared per substrate rather than derived from ``jurisdiction`` so
    that an operator can be stricter than geography — an EU cluster they do not
    control can be capped at ``public`` even though it is in the EU.
    """

    id: str
    kind: str
    max_class: SensitivityClass
    jurisdiction: str = "world"
    endpoint: str = ""
    model: str = ""
    api_key_env: str = ""
    """Environment variable holding this substrate's credential.

    Per substrate, because a policy that registers two frontier providers needs
    two keys and a single global variable can only hold one. The *name* lives in
    the policy and the *value* never does: a policy file is the document an
    operator shows an auditor, and a secret in it is a secret in a git history.
    """
    attestation: str = ""
    tools: bool = True
    vision: bool = False
    context_window: int = 0
    cost_per_mtok: float = 0.0
    quality: int = 50
    probe: bool = False

    @property
    def distance(self) -> int:
        """Jurisdictional distance, used by the ``privacy`` preference."""
        return _JURISDICTION_DISTANCE.get(self.jurisdiction.lower(), 99)

    def can_hold(self, klass: SensitivityClass) -> bool:
        """Whether this substrate is permitted to see material of ``klass``."""
        return klass <= self.max_class


@dataclass(frozen=True, slots=True)
class Rule:
    """What may run where, for one class of material."""

    klass: SensitivityClass
    allow: tuple[str, ...]
    on_unavailable: Unavailable = "hold"
    prefer: Prefer = "privacy"
    id: str = ""


@dataclass(frozen=True, slots=True)
class SealedSpec:
    """Material no transformation may release.

    This is the answer to a question redaction cannot answer. Replacing every
    identifier in an M&A memorandum leaves::

        Progetto Falcon — il nostro cliente [ORG_1] acquisisce il 70% di
        [ORG_2] per [AMOUNT_1]; signing entro il [DATE_1].

    Nothing personal remains and the secret is entirely intact, because the
    secret was never an identifier: it is the *proposition*, and the party who
    asked the question is known to whoever answers it. A firm that sends this to
    a frontier API has told that provider it is advising on a deal of that size
    on that timetable, and two facts plus a newspaper name the target.

    So sealing is not a class — classes say *where* material may run, and are
    lowered when identifiers are removed. Sealing is a property of the matter
    that survives every transformation: sealed material is never briefed, never
    redacted, never sent. It is the one control an operator can rely on for the
    documents that would end a mandate.
    """

    paths: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.paths or self.patterns)

    def matches_path(self, literal: str, resolved: str) -> bool:
        return any(glob_matches(literal, p) or glob_matches(resolved, p) for p in self.paths)

    def matches_content(self, content: str) -> bool:
        return any(p.search(content) for p in self.patterns)

    def reason(self, content: str) -> str:
        """Which rule sealed this, for the ledger and for the operator."""
        for pattern in self.patterns:
            if pattern.search(content):
                return f"sealed by pattern /{pattern.pattern}/"
        return "sealed"


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """What may cross when the class outranks every available substrate.

    ``allowed_for`` exists because a brief is a hole in the wall, however small:
    it is a mechanism whose whole purpose is to let *something* out. The shipped
    default permits it for ``internal`` and never for ``restricted``, and
    widening that is a decision an operator has to write down.

    Redaction has its own list, and it starts **empty**. The two mechanisms let
    out very different amounts:

    A brief is written by a local model told to abstract, and what leaves is
    only what that model chose to say — a few hundred tokens an operator can
    read. Redaction leaves the document *entire*, minus the identifiers: every
    fact, every number, every sentence about what the matter is. For material
    whose sensitivity was the identifiers, that is exactly right. For material
    whose sensitivity is the subject, it is the whole secret with the names
    filed off.

    Sharing one list would have made the safer instrument imply the more
    exposing one. So briefing is on for ``internal`` by default and redaction is
    off until somebody writes down which classes may take that route.

    Unlike ``allowed_for``, this list *may* name ``restricted``, and the reason
    is worth stating because it looks like an inconsistency. Most restricted
    material is restricted **because of its identifiers** — a letter carrying a
    codice fiscale is the case redaction exists for, and refusing it outright
    would delete the feature rather than secure it. No classifier distinguishes
    "restricted because of a tax code" from "restricted because of what it is
    about"; only a person can, and the place they say so is
    :class:`SealedSpec`. So the policy offers both readings in one line:

    ``allowed_for: [internal]``
        the strict deployment. Restricted material is never redacted out, and a
        letter about a client waits for the local model to come back.
    ``allowed_for: [internal, restricted]``
        the working deployment. Identifiers may buy passage — and the matter
        that must never travel is named under ``sealed``, not left to a detector
        that can only see tokens.
    """

    brief_produced_by: str = ""
    brief_max_tokens: int = 512
    brief_must_clear: bool = True
    allowed_for: tuple[SensitivityClass, ...] = (SensitivityClass.INTERNAL,)
    redact_allowed_for: tuple[SensitivityClass, ...] = ()
    canaries: tuple[str, ...] = ()
    sealed: SealedSpec = field(default_factory=SealedSpec)

    def permits_brief(self, klass: SensitivityClass) -> bool:
        return bool(self.brief_produced_by) and klass in self.allowed_for

    def permits_redaction(self, klass: SensitivityClass) -> bool:
        return klass in self.redact_allowed_for


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Which tools may run at all, and over which paths.

    Default-deny: a tool that is not named here does not run, and neither does a
    named tool asked to touch a path outside its allow-list. The legacy
    ``PermissionManager`` did the opposite — an unrecognised tool was permitted
    and an empty allow-list meant "allow everything" — which is the single
    behaviour this layer exists to invert.
    """

    allow: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    deny_paths: tuple[str, ...] = ()

    def permits(self, tool: str) -> bool:
        return tool in self.allow

    def permits_path(self, tool: str, path: str) -> tuple[bool, str]:
        """Whether ``tool`` may touch ``path``. Deny wins over allow, always."""
        literal, resolved = normalise_path(path)

        for denied in self.deny_paths:
            if glob_matches(literal, denied) or glob_matches(resolved, denied):
                return False, f"path is on the deny-list ({denied})"

        allowed = self.allow.get(tool, ())
        if not allowed:
            return False, f"tool '{tool}' has no path allow-list"

        for pattern in allowed:
            if glob_matches(literal, pattern) or glob_matches(resolved, pattern):
                return True, ""

        if resolved != literal:
            return False, f"path resolves outside the allow-list ({resolved})"
        return False, "path is not on the allow-list"


@dataclass(frozen=True, slots=True)
class SkillPolicy:
    """Which skills may be offered to a model.

    Default-deny. A skill is an instruction the model will follow and, when it
    declares ``pins: local``, a constraint the kernel will enforce for the rest
    of the run — both are decisions, and decisions are named in the policy.
    """

    allow: tuple[str, ...] = ()

    def permits(self, name: str) -> bool:
        return name in self.allow


@dataclass(frozen=True, slots=True)
class Policy:
    """A complete, validated policy document."""

    version: int
    classes: Mapping[SensitivityClass, ClassSpec]
    substrates: tuple[Substrate, ...]
    rules: tuple[Rule, ...]
    egress: EgressPolicy
    tools: ToolPolicy
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy)
    skills: SkillPolicy = field(default_factory=SkillPolicy)
    source: str = "<memory>"

    # ── Lookups ───────────────────────────────────────────────────────────────

    def substrate(self, substrate_id: str) -> Substrate | None:
        for sub in self.substrates:
            if sub.id == substrate_id:
                return sub
        return None

    def rule_for(self, klass: SensitivityClass) -> Rule | None:
        """First rule matching ``klass``, in file order. ``None`` means deny."""
        for rule in self.rules:
            if rule.klass == klass:
                return rule
        return None

    @property
    def default_class(self) -> SensitivityClass:
        """Class for material that matches nothing.

        The most restrictive class that declares itself the default, and
        ``RESTRICTED`` if none does. Unclassifiable material is treated as the
        most sensitive, never the least.
        """
        defaults = [k for k, spec in self.classes.items() if spec.default]
        return min(defaults) if defaults else SensitivityClass.RESTRICTED

    # ── Classification inputs ─────────────────────────────────────────────────

    def class_for_path(self, path: str) -> SensitivityClass | None:
        """Highest class whose paths match, or ``None`` if none do."""
        literal, resolved = normalise_path(path)
        hits = [k for k, spec in self.classes.items() if spec.matches_path(literal, resolved)]
        return max(hits) if hits else None

    def class_for_content(self, content: str) -> SensitivityClass | None:
        """Highest class whose patterns appear in ``content``, or ``None``."""
        hits = [k for k, spec in self.classes.items() if spec.matches_content(content)]
        return max(hits) if hits else None
