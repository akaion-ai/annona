"""E3 — does an auditor holding only the ledger and the policy catch a faulty perimeter?

Each mutant is a bug a real placement runtime could plausibly ship. It is
injected into the running daemon; the ledger it writes is then audited by
``audit_compliance`` against the *official* policy file. The question is not
whether the bug leaks (most do, and the leak column shows it) but whether an
auditor who never saw the daemon can tell from the record.

Mutants:

``failover-widening``  when nothing permitted is up, place on anything that is —
                       the gateway behaviour, as a regression.
``stale-class``        decide placement on the class of the current payload,
                       forgetting what the run has already read.
``inverted-preference``pick the rule's *last* choice among permitted candidates.
``spurious-hold``      hold restricted work although a permitted substrate is up.
``policy-drift``       the daemon runs under an edited policy (frontier allowed
                       for restricted) while the file shown to the auditor is
                       the approved one.
``egress-ceiling-off`` the network-tool ceiling is not enforced.

A consistent lie — a mutant that also rewrites ``klass`` and ``candidates`` to
match — would pass: that is the stated limit, and why attestation of the writer
is future work.

Run: ``python -m experiments.compliance``.
"""

from __future__ import annotations

import dataclasses
import json
import random
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from experiments.harness import RESTRICTED, corpus, policy_document, run_task
from runner.audit.compliance import INVARIANTS, audit_compliance
from runner.kernel.types import Placement, SensitivityClass
from runner.policy.loader import parse_policy
from runner.policy.models import ToolPolicy
from runner.services.enforcement import Enforcement


def _wrap_place(fn: Callable[[Enforcement, Callable, SensitivityClass, object], Placement]):
    def patch(enforcement: Enforcement) -> None:
        original = enforcement.engine.place
        enforcement.engine.place = (  # type: ignore[method-assign]
            lambda klass, requirement=None: fn(enforcement, original, klass, requirement)
        )

    return patch


def _failover_widening(e, place, klass, req):
    p = place(klass, req)
    if p.outcome != "held":
        return p
    for sid in ("eu-cluster", "frontier"):
        if e.registry.is_up(sid):
            return dataclasses.replace(p, outcome="placed", substrate=sid, candidates=(sid,))
    return p


def _stale_class(e, place, klass, req):
    return place(SensitivityClass.PUBLIC, req)


def _inverted_preference(e, place, klass, req):
    p = place(klass, req)
    if p.outcome == "placed" and len(p.candidates) > 1:
        others = [c for c in p.candidates if c != p.substrate]
        return dataclasses.replace(p, substrate=others[-1])
    return p


def _spurious_hold(e, place, klass, req):
    p = place(klass, req)
    if p.outcome == "placed" and klass is RESTRICTED:
        return dataclasses.replace(p, outcome="held", substrate="", reason="(bug) held")
    return p


def _egress_off(enforcement: Enforcement) -> None:
    ToolPolicy.egress_ceiling = lambda self, tool: None  # type: ignore[method-assign]


MUTANTS: dict[str, dict] = {
    "none": {},
    "failover-widening": {"patch": _wrap_place(_failover_widening)},
    "stale-class": {"patch": _wrap_place(_stale_class)},
    "inverted-preference": {"patch": _wrap_place(_inverted_preference)},
    "spurious-hold": {"patch": _wrap_place(_spurious_hold)},
    "policy-drift": {"drift": True},
    "egress-ceiling-off": {"patch": _egress_off},
}


def _drifted(root: Path) -> dict:
    document = policy_document(root)
    document["substrates"][2]["max_class"] = "restricted"
    document["rules"][0]["allow"] = ["local-gpu", "frontier"]
    return document


def run(*, n: int = 150, seeds: int = 3, misfile: float = 0.2, down: float = 0.25) -> dict:
    original_ceiling = ToolPolicy.egress_ceiling
    results = {}
    for name, cfg in MUTANTS.items():
        tally: Counter[str] = Counter()
        by_invariant: Counter[str] = Counter()
        for seed in range(seeds):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                docs = corpus(root, n, seed=seed, misfile=misfile)
                files = {d.path: d.text for d in docs}
                rng = random.Random(2000 + seed)
                ledger = root / "ledger.jsonl"
                for i, doc in enumerate(docs):
                    out = run_task(
                        doc,
                        files,
                        root=root,
                        ledger=ledger,
                        run_id=f"{name}-{seed}-{i}",
                        local_down=rng.random() < down,
                        patch=cfg.get("patch"),
                        policy_override=_drifted(root) if cfg.get("drift") else None,
                    )
                    ToolPolicy.egress_ceiling = original_ceiling  # type: ignore[method-assign]
                    if out.true_class is RESTRICTED:
                        tally["restricted"] += 1
                        tally["leaked"] += bool(out.leaked_to)
                report = audit_compliance(ledger, parse_policy(policy_document(root)))
                tally["entries"] += report.entries
                for v in report.violations:
                    by_invariant[v.invariant] += 1
        results[name] = {
            "leak_rate": tally["leaked"] / tally["restricted"],
            "entries_audited": tally["entries"],
            "violations": {k: by_invariant[k] for k in INVARIANTS if by_invariant[k]},
            "detected": bool(by_invariant),
        }
        print(
            f"{name:<22} leak {results[name]['leak_rate']:.3f}  "
            f"violations {results[name]['violations']}",
            file=sys.stderr,
        )
    return {
        "experiment": "E3 placement compliance under injected faults",
        "params": {"n": n, "seeds": seeds, "misfile": misfile, "local_down": down},
        "results": results,
    }


if __name__ == "__main__":
    out = run()
    target = Path(__file__).parent / "results" / "compliance.json"
    target.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {target}", file=sys.stderr)
