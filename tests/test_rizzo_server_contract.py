"""The rizzo-pii adapter against a server, over a real socket.

Every other test of this adapter injects a fake HTTP client, which proves the
parsing and proves nothing about the contract. This one starts a server that
answers the way the reference application does — the same routes, the same JSON
keys, the same status codes — and drives the real ``httpx`` path through it.

The stub is written from ``src/app/app.py`` of
`Rizzo-AI-Academy/rizzo-pii <https://github.com/Rizzo-AI-Academy/rizzo-pii>`_:

``POST /analyze``  → ``anonymized_text``, ``mapping``, ``mapping_enabled``,
                     ``by_label``, ``by_source``, ``n_entities``; ``{"error": …}``
                     with 400 when there is nothing to analyse
``GET  /health``   → ``model_loaded``, and **503** while the 0.3B model is still
                     loading

What it does not do is detect anything: the entity list is fixed. Detection
quality is the model's business and is measured in that repository. What is
measured here is the seam — and the seam is where an integration rots quietly,
because a redactor that returns the text unchanged still looks like a success.

To run the same tests against the real server::

    git clone https://github.com/Rizzo-AI-Academy/rizzo-pii && cd rizzo-pii
    pip install -r requirements.txt && python src/app/app.py
    ANNONA_RIZZO_ENDPOINT=http://127.0.0.1:5005 pytest tests/test_rizzo_server_contract.py
"""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from runner.capability.redactors.rizzo_pii import RizzoPiiRedactor
from runner.kernel.errors import BackendUnavailableError

LETTER = (
    "Il Sig. Mario Rossi, C.F. RSSMRA85T10A562S, residente in Via Garibaldi 12, "
    "Milano, chiede una proroga per la pratica 1234/2024."
)

ENTITIES = [
    ("FULLNAME", "Mario Rossi", "model"),
    ("CF", "RSSMRA85T10A562S", "regex"),
    ("STREET", "Via Garibaldi", "model"),
    ("CITY", "Milano", "model"),
    ("DOCID", "1234/2024", "regex"),
]


def _analyse(text: str, *, excluded: set[str], include_mapping: bool) -> dict:
    """The reference server's ``analyze``, with a fixed entity list."""
    counters: dict[str, int] = {}
    mapping: dict[str, str] = {}
    by_label: dict[str, int] = {}
    by_source: dict[str, int] = {}
    out = text

    for label, value, source in ENTITIES:
        if label in excluded or value not in out:
            continue
        counters[label] = counters.get(label, 0) + 1
        placeholder = f"[{label}_{counters[label]}]"
        out = out.replace(value, placeholder)
        by_label[label] = by_label.get(label, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        if include_mapping:
            mapping[placeholder] = value

    return {
        "anonymized_text": out,
        "mapping": mapping,
        "mapping_enabled": include_mapping,
        "by_label": by_label,
        "by_source": by_source,
        "n_entities": sum(by_label.values()),
        "n_chars": len(text),
    }


class Handler(BaseHTTPRequestHandler):
    model_loaded = True

    def log_message(self, *args):  # noqa: A003 - silence the default stderr logging
        pass

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.startswith("/health"):
            ready = type(self).model_loaded
            self._send(
                200 if ready else 503,
                {"status": "ok" if ready else "loading", "model_loaded": ready, "tags": 22},
            )
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self.path.startswith("/analyze"):
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        text = str(request.get("text", ""))

        if not text.strip():
            self._send(400, {"error": "Nessun testo da analizzare."})
            return

        self._send(
            200,
            _analyse(
                text,
                excluded={str(t).upper() for t in request.get("exclude_tags", [])},
                include_mapping=bool(request.get("include_mapping", True)),
            ),
        )


@pytest.fixture(scope="module")
def endpoint():
    """A rizzo-pii-shaped server, or the real one when one is running."""
    real = os.getenv("ANNONA_RIZZO_ENDPOINT")
    if real:
        yield real.rstrip("/")
        return

    Handler.model_loaded = True
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# ── The seam ──────────────────────────────────────────────────────────────────


def test_a_letter_comes_back_without_its_identifiers(endpoint):
    redaction = RizzoPiiRedactor(endpoint).analyse(LETTER)

    assert "Mario Rossi" not in redaction.text
    assert "RSSMRA85T10A562S" not in redaction.text
    assert "[FULLNAME_1]" in redaction.text
    assert redaction.count >= 2
    # And the sentence still reads, which is what makes the answer usable.
    assert "chiede una proroga" in redaction.text


def test_the_mapping_comes_back_and_reverses_exactly(endpoint):
    from runner.policy.redaction import restore

    redaction = RizzoPiiRedactor(endpoint).analyse(LETTER)

    assert restore(redaction.text, redaction.mapping) == LETTER


def test_the_labels_are_the_vocabulary_the_policy_maps(endpoint):
    """The classes a policy assigns are keyed on these names, so they matter."""
    redaction = RizzoPiiRedactor(endpoint).analyse(LETTER)

    assert set(redaction.labels) <= {
        "FULLNAME",
        "CF",
        "STREET",
        "CITY",
        "DOCID",
        "AGE",
        "GENDER",
        "DATE",
        "TIME",
        "BUILDINGNUM",
        "ZIPCODE",
        "PROVINCE",
        "EMAIL",
        "TELEPHONENUM",
        "PIVA",
        "ID_DOC",
        "IBAN",
        "CREDITCARDNUMBER",
        "AMOUNT",
        "TARGA",
        "ORG",
        "CATASTO",
        "URL",
    }
    assert redaction.labels.get("CF") == 1


def test_an_excluded_tag_is_left_in_place(endpoint):
    """A deployment can decide a category is not worth replacing. It is a decision."""
    redaction = RizzoPiiRedactor(endpoint, exclude_tags=["CITY"]).analyse(LETTER)

    assert "Milano" in redaction.text
    assert "Mario Rossi" not in redaction.text


def test_definitive_anonymisation_returns_nothing_to_restore_with(endpoint):
    redaction = RizzoPiiRedactor(endpoint, keep_mapping=False).analyse(LETTER)

    assert "[FULLNAME_1]" in redaction.text
    assert redaction.mapping == {}


def test_empty_text_never_reaches_the_server(endpoint):
    """The reference server answers 400 for empty input; the adapter never asks."""
    redaction = RizzoPiiRedactor(endpoint).analyse("   ")

    assert redaction.text == "   "
    assert redaction.mapping == {}


def test_health_is_readiness_not_liveness(endpoint):
    if os.getenv("ANNONA_RIZZO_ENDPOINT"):
        pytest.skip("the real server decides its own readiness")

    redactor = RizzoPiiRedactor(endpoint)
    assert redactor.health() is True

    Handler.model_loaded = False
    try:
        # The process still answers; the model is not there yet. Reporting this
        # as healthy would mean the first real request is the one that discovers
        # the redactor cannot redact.
        assert redactor.health() is False
    finally:
        Handler.model_loaded = True


def test_an_unreachable_server_raises_rather_than_returning_the_text():
    """The failure mode that must never be silent."""
    redactor = RizzoPiiRedactor("http://127.0.0.1:1")  # nothing listens on port 1

    with pytest.raises(BackendUnavailableError, match="unreachable"):
        redactor.analyse(LETTER)


def test_the_placeholder_shape_is_the_one_the_perimeter_can_reverse(endpoint):
    """The two halves have to agree, or restoration silently does nothing."""
    from runner.policy.redaction import PLACEHOLDER_PATTERN

    redaction = RizzoPiiRedactor(endpoint).analyse(LETTER)
    found = {f"[{label}_{index}]" for label, index in PLACEHOLDER_PATTERN.findall(redaction.text)}

    assert found, "no placeholder matched the pattern the perimeter restores"
    assert found <= set(redaction.mapping)
    assert not re.search(r"<[A-Z_]+>", redaction.text), "an unexpected placeholder dialect"
