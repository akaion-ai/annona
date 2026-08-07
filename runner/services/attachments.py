"""Files an operator hands to the kernel, and what happens to them.

Until this existed, Annona could read any file on the machine and there was no
way to give it one. The window had a text box; the answer to "read this
invoice" was to find its absolute path and type it. That is a workable
instruction for the person who wrote the tool and an absurd one for anybody
else.

The design decision worth stating is that an attachment is **a file on disk,
not a payload**. Uploading writes the bytes into a real directory the operator
can open in Finder, and the run then reads that path through the same tool,
under the same allow-list, recorded in the same ledger as any other read. The
alternative — inlining uploaded content straight into the prompt — would have
been fewer lines and would have created a second way into the model that the
perimeter never sees. There is no such thing here as material that entered a
run without a decision being recorded about it.

Two consequences follow, and both are visible in the API:

**An upload can be refused by policy after it succeeds.** The bytes are stored;
whether ``document_reader`` may read them is a separate question, answered by
:func:`describe`, which reports ``readable: false`` and the line to add to the
policy rather than pretending the file is unusable.

**The default inbox lives under ``~/Documents``** — inside the tree the shipped
policy already allows — so attaching works on a fresh install without editing
anything, and moving it elsewhere is a deliberate act.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from loguru import logger

from runner.kernel.types import Attachment, SensitivityClass
from runner.policy.classifier import PolicyClassifier
from runner.policy.models import Policy
from runner.tools.extractors import ReadOptions, extract, family_for
from runner.tools.extractors.av import VIDEO_SUFFIXES, still
from runner.tools.extractors.documents import render_first_page
from runner.tools.extractors.registry import IMAGE_SUFFIXES

__all__ = [
    "DEFAULT_MAX_MB",
    "StoredAttachment",
    "attachments_for",
    "describe",
    "display_name",
    "headline",
    "inbox_dir",
    "list_stored",
    "preamble",
    "remove",
    "resolve",
    "short_path",
    "store",
    "thumbnail",
    "thumbnail_source",
    "vision_families",
]

DEFAULT_MAX_MB = 512
"""Ceiling for one upload. Generous because video is one of the point of this."""

VISIBLE_TO_A_MODEL = ("image", "pdf")
"""Media a substrate can be *shown*. Everything else is read by a tool.

Audio and video are deliberately not here. A model that accepts them exists,
but requiring the capability for every attached recording would hold runs on a
policy whose local substrate reads text perfectly well — and the transcript,
produced on this machine, is the better answer anyway.
"""

_UNSAFE = re.compile(r"[^A-Za-z0-9._\- ]+")
_ID = re.compile(r"^[0-9a-f]{16}$")
_ID_PREFIX = re.compile(r"^[0-9a-f]{16}-")


def short_path(path: str | Path) -> str:
    """A path as a person writes it: ``~/Documents/Annona/Inbox``."""
    text = str(path)
    home = str(Path.home())
    return f"~{text[len(home):]}" if text.startswith(home) else text


def display_name(path: str | Path) -> str:
    """The name to show a person: the file they chose, without our id prefix."""
    return _ID_PREFIX.sub("", Path(path).name)


@dataclass(frozen=True)
class StoredAttachment:
    """One file taken in, as it now exists on disk."""

    id: str
    path: Path
    name: str
    bytes: int
    sha256: str


# ── Where things go ───────────────────────────────────────────────────────────


def inbox_dir(config: Mapping[str, Any] | None = None) -> Path:
    """The directory uploads land in.

    ``ANNONA_INBOX`` wins over the config, which wins over the default. The env
    var exists because the daemon and the CLI are frequently the same machine in
    two different shells, and an inbox that moves depending on which one started
    is the kind of thing nobody debugs twice.
    """
    explicit = os.getenv("ANNONA_INBOX")
    if explicit:
        return Path(explicit).expanduser()

    configured = (config or {}).get("attachments", {}).get("dir") if config else None
    if configured:
        return Path(str(configured)).expanduser()

    return Path.home() / "Documents" / "Annona" / "Inbox"


def max_bytes(config: Mapping[str, Any] | None = None) -> int:
    limit = (config or {}).get("attachments", {}).get("max_size_mb", DEFAULT_MAX_MB)
    return int(limit) * 1024 * 1024


def _safe_name(filename: str) -> str:
    """A filename that is only a filename: no directories, no surprises."""
    base = Path(filename).name.strip() or "attachment"
    cleaned = _UNSAFE.sub("_", base).strip("._ ") or "attachment"
    return cleaned[:120]


# ── Taking a file in ──────────────────────────────────────────────────────────


def store(
    filename: str,
    stream: IO[bytes],
    *,
    config: Mapping[str, Any] | None = None,
) -> StoredAttachment:
    """Write an upload into the inbox and return where it landed.

    Streamed in chunks and hashed on the way through, so a 2 GB video never
    exists in memory and the digest costs nothing extra. The identity of an
    attachment is the first 16 hex of its SHA-256: uploading the same file twice
    produces the same id and overwrites the same path rather than accumulating
    ``report (3).pdf``.

    Raises:
        ValueError: the file is larger than the configured ceiling. Raised
            *during* the copy, with the partial file removed — the alternative
            is discovering the size after writing it.
    """
    target_dir = inbox_dir(config)
    target_dir.mkdir(parents=True, exist_ok=True)

    limit = max_bytes(config)
    digest = hashlib.sha256()
    written = 0

    staged = target_dir / f".incoming-{os.getpid()}-{_safe_name(filename)}"
    try:
        with staged.open("wb") as handle:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ValueError(
                        f"the file is larger than the {limit // (1024 * 1024)} MB limit "
                        "for one attachment"
                    )
                digest.update(chunk)
                handle.write(chunk)

        identifier = digest.hexdigest()[:16]
        final = target_dir / f"{identifier}-{_safe_name(filename)}"
        shutil.move(str(staged), final)
    finally:
        staged.unlink(missing_ok=True)

    logger.info(f"attachment stored: {final} ({written} bytes)")
    return StoredAttachment(
        id=identifier,
        path=final,
        name=_safe_name(filename),
        bytes=written,
        sha256=digest.hexdigest(),
    )


def resolve(identifier: str, *, config: Mapping[str, Any] | None = None) -> Path | None:
    """The stored file with this id, or ``None``.

    The id is checked against a hex pattern before it is used in a glob: it
    arrives from an HTTP path segment, and ``../`` in a filename is the oldest
    trick there is.
    """
    if not _ID.match(identifier or ""):
        return None
    return next(iter(sorted(inbox_dir(config).glob(f"{identifier}-*"))), None)


def remove(identifier: str, *, config: Mapping[str, Any] | None = None) -> bool:
    """Delete one attachment. Returns whether there was anything to delete."""
    target = resolve(identifier, config=config)
    if target is None or not target.is_file():
        return False
    target.unlink()
    logger.info(f"attachment removed: {target}")
    return True


def list_stored(
    *, limit: int = 50, config: Mapping[str, Any] | None = None
) -> list[StoredAttachment]:
    """What is in the inbox, newest first."""
    directory = inbox_dir(config)
    if not directory.is_dir():
        return []

    files = [
        item
        for item in directory.iterdir()
        if item.is_file() and not item.name.startswith(".") and "-" in item.name
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    stored = []
    for item in files[:limit]:
        identifier, _, _rest = item.name.partition("-")
        stored.append(
            StoredAttachment(
                id=identifier,
                path=item,
                name=display_name(item),
                bytes=item.stat().st_size,
                sha256="",
            )
        )
    return stored


# ── Showing a file to the person who attached it ──────────────────────────────
#
# Note on why a thumbnail endpoint is not a hole in the perimeter: the perimeter
# governs what reaches a *model*, not what an operator may look at on their own
# screen. These bytes travel from a file the operator chose, over loopback, into
# the window they are already sitting in front of. Nothing here can be reached by
# a remote substrate, and nothing here is added to a transcript.


def thumbnail_source(path: Path) -> Path | None:
    """The image that stands for this file, without producing it yet.

    Cheap enough to call while describing an upload — it decides *whether* a
    thumbnail is possible, so the window can reserve the space instead of
    reflowing when one arrives.
    """
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return path
    if suffix == ".pdf" or suffix in VIDEO_SUFFIXES or suffix in (".dcm", ".dicom"):
        return path
    return None


def thumbnail(path: str | Path, *, size: int = 176) -> Path | None:
    """A small JPEG standing in for the file, generated once and cached.

    Images speak for themselves; a video shows a frame; a PDF shows its first
    page, because an operator recognises an invoice by its letterhead long
    before they read its number; a DICOM shows the normalised slice its reader
    already produced.
    """
    target = Path(path).expanduser()
    source = thumbnail_source(target)
    if source is None:
        return None

    cache_dir = target.parent / ".annona-thumbs"
    cached = cache_dir / f"{target.stem}-{size}.jpg"
    if cached.exists() and cached.stat().st_mtime >= target.stat().st_mtime:
        return cached

    try:
        from PIL import Image  # noqa: PLC0415 — optional, like everything visual
    except ImportError:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    frame = _still_of(target, cache_dir)
    if frame is None:
        return None

    try:
        with Image.open(frame) as opened:
            # RGB because the target is JPEG: a PNG with transparency saved
            # straight to JPEG raises, and a thumbnail that raises is a card
            # with a hole in it.
            square = opened.convert("RGB")
            square.thumbnail((size, size))
            square.save(cached, "JPEG", quality=82)
    except Exception as exc:  # noqa: BLE001 — a missing thumbnail is not a failure
        logger.debug(f"no thumbnail for {target}: {exc}")
        return None

    return cached


def _still_of(target: Path, scratch: Path) -> Path | None:
    """A full-size image of the file, whatever kind of file it is."""
    suffix = target.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return target
    if suffix == ".pdf":
        return render_first_page(target, scratch / f"{target.stem}-page1.png")
    if suffix in VIDEO_SUFFIXES:
        return still(target, scratch / f"{target.stem}-still.jpg")
    if suffix in (".dcm", ".dicom"):
        extraction = extract(target, ReadOptions(max_chars=1, workdir=scratch))
        return next((ref.path for ref in extraction.media if ref.media_type == "image"), None)
    return None


def headline(fmt: str, metadata: Mapping[str, Any]) -> str:
    """One line of fact under the filename: pages, duration, sheets, size.

    The point is recognition, not completeness. "14 pagine" and "2:04:11" are
    what tell an operator they attached the right file; a metadata dump tells
    them nothing at a glance.
    """
    bits: list[str] = []
    # A signed envelope's facts belong to the document inside it: nobody wants
    # to be told "p7m, 4 KB" when the answer is "fattura 243540, 7158,78 EUR".
    inner = metadata.get("inner")
    if isinstance(inner, Mapping):
        metadata = {**inner, **metadata}

    if subject := metadata.get("subject"):
        bits.append(str(subject)[:48])
    if pages := metadata.get("pages"):
        bits.append(f"{pages} pagine")
    if sheets := metadata.get("sheets"):
        bits.append(f"{len(sheets)} fogli")
    if slides := metadata.get("slides"):
        bits.append(f"{slides} slide")
    if rows := metadata.get("rows"):
        bits.append(f"{rows} righe" if fmt == "csv" else f"{rows} px di altezza")
    if (width := metadata.get("width")) and (height := metadata.get("height")):
        bits.append(f"{width}×{height}")
    if duration := metadata.get("duration_seconds"):
        minutes, seconds = divmod(int(duration), 60)
        hours, minutes = divmod(minutes, 60)
        bits.append(f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}")
    if members := metadata.get("members"):
        bits.append(f"{len(members)} file")
    if events := metadata.get("events"):
        bits.append(f"{events} eventi")
    if attachments := metadata.get("attachments"):
        bits.append(f"{len(attachments)} allegati")
    if documents := metadata.get("documents"):
        first = documents[0] if isinstance(documents, list) and documents else {}
        if first.get("number"):
            bits.append(f"n. {first['number']}")
        if first.get("total"):
            bits.append(f"{first['total']} {first.get('currency', '')}".strip())
    if modality := metadata.get("Modality"):
        bits.append(str(modality))
    if signers := metadata.get("signers"):
        subject = signers[0].get("subject", "") if signers else ""
        common = next((p[3:] for p in subject.split(",") if p.startswith("CN=")), "")
        if common:
            bits.append(f"firmato {common}")

    return " · ".join(bits)


# ── Saying what a file is ─────────────────────────────────────────────────────


def describe(
    path: str | Path,
    *,
    policy: Policy | None = None,
    preview_chars: int = 600,
) -> dict[str, Any]:
    """What this file is, what class it carries, and whether a tool may read it.

    The preview is deliberately cheap: no transcription, no keyframes, no OCR.
    An operator dropping a two-hour recording into the window should get a chip
    back in milliseconds saying "audio, 2:04:11" — the expensive reading happens
    inside a run, where it is placed and recorded like everything else.
    """
    target = Path(path).expanduser()
    stat = target.stat()

    info: dict[str, Any] = {
        "path": str(target),
        "name": display_name(target),
        "bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "family": family_for(target),
        "format": target.suffix.lstrip(".").lower(),
        "preview": "",
        "warnings": [],
        "media": [],
        "class": "",
        "readable": True,
        "reason": "",
    }

    try:
        extraction = extract(
            target,
            ReadOptions(max_chars=preview_chars, ocr="never", transcribe="never", frames=0),
        ).truncated(preview_chars)
    except Exception as exc:  # noqa: BLE001 — a preview that fails is still an attachment
        logger.debug(f"preview of {target} failed: {exc}")
        info["warnings"] = [f"the file could not be previewed: {exc}"]
    else:
        info["format"] = extraction.format
        info["preview"] = extraction.text.strip()
        info["warnings"] = list(extraction.warnings)
        info["media"] = [ref.as_dict() for ref in extraction.media]
        info["metadata"] = dict(extraction.metadata)
        info["headline"] = headline(extraction.format, extraction.metadata)

    info["thumbnail"] = thumbnail_source(target) is not None

    if policy is not None:
        classifier = PolicyClassifier(policy)
        klass = classifier.classify_path(str(target))
        # Content counts too, and it is the half a path glob cannot see: a
        # spreadsheet in ~/Downloads full of tax codes is not "internal because
        # of where it is", it is restricted because of what is in it.
        if info["preview"]:
            klass = max(klass, classifier.classify_content(info["preview"]))
        info["class"] = klass.label

        allowed, why = policy.tools.permits_path("document_reader", str(target))
        info["readable"] = allowed
        if not allowed:
            info["reason"] = why
            info["fix"] = (
                "add the directory to the policy:\n"
                "  tools:\n    allow:\n      document_reader:\n"
                f"        - {target.parent}/**"
            )

    return info


# ── Handing files to a run ────────────────────────────────────────────────────


def preamble(described: Iterable[Mapping[str, Any]]) -> str:
    """The lines prepended to a prompt when files are attached.

    Explicit to the point of being blunt, because the model that most needs this
    is a 7B one running locally: it names the absolute path, states the format,
    and says where the content already is. The reads themselves are performed by
    the caller before the first turn — see ``kernel_api._with_attachments`` for
    why an instruction was not enough.
    """
    files = list(described)
    if not files:
        return ""

    lines = ["[Attached by the operator]"]
    for index, item in enumerate(files, 1):
        size = f"{item.get('bytes', 0) / 1024:.0f} KB"
        lines.append(
            f"{index}. {item.get('name', '')} — {item.get('family', 'file')}"
            f"/{item.get('format', '')}, {size}\n   path: {item.get('path', '')}"
        )
        for warning in item.get("warnings", []):
            lines.append(f"   note: {warning}")

    lines.append(
        "Each of these has already been read for you — the extracted content is in the "
        "tool results of this conversation. Answer from it. Call document_reader on a "
        "path again only if you need more of a file than you were given."
    )
    return "\n".join(lines)


def vision_families(policy: Policy | None, klass: SensitivityClass | None = None) -> bool:
    """Whether any substrate this policy could place a turn on can see images.

    Asked before media blocks are attached at all. Without it, attaching a photo
    to a policy whose only substrate is a text model would hold the run with
    "cannot read images" — technically correct, and a worse experience than
    quietly falling back to OCR and metadata, which is what the extractor
    produces anyway.
    """
    if policy is None:
        return False
    return any(
        substrate.vision and (klass is None or substrate.can_hold(klass))
        for substrate in policy.substrates
    )


def attachments_for(described: Iterable[Mapping[str, Any]]) -> list[Attachment]:
    """The subset of attached files a substrate could be *shown*, as references."""
    out: list[Attachment] = []
    for item in described:
        for ref in item.get("media", []):
            if ref.get("media_type") in VISIBLE_TO_A_MODEL:
                out.append(
                    Attachment(
                        path=ref["path"],
                        media_type=ref["media_type"],
                        label=ref.get("label", "") or item.get("name", ""),
                    )
                )
    return out
