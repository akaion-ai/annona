"""Reading a file once per file, not once per run.

On a shared appliance the scarce resource is not the GPU, it is context: twelve
people opening the same data room means the same 40-page contract is extracted
twelve times, and each extraction costs seconds of CPU and — because the text
lands in a transcript — thousands of tokens that were already paid for once.

The fix is a cache, and the reason it is safe to build *this* one is that it
crosses no boundary. It is keyed on the file's own SHA-256, so:

- the same bytes always produce the same key, for any person, from any path;
- different bytes produce a different key, so there is no invalidation logic to
  get wrong — a modified file simply misses;
- the entry lives beside the file it came from, inheriting the same directory,
  the same class under the policy, and the same access control the filesystem
  already applies.

That last point is the whole design. A cache that lived in one shared directory
would be a copy of everyone's material in a place the policy never classified —
which is the difference between a cache and a leak, and it is why sharing
*derived* work between colleagues needs identity first. See
``docs/design/shared-context.md``.

What is stored is the text and the metadata: the cheap, deterministic part.
Media references are recomputed, because they point at derived artefacts whose
paths a later run may have cleaned up.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from loguru import logger

from runner.tools.extractors.types import Extraction, ReadOptions, writable_beside

__all__ = ["CACHE_DIRNAME", "cache_key", "disabled", "load", "store"]

CACHE_DIRNAME = ".annona-cache"
"""Where entries live: beside the source, like ``.annona-derived``."""

MAX_CACHEABLE_BYTES = 64 * 1024 * 1024
"""Largest source file whose text is worth caching.

Not a limit on what can be *read* — a 2 GB video is read fine. It is a limit on
what is worth hashing: past this size the digest costs more than the re-read
saves for the formats that get this big, which are the ones whose extraction is
metadata rather than text.
"""

_FORMAT = 1
"""Entry format version. A bump invalidates every entry by changing the key."""


def disabled() -> bool:
    """Whether caching is off for this process (``ANNONA_NO_CACHE=1``)."""
    return os.getenv("ANNONA_NO_CACHE", "").strip() not in ("", "0", "false", "no")


def _digest(path: Path) -> str:
    """SHA-256 of the file, streamed."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def cache_key(path: Path, opts: ReadOptions) -> str | None:
    """The key for this file under these options, or ``None`` if not cacheable.

    The options that change the *output* are part of the key; the ones that only
    change where derived artefacts land are not. Getting this wrong in the safe
    direction (too many keys) costs a re-read; getting it wrong in the other
    direction returns somebody's text for the wrong request.
    """
    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHEABLE_BYTES:
            return None
        digest = _digest(path)
    except OSError as exc:
        logger.debug(f"not caching {path}: {exc}")
        return None

    shape = json.dumps(
        {
            "v": _FORMAT,
            "sheet": opts.sheet_name,
            "ocr": opts.ocr,
            "transcribe": opts.transcribe,
            "members": opts.max_members,
            "depth": opts.max_depth,
            "seconds": opts.max_media_seconds,
        },
        sort_keys=True,
    )
    return f"{digest}-{hashlib.sha256(shape.encode()).hexdigest()[:8]}"


def _entry_path(path: Path, key: str) -> Path:
    """Beside the source when that is writable; under the daemon's home when it
    is not — an appliance mounts its material read-only, and a cache that
    silently never writes is worse than no cache."""
    return writable_beside(path, CACHE_DIRNAME) / f"{key}.json"


def load(path: Path, key: str) -> Extraction | None:
    """A previously stored extraction, or ``None``."""
    entry = _entry_path(path, key)
    if not entry.is_file():
        return None

    try:
        body = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # A corrupt entry is a cache miss, never an error: the file is still
        # right there and can simply be read again.
        logger.debug(f"unreadable cache entry {entry}: {exc}")
        return None

    logger.debug(f"cache hit for {path.name} ({key[:12]})")
    return Extraction(
        format=str(body.get("format", "")),
        text=str(body.get("text", "")),
        metadata={**body.get("metadata", {}), "cached": True},
        warnings=tuple(body.get("warnings", ())),
    )


def store(path: Path, key: str, extraction: Extraction) -> None:
    """Keep this extraction for the next read of the same bytes.

    Failure is silent by design: a cache that cannot be written is a cache that
    is not used, not a read that fails. Written through a temporary file so a
    killed process leaves no half-entry for the next run to parse.
    """
    if not extraction.text.strip():
        return

    entry = _entry_path(path, key)
    try:
        staged = entry.with_suffix(f".{os.getpid()}.tmp")
        staged.write_text(
            json.dumps(
                {
                    "format": extraction.format,
                    "text": extraction.text,
                    "metadata": dict(extraction.metadata),
                    "warnings": list(extraction.warnings),
                    "source": str(path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        staged.replace(entry)
    except OSError as exc:
        logger.debug(f"could not cache {path}: {exc}")
