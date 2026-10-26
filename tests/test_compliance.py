"""The compliance auditor: silent on a correct perimeter, loud on each injected fault.

A small version of ``experiments/compliance.py`` (E3), kept in the suite so the
detection result cannot regress without a red test.
"""

from __future__ import annotations

import random

import pytest

from experiments import compliance as e3
from experiments.harness import corpus, policy_document, run_task
from runner.audit.compliance import audit_compliance
from runner.policy.loader import parse_policy
from runner.policy.models import ToolPolicy

pytestmark = [pytest.mark.integration]

EXPECTED = {
    "none": set(),
    "failover-widening": {"sound"},
    "stale-class": {"monotone"},
    "inverted-preference": {"optimal"},
    "spurious-hold": {"hold-justified"},
    "policy-drift": {"bound"},
    "egress-ceiling-off": {"egress"},
}


@pytest.mark.parametrize("mutant", list(EXPECTED))
def test_each_fault_is_caught_by_the_invariant_that_names_it(tmp_path, mutant):
    cfg = e3.MUTANTS[mutant]
    original = ToolPolicy.egress_ceiling
    docs = corpus(tmp_path, 30, seed=7, misfile=0.2)
    files = {d.path: d.text for d in docs}
    rng = random.Random(7)
    ledger = tmp_path / "ledger.jsonl"
    try:
        for i, doc in enumerate(docs):
            run_task(
                doc,
                files,
                root=tmp_path,
                ledger=ledger,
                run_id=f"{mutant}-{i}",
                local_down=rng.random() < 0.3,
                patch=cfg.get("patch"),
                policy_override=e3._drifted(tmp_path) if cfg.get("drift") else None,
            )
            ToolPolicy.egress_ceiling = original  # type: ignore[method-assign]
    finally:
        ToolPolicy.egress_ceiling = original  # type: ignore[method-assign]

    report = audit_compliance(ledger, parse_policy(policy_document(tmp_path)))
    found = {v.invariant for v in report.violations}
    if EXPECTED[mutant]:
        assert EXPECTED[mutant] <= found, report.summary()
    else:
        assert not found, [str(v) for v in report.violations[:5]]
        assert report.ok


def test_a_ledger_audited_against_another_policy_is_unbound(tmp_path):
    docs = corpus(tmp_path, 3, seed=1)
    files = {d.path: d.text for d in docs}
    ledger = tmp_path / "ledger.jsonl"
    run_task(docs[0], files, root=tmp_path, ledger=ledger, run_id="r")
    other = policy_document(tmp_path)
    other["rules"][2]["prefer"] = "cost"
    report = audit_compliance(ledger, parse_policy(other))
    assert any(v.invariant == "bound" for v in report.violations)


def test_the_fingerprint_is_the_policy_not_the_file(tmp_path):
    import yaml

    from runner.policy.fingerprint import policy_fingerprint
    from runner.policy.loader import load_policy

    document = policy_document(tmp_path)
    (tmp_path / "policy.yaml").write_text("# a comment\n" + yaml.safe_dump(document))
    from_file = policy_fingerprint(load_policy(tmp_path / "policy.yaml"))
    assert from_file == policy_fingerprint(parse_policy(document))

    document["substrates"][2]["max_class"] = "internal"
    assert policy_fingerprint(parse_policy(document)) != from_file
