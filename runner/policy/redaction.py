"""Pseudonymisation: the fourth thing to do with material that may not cross (L2).

The perimeter has three answers when a step's class outranks every available
substrate — hold it, queue it, or send a locally written brief. This module adds
the fourth: **replace the identifiers and send the rest**.

    Il Sig. Mario Rossi, C.F. RSSMRA85T10A562S, chiede una proroga.
    ↓  locally
    Il Sig. [FULLNAME_1], C.F. [CF_1], chiede una proroga.

The mapping from placeholder back to real value stays on the machine, so the
answer that comes back can be re-identified here and nowhere else.

Three things this module is careful about, because each is a way the idea fails
in practice:

**Detection is a capability, not a policy.** The protocol below says what a
redactor must do; who does it — a 0.3B Italian PII model, a regex, a commercial
service — is wired at composition. That is why this file imports no HTTP client
and knows no vendor: swapping the detector must not touch the decision layer.

**Redacted output is reclassified, never trusted.** A redactor that missed an
identifier produces text that looks safe. The router classifies the result from
scratch and holds it if anything is still there, exactly as it does with a
brief. The redactor is the instrument; the perimeter remains the authority.

**Pseudonymous is not anonymous.** Under GDPR, replacing a name with a stable
token leaves personal data — the mapping exists and re-identification is
possible by design. This reduces exposure; it does not remove the need for a
lawful basis, and the documentation says so rather than implying otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from runner.kernel.types import SensitivityClass

__all__ = [
    "PLACEHOLDER_PATTERN",
    "Redaction",
    "RedactionPolicy",
    "Redactor",
    "class_for_labels",
    "restore",
]

PLACEHOLDER_PATTERN = re.compile(r"\[([A-Z_]+)_(\d+)\]")
"""What a placeholder looks like: ``[CF_1]``, ``[FULLNAME_2]``.

Matched rather than assumed, so a redactor that emits a different shape is
detected as producing nothing restorable instead of silently returning text with
tokens nobody can reverse.
"""


@dataclass(frozen=True, slots=True)
class Redaction:
    """The result of redacting one payload.

    ``mapping`` is the only part that must never leave the machine, and it is
    never written to the ledger — a record of what was replaced is a record of
    the material itself.
    """

    text: str
    mapping: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        """How many identifiers were replaced."""
        return sum(self.labels.values()) if self.labels else len(self.mapping)

    @property
    def changed(self) -> bool:
        return bool(self.mapping)

    def summary(self) -> dict[str, int]:
        """Counts by label, safe to record: no values, only kinds."""
        return dict(self.labels)


@runtime_checkable
class Redactor(Protocol):
    """Finds identifiers in text and replaces them with stable placeholders.

    Implementations must be deterministic within a run: the same value has to
    receive the same placeholder every time it appears, or the model on the
    other side loses the thread — "the client" in paragraph one and "the client"
    in paragraph four have to be recognisably the same person.
    """

    @property
    def name(self) -> str:
        """Identifier recorded in the ledger, e.g. ``rizzo-pii:0.3B``."""
        ...

    def analyse(self, text: str) -> Redaction:
        """Redact ``text``.

        Raises:
            runner.kernel.errors.BackendUnavailableError: the redactor could not
                be reached. The caller decides what that means; the policy's
                ``on_error`` setting is what makes it fail closed.
        """
        ...


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """How a policy wants redaction used.

    ``labels`` maps a detector's vocabulary onto this project's three classes,
    which is what keeps the two independent: rizzo-pii speaks in 22 Italian
    categories, Annona speaks in public/internal/restricted, and the translation
    is a policy decision an operator can read and change.
    """

    provider: str = ""
    endpoint: str = ""
    timeout: float = 15.0
    labels: Mapping[str, SensitivityClass] = field(default_factory=dict)
    default_label_class: SensitivityClass = SensitivityClass.INTERNAL
    classify: bool = False
    on_error: str = "hold"
    floor: SensitivityClass = SensitivityClass.PUBLIC
    """The lowest class redacted material may be reclassified to.

    Without a floor, redaction launders class. A memorandum is restricted
    *because it contains a codice fiscale*; remove the codice fiscale and the
    same text classifies as public, and a document that was refused a moment ago
    is cleared for a frontier model — with its subject matter untouched. The
    reclassification is still performed, and still able to hold the step; the
    floor stops it from concluding that material derived from a client file is
    public.

    ``public`` by default, because a floor above ``public`` makes redaction
    incapable of enabling anything: material would land back at the class it
    started from, which no substrate that refused it before will now accept. The
    knob exists for the deployment that wants the stricter reading — set it to
    ``internal`` and declare a frontier substrate that may hold internal
    material, and redacted work runs there and nowhere lower.

    The control that stops class laundering is not this field: it is
    ``egress.redact.allowed_for`` (empty by default) and ``egress.sealed``.
    """

    @property
    def enabled(self) -> bool:
        return bool(self.provider) and self.provider != "none"

    @property
    def fails_closed(self) -> bool:
        """Whether a detector outage stops the step rather than being ignored."""
        return self.on_error != "ignore"


def class_for_labels(
    labels: Mapping[str, int],
    policy: RedactionPolicy,
) -> SensitivityClass:
    """The class implied by the kinds of identifier found in a payload.

    The maximum over everything detected: a document containing a name and a
    tax code is as sensitive as the tax code, not as the average of the two.
    """
    klass = SensitivityClass.PUBLIC
    for label, count in labels.items():
        if not count:
            continue
        klass = max(klass, policy.labels.get(label.upper(), policy.default_label_class))
    return klass


def restore(text: str, mapping: Mapping[str, str]) -> str:
    """Put the real values back, locally.

    Longest placeholder first, so ``[CF_11]`` is not corrupted by a substitution
    for ``[CF_1]``. Deliberately a pure function in the decision layer: this is
    the step where material comes *back* into the perimeter, and it must not
    depend on a service being reachable — an answer that cannot be
    re-identified is an answer the user cannot use.
    """
    if not mapping:
        return text

    for placeholder in sorted(mapping, key=len, reverse=True):
        text = text.replace(placeholder, mapping[placeholder])
    return text


def unresolved_placeholders(text: str, mapping: Mapping[str, str]) -> tuple[str, ...]:
    """Placeholders in ``text`` that the mapping cannot resolve.

    A model that invents ``[FULLNAME_9]`` produces text that looks restored and
    is not. Naming them lets the caller decide — surface them, or refuse the
    answer — instead of shipping a sentence with a token in it.
    """
    found = {f"[{label}_{index}]" for label, index in PLACEHOLDER_PATTERN.findall(text)}
    return tuple(sorted(found - set(mapping)))
