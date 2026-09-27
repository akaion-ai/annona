"""Checkpoints of the ledger's head, kept somewhere the daemon cannot rewrite (layer L2).

A hash chain proves that nobody edited the *middle* of the record. It cannot
prove that the record was not cut short, or rewritten from some point onwards
by someone who re-hashed every later line — a chain prefix is a valid chain, and
so is a re-hashed suffix. Both are closed the way Certificate Transparency
closes them: somebody else remembers a head.

A checkpoint is ``(seq, hash)`` of one entry. It is published to a *witness* the
ledger's writer cannot edit — the control plane, the auditor's mailbox, a git
repository with signed commits, a transparency log. Verification then asks one
extra question per checkpoint: is this exact entry still in the ledger, at this
position, with this hash? Truncation below a checkpoint removes it; a rewrite
before a checkpoint changes its hash.

The witness file format is JSON Lines of ``{"seq": n, "hash": h}``. This module
writes to a path; publishing that path somewhere trustworthy is deployment.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from runner.audit.ledger import read_entries, verify_file

__all__ = ["Checkpoint", "checkpoint", "publish", "verify_with_witness"]


@dataclass(frozen=True, slots=True)
class Checkpoint:
    seq: int
    hash: str


def checkpoint(ledger_path: str | Path) -> Checkpoint | None:
    """The current head of the ledger, or ``None`` if it is empty."""
    head = None
    for entry in read_entries(ledger_path):
        head = Checkpoint(entry.seq, entry.hash)
    return head


def publish(ledger_path: str | Path, witness_path: str | Path) -> Checkpoint | None:
    """Append the current head to the witness file."""
    head = checkpoint(ledger_path)
    if head is not None:
        with Path(witness_path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": head.seq, "hash": head.hash}) + "\n")
    return head


def _load(witness: str | Path | Iterable[Checkpoint]) -> list[Checkpoint]:
    if isinstance(witness, str | Path):
        path = Path(witness)
        if not path.exists():
            return []
        return [Checkpoint(**json.loads(line)) for line in path.read_text().splitlines() if line]
    return list(witness)


def verify_with_witness(
    ledger_path: str | Path, witness: str | Path | Iterable[Checkpoint]
) -> tuple[bool, str]:
    """The chain is intact *and* contains every published checkpoint."""
    chain = verify_file(ledger_path)
    if not chain.ok:
        return False, str(chain)
    by_seq = {entry.seq: entry.hash for entry in read_entries(ledger_path)}
    for point in _load(witness):
        if point.seq not in by_seq:
            return False, f"checkpoint #{point.seq} is missing: the ledger was truncated"
        if by_seq[point.seq] != point.hash:
            return False, f"checkpoint #{point.seq} differs: the ledger was rewritten before it"
    return True, f"{chain} · {len(_load(witness))} checkpoints hold"
