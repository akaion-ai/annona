"""The placement decision (layer L2).

Given a class of material and what a step needs, produce the substrate it may
run on — or a refusal. This is the smallest module in the project and the one
the product is named after, so it is written to be read rather than to be
clever.

The algorithm, in full:

1. Find the first rule matching the class. **No rule means deny**, because the
   policy declares ``default: deny`` and a class nobody wrote a rule for is a
   class nobody thought about.
2. Take the substrates that rule allows, and keep the ones that (a) may hold
   this class, (b) can do what the step needs, and (c) are up. Everything
   dropped is recorded with the reason it was dropped.
3. If any survive, rank them by the rule's preference and take the first.
   Ties break on the order the rule lists them, so the same facts always give
   the same answer.
4. If none survive, apply ``on_unavailable``: hold, queue, or produce a brief —
   and never, under any of the three, widen the allowed set.

Step 4 is the whole product. Every gateway has a fallback list; the difference
is that a fallback list is ordered by availability, and this one cannot leave
the set the policy permits. **Failover may cost latency, money or model
quality. It may not cost jurisdiction.**
"""

from __future__ import annotations

from collections.abc import Callable

from runner.kernel.types import Placement, Requirement, SensitivityClass
from runner.placement.registry import SubstrateRegistry
from runner.policy.models import Policy, Rule, Substrate

__all__ = ["PlacementDecisionEngine"]


def _rank_key(prefer: str, order: dict[str, int]) -> Callable[[Substrate], tuple[float, ...]]:
    """Sort key for a preference, with the rule's order as the tie-break.

    The tie-break is not cosmetic: without it, two substrates with identical
    cost would be chosen by dictionary order, and the same policy would place
    the same step differently across processes. Reproducibility is a property an
    auditor tests.
    """
    if prefer == "cost":
        return lambda s: (s.cost_per_mtok, float(order.get(s.id, 999)))
    if prefer == "quality":
        return lambda s: (-float(s.quality), float(order.get(s.id, 999)))
    if prefer == "latency":
        # Latency is observed per call, not declared; until there is a
        # measurement, distance is the honest proxy — a nearer substrate is
        # rarely slower, and pretending to know better would be fiction.
        return lambda s: (float(s.distance), float(order.get(s.id, 999)))
    return lambda s: (float(s.distance), s.cost_per_mtok, float(order.get(s.id, 999)))


class PlacementDecisionEngine:
    """Decides where a step may run. Satisfies ``kernel.ports.PlacementEngine``."""

    def __init__(self, policy: Policy, registry: SubstrateRegistry) -> None:
        self._policy = policy
        self._registry = registry

    # ── Public API ────────────────────────────────────────────────────────────

    def place(self, klass: SensitivityClass, requirement: Requirement | None = None) -> Placement:
        """Choose a substrate for a step of ``klass``, or refuse.

        Never raises: a refusal is a :class:`~runner.kernel.types.Placement`
        with outcome ``held``, because it has to be recorded like any other
        decision, and an exception is not a record.
        """
        requirement = requirement or Requirement()
        rule = self._policy.rule_for(klass)

        if rule is None:
            return Placement(
                outcome="held",
                klass=klass,
                reason=(f"no rule covers class {klass.label}, and the policy is default-deny"),
                rejected=self._rejected_outside(klass, allowed=()),
            )

        eligible, rejected = self._eligible(klass, rule, requirement)
        rejected += self._rejected_outside(klass, allowed=rule.allow)

        if eligible:
            order = {sid: i for i, sid in enumerate(rule.allow)}
            # A caller may ask for the best permitted substrate rather than the
            # rule's usual preference. It reorders; it never widens.
            prefer = "quality" if requirement.prefer_quality else rule.prefer
            chosen = sorted(eligible, key=_rank_key(prefer, order))[0]
            return Placement(
                outcome="placed",
                klass=klass,
                substrate=chosen.id,
                rule_id=rule.id,
                reason=(
                    f"{rule.id} allows {chosen.id} for {klass.label}; prefer={prefer}"
                    + (" (asked for)" if requirement.prefer_quality else "")
                ),
                candidates=tuple(s.id for s in eligible),
                rejected=rejected,
            )

        return self._unavailable(klass, rule, rejected, requirement)

    def explain(self, klass: SensitivityClass, requirement: Requirement | None = None) -> str:
        """The decision as an operator reads it — the body of ``annona why``."""
        placement = self.place(klass, requirement)
        lines = [f"  class        {placement.klass.label}"]
        if placement.rule_id:
            rule = self._policy.rule_for(placement.klass)
            if rule:
                allow = ", ".join(rule.allow) or "nothing"
                lines.append(
                    f"  rule         {rule.id}  {klass.label} → [{allow}], "
                    f"on_unavailable: {rule.on_unavailable}"
                )
        lines.append(f"  candidates   {', '.join(placement.candidates) or 'none'}")
        for sid, why in placement.rejected:
            lines.append(f"  not chosen   {sid} — {why}")
        lines.append(f"  outcome      {placement.outcome}  {placement.reason}")
        return "\n".join(lines)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _eligible(
        self,
        klass: SensitivityClass,
        rule: Rule,
        requirement: Requirement,
    ) -> tuple[list[Substrate], tuple[tuple[str, str], ...]]:
        """Substrates that may take this step, and why the others may not."""
        eligible: list[Substrate] = []
        rejected: list[tuple[str, str]] = []

        for sid in rule.allow:
            substrate = self._registry.get(sid)
            if substrate is None:
                rejected.append((sid, "allowed by policy but not registered"))
                continue

            if not substrate.can_hold(klass):
                rejected.append((sid, f"max_class {substrate.max_class.label} < {klass.label}"))
                continue

            if requirement.tools and not substrate.tools:
                rejected.append((sid, "does not support tool use"))
                continue

            if requirement.vision and not substrate.vision:
                rejected.append((sid, "cannot read images"))
                continue

            if (
                requirement.min_context
                and substrate.context_window
                and substrate.context_window < requirement.min_context
            ):
                rejected.append(
                    (
                        sid,
                        f"context window {substrate.context_window} < "
                        f"{requirement.min_context} required",
                    )
                )
                continue

            health = self._registry.health(sid)
            if not health.up:
                rejected.append((sid, f"unhealthy: {health.reason}"))
                continue

            eligible.append(substrate)

        return eligible, tuple(rejected)

    def _rejected_outside(
        self,
        klass: SensitivityClass,
        *,
        allowed: tuple[str, ...],
    ) -> tuple[tuple[str, str], ...]:
        """Registered substrates the rule did not allow, and why they lost.

        Recorded because the interesting question after a hold is never "what
        did you use" but "why not the one that was sitting right there" — and
        because a substrate excluded by its ceiling is a different story from
        one excluded by an operator's rule.
        """
        rejected: list[tuple[str, str]] = []
        for sid, substrate in self._registry.substrates.items():
            if sid in allowed:
                continue
            if not substrate.can_hold(klass):
                rejected.append((sid, f"max_class {substrate.max_class.label} < {klass.label}"))
            else:
                rejected.append((sid, f"not allowed for class {klass.label} by policy"))
        return tuple(rejected)

    def _unavailable(
        self,
        klass: SensitivityClass,
        rule: Rule,
        rejected: tuple[tuple[str, str], ...],
        requirement: Requirement,
    ) -> Placement:
        """Apply ``on_unavailable`` — the three ways to not run something."""
        if requirement.sealed and rule.on_unavailable in ("brief", "redact"):
            # Sealed matter has no lowered form that may leave. A brief of an
            # M&A memorandum is still the deal; the same memorandum with the
            # names replaced is still the deal. Both are refused here rather
            # than being caught later by a classifier that can only see tokens.
            return Placement(
                outcome="held",
                klass=klass,
                rule_id=rule.id,
                reason=(
                    f"no permitted substrate is available and this material is sealed: "
                    f"{rule.on_unavailable} is an egress mechanism, and sealed matter has "
                    "no form that leaves"
                ),
                rejected=rejected,
            )

        if rule.on_unavailable == "queue":
            return Placement(
                outcome="queued",
                klass=klass,
                rule_id=rule.id,
                reason="no permitted substrate is available; queued until one returns",
                rejected=rejected,
            )

        if rule.on_unavailable == "redact":
            redaction = self._policy.redaction
            if not redaction.enabled:
                return Placement(
                    outcome="held",
                    klass=klass,
                    rule_id=rule.id,
                    reason="the rule asks for redaction and no redactor is configured",
                    rejected=rejected,
                )

            if not self._policy.egress.permits_redaction(klass):
                # The same gate the brief has always had, for the same reason.
                # Without it a policy could refuse to let a local model *summarise*
                # a restricted file and then permit the whole file to be sent with
                # the names swapped out — which is more material, not less.
                return Placement(
                    outcome="held",
                    klass=klass,
                    rule_id=rule.id,
                    reason=(
                        f"no permitted substrate is available and redaction is not "
                        f"permitted for class {klass.label}; removing identifiers does "
                        "not change what the text is about"
                    ),
                    rejected=rejected,
                )
            return Placement(
                outcome="redacted",
                klass=klass,
                rule_id=rule.id,
                reason=(
                    f"no permitted substrate is available; {redaction.provider} will "
                    "replace the identifiers, and the result is reclassified before it "
                    "may cross"
                ),
                rejected=rejected,
            )

        if rule.on_unavailable == "brief":
            egress = self._policy.egress
            producer = self._registry.get(egress.brief_produced_by)

            if not egress.permits_brief(klass):
                return Placement(
                    outcome="held",
                    klass=klass,
                    rule_id=rule.id,
                    reason=(
                        f"no permitted substrate is available and a brief is not "
                        f"permitted for class {klass.label}"
                    ),
                    rejected=rejected,
                )

            if producer is None or not producer.can_hold(klass):
                return Placement(
                    outcome="held",
                    klass=klass,
                    rule_id=rule.id,
                    reason=(
                        "no permitted substrate is available and the brief producer "
                        f"'{egress.brief_produced_by}' cannot hold {klass.label}"
                    ),
                    rejected=rejected,
                )

            if not self._registry.health(producer.id).up:
                return Placement(
                    outcome="held",
                    klass=klass,
                    rule_id=rule.id,
                    reason=(
                        "no permitted substrate is available and the brief producer "
                        f"'{producer.id}' is down"
                    ),
                    rejected=rejected,
                )

            return Placement(
                outcome="briefed",
                klass=klass,
                substrate=producer.id,
                rule_id=rule.id,
                reason=(
                    f"no permitted substrate is available; {producer.id} will produce a "
                    "brief, which is reclassified before it may cross"
                ),
                rejected=rejected,
                brief_of=producer.id,
            )

        return Placement(
            outcome="held",
            klass=klass,
            rule_id=rule.id,
            reason="no permitted substrate is available, and the rule holds rather than downgrades",
            rejected=rejected,
        )

    # ── Egress ────────────────────────────────────────────────────────────────

    def clears_egress(self, klass: SensitivityClass, substrate_id: str) -> tuple[bool, str]:
        """Final check immediately before bytes leave, by class and destination.

        Deliberately redundant with :meth:`place`. The placement decision is
        taken once per step against the class *as known then*; this runs against
        the payload that is actually about to be sent, after any brief, and it
        is the last thing between the process and the socket. Two checks that
        can only both be wrong together is the cheapest defence available.
        """
        substrate = self._registry.get(substrate_id)
        if substrate is None:
            return False, f"substrate {substrate_id!r} is not registered"
        if not substrate.can_hold(klass):
            return (
                False,
                f"{substrate_id} is capped at {substrate.max_class.label}, "
                f"payload is {klass.label}",
            )
        return True, ""
