"""Tests for attachments: what can be read, what happens on the way in, and
what the perimeter does about it.

The interesting assertions here are not "the parser works". They are the ones
about honesty and about ordering:

- a degraded read reports *why* it was degraded instead of returning nothing;
- an upload that policy will not let a tool read says so at intake, not after a
  run has already been placed;
- attaching a file classifies it before the first turn, and an image raises the
  vision requirement rather than being quietly dropped into a text model.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runner.kernel.blocks import media_block, media_path, text_block
from runner.kernel.types import (
    Attachment,
    CompletionRequest,
    Requirement,
    SensitivityClass,
    Turn,
)
from runner.policy.loader import parse_policy
from runner.services import attachments as inbox
from runner.tools.extractors import capabilities, extract, family_for, supported_extensions

# ── Sample material ───────────────────────────────────────────────────────────

FATTURA = """<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
 <FatturaElettronicaHeader>
  <CedentePrestatore><DatiAnagrafici>
    <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>01234567890</IdCodice></IdFiscaleIVA>
    <Anagrafica><Denominazione>Fornitore Srl</Denominazione></Anagrafica>
  </DatiAnagrafici></CedentePrestatore>
  <CessionarioCommittente><DatiAnagrafici>
    <CodiceFiscale>BLLMTT80A01F205X</CodiceFiscale>
    <Anagrafica><Denominazione>Akaion Srl</Denominazione></Anagrafica>
  </DatiAnagrafici></CessionarioCommittente>
 </FatturaElettronicaHeader>
 <FatturaElettronicaBody>
  <DatiGenerali><DatiGeneraliDocumento>
    <TipoDocumento>TD01</TipoDocumento><Divisa>EUR</Divisa>
    <Data>2026-07-27</Data><Numero>243540</Numero>
    <ImportoTotaleDocumento>7158.78</ImportoTotaleDocumento>
  </DatiGeneraliDocumento></DatiGenerali>
  <DatiBeniServizi><DettaglioLinee>
    <NumeroLinea>1</NumeroLinea><Descrizione>DGX Spark</Descrizione>
    <Quantita>1.00</Quantita><PrezzoUnitario>5818.85</PrezzoUnitario>
    <PrezzoTotale>5818.85</PrezzoTotale><AliquotaIVA>22.00</AliquotaIVA>
  </DettaglioLinee></DatiBeniServizi>
 </FatturaElettronicaBody>
</p:FatturaElettronica>
"""

POLICY = """
version: 1
default: deny

classes:
  restricted:
    paths: ["/**/*.dcm"]
    patterns: ['[A-Z]{6}\\d{2}[A-Z]\\d{2}[A-Z]\\d{3}[A-Z]']
  internal:
    paths: ["~/**"]
    default: true
  public: {}

substrates:
  - id: local-gpu
    kind: ollama
    endpoint: http://localhost:11434
    model: qwen2.5:3b
    jurisdiction: on-prem
    max_class: restricted
    tools: true

rules:
  - match: {class: restricted}
    allow: [local-gpu]
  - match: {class: internal}
    allow: [local-gpu]
  - match: {class: public}
    allow: [local-gpu]

tools:
  allow:
    document_reader: ["{inbox}/**"]
  deny_paths: ["~/.ssh/**"]
"""


@pytest.fixture
def policy(tmp_path):
    """A policy whose only readable directory is the inbox this test uses."""
    return parse_policy(
        __import__("yaml").safe_load(POLICY.replace("{inbox}", str(tmp_path / "inbox"))),
        source="<test>",
    )


@pytest.fixture
def inbox_dir(tmp_path, monkeypatch):
    target = tmp_path / "inbox"
    target.mkdir()
    monkeypatch.setenv("ANNONA_INBOX", str(target))
    return target


# ── The readers ───────────────────────────────────────────────────────────────


class TestExtraction:
    def test_an_italian_e_invoice_is_rendered_not_dumped(self, tmp_path):
        path = tmp_path / "fattura.xml"
        path.write_text(FATTURA, encoding="utf-8")

        result = extract(path)

        assert result.format == "fatturapa"
        # The facts an accountant asks for, not an XML outline.
        assert "Fornitore Srl" in result.text
        assert "243540" in result.text
        assert "7158.78" in result.text
        assert "DGX Spark" in result.text

    def test_a_signed_envelope_is_opened_and_its_document_read(self, tmp_path):
        if not shutil.which("openssl"):
            pytest.skip("openssl is needed to build a .p7m to open")

        source = tmp_path / "fattura.xml"
        source.write_text(FATTURA, encoding="utf-8")
        key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "2",
                "-nodes",
                "-subj",
                "/CN=Firmatario",
            ],
            capture_output=True,
            check=True,
        )
        signed = tmp_path / "fattura.xml.p7m"
        subprocess.run(
            [
                "openssl",
                "smime",
                "-sign",
                "-binary",
                "-in",
                str(source),
                "-out",
                str(signed),
                "-signer",
                str(cert),
                "-inkey",
                str(key),
                "-outform",
                "DER",
                "-nodetach",
            ],
            capture_output=True,
            check=True,
        )

        result = extract(signed)

        assert result.format == "p7m/fatturapa"
        assert "243540" in result.text
        # Never claim more than was checked: the envelope was opened, the
        # signature was not verified, and the reader says so every time.
        assert any("was not verified" in w for w in result.warnings)
        assert result.metadata["signature_verified"] is False
        assert result.metadata["signers"], "the certificate in the envelope should be reported"

    def test_an_archive_reads_its_members(self, tmp_path):
        path = tmp_path / "pacco.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("nota.txt", "contenuto interno")
            archive.writestr("dati.csv", "nome;importo\nA;10\n")

        result = extract(path)

        assert result.format == "zip"
        assert "contenuto interno" in result.text
        assert "importo" in result.text
        assert result.metadata["members_read"] == ["nota.txt", "dati.csv"]

    def test_an_archive_says_what_it_did_not_open(self, tmp_path):
        from runner.tools.extractors import ReadOptions

        path = tmp_path / "grosso.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for index in range(5):
                archive.writestr(f"file{index}.txt", f"riga {index}")

        result = extract(path, ReadOptions(max_members=2))

        assert any("listed but not" in warning for warning in result.warnings)

    def test_an_email_carries_its_attachment_through(self, tmp_path):
        path = tmp_path / "messaggio.eml"
        path.write_text(
            "From: mario@example.com\n"
            "To: akaion@example.com\n"
            "Subject: Offerta\n"
            'Content-Type: multipart/mixed; boundary="B"\n'
            "\n--B\nContent-Type: text/plain; charset=utf-8\n\nIn allegato.\n"
            '\n--B\nContent-Type: text/csv; name="listino.csv"\n'
            'Content-Disposition: attachment; filename="listino.csv"\n'
            "\nvoce;prezzo\nDGX;5818.85\n\n--B--\n",
            encoding="utf-8",
        )

        result = extract(path)

        assert result.format == "eml"
        assert "mario@example.com" in result.text
        assert "In allegato." in result.text
        # The attachment is read, and kept where the operator can find it again.
        assert "5818.85" in result.text
        assert Path(result.metadata["attachments"][0]["path"]).is_file()

    def test_a_calendar_unfolds_its_long_lines(self, tmp_path):
        path = tmp_path / "evento.ics"
        path.write_text(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Call\r\n"
            "DESCRIPTION:prima parte\r\n  e continuazione\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        result = extract(path)

        assert result.metadata["events"] == 1
        assert "prima parte e continuazione" in result.text

    def test_an_image_offers_its_pixels_without_inlining_them(self, tmp_path):
        pillow = pytest.importorskip("PIL.Image")
        path = tmp_path / "foto.png"
        pillow.new("RGB", (32, 24), (10, 20, 30)).save(path)

        result = extract(path)

        assert result.metadata["width"] == 32
        assert len(result.media) == 1
        assert result.media[0].media_type == "image"
        # A reference, never bytes: the transcript stays cheap and placement
        # still gets to decide whether anything may look at it.
        assert result.media[0].path == path

    def test_an_unknown_extension_is_read_as_text_and_admits_the_guess(self, tmp_path):
        path = tmp_path / "strano.qqq"
        path.write_text("contenuto")

        result = extract(path)

        assert "contenuto" in result.text
        assert result.metadata["guessed"] is True
        assert any("no reader is registered" in w for w in result.warnings)

    def test_a_missing_dependency_is_a_warning_not_a_failure(self, tmp_path):
        path = tmp_path / "studio.dcm"
        path.write_bytes(b"DICM not really")

        result = extract(path)

        assert result.format == "dicom"
        assert result.warnings, "an unreadable format must say what is missing"

    def test_the_installation_reports_what_it_can_read(self):
        report = capabilities()

        assert "families" in report and "extras" in report
        for family, entry in report["families"].items():
            # Either it works, or there is a command that makes it work. Never
            # a family that is simply off with no explanation.
            assert entry["ready"] or entry["install"], family
        assert ".p7m" in supported_extensions()
        assert family_for("x.dcm") == "medical"
        assert family_for("x.mp4") == "video"


# ── Taking a file in ──────────────────────────────────────────────────────────


class TestIntake:
    def test_a_stored_file_is_identified_by_its_digest(self, inbox_dir, tmp_path):
        source = tmp_path / "report.txt"
        source.write_bytes(b"contenuto")

        with source.open("rb") as handle:
            first = inbox.store("report.txt", handle)
        with source.open("rb") as handle:
            second = inbox.store("report.txt", handle)

        assert first.id == second.id
        assert first.path == second.path
        assert len(list(inbox_dir.iterdir())) == 1, "the same file twice is one attachment"
        assert inbox.display_name(first.path) == "report.txt"

    def test_a_filename_cannot_escape_the_inbox(self, inbox_dir, tmp_path):
        source = tmp_path / "evil"
        source.write_bytes(b"x")

        with source.open("rb") as handle:
            stored = inbox.store("../../../etc/passwd", handle)

        assert stored.path.parent == inbox_dir
        assert "/" not in stored.name

    def test_an_oversized_file_is_refused_and_leaves_nothing_behind(self, inbox_dir, tmp_path):
        source = tmp_path / "big.bin"
        source.write_bytes(b"x" * (2 * 1024 * 1024))

        with source.open("rb") as handle, pytest.raises(ValueError, match="larger than"):
            inbox.store("big.bin", handle, config={"attachments": {"max_size_mb": 1}})

        assert list(inbox_dir.iterdir()) == [], "a refused upload must not leave a partial file"

    def test_an_identifier_from_a_url_cannot_traverse(self, inbox_dir):
        assert inbox.resolve("../../etc") is None
        assert inbox.resolve("not-hex") is None
        assert inbox.remove("../../etc") is False


class TestDescribe:
    def test_content_decides_the_class_not_only_the_folder(self, inbox_dir, policy):
        # A codice fiscale in the file, in a directory the policy calls internal.
        target = inbox_dir / "fattura.xml"
        target.write_text(FATTURA, encoding="utf-8")

        described = inbox.describe(target, policy=policy)

        assert described["class"] == SensitivityClass.RESTRICTED.label
        assert described["family"] == "signed" or described["format"] == "fatturapa"

    def test_a_stored_file_outside_the_allow_list_says_so_at_intake(self, tmp_path, policy):
        elsewhere = tmp_path / "altrove"
        elsewhere.mkdir()
        target = elsewhere / "nota.txt"
        target.write_text("qualcosa")

        described = inbox.describe(target, policy=policy)

        assert described["readable"] is False
        assert described["reason"]
        # The fix, in the shape it has to be pasted in. An operator should never
        # have to guess the YAML from a refusal.
        assert "document_reader" in described["fix"]

    def test_the_preview_does_not_transcribe(self, inbox_dir):
        """Intake stays fast: expensive reading happens inside a placed run.

        A person dropping a two-hour recording into the window gets a chip back
        immediately; the speech model runs later, inside a run that was placed
        and recorded.
        """
        from runner.tools.extractors import ReadOptions
        from runner.tools.extractors.av import transcribe

        target = inbox_dir / "memo.wav"
        target.write_bytes(b"RIFF....WAVEfmt ")

        assert transcribe(target, ReadOptions(transcribe="never")) == ("", "")
        assert "Trascrizione" not in inbox.describe(target)["preview"]

    def test_the_preamble_names_every_path_and_the_tool_to_use(self):
        text = inbox.preamble(
            [
                {
                    "name": "a.pdf",
                    "path": "/tmp/a.pdf",
                    "family": "document",
                    "format": "pdf",
                    "bytes": 2048,
                    "warnings": ["scanned"],
                }
            ]
        )

        assert "/tmp/a.pdf" in text
        assert "document_reader" in text
        assert "scanned" in text


# ── What the perimeter does with them ─────────────────────────────────────────


class TestPerimeter:
    def test_media_blocks_carry_a_path_that_the_router_can_classify(self, tmp_path):
        block = media_block(tmp_path / "scan.dcm", "image")

        assert media_path(block) == str(tmp_path / "scan.dcm")
        assert media_path(text_block("hello")) == ""

    def test_an_attached_image_makes_the_turn_require_vision(self, tmp_path):
        from runner.placement.router import _carries_media

        with_image = CompletionRequest(
            system="",
            transcript=(
                Turn(role="user", blocks=(text_block("guarda"), media_block(tmp_path / "a.png"))),
            ),
        )
        text_only = CompletionRequest(
            system="", transcript=(Turn(role="user", blocks=(text_block("guarda"),)),)
        )

        assert _carries_media(with_image) is True
        assert _carries_media(text_only) is False

    def test_a_substrate_that_cannot_see_is_not_chosen_for_an_image(self, policy):
        from runner.placement.engine import PlacementDecisionEngine
        from runner.placement.registry import SubstrateRegistry

        engine = PlacementDecisionEngine(
            policy, SubstrateRegistry.from_substrates(policy.substrates, prober=None)
        )

        placement = engine.place(SensitivityClass.INTERNAL, Requirement(vision=True))

        assert placement.permitted is False
        assert any("cannot read images" in why for _, why in placement.rejected)

    def test_attachments_reach_the_first_turn_of_the_loop(self, tmp_path):
        from runner.agent.loop import AgentLoop
        from runner.kernel.types import Completion

        seen: list[CompletionRequest] = []

        class Backend:
            name = "spy"
            capabilities = None

            def complete(self, request):
                seen.append(request)
                return Completion(text_parts=("done",))

        class Executor:
            def specs(self):
                return ()

            def invoke(self, call):  # pragma: no cover - no tools in this run
                raise AssertionError

        class Gate:
            def permits(self, call):  # pragma: no cover
                return True

        AgentLoop(Backend(), Executor(), Gate()).run(
            "leggi", None, 1, [Attachment(path=str(tmp_path / "a.png"), media_type="image")]
        )

        blocks = seen[0].transcript[0].blocks
        assert [media_path(b) for b in blocks if media_path(b)] == [str(tmp_path / "a.png")]

    def test_attached_files_are_read_before_the_first_turn(self, tmp_path):
        """The reliability guarantee, and the one that had to be tested.

        Told to call a reader, a small local model complies most of the time.
        The times it does not, it answers about a document it never opened —
        so the read is performed by the kernel, through the same gate, before
        the model gets its first turn.
        """
        from runner.agent.loop import AgentLoop
        from runner.kernel.types import Completion, ToolCall, ToolResult

        seen: list[CompletionRequest] = []
        ran: list[str] = []

        class Backend:
            name = "spy"
            capabilities = None

            def complete(self, request):
                seen.append(request)
                return Completion(text_parts=("done",))

        class Executor:
            def specs(self):
                return ()

            def invoke(self, call):
                ran.append(call.arguments["path"])
                return ToolResult(
                    call_id=call.id, name=call.name, content={"content": "IBAN IT60…"}
                )

        class Gate:
            def permits(self, call):
                return True

        result = AgentLoop(Backend(), Executor(), Gate()).run(
            "riassumi",
            None,
            1,
            (),
            [ToolCall(id="attach_0", name="document_reader", arguments={"path": "/tmp/a.pdf"})],
        )

        assert ran == ["/tmp/a.pdf"], "the attachment must be read without being asked for"
        # The content reached the model on its very first turn, not the second.
        assert "IBAN" in str(seen[0].transcript[-1].blocks[0].result)
        assert result.tool_calls[0].tool == "document_reader"

    def test_a_refused_prefetch_is_content_not_silence(self, tmp_path):
        from runner.agent.loop import AgentLoop
        from runner.kernel.types import Completion, ToolCall

        class Backend:
            name = "spy"
            capabilities = None

            def complete(self, request):
                return Completion(text_parts=("done",))

        class Executor:
            def specs(self):
                return ()

            def invoke(self, call):  # pragma: no cover — the gate refuses first
                raise AssertionError("a refused call must not run")

        class Gate:
            def permits(self, call):
                return False

        result = AgentLoop(Backend(), Executor(), Gate()).run(
            "riassumi",
            None,
            1,
            (),
            [ToolCall(id="attach_0", name="document_reader", arguments={"path": "/etc/shadow"})],
        )

        assert result.tool_calls[0].error is True
        assert "Permission denied" in str(result.tool_calls[0].result)

    def test_media_is_only_offered_when_something_could_see_it(self, policy):
        assert inbox.vision_families(policy) is False
        assert (
            inbox.attachments_for(
                [
                    {
                        "name": "a.png",
                        "media": [
                            {
                                "path": "/tmp/a.png",
                                "media_type": "image",
                                "label": "",
                                "derived": False,
                            }
                        ],
                    }
                ]
            )[0].path
            == "/tmp/a.png"
        )
        # Audio is read by a tool, never turned into a vision requirement.
        assert (
            inbox.attachments_for(
                [
                    {
                        "name": "a.wav",
                        "media": [
                            {
                                "path": "/tmp/a.wav",
                                "media_type": "audio",
                                "label": "",
                                "derived": False,
                            }
                        ],
                    }
                ]
            )
            == []
        )


# ── The HTTP surface ──────────────────────────────────────────────────────────


@pytest.fixture
def client(inbox_dir):
    from runner.kernel_api import kernel_router

    app = FastAPI()
    app.include_router(kernel_router(None))
    return TestClient(app)


class TestEndpoints:
    def test_formats_reports_the_inbox_and_what_is_missing(self, client, inbox_dir):
        body = client.get("/api/kernel/formats").json()

        assert body["inbox"] == str(inbox_dir)
        assert body["max_upload_mb"] > 0
        assert ".p7m" in body["extensions"]
        assert "families" in body

    def test_upload_stores_the_file_and_describes_it(self, client, inbox_dir):
        response = client.post(
            "/api/kernel/attachments",
            files={"file": ("fattura.xml", FATTURA.encode(), "application/xml")},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "fattura.xml"
        assert body["format"] == "fatturapa"
        assert Path(body["path"]).is_file()
        assert Path(body["path"]).parent == inbox_dir

        listed = client.get("/api/kernel/attachments").json()["attachments"]
        assert [a["id"] for a in listed] == [body["id"]]

        assert client.delete(f"/api/kernel/attachments/{body['id']}").status_code == 200
        assert client.delete(f"/api/kernel/attachments/{body['id']}").status_code == 404

    def test_an_ask_without_an_executor_still_refuses_cleanly(self, client):
        response = client.post(
            "/api/kernel/ask", json={"prompt": "leggi", "attachments": ["/nope.pdf"]}
        )

        assert response.status_code == 503

    def test_a_missing_attachment_does_not_lose_the_run(self, tmp_path):
        from runner.kernel_api import AskRequest, _with_attachments

        prompt, media, reads = _with_attachments(
            AskRequest(prompt="riassumi", attachments=[str(tmp_path / "assente.pdf")]), None
        )

        assert "riassumi" in prompt
        assert "does not exist" in prompt
        assert media == []
        # And nothing is queued to read: a path that is not there must not
        # become a tool call that fails for a second, less obvious reason.
        assert reads == []


class TestCache:
    """Reading the same bytes twice costs one extraction.

    The saving is real on a shared appliance — twelve people opening the same
    data room extract the same contract twelve times — and the reason it is safe
    is that it crosses no boundary: the key is the file's own digest, and the
    entry lives beside the file, under the same access control.
    """

    def test_the_second_read_of_the_same_bytes_is_served_from_cache(self, tmp_path):
        from runner.tools.extractors import extract

        target = tmp_path / "nota.txt"
        target.write_text("contenuto della pratica")

        first = extract(target)
        second = extract(target)

        assert first.metadata.get("cached", False) is False
        assert second.metadata["cached"] is True
        assert second.text == first.text

    def test_changing_the_file_misses(self, tmp_path):
        """No invalidation logic to get wrong: different bytes, different key."""
        from runner.tools.extractors import extract

        target = tmp_path / "nota.txt"
        target.write_text("prima versione")
        extract(target)

        target.write_text("seconda versione")
        again = extract(target)

        assert again.metadata.get("cached", False) is False
        assert "seconda" in again.text

    def test_a_read_only_source_tree_still_caches(self, tmp_path, monkeypatch):
        """The appliance case: material is mounted read-only, on purpose.

        Writing beside the source fails there, and the failure would be silent —
        a signed invoice that never unwraps and a cache that never hits, on a
        deployment that looks like it is working.
        """
        from runner.tools.extractors import extract

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("ANNONA_HOME", str(home))

        material = tmp_path / "material"
        material.mkdir()
        target = material / "nota.txt"
        target.write_text("pratica del cliente")
        material.chmod(0o555)

        try:
            extract(target)
            second = extract(target)
        finally:
            material.chmod(0o755)

        assert second.metadata["cached"] is True
        assert list((home / "derived").rglob("*.json")), "the entry went somewhere writable"

    def test_caching_can_be_turned_off(self, tmp_path, monkeypatch):
        from runner.tools.extractors import extract

        monkeypatch.setenv("ANNONA_NO_CACHE", "1")
        target = tmp_path / "nota.txt"
        target.write_text("contenuto")

        extract(target)

        assert not list(tmp_path.rglob("*.annona-cache*"))
