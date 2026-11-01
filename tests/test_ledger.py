"""The ledger: what it proves, and what it must refuse to hide.

Four ways to tamper with a chained log — rewrite an entry, delete one, reorder
two, truncate a line — and all four have to be caught by a command that contacts
nobody. The tests are written from the attacker's side: each one performs the
edit an insider would actually make after an incident, then asserts that
verification names it.

The other half is the boring half, and it is where audit trails really fail: the
chain must survive a restart, concurrent writers, and a process killed
mid-append.
"""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from runner.audit.ledger import GENESIS_PREV, Ledger, LedgerEntry, digest, verify_file
from runner.kernel.types import SensitivityClass

pytestmark = pytest.mark.unit

RESTRICTED = SensitivityClass.RESTRICTED
PUBLIC = SensitivityClass.PUBLIC


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger.jsonl", run_id="test-run", fsync=False)


def lines(path):
    return [line for line in path.read_text().splitlines() if line.strip()]


# ── Shape ─────────────────────────────────────────────────────────────────────


def test_an_empty_ledger_verifies(tmp_path):
    assert verify_file(tmp_path / "nothing.jsonl").ok


def test_the_first_entry_starts_the_chain(ledger):
    ledger.record("inference", outcome="placed", klass=PUBLIC, substrate="local")
    entry = ledger.entries()[0]

    assert entry.seq == 1
    assert entry.prev == GENESIS_PREV
    assert entry.hash == entry.compute_hash()


def test_entries_chain_to_their_predecessor(ledger):
    for _ in range(5):
        ledger.record("inference", outcome="placed", klass=PUBLIC, substrate="local")

    entries = ledger.entries()
    assert [e.seq for e in entries] == [1, 2, 3, 4, 5]
    for previous, current in zip(entries[:-1], entries[1:], strict=True):
        assert current.prev == previous.hash


def test_a_full_chain_verifies(ledger):
    for i in range(20):
        ledger.record("tool_call", outcome="cleared", klass=PUBLIC, detail={"i": i})

    result = ledger.verify()
    assert result.ok
    assert result.entries == 20
    assert "20 entries" in str(result)


def test_the_payload_is_never_stored_only_its_digest(ledger):
    """An audit trail that leaks what it audits is a liability with a nice name."""
    secret = "cliente RSSMRA85T10A562S, IBAN IT60X0542811101000000123456"
    ledger.record("egress", outcome="held", klass=RESTRICTED, payload=secret)

    raw = ledger.path.read_text()
    assert secret not in raw
    assert "RSSMRA85T10A562S" not in raw
    assert digest(secret) in raw


def test_the_digest_does_not_let_the_holder_test_a_guess(ledger):
    """An unkeyed SHA-256 let anyone holding the file hash "RSSMRA85T10A562S" and
    grep for it, and brute-force anything short (#39)."""
    guess = "RSSMRA85T10A562S"
    ledger.record("egress", outcome="held", klass=RESTRICTED, payload=guess)

    recorded = ledger.entries()[0].payload_digest
    assert recorded.startswith("hmac-sha256:")
    assert hashlib.sha256(guess.encode()).hexdigest() not in ledger.path.read_text()
    assert recorded == digest(guess)  # but this machine can still check it


def test_another_machine_gets_another_digest(tmp_path, monkeypatch):
    here = digest("RSSMRA85T10A562S")
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / "elsewhere"))
    assert digest("RSSMRA85T10A562S") != here


def test_the_key_is_made_once_and_readable_only_by_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / "home"))
    first = digest("x")
    key = tmp_path / "home" / "digest.key"
    assert key.stat().st_mode & 0o777 == 0o600
    assert len(key.read_bytes()) == 32
    assert digest("x") == first


def test_a_ledger_written_with_unkeyed_digests_still_verifies(tmp_path):
    """0.1.3 wrote bare hex; the chain hashes whatever is in the field."""
    path = tmp_path / "ledger.jsonl"
    old = LedgerEntry(
        seq=1,
        ts="2026-10-23T00:00:00+00:00",
        run_id="r",
        step_id="s",
        kind="egress",
        outcome="held",
        klass="restricted",
        payload_digest=hashlib.sha256(b"x").hexdigest(),
    ).sealed()
    path.write_text(old.to_json() + "\n")

    Ledger(path, fsync=False).record("egress", outcome="held", klass=RESTRICTED, payload="y")

    assert verify_file(path).ok
    assert verify_file(path).entries == 2


def test_a_refusal_is_recorded_like_any_other_decision(ledger):
    """A perimeter that only logs what it allowed cannot be audited."""
    ledger.record("inference", outcome="held", klass=RESTRICTED, detail={"reason": "no substrate"})
    assert ledger.entries()[0].outcome == "held"


def test_the_step_id_is_minted_by_the_ledger_and_findable(ledger):
    step_id = ledger.record("inference", outcome="placed", klass=PUBLIC, substrate="local")
    assert ledger.find(step_id) is not None
    assert ledger.find("step_does_not_exist") is None


# ── Tampering ─────────────────────────────────────────────────────────────────


def test_rewriting_an_entry_is_detected(ledger):
    """The edit an insider makes: change 'held' to 'placed' after the fact."""
    for _ in range(3):
        ledger.record("inference", outcome="held", klass=RESTRICTED)

    path = ledger.path
    content = lines(path)
    entry = json.loads(content[1])
    entry["outcome"] = "placed"
    content[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(content) + "\n")

    result = verify_file(path)
    assert not result.ok
    assert result.at_seq == 2
    assert "altered" in result.problem


def test_deleting_an_entry_is_detected(ledger):
    for _ in range(3):
        ledger.record("inference", outcome="placed", klass=PUBLIC)

    content = lines(ledger.path)
    del content[1]
    ledger.path.write_text("\n".join(content) + "\n")

    result = verify_file(ledger.path)
    assert not result.ok
    assert "removed or reordered" in result.problem


def test_reordering_entries_is_detected(ledger):
    for _ in range(3):
        ledger.record("inference", outcome="placed", klass=PUBLIC)

    content = lines(ledger.path)
    content[0], content[1] = content[1], content[0]
    ledger.path.write_text("\n".join(content) + "\n")

    assert not verify_file(ledger.path).ok


def test_a_corrupt_line_is_detected_not_skipped(ledger):
    """A half-written line after a power cut must fail verification, loudly."""
    ledger.record("inference", outcome="placed", klass=PUBLIC)
    with ledger.path.open("a") as handle:
        handle.write('{"seq": 2, "ts": "2026-')

    result = verify_file(ledger.path)
    assert not result.ok
    assert "not a valid entry" in result.problem


def test_an_entry_with_unknown_fields_is_rejected(tmp_path):
    """Extra fields are not covered by the hash, so they are not accepted."""
    path = tmp_path / "ledger.jsonl"
    entry = LedgerEntry(
        seq=1, ts="t", run_id="r", step_id="s", kind="k", outcome="o", klass="public"
    )
    payload = json.loads(entry.sealed().to_json())
    payload["injected"] = "anything"
    path.write_text(json.dumps(payload) + "\n")

    assert not verify_file(path).ok


def test_replacing_the_whole_chain_with_a_valid_one_is_not_claimed_to_be_detected(tmp_path):
    """Honesty about the boundary of the claim.

    A chain rebuilt from scratch by someone with write access is internally
    consistent and this file cannot tell. Detecting that needs an external
    anchor for the head hash, which is stated as open in the HLD rather than
    quietly implied here.
    """
    original = Ledger(tmp_path / "ledger.jsonl", fsync=False)
    original.record("inference", outcome="held", klass=RESTRICTED)
    head_before = original.head

    tmp_path.joinpath("ledger.jsonl").unlink()
    forged = Ledger(tmp_path / "ledger.jsonl", fsync=False)
    forged.record("inference", outcome="placed", klass=PUBLIC)

    assert verify_file(tmp_path / "ledger.jsonl").ok
    assert forged.head != head_before, "which is the only trace an anchor would catch"


# ── Durability ────────────────────────────────────────────────────────────────


def test_a_restart_continues_the_chain(tmp_path):
    first = Ledger(tmp_path / "ledger.jsonl", fsync=False)
    first.record("inference", outcome="placed", klass=PUBLIC)
    first.record("inference", outcome="placed", klass=PUBLIC)

    second = Ledger(tmp_path / "ledger.jsonl", fsync=False)
    second.record("inference", outcome="placed", klass=PUBLIC)

    result = verify_file(tmp_path / "ledger.jsonl")
    assert result.ok and result.entries == 3
    assert second.entries()[-1].seq == 3


def test_concurrent_writers_do_not_break_the_chain(tmp_path):
    """The daemon serves the local API and a run at the same time.

    Two interleaved appends claiming the same predecessor would look exactly
    like tampering to an auditor, which is a spectacular way to lose trust in a
    working system.
    """
    ledger = Ledger(tmp_path / "ledger.jsonl", fsync=False)
    errors: list[Exception] = []

    def writer(index: int):
        try:
            for _ in range(25):
                ledger.record("tool_call", outcome="cleared", klass=PUBLIC, detail={"w": index})
        except Exception as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    result = verify_file(ledger.path)
    assert result.ok, result.problem
    assert result.entries == 100


def test_a_damaged_ledger_is_not_silently_replaced(tmp_path):
    """Appending a fresh chain over a damaged one destroys the evidence."""
    path = tmp_path / "ledger.jsonl"
    path.write_text("this is not json\n")

    with pytest.raises((ValueError, json.JSONDecodeError)):
        Ledger(path, fsync=False)


# ── Reporting ─────────────────────────────────────────────────────────────────


def test_the_summary_counts_what_an_operator_asks_about(ledger):
    ledger.record("inference", outcome="placed", klass=PUBLIC, substrate="frontier")
    ledger.record("inference", outcome="placed", klass=PUBLIC, substrate="local-gpu")
    ledger.record("inference", outcome="held", klass=RESTRICTED)

    summary = ledger.summary()
    assert summary["placements"] == {"frontier": 1, "local-gpu": 1}
    assert summary["outcomes"] == {"placed": 2, "held": 1}
    assert summary["classes"] == {"public": 2, "restricted": 1}
