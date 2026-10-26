"""Default-deny clearance for tool calls (layer L2).

This is the inversion. The legacy ``PermissionManager`` permitted any tool it
did not recognise and treated an empty allow-list as "allow everything"; both
are defensible in a personal utility and indefensible in a perimeter. Here a
tool that is not named in the policy does not run, and a named tool asked to
touch a path outside its allow-list does not run either.

Three properties are worth stating because they are the ones a reviewer should
check:

**Deny wins.** The deny-list is evaluated before the allow-list, so adding a
path to a tool's allow-list cannot accidentally expose ``~/.ssh``.

**Symlinks are resolved.** A path is matched both literally and resolved, so a
link from an allowed directory to a protected one is refused by its target.

**A tool that reaches the network is an egress.** Its arguments leave the
machine, and the router — which guards inference — never sees them. So a
network tool runs only while the run's class is at or below the tool's ceiling
(``tools.egress``; ``public`` for the shipped network tools), and the ledger
records it on a ``network:<tool>`` substrate rather than ``local``.

**A refusal does not taint the run.** The working set is only advanced when a
call is *permitted*, because nothing entered the transcript otherwise. Raising
the class on refused attempts would let a hostile plan escalate a run to
restricted by asking for files it was never going to get.
"""

from __future__ import annotations

from runner.audit.ledger import Ledger
from runner.kernel.types import Clearance, SensitivityClass, ToolCall
from runner.policy.classifier import PolicyClassifier, WorkingSet, path_like_values
from runner.policy.models import Policy

__all__ = ["DefaultDenyGate"]


class DefaultDenyGate:
    """Clears or refuses tool calls. Satisfies ``kernel.ports.PolicyGate``."""

    def __init__(
        self,
        policy: Policy,
        classifier: PolicyClassifier,
        working_set: WorkingSet,
        ledger: Ledger | None = None,
    ) -> None:
        self._policy = policy
        self._classifier = classifier
        self._working_set = working_set
        self._ledger = ledger

    # ── Port ──────────────────────────────────────────────────────────────────

    def permits(self, call: ToolCall) -> bool:
        """Whether this call may run. The loop's view of the decision."""
        return self.clear(call).permitted

    # ── Full decision ─────────────────────────────────────────────────────────

    def clear(self, call: ToolCall) -> Clearance:
        """Decide, record, and return the decision with its reason."""
        clearance = self._decide(call)

        if clearance.permitted:
            self._advance_working_set(call, clearance.klass)

        if self._ledger is not None:
            self._ledger.record(
                "tool_call",
                outcome=clearance.outcome,
                klass=clearance.klass,
                rule_id=clearance.rule_id,
                substrate=(
                    f"network:{call.name}"
                    if self._policy.tools.reaches_network(call.name)
                    else "local"
                ),
                payload=repr(sorted(call.arguments.items())),
                detail={
                    "tool": call.name,
                    "reason": clearance.reason,
                    "run_class": self._working_set.klass.label,
                    "paths": list(path_like_values(call.arguments)),
                },
            )

        return clearance

    def _decide(self, call: ToolCall) -> Clearance:
        klass = self._classifier.classify_call(call)
        tools = self._policy.tools

        if not tools.permits(call.name):
            return Clearance(
                permitted=False,
                klass=klass,
                reason=f"tool '{call.name}' is not on the allow-list (policy is default-deny)",
                rule_id="tools.allow",
            )

        for path in path_like_values(call.arguments):
            allowed, why = tools.permits_path(call.name, path)
            if not allowed:
                return Clearance(
                    permitted=False,
                    klass=klass,
                    reason=f"{call.name} may not touch {path}: {why}",
                    rule_id="tools.deny_paths" if "deny-list" in why else "tools.allow",
                )

        ceiling = tools.egress_ceiling(call.name)
        if ceiling is not None:
            carried = max(klass, self._working_set.klass)
            if carried > ceiling:
                return Clearance(
                    permitted=False,
                    klass=klass,
                    reason=(
                        f"{call.name} sends its arguments off this machine; the run is "
                        f"{carried.label} and the tool's ceiling is {ceiling.label}"
                    ),
                    rule_id="tools.egress",
                )

        return Clearance(
            permitted=True,
            klass=klass,
            reason=f"{call.name} is allowed for these paths",
            rule_id="tools.allow",
        )

    def _advance_working_set(self, call: ToolCall, klass: SensitivityClass) -> None:
        """Fold what this call is about to read into the run's class.

        Done before execution rather than after, so that a run which is killed
        between the read and the next inference has already recorded that it
        became sensitive.
        """
        paths = path_like_values(call.arguments)
        if paths:
            for path in paths:
                self._working_set.observe(path, self._classifier.classify_path(path))
        elif klass > SensitivityClass.PUBLIC:
            self._working_set.observe(f"{call.name} arguments", klass)
