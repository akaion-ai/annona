"""Containers: zip, tar, and whatever is inside them.

An archive is the case where the interesting question is not "what is this
file" but "what did somebody send me". A listing alone is nearly useless — the
answer to "what is in this export" is in the members — so this extracts them
and reads each one through the same registry that read the archive.

Two ceilings, because an archive is also the easiest way to hand a laptop a
zip bomb: at most :attr:`ReadOptions.max_members` members are opened, and
nesting stops at :attr:`ReadOptions.max_depth`. Both are reported in the text,
so a model never has to guess whether it saw everything.
"""

from __future__ import annotations

import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions

__all__ = ["read_tar", "read_zip"]

_SKIP = ("__MACOSX/", ".DS_Store", "Thumbs.db")


def read_zip(path: Path, opts: ReadOptions) -> Extraction:
    with zipfile.ZipFile(path) as archive:
        members = [
            (info.filename, info.file_size)
            for info in archive.infolist()
            if not info.is_dir() and not any(skip in info.filename for skip in _SKIP)
        ]
        return _read_members(
            path, opts, members, lambda name, target: _extract_zip(archive, name, target), "zip"
        )


def read_tar(path: Path, opts: ReadOptions) -> Extraction:
    with tarfile.open(path) as archive:
        members = [
            (member.name, member.size)
            for member in archive.getmembers()
            if member.isfile() and not any(skip in member.name for skip in _SKIP)
        ]
        return _read_members(
            path, opts, members, lambda name, target: _extract_tar(archive, name, target), "tar"
        )


def _extract_zip(archive: zipfile.ZipFile, name: str, target: Path) -> None:
    target.write_bytes(archive.read(name))


def _extract_tar(archive: tarfile.TarFile, name: str, target: Path) -> None:
    handle = archive.extractfile(name)
    if handle is None:
        raise OSError(f"{name} could not be read from the archive")
    with handle:
        target.write_bytes(handle.read())


def _read_members(
    path: Path,
    opts: ReadOptions,
    members: list[tuple[str, int]],
    extract,
    kind: str,
) -> Extraction:
    """List everything, read what the ceilings allow, and say which is which."""
    lines = [f"=== Archive: {path.name} ({len(members)} file) ==="]
    for name, size in members:
        lines.append(f"  {name}  ({size / 1024:.1f} KB)")

    metadata: dict[str, Any] = {
        "format": kind,
        "members": [name for name, _ in members],
        "members_read": [],
    }
    warnings: list[str] = []
    media: list[MediaRef] = []

    if opts.depth >= opts.max_depth:
        return Extraction(
            format=kind,
            text="\n".join(lines),
            metadata=metadata,
            warnings=(f"nesting stopped at depth {opts.max_depth}; members were listed only",),
        )

    selected = members[: opts.max_members]
    if len(members) > len(selected):
        warnings.append(
            f"{len(members) - len(selected)} of {len(members)} members were listed but not "
            f"opened (ceiling: {opts.max_members} per read)"
        )

    # Extracted into a temp directory that is removed on the way out: an
    # archive's members are not files the operator put on their disk, and
    # leaving them behind would silently multiply the material a policy has to
    # reason about. Anything worth keeping is written out by the caller.
    with tempfile.TemporaryDirectory(prefix="annona-archive-") as scratch:
        for name, _size in selected:
            target = Path(scratch) / Path(name).name
            try:
                extract(name, target)
                inner = opts.read(target)
            except Exception as exc:  # noqa: BLE001 — one bad member is not a bad archive
                logger.debug(f"member {name} of {path} could not be read: {exc}")
                warnings.append(f"{name} could not be read: {exc}")
                continue

            if not inner.text.strip():
                continue

            metadata["members_read"].append(name)
            lines += ["", f"--- {name} ({inner.format}) ---", inner.text]
            warnings += [f"{name}: {w}" for w in inner.warnings]
            # Media inside an archive is deliberately dropped: the bytes live in
            # a directory that is about to be deleted, and a MediaRef pointing at
            # a path that no longer exists is worse than no MediaRef at all.

    return Extraction(
        format=kind,
        text="\n".join(lines),
        metadata=metadata,
        media=tuple(media),
        warnings=tuple(warnings),
    )
