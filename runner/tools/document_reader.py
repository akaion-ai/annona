"""The tool the model calls to read a file.

Thin on purpose. Everything about *how* a format is read lives in
:mod:`runner.tools.extractors`; what is left here is the part that belongs to a
tool — the schema the model sees, the size ceiling the operator set, and a
result shape that has not changed since this tool only knew about PDFs.

Two things it does not do, and must not start doing:

**It does not decide whether a file may be read.** That is the gate's job
(``runner.policy.gate``), which sees this call before it runs and refuses it if
the path is outside the tool's allow-list. A reader that consulted permissions
itself would be a second, quieter policy.

**It does not put images in the transcript.** Anything visual comes back as a
*reference* under ``media``. Whether a model ever sees those pixels is decided
by placement, after classification — which is the whole point of the kernel and
would be undone by a tool that inlined base64 into its own result.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from runner.tools.extractors import ReadOptions, capabilities, extract
from runner.tools.extractors.av import AUDIO_SUFFIXES, VIDEO_SUFFIXES
from runner.tools.extractors.registry import CODE_SUFFIXES, IMAGE_SUFFIXES, TEXT_SUFFIXES

from .base import Tool

# Kept as a module constant with its original key names because `explorer` and
# the existing tests read it. New families were appended rather than folded into
# the old ones: `get_file_format("scan.dcm")` returning "medical" is worth more
# to a caller than it returning "unknown".
SUPPORTED_FORMATS = {
    "text": list(TEXT_SUFFIXES),
    "code": list(CODE_SUFFIXES),
    "pdf": [".pdf"],
    "word": [".docx", ".doc", ".odt"],
    "excel": [".xlsx", ".xlsm", ".xls", ".ods"],
    "csv": [".csv"],
    "rtf": [".rtf"],
    "presentation": [".pptx", ".odp"],
    "ebook": [".epub"],
    "structured": [".xml", ".ics"],
    "signed": [".p7m", ".p7s"],
    "mail": [".eml", ".msg"],
    "archive": [".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"],
    "image": list(IMAGE_SUFFIXES),
    "audio": list(AUDIO_SUFFIXES),
    "video": list(VIDEO_SUFFIXES),
    "medical": [".dcm", ".dicom"],
}

MAX_FILE_SIZE_MB = 50


class DocumentReaderTool(Tool):
    """Reads a file of almost any format and returns it as text."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(
            name="document_reader",
            description=(
                "Read a file and return its content as text. Handles documents (PDF, Word, "
                "Excel, PowerPoint, OpenDocument, EPUB, RTF, CSV, text and code), structured "
                "data (XML, including Italian FatturaPA e-invoices, and iCalendar), signed "
                "envelopes (.p7m/CAdES — the envelope is opened and the document inside is "
                "read), saved email (.eml/.msg, attachments included), archives (zip/tar, "
                "members read individually), images (metadata, EXIF and OCR when available), "
                "audio and video (duration, streams, and a locally produced transcript when a "
                "speech model is installed), and DICOM medical imaging (header). "
                "Reading is best-effort and always honest: whatever could not be read comes "
                "back under 'warnings'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or ~ path to the file to read",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default: 100000). Use 0 for no limit.",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "For spreadsheets: the sheet to read (default: every sheet)",
                    },
                },
                "required": ["path"],
            },
        )
        self.config = config
        cfg_perms = config.get("permissions", {}).get("filesystem", {})
        self.max_size_mb = cfg_perms.get("max_file_size_mb", MAX_FILE_SIZE_MB)

    def execute(
        self, path: str, max_chars: int = 100_000, sheet_name: Optional[str] = None, **kwargs
    ) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()

        if not target.exists():
            return {"success": False, "error": f"File not found: {target}"}
        if not target.is_file():
            return {"success": False, "error": f"Not a file: {target}"}

        size_mb = target.stat().st_size / (1024 * 1024)
        if size_mb > self.max_size_mb:
            return {
                "success": False,
                "error": f"File too large: {size_mb:.1f} MB (limit {self.max_size_mb} MB)",
            }

        logger.info(f"Reading document: {target} ({target.suffix.lower()}, {size_mb:.2f} MB)")

        try:
            extraction = extract(
                target,
                ReadOptions(
                    max_chars=max_chars,
                    sheet_name=sheet_name,
                    ocr=kwargs.get("ocr", "auto"),
                    transcribe=kwargs.get("transcribe", "auto"),
                ),
            ).truncated(max_chars)
        except Exception as e:  # noqa: BLE001 — surfaced to the model, not swallowed
            logger.error(f"Error reading {target}: {e}")
            return {"success": False, "error": str(e), "path": str(target)}

        return {
            "success": True,
            "path": str(target),
            "format": extraction.format,
            "size_mb": round(size_mb, 3),
            "char_count": len(extraction.text),
            "metadata": dict(extraction.metadata),
            "content": extraction.text,
            # Present so the model knows the file has a visual side it did not
            # get to see. It carries paths, never bytes.
            "media": [ref.as_dict() for ref in extraction.media],
            # A degraded read that reports nothing is the failure mode this tool
            # is most likely to have: "the invoice is empty" when the truth is
            # "nobody installed the thing that opens signed envelopes".
            "warnings": list(extraction.warnings),
        }


def get_file_format(path: str) -> str:
    """The family a path belongs to, in this module's vocabulary."""
    suffix = Path(path).suffix.lower()
    for fmt, exts in SUPPORTED_FORMATS.items():
        if suffix in exts:
            return fmt
    return "unknown"


def is_readable(path: str) -> bool:
    """Whether a registered reader claims this extension.

    ``False`` does not mean the file is unreadable — :func:`extract` falls back
    to reading anything as text — it means nothing here knows what it is.
    """
    suffix = Path(path).suffix.lower()
    return any(suffix in exts for exts in SUPPORTED_FORMATS.values())


def reader_capabilities() -> Dict[str, Any]:
    """What this installation can actually read right now.

    Re-exported from the extractor registry so callers outside the tool layer —
    the HTTP surface, the CLI — have one import for "what can I attach".
    """
    return capabilities()


__all__: List[str] = [
    "MAX_FILE_SIZE_MB",
    "SUPPORTED_FORMATS",
    "DocumentReaderTool",
    "get_file_format",
    "is_readable",
    "reader_capabilities",
]
