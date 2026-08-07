"""Reading attached bytes, at the last possible moment (layer L1).

A media block travels through the loop and the perimeter as a path. This module
is where that path finally becomes bytes — inside an adapter, after placement
has chosen a substrate that policy permits to see the material.

That ordering is the point, and it is why this helper lives in L1 rather than
somewhere convenient. Base64 produced earlier would mean the payload existed in
memory, in the transcript and in the ledger's digest before anything decided it
could cross; producing it here means a held turn never encoded the file at all.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from loguru import logger

__all__ = ["MAX_INLINE_BYTES", "encode_media", "mime_for"]

MAX_INLINE_BYTES = 20 * 1024 * 1024
"""Largest file inlined into one request.

Twenty megabytes is above every provider's per-image limit and below the point
where a laptop building the request starts swapping. A file past it is dropped
with a log line rather than sent: a request that fails at the provider after a
90-second upload is a worse outcome than one that says the image was too large.
"""

_EXTRA_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".mkv": "video/x-matroska",
}


def mime_for(path: str | Path) -> str:
    """The MIME type a provider will accept for this file."""
    target = Path(path)
    guessed, _ = mimetypes.guess_type(target.name)
    return guessed or _EXTRA_TYPES.get(target.suffix.lower(), "application/octet-stream")


def encode_media(path: str | Path) -> tuple[str, str] | None:
    """``(base64, mime)`` for a file, or ``None`` if it cannot be sent.

    Returns ``None`` rather than raising: one unreadable attachment must not
    lose the turn it was attached to, and the model still has the extracted
    text, which is the fallback this whole design keeps available.
    """
    target = Path(path).expanduser()
    try:
        size = target.stat().st_size
    except OSError as exc:
        logger.warning(f"attachment {target} could not be read: {exc}")
        return None

    if size > MAX_INLINE_BYTES:
        logger.warning(
            f"attachment {target} is {size / 1_048_576:.1f} MB, past the "
            f"{MAX_INLINE_BYTES / 1_048_576:.0f} MB inline ceiling; it was not sent"
        )
        return None

    return base64.b64encode(target.read_bytes()).decode("ascii"), mime_for(target)
