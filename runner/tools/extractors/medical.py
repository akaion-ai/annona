"""DICOM — medical imaging, and the reason the default policy treats it as restricted.

A ``.dcm`` file is a picture with a patient's identity attached to it: name,
date of birth, hospital, the referring physician, the study that was ordered.
That header is the most sensitive material this kernel is likely to be pointed
at, and it is also, mechanically, the easiest to read — which is exactly why
the shipped policy classifies ``**/*.dcm`` as restricted, so a study cannot be
placed on a substrate outside the machine even if someone adds one.

What is extracted is the header, in words, plus an optional greyscale preview
of the first frame for a substrate that is permitted to look at it. Pixel data
is never inlined into the transcript.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions, missing_dependency

__all__ = ["read_dicom"]

_FIELDS = (
    ("PatientName", "Paziente"),
    ("PatientID", "ID paziente"),
    ("PatientBirthDate", "Data di nascita"),
    ("PatientSex", "Sesso"),
    ("StudyDate", "Data studio"),
    ("StudyTime", "Ora studio"),
    ("Modality", "Modalità"),
    ("StudyDescription", "Descrizione studio"),
    ("SeriesDescription", "Descrizione serie"),
    ("BodyPartExamined", "Distretto"),
    ("InstitutionName", "Struttura"),
    ("ReferringPhysicianName", "Medico richiedente"),
    ("Manufacturer", "Produttore"),
    ("ManufacturerModelName", "Apparecchiatura"),
    ("AccessionNumber", "Accession number"),
    ("StudyInstanceUID", "Study UID"),
    ("SeriesInstanceUID", "Series UID"),
)


def read_dicom(path: Path, opts: ReadOptions) -> Extraction:
    """One DICOM instance: the header as text, the first frame as an image."""
    try:
        import pydicom  # noqa: PLC0415 — optional, and only medical imaging needs it
    except ImportError:
        return Extraction(
            format="dicom",
            text=f"=== DICOM: {path.name} ===",
            metadata={"format": "dicom"},
            warnings=(missing_dependency("pydicom", "reading DICOM studies", extra="medical"),),
        )

    dataset = pydicom.dcmread(str(path), force=True)

    metadata: dict[str, Any] = {"format": "dicom"}
    lines = [f"=== DICOM: {path.name} ==="]
    for tag, label in _FIELDS:
        value = getattr(dataset, tag, None)
        if value not in (None, ""):
            metadata[tag] = str(value)
            lines.append(f"{label}: {value}")

    rows = getattr(dataset, "Rows", None)
    columns = getattr(dataset, "Columns", None)
    frames = int(getattr(dataset, "NumberOfFrames", 1) or 1)
    if rows and columns:
        metadata |= {"rows": int(rows), "columns": int(columns), "frames": frames}
        lines.append(f"Immagine: {columns}×{rows} px, {frames} frame")

    media: list[MediaRef] = []
    preview, note = _preview(dataset, path, opts)
    if preview is not None:
        media.append(
            MediaRef(path=preview, media_type="image", label=f"{path.name} (frame 1)", derived=True)
        )
        metadata["preview"] = str(preview)

    return Extraction(
        format="dicom",
        text="\n".join(lines),
        metadata=metadata,
        media=tuple(media),
        warnings=(note,) if note else (),
    )


def _preview(dataset: Any, path: Path, opts: ReadOptions) -> tuple[Path | None, str]:
    """Render the first frame as PNG, windowed for viewing rather than diagnosis."""
    try:
        import numpy  # noqa: PLC0415 — pixel data is an array
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None, missing_dependency(
            "numpy pillow", "rendering a DICOM preview", extra="medical"
        )

    try:
        pixels = dataset.pixel_array
    except Exception as exc:  # noqa: BLE001 — a header-only instance is normal
        logger.debug(f"no readable pixel data in {path}: {exc}")
        return None, ""

    frame = pixels[0] if pixels.ndim == 3 else pixels
    frame = frame.astype("float32")
    span = float(frame.max() - frame.min()) or 1.0
    normalised = ((frame - frame.min()) / span * 255).astype(numpy.uint8)

    target = opts.derived_dir(path) / f"{path.stem}-preview.png"
    Image.fromarray(normalised).save(target)
    return target, (
        "the preview is normalised for viewing and is not a diagnostic rendering "
        "(no window centre/width applied)"
    )
