"""The ledger: an append-only, hash-chained record of every decision (layer L2).

The claim this module makes is narrow and worth stating precisely, because
security features are usually sold wider than they are built.

**It claims:** no entry can be altered, reordered or removed without the change
being detectable by anyone holding the file, using a command that contacts
nobody.

**It also claims:** that the file does not let its holder test a guess. What
crossed is recorded as an HMAC under a key that stays on this machine
(``$AKAION_HOME/digest.key``), not as a bare SHA-256 — anybody can hash
"RSSMRA80A01H501U" and grep for it, and a short text can be brute-forced (#39).
Entries written before the key existed carry a bare hex digest and still verify:
the chain hashes whatever string is in the field.

**It does not claim:** that the daemon ran the code it says it ran, or that an
attacker with write access could not append plausible new entries after
truncating the file. The first needs hardware attestation; the second needs an
external anchor for the head hash. Both are named in the HLD as open, and
neither is quietly implied here.

Format is JSON Lines, one entry per line, because the file has to survive being
copied to an auditor, read by `grep`, and appended to by a process that may be
killed mid-write. A partially written last line fails verification as a
malformed entry rather than corrupting the chain.

Chaining is the standard construction::

    entry.hash = sha256(canonical_json(entry without hash))
    entry.prev = hash of the previous entry, or 64 zeros for the genesis entry
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runner.audit.metrics import METRICS, hold_reason
from runner.kernel.types import SensitivityClass, Subject

__all__ = [
    "DIGEST_PREFIX",
    "GENESIS_PREV",
    "Ledger",
    "LedgerEntry",
    "VerificationResult",
    "digest",
    "read_entries",
    "verify_file",
]

GENESIS_PREV = "0" * 64
"""Predecessor hash of the first entry. Fixed, so a chain has one valid start."""


DIGEST_PREFIX = "hmac-sha256:"
"""Marks a keyed digest. A bare 64-hex digest is the unkeyed one 0.1.3 wrote."""


def _digest_key_path() -> Path:
    home = os.getenv("AKAION_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".akaion") / "digest.key"


@functools.lru_cache(maxsize=4)
def _digest_key(path: Path) -> bytes:
    """This machine's digest key: 32 random bytes, created once, mode 0600.

    Beside the credentials rather than beside the ledger, so the folder a
    ledger is copied out of does not carry the key with it. Its own file, not
    the one that encrypts ``auth.json``: one key, one job, and signing out or
    rotating credentials must not make old digests uncheckable.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process made it first; use theirs
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(os.urandom(32))
    return path.read_bytes()


def digest(payload: str) -> str:
    """A keyed digest of a payload, as recorded instead of the payload itself.

    The ledger never stores what crossed, only a digest of it. A record of
    sensitive material is still sensitive material, and an audit trail that
    leaks the thing it is auditing is a liability with a nice name — which an
    unkeyed hash is, for anything short enough to guess (#39).
    """
    key = _digest_key(_digest_key_path())
    return DIGEST_PREFIX + hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical(mapping: Mapping[str, Any]) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, UTF-8."""
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One decision, as it appears on disk."""

    seq: int
    ts: str
    run_id: str
    step_id: str
    kind: str
    outcome: str
    klass: str
    substrate: str = ""
    rule_id: str = ""
    payload_digest: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    prev: str = GENESIS_PREV
    hash: str = ""
    subject: str = ""
    """Who asked, as proven by an identity provider. Empty: anonymous."""
    groups: tuple[str, ...] = ()

    def body(self) -> dict[str, Any]:
        """Everything the hash covers — that is, everything except the hash.

        ``subject`` and ``groups`` are left out when empty, so an anonymous entry
        hashes exactly as every entry did before subjects existed and older
        ledgers still verify. When present they are inside the hash: rewriting
        who asked breaks the chain like rewriting what was decided.
        """
        data = asdict(self)
        data.pop("hash", None)
        data["detail"] = dict(self.detail)
        data["groups"] = list(self.groups)
        for key in ("subject", "groups"):
            if not data[key]:
                del data[key]
        return data

    def compute_hash(self) -> str:
        return hashlib.sha256(_canonical(self.body()).encode("utf-8")).hexdigest()

    def sealed(self) -> LedgerEntry:
        """A copy carrying its own hash."""
        return replace(self, hash=self.compute_hash())

    def to_json(self) -> str:
        return _canonical({**self.body(), "hash": self.hash})

    @classmethod
    def from_json(cls, line: str) -> LedgerEntry:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("ledger entry is not an object")
        known = set(cls.__slots__)  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown fields in ledger entry: {', '.join(sorted(unknown))}")
        if "groups" in raw:
            raw["groups"] = tuple(raw["groups"])
        return cls(**raw)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """The outcome of checking a chain."""

    ok: bool
    entries: int
    problem: str = ""
    at_seq: int | None = None

    def __str__(self) -> str:
        if self.ok:
            return f"{self.entries} entries · chain intact · 0 gaps"
        where = f" at entry #{self.at_seq}" if self.at_seq is not None else ""
        return f"chain BROKEN{where}: {self.problem}"


def read_entries(path: str | Path) -> Iterator[LedgerEntry]:
    """Yield every entry in a ledger file, in order."""
    p = Path(path).expanduser()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield LedgerEntry.from_json(line)


def verify_file(path: str | Path) -> VerificationResult:
    """Check that a ledger file is an unbroken chain.

    Four failure modes are distinguished, because "the ledger is broken" is not
    an actionable sentence: a rewritten entry, a removed entry, a reordered
    entry, and a corrupt line each name a different incident.
    """
    prev = GENESIS_PREV
    expected_seq = 1
    count = 0

    p = Path(path).expanduser()
    if not p.exists():
        return VerificationResult(ok=True, entries=0)

    with p.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue

            try:
                entry = LedgerEntry.from_json(line)
            except (ValueError, json.JSONDecodeError) as exc:
                return VerificationResult(
                    ok=False,
                    entries=count,
                    problem=f"line {line_number} is not a valid entry: {exc}",
                    at_seq=expected_seq,
                )

            if entry.seq != expected_seq:
                return VerificationResult(
                    ok=False,
                    entries=count,
                    problem=f"sequence jumps to {entry.seq}, expected {expected_seq} "
                    "(an entry was removed or reordered)",
                    at_seq=entry.seq,
                )

            if entry.prev != prev:
                return VerificationResult(
                    ok=False,
                    entries=count,
                    problem="predecessor hash does not match the previous entry "
                    "(an earlier entry was altered or removed)",
                    at_seq=entry.seq,
                )

            if entry.hash != entry.compute_hash():
                return VerificationResult(
                    ok=False,
                    entries=count,
                    problem="entry hash does not match its contents (this entry was altered)",
                    at_seq=entry.seq,
                )

            prev = entry.hash
            expected_seq += 1
            count += 1

    return VerificationResult(ok=True, entries=count)


class Ledger:
    """Appends decisions to a chain on disk.

    Durability over throughput: every entry is flushed and ``fsync``-ed before
    the call returns. A decision that is not on disk when the machine loses
    power is a decision that did not happen as far as an auditor is concerned,
    and the perimeter takes single-digit decisions per second, not thousands.

    Thread-safe through a lock, because the daemon serves the local API and a
    run at the same time, and two interleaved appends would produce two entries
    claiming the same predecessor — which verification would then report as
    tampering.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        fsync: bool = True,
        subject: Subject | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._subject = subject or Subject()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id or uuid.uuid4().hex[:12]
        self._fsync = fsync
        self._lock = threading.Lock()
        self._seq, self._prev = self._resume()

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def head(self) -> str:
        """Hash of the last entry — the value worth anchoring externally."""
        return self._prev

    def _resume(self) -> tuple[int, str]:
        """Continue an existing chain, or start one.

        A ledger that cannot be read is not silently replaced: appending a fresh
        chain over a damaged one destroys the evidence that it was damaged.
        """
        seq, prev = 0, GENESIS_PREV
        for entry in read_entries(self._path):
            seq, prev = entry.seq, entry.hash
        return seq, prev

    # ── Appending ─────────────────────────────────────────────────────────────

    def record(
        self,
        kind: str,
        *,
        outcome: str,
        klass: SensitivityClass,
        detail: Mapping[str, Any] | None = None,
        payload: str = "",
        substrate: str = "",
        rule_id: str = "",
        step_id: str = "",
    ) -> str:
        """Append one entry and return its step id.

        The step id is what ``annona why`` takes, so it is returned rather than
        generated by the caller: an identifier the ledger did not mint is an
        identifier the ledger cannot find.
        """
        # Counted where it is recorded, so the numbers cannot drift from the
        # ledger: decisions_total{kind="retrieval"} *is* the memory's usage.
        METRICS.inc("annona_decisions_total", kind=kind, outcome=outcome, **{"class": klass.label})
        if outcome == "held":
            METRICS.inc(
                "annona_holds_total", reason=hold_reason(str((detail or {}).get("reason", "")))
            )
        with self._lock:
            seq = self._seq + 1
            entry = LedgerEntry(
                seq=seq,
                ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                run_id=self._run_id,
                step_id=step_id or f"step_{uuid.uuid4().hex[:6]}",
                kind=kind,
                outcome=outcome,
                klass=klass.label,
                substrate=substrate,
                rule_id=rule_id,
                payload_digest=digest(payload) if payload else "",
                detail=dict(detail or {}),
                prev=self._prev,
                subject=self._subject.id,
                groups=self._subject.groups,
            ).sealed()

            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(entry.to_json() + "\n")
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

            self._seq, self._prev = seq, entry.hash
            return entry.step_id

    # ── Reading ───────────────────────────────────────────────────────────────

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(read_entries(self._path))

    def find(self, step_id: str) -> LedgerEntry | None:
        """The entry with this step id, or ``None``. Last match wins."""
        found = None
        for entry in read_entries(self._path):
            if entry.step_id == step_id:
                found = entry
        return found

    def verify(self) -> VerificationResult:
        return verify_file(self._path)

    def summary(self) -> dict[str, Any]:
        """Counts an operator actually looks at, for ``annona verify``."""
        placements: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        classes: dict[str, int] = {}

        for entry in read_entries(self._path):
            outcomes[entry.outcome] = outcomes.get(entry.outcome, 0) + 1
            classes[entry.klass] = classes.get(entry.klass, 0) + 1
            if entry.substrate:
                placements[entry.substrate] = placements.get(entry.substrate, 0) + 1

        return {"placements": placements, "outcomes": outcomes, "classes": classes}
