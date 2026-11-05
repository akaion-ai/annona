"""A published head closes the two edits a hash chain cannot see."""

from __future__ import annotations

import json

import pytest

from runner.audit.ledger import Ledger
from runner.audit.witness import publish, verify_with_witness
from runner.kernel.types import SensitivityClass

pytestmark = [pytest.mark.unit]


def _ledger(path, n):
    ledger = Ledger(path, run_id="w", fsync=False)
    for i in range(n):
        ledger.record(
            "inference", outcome="placed", klass=SensitivityClass.PUBLIC, substrate=f"s{i}"
        )


def test_an_untouched_ledger_holds_its_checkpoints(tmp_path):
    _ledger(tmp_path / "l.jsonl", 5)
    publish(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    ok, why = verify_with_witness(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    assert ok, why


def test_truncation_below_a_checkpoint_is_caught(tmp_path):
    _ledger(tmp_path / "l.jsonl", 5)
    publish(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    lines = (tmp_path / "l.jsonl").read_text().splitlines()
    (tmp_path / "l.jsonl").write_text("\n".join(lines[:3]) + "\n")
    ok, why = verify_with_witness(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    assert not ok and "truncated" in why


def test_a_rehashed_rewrite_before_a_checkpoint_is_caught(tmp_path):
    from experiments.tamper import _rechain

    _ledger(tmp_path / "l.jsonl", 5)
    publish(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    lines = (tmp_path / "l.jsonl").read_text().splitlines()
    edited = dict(json.loads(lines[1]), outcome="held")
    rewritten = _rechain(lines[:1] + [json.dumps(edited)] + lines[2:], 1)
    (tmp_path / "l.jsonl").write_text("\n".join(rewritten) + "\n")
    ok, why = verify_with_witness(tmp_path / "l.jsonl", tmp_path / "w.jsonl")
    assert not ok and "rewritten" in why
