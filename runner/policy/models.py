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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from runner.kernel.types import SensitivityClass, Subject
from runner.policy.redaction import RedactionPolicy

__all__ = [
    "ClassSpec",
    "EgressPolicy",
    "Policy",
    "Prefer",
    "Rule",
    "LinkPolicy",
    "NETWORK_TOOLS",
    "SKILL_NAME",
    "SkillCatalog",
    "SkillPolicy",
    "Substrate",
    "ToolPolicy",
    "Unavailable",
    "glob_matches",
    "normalise_endpoint",
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
    group: str = ""
    """Applies only when the subject is in this group. Empty: to everyone."""


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


NETWORK_TOOLS: frozenset[str] = frozenset({"browser", "shell"})
"""Shipped tools that can send their arguments off the machine.

``browser`` fetches URLs it is given; ``shell`` can run ``curl``. Named here so
that allowing one of them is not, silently, allowing an egress: their ceiling is
``public`` until the policy says otherwise.
"""


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
    egress: Mapping[str, SensitivityClass] = field(default_factory=dict)
    """Ceiling per tool whose arguments leave the machine.

    A tool that reaches the network is a substrate the policy did not declare:
    its arguments go to whoever answers the URL or the command, and the router's
    egress check never sees them, because it guards inference only. So such a
    tool may run only while the run's class is at or below its ceiling. Tools in
    :data:`NETWORK_TOOLS` get ``public`` unless the policy writes a ceiling down;
    widening it is a decision, like widening a substrate's ``max_class``.
    """

    def permits(self, tool: str) -> bool:
        return tool in self.allow

    def reaches_network(self, tool: str) -> bool:
        """Whether ``tool`` sends its arguments off the machine.

        Kept apart from the ceiling on purpose: the ledger labels a call by what
        the tool *is*, not by the limit applied to it, so a defect in the limit
        cannot also erase the evidence that the limit mattered.
        """
        return tool in self.egress or tool in NETWORK_TOOLS

    def egress_ceiling(self, tool: str) -> SensitivityClass | None:
        """The class above which ``tool`` may not run, or ``None`` if it stays local."""
        if tool in self.egress:
            return self.egress[tool]
        if tool in NETWORK_TOOLS:
            return SensitivityClass.PUBLIC
        return None

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


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
"""What a skill name may be when it travels: in a catalog, a heartbeat, a job.

Narrow on purpose. A name from the network becomes a directory under
``$ANNONA_HOME/skills``, so it can hold no separator, no ``..``, no wildcard.
"""


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """A published index of skills this machine may fetch. See ADR 0008.

    ``enable`` is the pre-approval: each name listed may be installed when
    Studio asks, and is enabled once installed, exactly as if it were under
    ``skills:``. A catalog entry not listed may still be installed by the
    operator at the machine, and stays disabled until the policy names it.
    """

    name: str
    url: str
    """The index, normalised by :func:`normalise_endpoint`: https only."""
    enable: tuple[str, ...] = ()
    trust: bool = False
    """Keep each skill's own ``pins`` instead of pinning it local on install."""


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """The company's memory: which folders the local retrieval index covers.

    The index is a copy of those folders in another shape, so it is embedded by
    a substrate the policy names (``embed_with``) and that substrate must be able
    to hold the folders' class — an embedder is an egress of the whole corpus,
    and it goes through placement like any other. See
    ``docs/design/memoria-storica.md``.
    """

    folders: tuple[str, ...] = ()
    embed_with: str = ""
    model: str = "bge-m3"
    prefetch: bool = False
    top_k: int = 6
    entities: bool = False
    company: str = ""
    """This company's own name, so the graph writes it instead of "noi"/"we"."""
    """Build the graph of who is whose partner, customer or bound by what, with
    the ``embed_with`` substrate's chat model, at indexing time."""
    folder_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Groups that may retrieve from a folder. A folder not listed: everyone."""

    @property
    def active(self) -> bool:
        return bool(self.folders)


@dataclass(frozen=True, slots=True)
class LinkPolicy:
    """What a linked control plane (Agents Studio) may receive back.

    ``release`` is the highest class whose *answer* may leave for the control
    plane. ``None`` — no ``link:`` section — releases metadata only: a policy
    that never mentions the link has not decided to send anything through it.
    See ADR 0006.
    """

    release: SensitivityClass | None = None
    endpoints: Mapping[str, SensitivityClass] = field(default_factory=dict)
    """A ceiling per named control plane, keyed by :func:`normalise_endpoint`.

    It replaces ``release`` for that endpoint only, so a Studio inside the
    company network can receive what the public one cannot. See ADR 0007.
    """


@dataclass(frozen=True, slots=True)
class IdentityProvider:
    """One way a request proves who it comes from. See ``docs/design/multi-user.md``.

    ``jwt``: a bearer token checked against the issuer's public keys — any OIDC
    provider, and the Akaion platform as a preset of it. ``proxy``: headers set
    by an authenticating proxy, trusted only with the proxy's shared secret.
    """

    kind: Literal["jwt", "proxy"]
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""
    subject_claim: str = "email"
    groups_claim: str = "groups"
    email_header: str = "X-Forwarded-Email"
    groups_header: str = "X-Forwarded-Groups"
    secret_env: str = ""


@dataclass(frozen=True, slots=True)
class IdentityPolicy:
    """Where subjects come from, and whether a request may come from nobody."""

    required: bool = False
    providers: tuple[IdentityProvider, ...] = ()


SUBJECT_IN_PATH = "${subject}"
_SAFE_SUBJECT = re.compile(r"[A-Za-z0-9_@+-][A-Za-z0-9._@+-]*")


def _personal(pattern: str, subject: Subject) -> str | None:
    """``pattern`` with the subject's id in place of ``${subject}``, or ``None``.

    Dropped, not expanded, for the anonymous subject and for any id that is not
    one plain path segment: "", "..", "a/b" or "*" would turn one person's
    folder into everyone's.
    """
    if SUBJECT_IN_PATH not in pattern:
        return pattern
    if subject.anonymous or not _SAFE_SUBJECT.fullmatch(subject.id) or ".." in subject.id:
        return None
    return pattern.replace(SUBJECT_IN_PATH, subject.id)


_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def normalise_endpoint(url: str) -> str:
    """The form in which two control-plane URLs are the same one.

    Scheme, host, port and path; the host lower-cased, a trailing slash dropped,
    credentials, query and fragment ignored. Nothing else is equated — not
    ``:443`` with no port, not ``www.`` with none — because a near miss here
    falls back to ``link.release``, which is the safe direction to be wrong in.

    Raises :class:`ValueError` unless the scheme is https (http on loopback,
    for development): a ceiling bound to a plain-HTTP endpoint is bound to
    whoever answers on the network path.
    """
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    secure = parsed.scheme == "https" or (parsed.scheme == "http" and host in _LOOPBACK)
    if not (host and secure):
        raise ValueError(
            f"refusing {url!r}: the link only speaks https (http is allowed on loopback)"
        )
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port:
        netloc += f":{parsed.port}"
    return f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"


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
    link: LinkPolicy = field(default_factory=LinkPolicy)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)
    skill_catalogs: tuple[SkillCatalog, ...] = ()
    source: str = "<memory>"
    identity: IdentityPolicy = field(default_factory=IdentityPolicy)
    groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Group name → members, as ids or globs (``*@legal.acme.it``)."""

    # ── Subjects ──────────────────────────────────────────────────────────────

    def groups_of(self, subject: Subject) -> tuple[str, ...]:
        """The provider's groups plus the policy's own membership, in that order."""
        if subject.anonymous:
            return ()
        who = subject.id.lower()
        mine = [
            name
            for name, members in self.groups.items()
            if any(fnmatch.fnmatchcase(who, m.lower()) for m in members)
        ]
        return tuple(dict.fromkeys([*subject.groups, *mine]))

    def for_subject(self, subject: Subject) -> Policy:
        """This policy as it applies to one subject.

        Rules for other groups are gone (``rule_for`` then finds the next rule
        for the class, or none: deny), ``${subject}`` is expanded in the tool
        allow-lists, and memory folders of other groups are dropped. Seals and
        classes are about the material, not the person: untouched.
        """
        groups = set(self.groups_of(subject))
        allow = {
            tool: tuple(p for p in (_personal(q, subject) for q in paths) if p is not None)
            for tool, paths in self.tools.allow.items()
        }
        folders = tuple(
            f
            for f in self.memory.folders
            if not self.memory.folder_groups.get(f) or groups & set(self.memory.folder_groups[f])
        )
        return replace(
            self,
            rules=tuple(r for r in self.rules if not r.group or r.group in groups),
            tools=replace(self.tools, allow=allow),
            memory=replace(self.memory, folders=folders),
        )

    @property
    def enabled_skills(self) -> tuple[str, ...]:
        """Every skill this policy enables: ``skills:`` plus each catalog's ``enable``.

        The one list every consumer reads, so a pre-approved catalog skill and
        a hand-named one cannot be treated differently by accident. Enabled is
        still not usable: the ``skill`` tool has to be allowed too.
        """
        named = [*self.skills.allow, *(n for c in self.skill_catalogs for n in c.enable)]
        return tuple(dict.fromkeys(named))

    def catalog_enabling(self, name: str) -> SkillCatalog | None:
        """The catalog that pre-approves ``name``, if any. At most one can."""
        return next((c for c in self.skill_catalogs if name in c.enable), None)

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
