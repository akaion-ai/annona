"""Images: what is written on them, and what they record about where they were.

An image is the one attachment where the extracted text is usually the least
interesting part. What a kernel can say about it without a vision model is
still worth having — dimensions, camera, timestamp, and whether the file is
carrying GPS coordinates, which is the part people forget a photo contains.

The pixels are offered separately as a :class:`~runner.tools.extractors.types.MediaRef`.
Whether a model ever sees them is not decided here: placement decides, and on a
policy with no vision-capable substrate the answer is no, and the OCR text and
the metadata are what the run gets.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions, missing_dependency

__all__ = ["ocr_image", "read_image"]

_GPS_TAG = 34853
_INTERESTING_EXIF = {
    271: "camera_make",
    272: "camera_model",
    274: "orientation",
    306: "datetime",
    36867: "datetime_original",
    37377: "shutter_speed",
    37378: "aperture",
    34855: "iso",
    42036: "lens",
    315: "artist",
    33432: "copyright",
}


def read_image(path: Path, opts: ReadOptions) -> Extraction:
    """One image: metadata, OCR when possible, and the pixels for whoever may see them."""
    metadata: dict[str, Any] = {"format": path.suffix.lstrip(".").lower() or "image"}
    warnings: list[str] = []
    lines: list[str] = [f"=== Image: {path.name} ==="]

    try:
        from PIL import Image  # noqa: PLC0415 — optional, and only images need it
    except ImportError:
        return Extraction(
            format="image",
            text="\n".join(lines),
            metadata=metadata,
            media=(MediaRef(path=path, media_type="image", label=path.name),),
            warnings=(missing_dependency("Pillow", "reading image metadata"),),
        )

    if path.suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif  # noqa: PLC0415 — Apple's default camera format

            pillow_heif.register_heif_opener()
        except ImportError:
            warnings.append(missing_dependency("pillow-heif", "reading HEIC photos"))

    try:
        with Image.open(path) as image:
            metadata.update(
                {
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "pillow_format": image.format,
                }
            )
            lines.append(f"Dimensioni: {image.width}×{image.height} px, {image.mode}")
            exif = image.getexif()
    except Exception as exc:  # noqa: BLE001 — an unreadable image is a result, not a crash
        return Extraction(
            format="image",
            text="\n".join(lines),
            metadata=metadata,
            media=(MediaRef(path=path, media_type="image", label=path.name),),
            warnings=(f"the image could not be decoded: {exc}",),
        )

    if exif:
        recorded = {
            name: str(exif.get(tag)).strip()
            for tag, name in _INTERESTING_EXIF.items()
            if exif.get(tag) not in (None, "")
        }
        if recorded:
            metadata["exif"] = recorded
            lines.append("EXIF: " + ", ".join(f"{k}={v}" for k, v in recorded.items()))

        gps = exif.get_ifd(_GPS_TAG) if hasattr(exif, "get_ifd") else None
        if gps:
            coordinates = _coordinates(gps)
            metadata["gps"] = coordinates or {"present": True}
            # Said in words, not only in a dict: the classifier reads text, and
            # "this photo records where it was taken" is exactly the kind of
            # fact that should be able to raise the class of a run.
            lines.append(
                "GPS: the photo carries location coordinates"
                + (
                    f" ({coordinates['latitude']}, {coordinates['longitude']})"
                    if coordinates
                    else ""
                )
            )

    text, note = ocr_image(path, opts)
    if text.strip():
        metadata["ocr"] = True
        lines.append("")
        lines.append("--- Testo riconosciuto (OCR) ---")
        lines.append(text.strip())
    elif note:
        warnings.append(note)

    return Extraction(
        format=metadata["format"],
        text="\n".join(lines),
        metadata=metadata,
        media=(MediaRef(path=path, media_type="image", label=path.name),),
        warnings=tuple(warnings),
    )


def _coordinates(gps: dict) -> dict[str, float] | None:
    """Decode EXIF GPS rationals into decimal degrees."""

    def decimal(value, reference) -> float | None:
        try:
            degrees, minutes, seconds = (float(part) for part in value)
        except (TypeError, ValueError):
            return None
        result = degrees + minutes / 60 + seconds / 3600
        return -result if str(reference).upper() in ("S", "W") else result

    latitude = decimal(gps.get(2), gps.get(1))
    longitude = decimal(gps.get(4), gps.get(3))
    if latitude is None or longitude is None:
        return None
    return {"latitude": round(latitude, 6), "longitude": round(longitude, 6)}


def ocr_image(path: Path, opts: ReadOptions) -> tuple[str, str]:
    """Text in the pixels, and — when there is none — why.

    Returns ``(text, note)``. The note is never an error: OCR that is not
    installed is a capability this machine does not have, and a run that says
    so is more useful than one that reports an empty document.
    """
    if opts.ocr == "never":
        return "", ""

    try:
        import pytesseract  # noqa: PLC0415 — optional
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return "", missing_dependency("pytesseract", "reading text inside images (OCR)")

    if not shutil.which("tesseract"):
        return "", (
            "OCR needs the tesseract binary: brew install tesseract tesseract-lang "
            "(or: apt install tesseract-ocr tesseract-ocr-ita)"
        )

    try:
        with Image.open(path) as image:
            # Italian first, then English: this runs on machines whose documents
            # are Italian, and tesseract falls back on its own if the pack is
            # missing.
            return pytesseract.image_to_string(image, lang="ita+eng"), ""
    except Exception as exc:  # noqa: BLE001 — OCR failing is not the read failing
        logger.debug(f"OCR failed on {path}: {exc}")
        return "", f"OCR failed: {exc}"
