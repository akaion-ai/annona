"""Ports: the interfaces the core depends on (layer L0).

This is a ports-and-adapters boundary. The agent loop depends on the protocols
declared here and on nothing else; concrete inference backends, tool registries
and policy engines are adapters that satisfy them and are injected by the
composition root.

Two consequences worth stating, because they are the reason the indirection
exists rather than decoration:

1. The loop cannot import a provider SDK. That is asserted mechanically in
   ``.importlinter``, so "the loop is provider-agnostic" is a checked fact.
2. Phase 1 can insert the perimeter between the loop and a backend by supplying
   a different :class:`InferenceBackend` — one that classifies and gates before
   delegating — without either side changing. The seam already exists.

Protocols are structural: an adapter satisfies one by shape, with no base class
to inherit and no registration step.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    Placement,
    Requirement,
    SensitivityClass,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "Classifier",
    "ContentModel",
    "InferenceBackend",
    "Ledger",
    "PlacementEngine",
    "PolicyGate",
    "ToolExecutor",
]


@runtime_checkable
class InferenceBackend(Protocol):
    """A single turn of model inference, normalised.

    Implementations own their wire format and nothing else: they translate a
    :class:`~runner.kernel.types.CompletionRequest` into a provider call and the
    provider's answer back into a :class:`~runner.kernel.types.Completion`. They
    hold no conversation state and make no control-flow decisions — the loop
    decides whether to continue, and the perimeter decides whether it may.
    """

    @property
    def name(self) -> str:
        """Stable identifier used in logs, traces and configuration."""
        ...

    @property
    def capabilities(self) -> Capabilities:
        """What this backend can do, including whether it is local."""
        ...

    def complete(self, request: CompletionRequest) -> Completion:
        """Perform one turn of inference.

        Raises:
            runner.kernel.errors.BackendUnavailableError: the backend could not
                be reached or returned nothing usable. The loop treats this as
                "stop and report what you have", never as a crash.
        """
        ...


@runtime_checkable
class ToolExecutor(Protocol):
    """Advertises tools and runs them.

    Implementations are expected to be *total*: :meth:`invoke` returns a
    :class:`~runner.kernel.types.ToolResult` for every input, including for
    unknown tools and for tools that raise. A model must always receive an
    answer it can reason about — an unhandled exception would end the run and
    tell the model nothing.
    """

    def specs(self) -> tuple[ToolSpec, ...]:
        """The tools to advertise to the model this turn."""
        ...

    def invoke(self, call: ToolCall) -> ToolResult:
        """Run one tool call and capture its outcome, error included."""
        ...


@runtime_checkable
class PolicyGate(Protocol):
    """Decides whether a tool call may proceed.

    Two implementations ship. ``PermissionGate`` (L1) wraps the legacy
    allow-by-default permission manager and is what an existing installation
    keeps using. ``PrefectGate`` (L2) is default-deny, classifies what the call
    touches, and writes every decision to the ledger. The port is the same,
    which is why the perimeter could be added without the loop noticing.
    """

    def permits(self, call: ToolCall) -> bool:
        """Return ``True`` if the call is allowed to run."""
        ...


@runtime_checkable
class Classifier(Protocol):
    """Assigns a sensitivity class to material entering the working set.

    Implementations must be *total* and must fail upward: material that cannot
    be classified is the most restrictive class, never the least. A classifier
    that returns ``PUBLIC`` when it does not know is not a weaker perimeter, it
    is no perimeter.
    """

    def classify_path(self, path: str) -> SensitivityClass:
        """Class implied by a filesystem path, before anything is read."""
        ...

    def classify_content(self, content: str) -> SensitivityClass:
        """Class implied by the content itself — identifiers, patterns, markers."""
        ...


@runtime_checkable
class ContentModel(Protocol):
    """A learned judgment of how sensitive a piece of text is.

    Consulted *in addition to* the policy's paths and patterns, never instead:
    the classifier takes the maximum, so a model can raise a class and never
    lower one. It may raise on any failure — the classifier turns an exception
    into the most restrictive class, because a judge that cannot answer is not
    evidence that the material is harmless.
    """

    def classify(self, text: str) -> SensitivityClass:
        """Class of ``text`` by what it says, not where it came from."""
        ...


@runtime_checkable
class PlacementEngine(Protocol):
    """Decides where a step may execute, or that it may not execute at all."""

    def place(self, klass: SensitivityClass, requirement: Requirement) -> Placement:
        """Choose a substrate for a step of ``klass``, or hold it.

        Implementations never raise to signal refusal: a refusal is a
        :class:`~runner.kernel.types.Placement` with outcome ``held``, because
        it has to be recorded like any other decision.
        """
        ...


@runtime_checkable
class Ledger(Protocol):
    """Append-only, tamper-evident record of every decision taken."""

    def record(
        self,
        kind: str,
        *,
        outcome: str,
        klass: SensitivityClass,
        detail: dict[str, object] | None = None,
        payload: str = "",
        substrate: str = "",
        rule_id: str = "",
        step_id: str = "",
    ) -> str:
        """Append one entry and return its step id."""
        ...
