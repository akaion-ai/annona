"""Classification and the working set (layer L2).

Two objects, and the second is the one that matters.

:class:`PolicyClassifier` answers "how sensitive is this?" for a path or a piece
of content. It is deliberately cheap — globs and regexes, no model call — and
deliberately biased upward: material that matches nothing is the policy's
default class, which is the most restrictive class unless the operator says
otherwise.

:class:`WorkingSet` is the memory of everything a run has touched, and it is
where the project's central claim lives. Tool calls running locally is *not* the
same as data staying local: a tool result joins the transcript, and the
transcript travels with the next inference. So the class of a run only ever goes
up, and the next placement is decided against the whole accumulated set rather
than against the current step in isolation.

That is the monotonicity invariant, and it is one line of code and the reason
the perimeter cannot be walked around by ordering the steps cleverly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from runner.kernel.types import SensitivityClass, ToolCall, ToolResult
from runner.policy.models import Policy

__all__ = ["PolicyClassifier", "WorkingSet", "path_like_values", "paths_in_text"]

_PATH_IN_TEXT = re.compile(r"(?<![\w:])(?:~|/)[\w./\-]{2,}")
"""Absolute or home-relative paths appearing inside free text.

Deliberately conservative: it wants a leading ``/`` or ``~`` and at least three
characters, and the negative lookbehind keeps it off ``https://…``. Prose about
"the 50/50 split" does not become a filename.
"""


def paths_in_text(text: str) -> tuple[str, ...]:
    """Paths named in a payload, whether or not anything has read them yet.

    Naming a client file in a prompt is already an act with a class: the model
    is being pointed at material, and the request itself carries that
    information. Classifying only what a *tool* touched would place the first
    turn of "summarise /mnt/clients/BG-114.pdf" on a frontier API and only
    discover the problem afterwards.
    """
    return tuple(
        match.group(0)
        for match in _PATH_IN_TEXT.finditer(text or "")
        if "://" not in match.group(0)
    )


_PATH_KEYS = ("path", "file", "filename", "filepath", "directory", "dir", "target", "source")
"""Argument names that carry a filesystem path.

Named explicitly rather than sniffed: guessing that any string containing a
slash is a path classifies URLs and prose as files, and a classifier that cries
wolf is one an operator turns off.
"""


def path_like_values(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Every value in ``arguments`` that names a filesystem path.

    Nested one level deep, because tools take ``{"operation": "read", "path":
    ...}`` and occasionally ``{"target": {"path": ...}}``.
    """
    found: list[str] = []

    def visit(mapping: Mapping[str, Any], depth: int) -> None:
        for key, value in mapping.items():
            if isinstance(value, Mapping) and depth < 2:
                visit(value, depth + 1)
            elif isinstance(value, str) and key.lower() in _PATH_KEYS and value.strip():
                found.append(value)
            elif isinstance(value, list | tuple) and key.lower() in _PATH_KEYS:
                found.extend(str(v) for v in value if isinstance(v, str) and v.strip())

    visit(arguments, 0)
    return tuple(found)


class PolicyClassifier:
    """Classifies material against a policy. Satisfies ``kernel.ports.Classifier``."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    @property
    def default_class(self) -> SensitivityClass:
        return self._policy.default_class

    def classify_path(self, path: str) -> SensitivityClass:
        """Class implied by where something lives.

        Both the literal and the resolved path are considered by the policy, so
        a symlink from an innocuous directory into a protected one is classified
        by its target rather than by its name.
        """
        hit = self._policy.class_for_path(path)
        return hit if hit is not None else self._policy.default_class

    def classify_content(self, content: str) -> SensitivityClass:
        """Class implied by what something contains.

        Unlike :meth:`classify_path` this returns ``PUBLIC`` when nothing
        matches, because content is always classified *in addition to* its
        source: the caller takes the maximum, and defaulting both inputs to the
        policy default would make every string restricted.
        """
        if not content:
            return SensitivityClass.PUBLIC
        hit = self._policy.class_for_content(content)
        return hit if hit is not None else SensitivityClass.PUBLIC

    def classify_text(self, text: str) -> SensitivityClass:
        """Class of a whole payload: what it contains *and* what it points at.

        The maximum of the content patterns and the class of every path named in
        the text. This is what the router asks before a turn crosses anywhere.
        """
        klass = self.classify_content(text)
        for path in paths_in_text(text):
            klass = max(klass, self.classify_path(path))
        return klass

    def classify_call(self, call: ToolCall) -> SensitivityClass:
        """Class of what a call is about to touch, before it touches it."""
        klass = SensitivityClass.PUBLIC

        for path in path_like_values(call.arguments):
            klass = max(klass, self.classify_path(path))

        rendered = " ".join(str(v) for v in call.arguments.values())
        return max(klass, self.classify_content(rendered))

    def classify_result(self, result: ToolResult) -> SensitivityClass:
        """Class of what a call brought back into the transcript."""
        return self.classify_content(_render(result.content))


def _render(content: Any, *, limit: int = 200_000) -> str:
    """Flatten tool output to text for pattern matching.

    Truncated because a classifier that spends a second on a 40 MB file will be
    removed from the hot path by whoever owns latency, and a perimeter that is
    switched off has a leak rate of one. The limit is high enough that no
    realistic tool result is trimmed.
    """
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, Mapping):
        return " ".join(f"{k} {_render(v, limit=limit)}" for k, v in content.items())[:limit]
    if isinstance(content, list | tuple):
        return " ".join(_render(v, limit=limit) for v in content)[:limit]
    return str(content)[:limit]


class WorkingSet:
    """Everything this run has touched, and the class that follows from it.

    Monotone by construction: :meth:`observe` can only raise the class, never
    lower it. The provenance list is kept so ``annona why`` can name the file
    that made a run restricted — "class restricted (working set touched
    /mnt/pratiche/BG-114.pdf)" is the sentence an auditor needs, and it cannot
    be reconstructed after the fact from a class alone.
    """

    def __init__(self, initial: SensitivityClass = SensitivityClass.PUBLIC) -> None:
        self._klass = initial
        self._provenance: list[tuple[str, SensitivityClass]] = []
        self._sealed = ""

    @property
    def klass(self) -> SensitivityClass:
        return self._klass

    @property
    def provenance(self) -> tuple[tuple[str, SensitivityClass], ...]:
        """What raised the class, in the order it happened."""
        return tuple(self._provenance)

    @property
    def reason(self) -> str:
        """One line naming what put the working set at its current class."""
        raisers = [source for source, klass in self._provenance if klass == self._klass]
        if not raisers:
            return f"class {self._klass.label}"
        return f"class {self._klass.label} (working set touched {raisers[0]})"

    @property
    def sealed(self) -> str:
        """Why this run may not be transformed for egress, or ``""``.

        Kept here rather than on the backend for the same reason the class is:
        it is a fact about the *run*. A backend is constructed per composition
        and could be constructed twice; the working set is the one object whose
        whole job is to remember what has been touched, and forgetting a seal
        between turns would be the most expensive kind of amnesia.
        """
        return self._sealed

    def seal(self, reason: str) -> None:
        """Mark this run sealed. Like the class, this only ever goes one way."""
        if reason and not self._sealed:
            self._sealed = reason
            self.observe(reason, SensitivityClass.RESTRICTED)

    def observe(self, source: str, klass: SensitivityClass) -> SensitivityClass:
        """Fold one observation in and return the new class."""
        if klass > self._klass:
            self._klass = klass
        self._provenance.append((source, klass))
        return self._klass

    def observe_all(self, observations: Iterable[tuple[str, SensitivityClass]]) -> SensitivityClass:
        for source, klass in observations:
            self.observe(source, klass)
        return self._klass
