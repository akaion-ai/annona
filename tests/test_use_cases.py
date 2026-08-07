"""The product, told as the six situations it exists for.

Every other test file checks a mechanism. This one checks the *outcomes a
customer is buying*, one test per scenario, written so that the name and the
docstring are the sales argument and the assertions are the proof.

They run against the real agent loop, the real policy engine and the real
ledger, with scripted substrates so the result is deterministic. If a claim in
the README stops being true, one of these goes red.
"""

from __future__ import annotations

import pytest

from runner.agent.loop import AgentLoop
from runner.audit.ledger import verify_file
from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    SensitivityClass,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from runner.policy.loader import parse_policy
from runner.policy.redaction import Redaction
from runner.services.enforcement import Enforcement

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


# ── The deployment every scenario runs in ─────────────────────────────────────


class Substrate:
    """A scripted substrate that records every payload it is sent."""

    def __init__(self, name: str, script=(), *, local: bool = False, fail: bool = False):
        self._name, self._script = name, list(script)
        self._local, self._fail = local, fail
        self.received: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(native_tools=True, is_local=self._local, context_window=128_000)

    @property
    def calls(self) -> int:
        return len(self.received)

    def complete(self, request: CompletionRequest) -> Completion:
        self.received.append(
            request.system
            + "\n"
            + "\n".join(str(getattr(b, "content", b)) for t in request.transcript for b in t.blocks)
        )
        if self._fail:
            from runner.kernel.errors import BackendUnavailableError

            raise BackendUnavailableError(f"{self._name} is unreachable")
        if self._script:
            return self._script.pop(0)
        return Completion(text_parts=(f"{self._name} answered",), stop_reason="end_turn")


READER = ToolSpec(
    name="document_reader",
    description="Read a document.",
    schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
SHELL = ToolSpec(
    name="shell",
    description="Run a shell command.",
    schema={"type": "object", "properties": {"command": {"type": "string"}}},
)


class Desk:
    """The tools a professional's machine actually exposes."""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.executed: list[str] = []

    def specs(self):
        return (READER, SHELL)

    def invoke(self, call: ToolCall) -> ToolResult:
        self.executed.append(call.name)
        if call.name == "shell":
            return ToolResult(call_id=call.id, name=call.name, content="(command output)")
        path = str(call.arguments.get("path", ""))
        if path not in self.files:
            return ToolResult(call_id=call.id, name=call.name, content="not found", is_error=True)
        return ToolResult(call_id=call.id, name=call.name, content=self.files[path])


def studio_policy(root, *, on_unavailable="hold", redaction=None, egress=None):
    """A professional practice: client files on-prem, everything else by policy.

    This is the shape every Italian studio has — a folder of client matters that
    is privileged by law, a folder of ordinary work, and a wish to use the best
    model available for everything that is neither.
    """
    return parse_policy(
        {
            "version": 1,
            "default": "deny",
            "classes": {
                "restricted": {
                    "paths": [f"{root}/clienti/**"],
                    "patterns": [
                        r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]",  # codice fiscale
                        r"\bIT\d{2}[A-Z]\d{10}[0-9A-Z]{12}\b",  # IBAN
                    ],
                },
                "internal": {"paths": [f"{root}/studio/**"]},
                "public": {"default": True},
            },
            "substrates": [
                {
                    "id": "local-gpu",
                    "kind": "echo",
                    "jurisdiction": "on-prem",
                    "max_class": "restricted",
                    "quality": 60,
                    "cost_per_mtok": 0.0,
                },
                {
                    "id": "eu-cluster",
                    "kind": "echo",
                    "jurisdiction": "eu",
                    "max_class": "internal",
                    "quality": 75,
                    "cost_per_mtok": 2.0,
                },
                {
                    "id": "frontier",
                    "kind": "echo",
                    "jurisdiction": "us",
                    "max_class": "public",
                    "quality": 98,
                    "cost_per_mtok": 15.0,
                },
            ],
            "rules": [
                {
                    "id": "R-clienti",
                    "match": {"class": "restricted"},
                    "allow": ["local-gpu"],
                    "on_unavailable": on_unavailable,
                },
                {
                    "id": "R-studio",
                    "match": {"class": "internal"},
                    "allow": ["local-gpu", "eu-cluster"],
                    "on_unavailable": on_unavailable,
                },
                {
                    "id": "R-pubblico",
                    "match": {"class": "public"},
                    "allow": ["frontier", "eu-cluster", "local-gpu"],
                    "prefer": "quality",
                },
            ],
            "tools": {
                # Default-deny in practice: the reader may see the practice's
                # folders, and `shell` is not on the list at all.
                "allow": {"document_reader": [f"{root}/**"]},
                "deny_paths": ["~/.ssh/**", "**/*.pem"],
            },
            **({"redaction": redaction} if redaction else {}),
            **({"egress": egress} if egress else {}),
        }
    )


def desk(tmp_path, *, policy=None, substrates=None, files=None, redactor=None):
    enforcement = Enforcement.for_run(
        policy=policy or studio_policy(tmp_path),
        ledger_path=tmp_path / "ledger.jsonl",
        backends=substrates,
        redactor=redactor,
        probe=False,
        fsync=False,
    )
    tools = Desk(files or {})
    loop = AgentLoop(enforcement.backend(), enforcement.executor(tools), enforcement.gate())
    return enforcement, loop, tools


def reads(path: str):
    return [
        Completion(
            tool_calls=(ToolCall(id="c1", name="document_reader", arguments={"path": path}),),
            stop_reason="tool_use",
        ),
        Completion(text_parts=("done",), stop_reason="end_turn"),
    ]


def matter(tmp_path) -> str:
    folder = tmp_path / "clienti"
    folder.mkdir(exist_ok=True)
    path = folder / "BG-114.txt"
    path.write_text(
        "Pratica 2026/114 — cliente RSSMRA85T10A562S, IBAN IT60X0542811101000000123456.\n"
        "Scadenza deposito: 15 marzo 2026."
    )
    return str(path)


# ── 1 · The law firm: privileged material, best effort, no exceptions ─────────


def test_a_client_matter_is_answered_without_the_matter_leaving(tmp_path):
    """**Studio legale.** An associate asks a question about a client file.

    The frontier model is registered, healthy, and would answer better. It is
    not called, and nobody had to remember not to call it.
    """
    path = matter(tmp_path)
    frontier = Substrate("frontier")
    # Naming the file is already an act with a class: the first turn is placed
    # on-prem before anything has been read.
    local = Substrate(
        "local-gpu",
        [
            Completion(
                tool_calls=(ToolCall(id="c1", name="document_reader", arguments={"path": path}),),
                stop_reason="tool_use",
            ),
            Completion(text_parts=("scadenza: 15 marzo 2026",), stop_reason="end_turn"),
        ],
        local=True,
    )

    enforcement, loop, tools = desk(
        tmp_path,
        substrates={"local-gpu": local, "eu-cluster": Substrate("eu"), "frontier": frontier},
        files={path: (tmp_path / "clienti" / "BG-114.txt").read_text()},
    )
    result = loop.run(f"Leggi {path} e dimmi la scadenza")

    assert "15 marzo" in result.response
    assert enforcement.klass is SensitivityClass.RESTRICTED
    assert frontier.calls == 0, "the better model was available and was not used"
    assert "document_reader" in tools.executed


# ── 2 · The same firm, ordinary questions: the good model, cheaply ────────────


def test_a_question_with_no_client_data_goes_to_the_best_model(tmp_path):
    """**Same deployment, different question.** Sovereignty is not a tax on
    everything: a question about public law has no reason to run on a 14B model
    in a cupboard, and does not."""
    frontier = Substrate("frontier", [Completion(text_parts=("la norma prevede…",))])
    local = Substrate("local-gpu", local=True)

    _, loop, _ = desk(
        tmp_path,
        substrates={"local-gpu": local, "eu-cluster": Substrate("eu"), "frontier": frontier},
    )
    result = loop.run("Riassumi la disciplina generale della prescrizione civile")

    assert result.response == "la norma prevede…"
    assert frontier.calls == 1
    assert local.calls == 0


# ── 3 · Health: the identifier is in the question itself ─────────────────────


def test_an_identifier_typed_into_the_prompt_never_reaches_a_frontier_model(tmp_path):
    """**Sanità / HR.** Nothing was read from disk — someone pasted a codice
    fiscale into the chat box.

    Perimeters that classify only files miss this every time. Annona classifies
    the bytes about to be sent, so the first turn is already restricted.
    """
    frontier = Substrate("frontier")
    local = Substrate("local-gpu", [Completion(text_parts=("gestito in locale",))], local=True)

    _, loop, _ = desk(
        tmp_path,
        substrates={"local-gpu": local, "eu-cluster": Substrate("eu"), "frontier": frontier},
    )
    result = loop.run("Il paziente RSSMRA85T10A562S ha diritto all'esenzione?")

    assert frontier.calls == 0
    assert result.response == "gestito in locale"


# ── 4 · The outage: the moment a gateway becomes a leak ──────────────────────


def test_when_the_gpu_dies_the_work_stops_instead_of_moving_abroad(tmp_path):
    """**The audit question.** The on-prem GPU fails at 14:02 on a Tuesday.

    Every gateway with a fallback list keeps working, by sending the next
    request somewhere else. This one stops, and the ledger explains to an
    auditor exactly what was refused and why.
    """
    path = matter(tmp_path)
    frontier = Substrate("frontier", reads(path))
    local = Substrate("local-gpu", local=True, fail=True)

    enforcement, loop, _ = desk(
        tmp_path,
        substrates={"local-gpu": local, "eu-cluster": Substrate("eu"), "frontier": frontier},
        files={path: (tmp_path / "clienti" / "BG-114.txt").read_text()},
    )
    result = loop.run(f"Riassumi {path}")

    assert result.response == "", "the run stopped rather than answering from elsewhere"
    assert not any("RSSMRA85T10A562S" in seen for seen in frontier.received)

    held = [e for e in enforcement.ledger.entries() if e.outcome == "held"]
    assert held, "a refusal must be recorded"
    assert verify_file(enforcement.ledger.path).ok

    reasons = " ".join(str(e.detail.get("reason", "")) for e in held)
    assert "holds rather than downgrades" in reasons


# ── 5 · Redaction: the frontier model, without the client ────────────────────


def test_with_a_redactor_the_frontier_model_answers_and_never_sees_a_name(tmp_path):
    """**The case for rizzo-pii.** Same outage, two lines of policy different.

    The studio's own working files are sensitive because of the names in them.
    For that material the practice has opted into redaction — ``egress.redact``
    names the class — so instead of stopping, the identifiers are replaced
    locally, the redacted question goes to the best model, and the answer is
    re-identified here.
    """

    class FakeRedactor:
        name = "rizzo-pii"

        def analyse(self, text: str) -> Redaction:
            return Redaction(
                text="Il cliente [FULLNAME_1] chiede una proroga: quali termini si applicano?",
                mapping={"[FULLNAME_1]": "Mario Rossi"},
                labels={"FULLNAME": 1, "CF": 1},
            )

    policy = studio_policy(
        tmp_path,
        on_unavailable="redact",
        redaction={"provider": "rizzo-pii", "labels": {"CF": "restricted", "FULLNAME": "internal"}},
        egress={"redact": {"allowed_for": ["internal"]}},
    )
    frontier = Substrate(
        "frontier", [Completion(text_parts=("[FULLNAME_1] ha 30 giorni dalla notifica.",))]
    )
    enforcement, _, _ = desk(
        tmp_path,
        policy=policy,
        substrates={
            "local-gpu": Substrate("local-gpu", local=True, fail=True),
            "eu-cluster": Substrate("eu", fail=True),
            "frontier": frontier,
        },
        redactor=FakeRedactor(),
    )
    enforcement.registry.mark_down("local-gpu", "outage")
    enforcement.registry.mark_down("eu-cluster", "outage")
    enforcement.working_set.observe(f"{tmp_path}/studio/nota.txt", SensitivityClass.INTERNAL)

    completion = enforcement.backend().complete(
        CompletionRequest(system="rispondi in italiano", transcript=())
    )

    assert completion.text == "Mario Rossi ha 30 giorni dalla notifica."
    assert "Mario Rossi" not in frontier.received[0]
    assert enforcement.ledger.entries()[-1].outcome == "redacted"
    assert "Mario Rossi" not in enforcement.ledger.path.read_text()


# ── 6 · The tools are still there, and now they are governed ─────────────────


def test_the_toolbox_still_works_and_the_policy_decides_which_part_of_it(tmp_path):
    """**Nothing was removed; everything became governed.**

    The runner's five tools — filesystem, shell, browser, document reader,
    explorer — are all still registered. What changed is that a tool the policy
    does not name does not run, and the refusal is recorded rather than logged
    and forgotten.
    """
    path = matter(tmp_path)
    local = Substrate(
        "local-gpu",
        [
            Completion(
                tool_calls=(
                    ToolCall(id="c1", name="document_reader", arguments={"path": path}),
                    ToolCall(id="c2", name="shell", arguments={"command": "rm -rf /"}),
                ),
                stop_reason="tool_use",
            ),
            Completion(text_parts=("fatto",), stop_reason="end_turn"),
        ],
        local=True,
    )
    enforcement, loop, tools = desk(
        tmp_path,
        substrates={
            "local-gpu": local,
            "eu-cluster": Substrate("eu"),
            "frontier": Substrate("frontier"),
        },
        files={path: (tmp_path / "clienti" / "BG-114.txt").read_text()},
    )
    result = loop.run(f"Leggi {path} e poi pulisci il disco")

    assert tools.executed == ["document_reader"], "the shell command never ran"
    denied = [c for c in result.tool_calls if c.error]
    assert denied and "Permission denied" in str(denied[0].result)

    refusals = [
        e for e in enforcement.ledger.entries() if e.kind == "tool_call" and e.outcome == "held"
    ]
    assert refusals, "a denied tool call belongs in the ledger, not only in a log file"
    assert "shell" in str(refusals[0].detail.get("tool", ""))


def test_the_vault_and_the_sync_engine_are_still_part_of_the_product(tmp_path):
    """The local-first half of the runner did not go anywhere.

    Imported here rather than described, so that a refactor which quietly drops
    them fails a test instead of surprising a user at upgrade time.
    """
    from runner.brain.manager import BrainManager
    from runner.sync.engine import SyncEngine
    from runner.tools.registry import ToolRegistry

    registry = ToolRegistry(
        {
            "tools": {"enabled": ["filesystem", "shell", "browser", "document_reader", "explorer"]},
            "permissions": {"filesystem": {"allowed_paths": [str(tmp_path)]}},
        }
    )
    assert {"filesystem", "shell", "browser", "document_reader", "explorer"} <= set(registry.tools)

    brain = BrainManager(tmp_path / "vault")
    note = brain.create(title="verbale", content="riunione del 15 marzo")
    assert brain.get(note.id).content == "riunione del 15 marzo"

    assert SyncEngine is not None
