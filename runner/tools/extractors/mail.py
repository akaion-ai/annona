"""Email on disk: ``.eml`` and Outlook's ``.msg``.

A saved message is a container like an archive, and it is usually the container
the actual document arrived in — the PEC receipt, the signed invoice, the
contract. Reading the headers and the body without reading the attachments
answers the least interesting half of the question, so attachments are written
out beside the message and read through the registry like anything else.

Attachments are kept, not thrown away like an archive's members: the file
somebody was sent is a file they will want to point at again, and ``.eml`` is
usually a message they saved deliberately.
"""

from __future__ import annotations

from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors.documents import strip_html
from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions, missing_dependency

__all__ = ["read_eml", "read_msg"]

_HEADERS = (
    ("From", "Da"),
    ("To", "A"),
    ("Cc", "Cc"),
    ("Date", "Data"),
    ("Subject", "Oggetto"),
    ("Message-ID", "Message-ID"),
)


def read_eml(path: Path, opts: ReadOptions) -> Extraction:
    """An RFC 822 message: headers, best text body, and every attachment."""
    message = BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())

    lines = [f"=== Email: {path.name} ==="]
    metadata: dict[str, Any] = {"format": "eml", "attachments": []}
    for header, label in _HEADERS:
        value = message.get(header)
        if value:
            metadata[header.lower()] = str(value)
            lines.append(f"{label}: {value}")

    body = message.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        if body.get_content_subtype() == "html":
            # Same crude strip as the EPUB reader: a mail client's HTML is
            # tables inside tables, and none of that is information.
            content = strip_html(content)
        lines += ["", content.strip()]

    warnings: list[str] = []
    media: list[MediaRef] = []
    for part in message.iter_attachments():
        name = part.get_filename() or "allegato.bin"
        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        target = opts.derived_dir(path) / f"{path.stem}-{Path(name).name}"
        target.write_bytes(payload)
        metadata["attachments"].append({"name": name, "path": str(target), "bytes": len(payload)})
        lines += ["", f"--- Allegato: {name} ({len(payload) / 1024:.1f} KB) → {target} ---"]

        inner = opts.read(target)
        if inner.text.strip():
            lines.append(inner.text)
        media += list(inner.media)
        warnings += [f"{name}: {w}" for w in inner.warnings]

    return Extraction(
        format="eml",
        text="\n".join(lines),
        metadata=metadata,
        media=tuple(media),
        warnings=tuple(warnings),
    )


def read_msg(path: Path, opts: ReadOptions) -> Extraction:
    """Outlook's own format, which is a compound file rather than a message."""
    try:
        import extract_msg  # noqa: PLC0415 — optional
    except ImportError:
        return Extraction(
            format="msg",
            warnings=(missing_dependency("extract-msg", "reading Outlook .msg files"),),
        )

    message = extract_msg.Message(str(path))
    lines = [
        f"=== Email: {path.name} ===",
        f"Da: {message.sender or '—'}",
        f"A: {message.to or '—'}",
        f"Data: {message.date or '—'}",
        f"Oggetto: {message.subject or '—'}",
        "",
        (message.body or "").strip(),
    ]

    metadata: dict[str, Any] = {"format": "msg", "attachments": []}
    warnings: list[str] = []
    media: list[MediaRef] = []

    for attachment in message.attachments:
        name = attachment.longFilename or attachment.shortFilename or "allegato.bin"
        payload = attachment.data
        if not isinstance(payload, bytes):
            continue

        target = opts.derived_dir(path) / f"{path.stem}-{Path(name).name}"
        target.write_bytes(payload)
        metadata["attachments"].append({"name": name, "path": str(target), "bytes": len(payload)})
        lines += ["", f"--- Allegato: {name} → {target} ---"]

        inner = opts.read(target)
        if inner.text.strip():
            lines.append(inner.text)
        media += list(inner.media)
        warnings += [f"{name}: {w}" for w in inner.warnings]

    logger.debug(f"read {path} with extract-msg ({len(metadata['attachments'])} attachments)")
    return Extraction(
        format="msg",
        text="\n".join(lines),
        metadata=metadata,
        media=tuple(media),
        warnings=tuple(warnings),
    )
