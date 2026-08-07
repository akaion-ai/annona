"""Wiring the perimeter together (composition of layer L2).

Everything in :mod:`runner.policy`, :mod:`runner.placement` and
:mod:`runner.audit` is deliberately unaware of the others' construction: the
classifier does not build a ledger, the placement engine does not open sockets.
This module is where those parts become one object with a lifetime — one per
run, because the working set is per run and sharing it across runs would let one
task's material decide another task's placement.

It also owns the one piece of knowledge that has to live somewhere: which
adapter class serves which substrate ``kind``. That mapping is data, not
policy — an operator declares ``kind: openai-compatible`` and this decides that
means :class:`~runner.capability.backends.openai_compatible.OpenAICompatibleBackend`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from runner.audit.ledger import Ledger
from runner.capability.backends import (
    AnthropicBackend,
    EchoBackend,
    OllamaBackend,
    OpenAICompatibleBackend,
)
from runner.kernel.errors import ConfigurationError
from runner.kernel.types import SensitivityClass
from runner.placement.engine import PlacementDecisionEngine
from runner.placement.registry import SubstrateRegistry, http_prober
from runner.placement.router import RoutingBackend
from runner.policy.classifier import PolicyClassifier, WorkingSet
from runner.policy.gate import DefaultDenyGate
from runner.policy.loader import default_policy, load_policy
from runner.policy.models import Policy, Substrate
from runner.policy.redaction import Redactor
from runner.policy.tracking import TrackingExecutor
from runner.skills.loader import discover_skills
from runner.skills.models import Skill
from runner.skills.registry import SkillfulExecutor, SkillRegistry

__all__ = ["Enforcement", "build_backend", "build_redactor", "policy_path"]


def policy_path(home: str | Path | None = None) -> Path:
    """Where the policy lives.

    ``$ANNONA_HOME/policy.yaml``, defaulting to ``~/.annona/policy.yaml``. The
    legacy ``~/.akaion`` directory is consulted as a fallback so an existing
    installation keeps working after the rename, and neither location is
    created as a side effect of asking.
    """
    if home:
        return Path(home).expanduser() / "policy.yaml"

    explicit = os.getenv("ANNONA_HOME")
    if explicit:
        return Path(explicit).expanduser() / "policy.yaml"

    preferred = Path.home() / ".annona" / "policy.yaml"
    if preferred.exists():
        return preferred

    legacy = Path.home() / ".akaion" / "policy.yaml"
    return legacy if legacy.exists() else preferred


def build_backend(substrate: Substrate, *, secrets: Mapping[str, str] | None = None) -> Any:
    """Construct the adapter that serves one substrate.

    Raises:
        ConfigurationError: the substrate names a kind this build does not
            support, or omits something that kind requires. Fatal on purpose: a
            substrate that cannot be built must not silently disappear from the
            candidate set, because its absence would look like a policy decision
            in the ledger.
    """
    secrets = secrets or os.environ
    kind = substrate.kind.lower()

    def credential(*fallbacks: str) -> tuple[str, str]:
        """This substrate's key, and the variable name to name in an error.

        The substrate's own ``api_key_env`` wins; the historical global names
        remain as fallbacks so an existing single-provider deployment keeps
        working. Returning the *name* alongside the value is what lets a failure
        say `set OPENROUTER_KEY` rather than `no credentials`.
        """
        names = (substrate.api_key_env, *fallbacks) if substrate.api_key_env else fallbacks
        for name in names:
            if name and secrets.get(name):
                return secrets[name], name
        return "", (names[0] if names else "")

    if kind == "ollama":
        if not substrate.model:
            raise ConfigurationError(f"substrate {substrate.id!r} (ollama) declares no model")
        return OllamaBackend(
            model=substrate.model,
            endpoint=substrate.endpoint or "http://localhost:11434",
            context_window=substrate.context_window or 32_768,
        )

    if kind in ("openai-compatible", "openai", "vllm", "llamacpp"):
        if not substrate.endpoint:
            raise ConfigurationError(f"substrate {substrate.id!r} ({kind}) declares no endpoint")
        if not substrate.model:
            raise ConfigurationError(f"substrate {substrate.id!r} ({kind}) declares no model")
        key, _ = credential("ANNONA_SUBSTRATE_KEY", "OPENAI_API_KEY")
        return OpenAICompatibleBackend(
            model=substrate.model,
            endpoint=substrate.endpoint,
            api_key=key,
            context_window=substrate.context_window or 32_768,
            # Locality is the operator's claim about their own network, not
            # something an HTTP client can determine. The policy says it.
            is_local=substrate.distance == 0,
            name=f"{kind}:{substrate.id}",
        )

    if kind == "anthropic":
        key, name = credential("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigurationError(
                f"substrate {substrate.id!r} (anthropic) has no credential: set {name}, "
                "or point the substrate at another variable with api_key_env"
            )
        # Imported here rather than at module scope: the SDK is an optional
        # extra, and a policy with no Anthropic substrate must not require it.
        from anthropic import Anthropic  # noqa: PLC0415

        return AnthropicBackend(
            client=Anthropic(api_key=key),
            # A default that is current rather than whatever was current when
            # this line was written: a policy that omits `model` should get a
            # model that exists.
            model=substrate.model or "claude-opus-5",
        )

    if kind == "echo":
        return EchoBackend()

    raise ConfigurationError(
        f"substrate {substrate.id!r} declares unknown kind {substrate.kind!r}; "
        "supported: ollama, openai-compatible, anthropic, echo"
    )


def build_redactor(policy: Policy) -> Redactor | None:
    """Construct the redactor a policy asks for, or ``None``.

    Kept beside :func:`build_backend` because it is the same kind of decision:
    the policy names a provider, and this is the one place that knows which
    adapter that name means.

    Raises:
        ConfigurationError: the policy names a provider this build does not
            have. Failing here is the point — a deployment whose redactor
            silently did not exist would send material it believed was clean.
    """
    redaction = policy.redaction
    if not redaction.enabled:
        return None

    provider = redaction.provider.lower()
    if provider in ("rizzo-pii", "rizzo_pii", "rizzo"):
        # Imported lazily: the adapter needs an HTTP client, and a policy with
        # no redactor must not pay for it.
        from runner.capability.redactors.rizzo_pii import (  # noqa: PLC0415
            DEFAULT_ENDPOINT,
            RizzoPiiRedactor,
        )

        return RizzoPiiRedactor(
            endpoint=redaction.endpoint or DEFAULT_ENDPOINT,
            timeout=redaction.timeout,
        )

    raise ConfigurationError(
        f"redaction.provider {redaction.provider!r} is unknown; supported: rizzo-pii"
    )


@dataclass
class Enforcement:
    """The perimeter, assembled: policy, classification, placement, record.

    Construct one per run with :meth:`for_run`, then hand its three adapters to
    the agent loop. The loop takes them as the ports it already depends on, so
    nothing about the loop changes when the perimeter is switched on or off.
    """

    policy: Policy
    classifier: PolicyClassifier
    working_set: WorkingSet
    registry: SubstrateRegistry
    engine: PlacementDecisionEngine
    ledger: Ledger
    backends: Mapping[str, Any]
    redactor: Redactor | None = None
    skills: Mapping[str, Skill] = field(default_factory=dict)

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def for_run(
        cls,
        *,
        policy: Policy | None = None,
        policy_file: str | Path | None = None,
        ledger_path: str | Path | None = None,
        backends: Mapping[str, Any] | None = None,
        redactor: Redactor | None = None,
        skills: Mapping[str, Skill] | None = None,
        run_id: str | None = None,
        probe: bool = True,
        fsync: bool = True,
        secrets: Mapping[str, str] | None = None,
    ) -> Enforcement:
        """Assemble a perimeter for one run.

        Args:
            policy: An already-validated policy. Wins over ``policy_file``.
            policy_file: Where to read the policy from. Defaults to
                :func:`policy_path`; if that file does not exist, the shipped
                default policy is used — which registers nothing but the local
                runtime, so a missing policy fails closed rather than open.
            ledger_path: Where decisions are recorded. Defaults to the policy
                directory's ``ledger.jsonl``.
            backends: Pre-built adapters by substrate id, for tests. When
                omitted, one is built per substrate from its ``kind``.
            run_id: Correlates every entry of one run in the ledger.
            probe: Whether to actively probe substrate liveness over HTTP.
            fsync: Whether to fsync every ledger append. Off only in tests.
        """
        if policy is None:
            path = Path(policy_file) if policy_file else policy_path()
            if path.exists():
                policy = load_policy(path)
                logger.debug(f"policy loaded from {path}")
            else:
                policy = default_policy()
                logger.warning(
                    f"no policy at {path}; using the built-in default, which registers "
                    "only the local runtime. Run `annona init` to write one."
                )

        classifier = PolicyClassifier(policy)
        # The floor is the policy's, not this constructor's. `WorkingSet()`
        # defaults to PUBLIC, so until now a policy declaring `internal` as
        # its default class was quietly ignored and every run started at the
        # class that grants the widest permission — the exact inversion of
        # what `Policy.default_class` documents ("unclassifiable material is
        # treated as the most sensitive, never the least").
        working_set = WorkingSet(initial=policy.default_class)
        registry = SubstrateRegistry.from_substrates(
            policy.substrates,
            prober=http_prober() if probe else None,
        )
        engine = PlacementDecisionEngine(policy, registry)

        if ledger_path is None:
            ledger_path = policy_path().parent / "ledger.jsonl"
        ledger = Ledger(ledger_path, run_id=run_id, fsync=fsync)

        if backends is None:
            backends = {s.id: build_backend(s, secrets=secrets) for s in policy.substrates}

        if redactor is None:
            redactor = build_redactor(policy)

        if skills is None:
            # Discovered even when the policy allows none: `annona skills` has to
            # be able to say "this exists and you have not enabled it", which is
            # a different sentence from "this does not exist".
            skills = discover_skills() if policy.skills.allow else {}

        return cls(
            policy=policy,
            classifier=classifier,
            working_set=working_set,
            registry=registry,
            engine=engine,
            ledger=ledger,
            backends=dict(backends),
            redactor=redactor,
            skills=dict(skills),
        )

    # ── The three adapters the loop consumes ──────────────────────────────────

    def gate(self) -> DefaultDenyGate:
        """Default-deny clearance for tool calls."""
        return DefaultDenyGate(self.policy, self.classifier, self.working_set, self.ledger)

    def executor(self, inner: Any) -> TrackingExecutor:
        """The real executor, wrapped twice.

        Innermost the real tools; then the skill loader, which adds one tool and
        enforces what loading a skill implies; then the tracker, which classifies
        everything that comes back. Order matters: a skill body is material
        entering the transcript like any other, so the tracker has to see it too.
        """
        with_skills = SkillfulExecutor(inner, self.skill_registry(), self.working_set, self.ledger)
        return TrackingExecutor(with_skills, self.classifier, self.working_set, self.ledger)

    def skill_registry(self) -> SkillRegistry:
        """Skills this policy permits and this deployment can actually run."""
        return SkillRegistry(
            self.skills,
            allowed=self.policy.skills.allow,
            vision=any(s.vision for s in self.policy.substrates),
            allowed_tools=tuple(self.policy.tools.allow),
            context_window=max((s.context_window for s in self.policy.substrates), default=0),
        )

    def backend(self, *, prefer_quality: bool = False) -> RoutingBackend:
        """The backend that places every turn before it is served.

        Args:
            prefer_quality: The operator asked for the best substrate this
                policy already permits. Reordering only — see
                :class:`~runner.kernel.types.Requirement`.
        """
        return RoutingBackend(
            prefer_quality=prefer_quality,
            policy=self.policy,
            engine=self.engine,
            registry=self.registry,
            backends=self.backends,
            classifier=self.classifier,
            working_set=self.working_set,
            ledger=self.ledger,
            redactor=self.redactor,
        )

    # ── Reporting ─────────────────────────────────────────────────────────────

    @property
    def klass(self) -> SensitivityClass:
        """The run's current class."""
        return self.working_set.klass

    def status(self) -> dict[str, Any]:
        """What ``annona status`` prints about the perimeter."""
        return {
            "policy": self.policy.source,
            "class": self.working_set.klass.label,
            "substrates": {
                sid: {
                    "up": health.up,
                    "reason": health.reason,
                    "jurisdiction": self.registry.substrates[sid].jurisdiction,
                    "max_class": self.registry.substrates[sid].max_class.label,
                }
                for sid, health in self.registry.snapshot().items()
            },
            "redactor": self.redactor.name if self.redactor else "none",
            "skills": [s.name for s in self.skill_registry().available()],
            "ledger": {
                "path": str(self.ledger.path),
                "head": self.ledger.head[:16],
                **self.ledger.summary(),
            },
        }
