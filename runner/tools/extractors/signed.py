"""CAdES / PKCS#7 envelopes — the ``.p7m`` an Italian invoice arrives in.

A ``.p7m`` is a signed container: the document, plus who signed it, plus the
signature. Every electronic invoice sent through SDI, and most of what a
notary, an accountant or a public administration emails, arrives in one. A
kernel that cannot open the envelope cannot read any of that material, which is
why this exists.

Three ways in, tried in order, because the machines this runs on differ:

1. ``asn1crypto`` — a small pure-Python ASN.1 parser. Correct, and the only one
   that gets the structure right rather than guessing at it.
2. ``openssl`` on ``PATH`` — present on every macOS and Linux box.
3. A byte scan for the payload's own signature (``%PDF``, ``<?xml``, ``PK``).
   A last resort, and it says so in the metadata rather than pretending it
   parsed anything.

**The signature is not verified, and the output says so.** Verifying a CAdES
signature means checking a chain against the qualified-trust list, which is a
real piece of work and a different feature; claiming "signed by X" from an
unverified envelope would be worse than saying nothing. What is reported is
what the envelope *asserts*: the certificates it carries.
"""

from __future__ import annotations

import base64
import binascii
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs7
from loguru import logger

from runner.tools.extractors.types import Extraction, ReadOptions

__all__ = ["read_detached_signature", "read_signed"]

_PEM_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]*-----")
_XML_ROOT = re.compile(rb"<([A-Za-z_][\w:.\-]*)")

_MAGIC = (
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
    (b"<?xml", ".xml"),
    (b"{\\rtf", ".rtf"),
    (b"\x89PNG", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


def read_signed(path: Path, opts: ReadOptions) -> Extraction:
    """Open the envelope, then read whatever was inside it."""
    envelope = _as_der(path.read_bytes())
    metadata: dict[str, Any] = {"format": "p7m", "signature_verified": False}
    metadata["signers"] = _signers(envelope)

    payload, how = _unwrap(envelope)
    if payload is None:
        return Extraction(
            format="p7m",
            text=_header(path, metadata),
            metadata=metadata,
            warnings=(
                "the signed envelope could not be opened. "
                "pip install asn1crypto, or install openssl, and try again.",
            ),
        )

    metadata["unwrapped_by"] = how
    inner_path = _write_payload(path, payload, opts)
    metadata["payload"] = str(inner_path)

    inner = opts.read(inner_path)
    metadata["payload_format"] = inner.format

    text = "\n\n".join(part for part in (_header(path, metadata), inner.text) if part)
    return Extraction(
        format=f"p7m/{inner.format}",
        text=text,
        metadata={**metadata, "inner": dict(inner.metadata)},
        media=inner.media,
        warnings=(
            *inner.warnings,
            "the envelope was opened and the document inside it was read in full; "
            "the cryptographic signature itself was not verified",
        ),
    )


def read_detached_signature(path: Path, opts: ReadOptions) -> Extraction:
    """A ``.p7s`` signs a file that is not inside it."""
    metadata = {"format": "p7s", "signers": _signers(_as_der(path.read_bytes()))}
    return Extraction(
        format="p7s",
        text=_header(path, metadata),
        metadata=metadata,
        warnings=(
            "this is a detached signature: the document it signs is a separate file, "
            "usually the one with the same name without the .p7s suffix",
        ),
    )


# ── Envelope ──────────────────────────────────────────────────────────────────


def _as_der(raw: bytes) -> bytes:
    """Normalise to DER: some senders base64 the whole envelope, some PEM it."""
    if _PEM_HEADER.search(raw[:200]):
        body = b"".join(line for line in raw.splitlines() if line and not line.startswith(b"-----"))
        try:
            return base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            return raw

    stripped = bytes(c for c in raw[:512] if c not in b"\r\n")
    if stripped[:1] != b"\x30" and re.fullmatch(rb"[A-Za-z0-9+/=]+", stripped or b"x"):
        try:
            return base64.b64decode(b"".join(raw.split()), validate=False)
        except (binascii.Error, ValueError):
            return raw

    return raw


def _signers(envelope: bytes) -> list[dict[str, str]]:
    """What the envelope claims about who signed it. Asserted, not verified."""
    certificates: list[x509.Certificate] = []
    for loader in (pkcs7.load_der_pkcs7_certificates, pkcs7.load_pem_pkcs7_certificates):
        try:
            certificates = loader(envelope)
            break
        except Exception:  # noqa: BLE001 — the other loader is the recovery
            continue

    out = []
    for certificate in certificates:
        try:
            out.append(
                {
                    "subject": certificate.subject.rfc4514_string(),
                    "issuer": certificate.issuer.rfc4514_string(),
                    "serial": str(certificate.serial_number),
                    "not_valid_after": certificate.not_valid_after_utc.isoformat(),
                }
            )
        except Exception as exc:  # noqa: BLE001 — a malformed cert is not a failed read
            logger.debug(f"unreadable certificate in envelope: {exc}")
    return out


def _unwrap(envelope: bytes) -> tuple[bytes | None, str]:
    """The document inside, and how it was got out."""
    payload = _unwrap_asn1(envelope)
    if payload:
        return payload, "asn1crypto"

    payload = _unwrap_openssl(envelope)
    if payload:
        return payload, "openssl"

    payload = _unwrap_by_magic(envelope)
    if payload:
        return payload, "byte-scan (unparsed)"

    return None, ""


def _unwrap_asn1(envelope: bytes) -> bytes | None:
    try:
        from asn1crypto import cms  # noqa: PLC0415 — optional, and the best path
    except ImportError:
        return None

    try:
        info = cms.ContentInfo.load(envelope)
        content = info["content"]["encap_content_info"]["content"]
        return bytes(content.native) if content.native is not None else None
    except Exception as exc:  # noqa: BLE001 — fall through to the next strategy
        logger.debug(f"asn1crypto could not open the envelope: {exc}")
        return None


def _unwrap_openssl(envelope: bytes) -> bytes | None:
    binary = shutil.which("openssl")
    if not binary:
        return None

    with tempfile.TemporaryDirectory(prefix="annona-p7m-") as scratch:
        source = Path(scratch) / "envelope.p7m"
        source.write_bytes(envelope)
        for form in ("DER", "PEM"):
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [
                    binary,
                    "smime",
                    "-verify",
                    "-noverify",
                    "-binary",
                    "-inform",
                    form,
                    "-in",
                    str(source),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
    return None


def _unwrap_by_magic(envelope: bytes) -> bytes | None:
    """Find the payload by looking for what it starts with.

    Only reached when nothing on the machine can parse ASN.1. It finds the
    document; it does not tell you the envelope was well-formed, and the
    metadata records that distinction.
    """
    for magic, suffix in _MAGIC:
        start = envelope.find(magic)
        if start < 0:
            continue
        if suffix == ".xml":
            match = _XML_ROOT.search(envelope, start)
            if match:
                closing = b"</" + match.group(1) + b">"
                end = envelope.rfind(closing)
                if end > start:
                    return envelope[start : end + len(closing)]
        return envelope[start:]
    return None


# ── Output ────────────────────────────────────────────────────────────────────


def _write_payload(path: Path, payload: bytes, opts: ReadOptions) -> Path:
    """Put the unwrapped document beside its envelope, so it can be read again.

    Beside it rather than in a temp directory on purpose: the payload of a
    restricted file is restricted, and a temp directory is exactly the place a
    policy was never written about.
    """
    name = path.name
    for suffix in (".p7m", ".P7M"):
        name = name.removesuffix(suffix)
    if not Path(name).suffix:
        name += next((s for magic, s in _MAGIC if payload.startswith(magic)), ".bin")

    target = opts.derived_dir(path) / name
    target.write_bytes(payload)
    logger.info(f"unwrapped {path.name} → {target}")
    return target


def _header(path: Path, metadata: dict[str, Any]) -> str:
    lines = [f"=== Signed envelope: {path.name} ==="]
    signers = metadata.get("signers") or []
    if signers:
        for signer in signers:
            lines.append(f"Certificato: {signer['subject']}")
            lines.append(f"  rilasciato da: {signer['issuer']}")
    else:
        lines.append("Nessun certificato leggibile nell'envelope.")
    lines.append(
        "Contenitore aperto: il documento firmato è riportato qui sotto. "
        "La firma crittografica non è stata verificata."
    )
    return "\n".join(lines)
