"""Placement compliance: re-derive every decision from the ledger and the policy (layer L2).

``annona verify`` answers *was the record altered?* This answers the question an
auditor actually has: *was every decision in the record one the policy allowed,
and was it the decision the policy prescribed?* It needs the ledger and the
policy file, and nothing else — not the daemon, not its logs, not network
access — so it can run on the auditor's laptop.

Five invariants, each a property the paper's claims rest on:

``bound``
    Every run opens with the fingerprint of the policy it ran under, and it is
    the fingerprint of the policy being audited against.
``sound``
    Every permitted inference ran on a substrate the policy allows for its class
    and that may hold it.
``optimal``
    It ran on the substrate the rule's preference ranks first among the
    candidates recorded — the same facts always give the same answer.
``hold-justified``
    Every hold recorded no candidate: a step was refused only when no permitted
    substrate could take it.
``monotone``
    Within a run, inference is never placed below a class the run has already
    reached — by a cleared read, a tainting result or an earlier placement.
``egress``
    Every tool call that reached the network did so while the run's class was
    within that tool's ceiling.

What it cannot check is that the daemon wrote true entries: an entry that lies
consistently passes. That needs attestation of the code that wrote it (see
``design/hld.md`` §7.3), and the report says so.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from runner.audit.ledger import LedgerEntry, read_entries, verify_file
from runner.kernel.types import SensitivityClass, Subject
from runner.policy.fingerprint import policy_fingerprint
from runner.policy.models import Policy
from runner.policy.ranking import rank_key as _rank_key

__all__ = ["INVARIANTS", "ComplianceReport", "Violation", "audit_compliance"]

INVARIANTS = ("bound", "sound", "optimal", "hold-justified", "monotone", "egress")

_PREFER = re.compile(r"prefer=(\w+)")
_PLACED_ON_SUBSTRATE = ("placed",)
_PLACEMENT_KINDS = ("inference", "brief", "egress")
"""Kinds that record a placement. ``egress`` is a brief or redaction crossing, placed
under the class of what actually crossed."""


@dataclass(frozen=True, slots=True)
class Violation:
    invariant: str
    seq: int
    run_id: str
    detail: str

    def __str__(self) -> str:
        return f"#{self.seq} [{self.invariant}] run {self.run_id}: {self.detail}"


@dataclass(slots=True)
class ComplianceReport:
    entries: int = 0
    chain: str = ""
    checked: dict[str, int] = field(default_factory=lambda: dict.fromkeys(INVARIANTS, 0))
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and self.chain.endswith("0 gaps")

    def summary(self) -> str:
        lines = [f"chain        {self.chain}"]
        for name in INVARIANTS:
            bad = sum(1 for v in self.violations if v.invariant == name)
            lines.append(f"{name:<14}{self.checked[name]:>6} checked · {bad} violations")
        lines.append(
            "limit        entries are checked against the policy, not against what ran; "
            "a consistent lie passes without attestation"
        )
        return "\n".join(lines)


def audit_compliance(ledger_path: str | Path, policy: Policy) -> ComplianceReport:
    """Check every entry of ``ledger_path`` against ``policy``."""
    report = ComplianceReport(chain=str(verify_file(ledger_path)))
    expected = policy_fingerprint(policy)
    bound: set[str] = set()
    floor: dict[str, SensitivityClass] = defaultdict(lambda: SensitivityClass.PUBLIC)

    for entry in read_entries(ledger_path):
        report.entries += 1
        run = entry.run_id

        if entry.kind == "policy":
            report.checked["bound"] += 1
            got = str(entry.detail.get("fingerprint", ""))
            if got != expected:
                report.violations.append(
                    Violation(
                        "bound",
                        entry.seq,
                        run,
                        f"ran under policy {got[:12]}…, audited " f"against {expected[:12]}…",
                    )
                )
            else:
                bound.add(run)
            continue

        scoped = policy.for_subject(Subject(entry.subject, tuple(entry.groups)))
        klass = SensitivityClass.parse(entry.klass)

        # What the run has touched: a cleared read of restricted material, or a
        # result that raised the working set. Inference after it may not be
        # placed below it.
        touched = entry.kind in ("taint", "tool_call") and entry.outcome == "cleared"
        reaches = policy.tools.reaches_network(str(entry.detail.get("tool", "")))
        if touched and not entry.substrate.startswith("network:") and not reaches:
            floor[run] = max(floor[run], klass)

        if entry.kind in _PLACEMENT_KINDS:
            _check_placement(report, entry, scoped, klass, run in bound)
            if entry.kind == "inference" and entry.outcome in ("placed", "held", "queued"):
                report.checked["monotone"] += 1
                if klass < floor[run]:
                    report.violations.append(
                        Violation(
                            "monotone",
                            entry.seq,
                            run,
                            f"placed as {klass.label}, but the run had reached "
                            f"{floor[run].label}",
                        )
                    )
                floor[run] = max(floor[run], klass)

        elif entry.kind == "tool_call" and (
            entry.substrate.startswith("network:")
            or policy.tools.reaches_network(str(entry.detail.get("tool", "")))
        ):
            # Which tools reach the network is read from the audited policy, not
            # from the label the daemon wrote: a defect that mislabels a call
            # must not also hide it.
            _check_egress(report, entry, scoped)

    return report


def _check_placement(
    report: ComplianceReport,
    entry: LedgerEntry,
    policy: Policy,
    klass: SensitivityClass,
    bound: bool,
) -> None:
    run, seq = entry.run_id, entry.seq
    if not bound:
        report.checked["bound"] += 1
        report.violations.append(
            Violation("bound", seq, run, "decision in a run that never named its policy")
        )
    rule = policy.rule_for(klass)
    candidates = [str(c) for c in entry.detail.get("candidates", [])]

    if entry.outcome in _PLACED_ON_SUBSTRATE:
        report.checked["sound"] += 1
        substrate = policy.substrate(entry.substrate)
        if substrate is None:
            report.violations.append(
                Violation("sound", seq, run, f"placed on {entry.substrate!r}, not in the policy")
            )
            return
        if rule is None or entry.substrate not in rule.allow:
            report.violations.append(
                Violation(
                    "sound",
                    seq,
                    run,
                    f"{entry.substrate} is not allowed for {klass.label} by any rule",
                )
            )
            return
        if not substrate.can_hold(klass):
            report.violations.append(
                Violation(
                    "sound",
                    seq,
                    run,
                    f"{entry.substrate} has max_class {substrate.max_class.label} "
                    f"< {klass.label}",
                )
            )
            return

        if not candidates:
            return  # a crossing is placed by its own onward decision, recorded before it
        report.checked["optimal"] += 1
        permitted = [
            s
            for s in (policy.substrate(c) for c in candidates)
            if s is not None and s.id in rule.allow and s.can_hold(klass)
        ]
        if len(permitted) != len(candidates):
            report.violations.append(
                Violation("optimal", seq, run, "a recorded candidate is not permitted")
            )
            return
        match = _PREFER.search(str(entry.detail.get("reason", "")))
        prefer = match.group(1) if match else rule.prefer
        order = {sid: i for i, sid in enumerate(rule.allow)}
        best = sorted(permitted, key=_rank_key(prefer, order))[0].id if permitted else ""
        if best != entry.substrate:
            report.violations.append(
                Violation(
                    "optimal",
                    seq,
                    run,
                    f"placed on {entry.substrate}, but prefer={prefer} ranks {best} first",
                )
            )

    elif entry.outcome == "held":
        report.checked["hold-justified"] += 1
        if candidates:
            report.violations.append(
                Violation(
                    "hold-justified",
                    seq,
                    run,
                    f"held although {', '.join(candidates)} could take it",
                )
            )


def _check_egress(report: ComplianceReport, entry: LedgerEntry, policy: Policy) -> None:
    if entry.outcome != "cleared":
        return
    report.checked["egress"] += 1
    tool = str(entry.detail.get("tool", "")) or entry.substrate.split(":", 1)[-1]
    ceiling = policy.tools.egress_ceiling(tool)
    carried = max(
        SensitivityClass.parse(entry.klass),
        SensitivityClass.parse(str(entry.detail.get("run_class", "restricted"))),
    )
    if ceiling is None or carried > ceiling:
        limit = "none" if ceiling is None else ceiling.label
        report.violations.append(
            Violation(
                "egress",
                entry.seq,
                entry.run_id,
                f"{tool} reached the network at {carried.label}; ceiling {limit}",
            )
        )
