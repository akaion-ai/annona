"""Which reader gets which file, and what this build can actually read.

One table, keyed by extension. It is deliberately data rather than a chain of
``if suffix in …``: the set of formats is the product claim ("attach what you
have"), and a claim you can only verify by reading control flow is one that
drifts.

:func:`capabilities` exists for the same reason. Half of these readers depend
on something optional, so "can this machine read a DICOM study" is a question
about the installation, not about the code — and the UI, the CLI and the model
all need to be able to ask it and get the same answer.
"""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors import (
    archives,
    av,
    documents,
    images,
    mail,
    medical,
    signed,
    structured,
)
from runner.tools.extractors import cache as cache_module
from runner.tools.extractors.types import Extraction, ReadOptions

__all__ = [
    "FAMILIES",
    "READERS",
    "capabilities",
    "extract",
    "family_for",
    "is_readable",
    "supported_extensions",
]

Reader = Callable[[Path, ReadOptions], Extraction]

TEXT_SUFFIXES = (
    ".txt",
    ".md",
    ".rst",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".env",
    ".json",
    ".yaml",
    ".yml",
    ".tsv",
    ".sql",
    ".srt",
    ".vtt",
)
CODE_SUFFIXES = (
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sh",
    ".bash",
    ".zsh",
    ".rb",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".php",
    ".pl",
    ".lua",
    ".r",
    ".m",
    ".dart",
    ".vue",
    ".svelte",
)
IMAGE_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
)

READERS: dict[str, Reader] = {
    # Documents
    ".pdf": documents.read_pdf,
    ".docx": documents.read_word,
    ".doc": documents.read_word,
    ".xlsx": documents.read_excel,
    ".xlsm": documents.read_excel,
    ".xls": documents.read_excel,
    ".pptx": documents.read_powerpoint,
    ".odt": documents.read_odf,
    ".ods": documents.read_odf,
    ".odp": documents.read_odf,
    ".epub": documents.read_epub,
    ".rtf": documents.read_rtf,
    ".csv": documents.read_csv,
    # Structured
    ".xml": structured.read_xml,
    ".ics": structured.read_calendar,
    ".ifc": documents.read_text,
    # Signed envelopes
    ".p7m": signed.read_signed,
    ".p7s": signed.read_detached_signature,
    # Mail
    ".eml": mail.read_eml,
    ".msg": mail.read_msg,
    # Containers
    ".zip": archives.read_zip,
    ".tar": archives.read_tar,
    ".gz": archives.read_tar,
    ".tgz": archives.read_tar,
    ".bz2": archives.read_tar,
    ".xz": archives.read_tar,
    # Medical imaging
    ".dcm": medical.read_dicom,
    ".dicom": medical.read_dicom,
}

READERS |= {suffix: documents.read_text for suffix in TEXT_SUFFIXES + CODE_SUFFIXES}
READERS |= {suffix: images.read_image for suffix in IMAGE_SUFFIXES}
READERS |= {suffix: av.read_audio for suffix in av.AUDIO_SUFFIXES}
READERS |= {suffix: av.read_video for suffix in av.VIDEO_SUFFIXES}

FAMILIES: dict[str, tuple[str, ...]] = {
    "document": (".pdf", ".docx", ".doc", ".odt", ".rtf", ".epub"),
    "spreadsheet": (".xlsx", ".xlsm", ".xls", ".ods", ".csv"),
    "presentation": (".pptx", ".odp"),
    "text": TEXT_SUFFIXES + CODE_SUFFIXES,
    "structured": (".xml", ".ics"),
    "signed": (".p7m", ".p7s"),
    "mail": (".eml", ".msg"),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"),
    "image": IMAGE_SUFFIXES,
    "audio": av.AUDIO_SUFFIXES,
    "video": av.VIDEO_SUFFIXES,
    "medical": (".dcm", ".dicom"),
}


def _double_suffix(path: Path) -> str:
    """``.tar.gz`` and ``fattura.xml.p7m`` are one format, not two.

    Matched on the last suffix in the ordinary case; the compound names are
    listed because ``.gz`` alone means "compressed something" and the reader
    for it depends on what the something is.
    """
    lowered = path.name.lower()
    for compound in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lowered.endswith(compound):
            return ".tar"
    return path.suffix.lower()


def family_for(path: str | Path) -> str:
    """The family a file belongs to — what the UI puts on a chip."""
    suffix = _double_suffix(Path(path))
    for family, suffixes in FAMILIES.items():
        if suffix in suffixes:
            return family
    return "unknown"


def supported_extensions() -> tuple[str, ...]:
    """Every extension with a reader, sorted. The honest answer to "what can I attach"."""
    return tuple(sorted(READERS))


def is_readable(path: str | Path) -> bool:
    return _double_suffix(Path(path)) in READERS


def extract(path: str | Path, opts: ReadOptions | None = None) -> Extraction:
    """Read one file as whatever it is.

    Files with no registered reader are attempted as text rather than refused.
    A refusal on an unknown extension would be wrong far more often than it is
    right — half the configuration formats in the world have a bespoke suffix
    and are UTF-8 underneath — and the metadata says the guess was made.
    """
    target = Path(path).expanduser()
    opts = opts or ReadOptions()
    # Wired here rather than at construction so every nested read — an archive
    # member, an email attachment, a document inside a signed envelope — comes
    # back through this same table.
    opts.recurse = lambda inner, inner_opts: extract(inner, inner_opts)

    suffix = _double_suffix(target)
    reader = READERS.get(suffix)

    # The same bytes always extract to the same text, so the second read of a
    # file is free. Keyed on the file's digest and stored beside it — no shared
    # directory, no new trust boundary. See `runner.tools.extractors.cache`.
    key = None if (opts.depth or cache_module.disabled()) else cache_module.cache_key(target, opts)
    if key:
        cached = cache_module.load(target, key)
        if cached is not None:
            return cached

    if reader is None:
        logger.debug(f"no registered reader for {suffix!r}; reading {target} as text")
        result = documents.read_text(target, opts)
        return Extraction(
            format=result.format,
            text=result.text,
            metadata={**result.metadata, "guessed": True},
            warnings=(f"no reader is registered for {suffix or 'this file'}; it was read as text",),
        )

    extraction = reader(target, opts)
    if key:
        cache_module.store(target, key, extraction)
    return extraction


# ── What this installation can do ─────────────────────────────────────────────


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def capabilities() -> dict[str, Any]:
    """Per-family readiness, with the install line for whatever is missing.

    Reported rather than assumed because the answer differs machine to machine,
    and a UI that offers to read a video on a box without ffmpeg is a UI that
    lies once and is never trusted again.
    """
    checks: list[tuple[str, bool, str]] = [
        ("document", _installed("pdfplumber") or _installed("pypdf"), "pip install pdfplumber"),
        ("spreadsheet", _installed("openpyxl"), "pip install openpyxl"),
        ("presentation", _installed("pptx"), "pip install python-pptx"),
        ("text", True, ""),
        ("structured", True, ""),
        (
            "signed",
            _installed("asn1crypto") or bool(shutil.which("openssl")),
            "pip install asn1crypto",
        ),
        ("mail", True, ""),
        ("archive", True, ""),
        ("image", _installed("PIL"), "pip install pillow"),
        ("audio", bool(shutil.which("ffprobe")), "brew install ffmpeg"),
        ("video", bool(shutil.which("ffmpeg")), "brew install ffmpeg"),
        ("medical", _installed("pydicom"), "pip install pydicom"),
    ]

    return {
        "families": {
            family: {
                "ready": ready,
                "extensions": list(FAMILIES.get(family, ())),
                "install": "" if ready else install,
            }
            for family, ready, install in checks
        },
        "extras": {
            "ocr": bool(shutil.which("tesseract")) and _installed("pytesseract"),
            "transcription": _installed("faster_whisper") or _installed("whisper"),
            "heic": _installed("pillow_heif"),
            "outlook_msg": _installed("extract_msg"),
            "pdf_rasteriser": _installed("pypdfium2"),
        },
    }
