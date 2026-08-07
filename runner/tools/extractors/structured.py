"""Readers for structured text: XML, the Italian e-invoice, and calendars.

XML gets its own reader because dumping the raw markup at a 7B model wastes
most of the context on angle brackets. It gets a *second* reader because one
XML dialect matters more than the rest on the machines this runs on: FatturaPA,
the format every invoice issued in Italy is transmitted in. A generic outline
of that document is technically correct and commercially useless — the answer
someone wants is who billed whom, for what, and how much, and that is a
rendering job, not a parsing job.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from runner.tools.extractors.types import Extraction, ReadOptions

__all__ = ["is_fattura", "read_calendar", "read_xml", "render_fattura"]

_NS = re.compile(r"^\{[^}]*\}")
_MAX_NODES = 4000


def _tag(element: ET.Element) -> str:
    return _NS.sub("", element.tag)


def _find(root: ET.Element, *names: str) -> ET.Element | None:
    """Walk down a path of tag names, ignoring namespaces."""
    current: ET.Element | None = root
    for name in names:
        if current is None:
            return None
        current = next((child for child in current if _tag(child) == name), None)
    return current


def _text(root: ET.Element | None, *names: str) -> str:
    if root is None:
        return ""
    node = _find(root, *names) if names else root
    return (node.text or "").strip() if node is not None else ""


def _all(root: ET.Element | None, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _tag(e) == name] if root is not None else []


def is_fattura(root: ET.Element) -> bool:
    """Whether this XML is an Italian electronic invoice."""
    return _tag(root).startswith("FatturaElettronica")


def read_xml(path: Path, opts: ReadOptions) -> Extraction:
    """One XML file, rendered as whatever it turns out to be."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return Extraction(
            format="xml",
            text=path.read_text(encoding="utf-8", errors="replace"),
            metadata={"format": "xml", "parsed": False},
            warnings=(f"the XML is malformed ({exc}); the raw text is above",),
        )

    if is_fattura(root):
        return render_fattura(root)

    return Extraction(
        format="xml",
        text=_outline(root),
        metadata={"format": "xml", "root": _tag(root)},
    )


def _outline(root: ET.Element) -> str:
    """A flat ``path: value`` rendering — every leaf that carries text.

    Bounded at :data:`_MAX_NODES`: an export with 200,000 elements is a
    database, and a model asked to read one of those needs a query, not a dump.
    """
    lines: list[str] = []

    def walk(element: ET.Element, prefix: str) -> None:
        if len(lines) >= _MAX_NODES:
            return
        name = f"{prefix}/{_tag(element)}" if prefix else _tag(element)
        value = (element.text or "").strip()
        if value:
            lines.append(f"{name}: {value}")
        for key, attribute in element.attrib.items():
            lines.append(f"{name}@{_NS.sub('', key)}: {attribute}")
        for child in element:
            walk(child, name)

    walk(root, "")
    if len(lines) >= _MAX_NODES:
        lines.append(f"[... outline stopped at {_MAX_NODES} nodes ...]")
    return "\n".join(lines)


# ── FatturaPA ─────────────────────────────────────────────────────────────────

_DOCUMENT_TYPES = {
    "TD01": "Fattura",
    "TD02": "Acconto/anticipo su fattura",
    "TD03": "Acconto/anticipo su parcella",
    "TD04": "Nota di credito",
    "TD05": "Nota di debito",
    "TD06": "Parcella",
    "TD16": "Integrazione fattura reverse charge interno",
    "TD17": "Integrazione/autofattura acquisto servizi dall'estero",
    "TD18": "Integrazione acquisto beni intracomunitari",
    "TD19": "Integrazione/autofattura acquisto beni ex art.17 c.2",
    "TD20": "Autofattura per regolarizzazione",
    "TD24": "Fattura differita",
    "TD25": "Fattura differita (art.21 c.4)",
    "TD28": "Acquisti da San Marino con IVA",
}


def _party(node: ET.Element | None) -> str:
    """One line naming a party: business name, VAT number, tax code."""
    if node is None:
        return "—"
    anagrafica = _find(node, "DatiAnagrafici")
    name = _text(anagrafica, "Anagrafica", "Denominazione")
    if not name:
        first = _text(anagrafica, "Anagrafica", "Nome")
        last = _text(anagrafica, "Anagrafica", "Cognome")
        name = " ".join(part for part in (first, last) if part)

    country = _text(anagrafica, "IdFiscaleIVA", "IdPaese")
    vat = _text(anagrafica, "IdFiscaleIVA", "IdCodice")
    fiscal = _text(anagrafica, "CodiceFiscale")

    sede = _find(node, "Sede")
    address = ", ".join(
        part
        for part in (
            _text(sede, "Indirizzo"),
            f"{_text(sede, 'CAP')} {_text(sede, 'Comune')}".strip(),
            _text(sede, "Provincia"),
            _text(sede, "Nazione"),
        )
        if part.strip()
    )

    bits = [name or "—"]
    if vat:
        bits.append(f"P.IVA {country}{vat}")
    if fiscal:
        bits.append(f"CF {fiscal}")
    if address:
        bits.append(address)
    return " · ".join(bits)


def render_fattura(root: ET.Element) -> Extraction:
    """An Italian e-invoice, rendered the way an accountant reads one."""
    header = _find(root, "FatturaElettronicaHeader")
    transmission = _find(header, "DatiTrasmissione") if header is not None else None
    supplier = _find(header, "CedentePrestatore") if header is not None else None
    customer = _find(header, "CessionarioCommittente") if header is not None else None

    lines: list[str] = ["=== Fattura elettronica (FatturaPA) ==="]
    lines.append(f"Cedente/prestatore: {_party(supplier)}")
    lines.append(f"Cessionario/committente: {_party(customer)}")
    if transmission is not None:
        code = _text(transmission, "CodiceDestinatario")
        pec = _text(transmission, "PECDestinatario")
        lines.append(
            f"Trasmissione: progressivo {_text(transmission, 'ProgressivoInvio') or '—'}"
            + (f", destinatario {code}" if code else "")
            + (f", PEC {pec}" if pec else "")
        )

    metadata: dict[str, Any] = {"format": "fatturapa", "bodies": 0, "documents": []}

    for body in _all(root, "FatturaElettronicaBody"):
        metadata["bodies"] += 1
        general = _find(body, "DatiGenerali", "DatiGeneraliDocumento")
        kind = _text(general, "TipoDocumento")
        number = _text(general, "Numero")
        date = _text(general, "Data")
        total = _text(general, "ImportoTotaleDocumento")
        currency = _text(general, "Divisa") or "EUR"

        lines.append("")
        lines.append(
            f"--- {_DOCUMENT_TYPES.get(kind, kind or 'Documento')} n. {number or '—'} "
            f"del {date or '—'} ---"
        )
        metadata["documents"].append(
            {"type": kind, "number": number, "date": date, "total": total, "currency": currency}
        )

        for cause in _all(general, "Causale"):
            if (cause.text or "").strip():
                lines.append(f"Causale: {cause.text.strip()}")

        details = _all(body, "DettaglioLinee")
        if details:
            lines.append("Righe:")
            for detail in details:
                lines.append(
                    "  {n:>3}. {desc} — {qty} {unit} × {price} = {amount} {cur} (IVA {vat}%)".format(
                        n=_text(detail, "NumeroLinea") or "?",
                        desc=_text(detail, "Descrizione") or "—",
                        qty=_text(detail, "Quantita") or "1",
                        unit=_text(detail, "UnitaMisura") or "",
                        price=_text(detail, "PrezzoUnitario") or "—",
                        amount=_text(detail, "PrezzoTotale") or "—",
                        cur=currency,
                        vat=_text(detail, "AliquotaIVA") or "0",
                    ).replace("  ", " ")
                )

        for summary in _all(body, "DatiRiepilogo"):
            lines.append(
                f"Riepilogo IVA {_text(summary, 'AliquotaIVA') or '0'}%: "
                f"imponibile {_text(summary, 'ImponibileImporto') or '—'} {currency}, "
                f"imposta {_text(summary, 'Imposta') or '—'} {currency}"
                + (f" — {_text(summary, 'Natura')}" if _text(summary, "Natura") else "")
            )

        if total:
            lines.append(f"TOTALE DOCUMENTO: {total} {currency}")

        for payment in _all(body, "DettaglioPagamento"):
            lines.append(
                "Pagamento: "
                + ", ".join(
                    part
                    for part in (
                        _text(payment, "ModalitaPagamento"),
                        f"scadenza {_text(payment, 'DataScadenzaPagamento')}"
                        if _text(payment, "DataScadenzaPagamento")
                        else "",
                        f"importo {_text(payment, 'ImportoPagamento')} {currency}"
                        if _text(payment, "ImportoPagamento")
                        else "",
                        f"IBAN {_text(payment, 'IBAN')}" if _text(payment, "IBAN") else "",
                    )
                    if part
                )
            )

        attachments = _all(body, "Allegati")
        for attachment in attachments:
            lines.append(
                f"Allegato: {_text(attachment, 'NomeAttachment') or '—'} "
                f"({_text(attachment, 'FormatoAttachment') or 'sconosciuto'}) — "
                "base64 inside the invoice, not extracted"
            )

    return Extraction(format="fatturapa", text="\n".join(lines), metadata=metadata)


# ── Calendars ─────────────────────────────────────────────────────────────────

_ICS_ESCAPES = {"\\n": "\n", "\\,": ",", "\\;": ";", "\\\\": "\\"}


def read_calendar(path: Path, opts: ReadOptions) -> Extraction:
    """An .ics file as a list of events, without a calendar library.

    iCalendar folds long lines by starting the continuation with a space; a
    reader that ignores that splits every long description in half, which is
    why this unfolds before it parses.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    unfolded: list[str] = []
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in unfolded:
        if line.startswith("BEGIN:VEVENT"):
            current = {}
        elif line.startswith("END:VEVENT"):
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            name, _, value = line.partition(":")
            key = name.split(";", 1)[0].upper()
            for escape, plain in _ICS_ESCAPES.items():
                value = value.replace(escape, plain)
            if key in ("ATTENDEE", "ORGANIZER"):
                current[key] = f"{current.get(key, '')}{value}; ".strip()
            else:
                current.setdefault(key, value)

    blocks = []
    for event in events:
        blocks.append(
            "\n".join(
                f"{label}: {event[key]}"
                for label, key in (
                    ("Titolo", "SUMMARY"),
                    ("Inizio", "DTSTART"),
                    ("Fine", "DTEND"),
                    ("Luogo", "LOCATION"),
                    ("Organizzatore", "ORGANIZER"),
                    ("Partecipanti", "ATTENDEE"),
                    ("Descrizione", "DESCRIPTION"),
                )
                if event.get(key)
            )
        )

    return Extraction(
        format="ics",
        text="\n\n".join(blocks),
        metadata={"format": "ics", "events": len(events)},
    )
