"""E4 — tamper evidence, measured by exhaustive single-edit mutation of a real ledger.

For every entry of a ledger produced by the harness, apply each edit an insider
with write access could make — change a field, delete the line, swap it with the
next, truncate the file there, corrupt a byte — and check that ``verify_file``
fails and names the right entry. Two edits are *expected* to pass and are
reported as the stated limits rather than hidden: truncating the tail (a chain
prefix is a valid chain) and rewriting the whole chain from an edit onwards.
Both need an external anchor of the head hash, which is the open item in
``audit/ledger.py``.

Run: ``python -m experiments.tamper``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from experiments.harness import corpus, run_task
from runner.audit.ledger import LedgerEntry, verify_file
from runner.audit.witness import Checkpoint, verify_with_witness

FIELDS = ("outcome", "klass", "substrate", "rule_id", "payload_digest", "run_id", "ts")


def _alter(value: str) -> str:
    return (value[:-1] + ("X" if not value.endswith("X") else "Y")) if value else "X"


def _rechain(lines: list[str], start: int) -> list[str]:
    """What a careful insider does: re-hash from the edit to the end."""
    out = lines[:start]
    prev = json.loads(out[-1])["hash"] if out else "0" * 64
    for line in lines[start:]:
        raw = json.loads(line)
        raw["prev"] = prev
        raw.pop("hash")
        entry = LedgerEntry.from_json(json.dumps({**raw, "hash": ""})).sealed()
        out.append(entry.to_json())
        prev = entry.hash
    return out


def run(n_docs: int = 60) -> dict:
    root = Path(tempfile.mkdtemp())
    docs = corpus(root, n_docs, seed=0, misfile=0.2)
    files = {d.path: d.text for d in docs}
    ledger = root / "ledger.jsonl"
    for i, doc in enumerate(docs):
        run_task(doc, files, root=root, ledger=ledger, run_id=str(i))
    lines = ledger.read_text().splitlines()
    assert verify_file(ledger).ok

    tally: Counter[str] = Counter()
    located: Counter[str] = Counter()
    target = root / "edited.jsonl"

    def check(kind: str, edited: list[str], seq: int | None) -> None:
        target.write_text("\n".join(edited) + ("\n" if edited else ""))
        result = verify_file(target)
        tally[f"{kind}:trials"] += 1
        tally[f"{kind}:detected"] += not result.ok
        if not result.ok and seq is not None and result.at_seq in (seq, seq + 1):
            located[kind] += 1

    for i, line in enumerate(lines):
        seq = i + 1
        raw = json.loads(line)
        for field in FIELDS:
            edited = dict(raw, **{field: _alter(str(raw[field]))})
            check("field-edit", lines[:i] + [json.dumps(edited)] + lines[i + 1 :], seq)
        check("delete", lines[:i] + lines[i + 1 :], seq)
        if i + 1 < len(lines):
            check("swap", lines[:i] + [lines[i + 1], line] + lines[i + 2 :], seq)
        byte = hashlib.sha256(line.encode()).digest()[0] % len(line)
        check(
            "byte-flip",
            lines[:i]
            + [line[:byte] + chr(ord(line[byte]) ^ 1) + line[byte + 1 :]]
            + lines[i + 1 :],
            seq,
        )
        if 0 < i:
            check("truncate-tail", lines[:i], None)
            edited = dict(raw, outcome=_alter(raw["outcome"]))
            check(
                "edit-and-rechain",
                _rechain(lines[:i] + [json.dumps(edited)] + lines[i + 1 :], i),
                None,
            )

    # The two edits a chain cannot see, against a witness that kept a head every
    # k entries. What remains undetectable is exactly the window after the last
    # checkpoint — the exposure the publishing interval buys.
    heads = [Checkpoint(json.loads(x)["seq"], json.loads(x)["hash"]) for x in lines]
    witnessed = {}
    for k in (1, 10, 50):
        points = [h for h in heads if h.seq % k == 0]
        caught = {"truncate-tail": 0, "edit-and-rechain": 0}
        for i in range(1, len(lines)):
            for kind, edited in (
                ("truncate-tail", lines[:i]),
                (
                    "edit-and-rechain",
                    _rechain(
                        lines[:i]
                        + [
                            json.dumps(
                                dict(
                                    json.loads(lines[i]),
                                    outcome=_alter(json.loads(lines[i])["outcome"]),
                                )
                            )
                        ]
                        + lines[i + 1 :],
                        i,
                    ),
                ),
            ):
                target.write_text("\n".join(edited) + "\n")
                caught[kind] += not verify_with_witness(target, points)[0]
        witnessed[f"every_{k}"] = {
            kind: round(v / (len(lines) - 1), 4) for kind, v in caught.items()
        }

    kinds = sorted({k.split(":")[0] for k in tally})
    results = {
        k: {
            "trials": tally[f"{k}:trials"],
            "detected": tally[f"{k}:detected"],
            "rate": round(tally[f"{k}:detected"] / tally[f"{k}:trials"], 4),
            "located_at_edit": located[k],
        }
        for k in kinds
    }
    return {
        "experiment": "E4 tamper evidence",
        "entries": len(lines),
        "results": results,
        "with_witness": witnessed,
    }


if __name__ == "__main__":
    out = run()
    (Path(__file__).parent / "results" / "tamper.json").write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out["with_witness"], indent=1), file=sys.stderr)
