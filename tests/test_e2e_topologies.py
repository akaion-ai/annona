"""End-to-end tests for both deployment topologies.

Two things are verified here that no other test covers:

**Detached** — the runner does its whole job with no credentials, no
configuration pointing anywhere, and no socket to the outside world.

**Attached** — the runner speaks the documented three-endpoint contract
correctly: it authenticates, pushes a note, and drives an agentic turn through a
remote control plane, executing the resulting tools locally.

The attached tests run against a **real HTTP server** started in-process, not a
mocked client. That is deliberate: the README tells people they can point the
runner at their own backend by implementing three endpoints, and this is the test
that proves the claim. It also means the suite exercises the attached path
without depending on a vendor being up — including ours.

These do not touch the real gateway. ``tests/test_live_cloud.py`` does, opt-in
via ``AKAION_LIVE=1``; keeping the two apart means the everyday suite never fails
because someone else's deployment is rolling.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from runner.agent.loop import AgentLoop
from runner.capability.backends import AkaionBackend, EchoBackend
from runner.capability.tooling import PermissionGate, RegistryToolExecutor
from runner.cloud_client import AIBackendClient, MainBackendClient
from runner.permissions.manager import PermissionManager
from runner.tools.registry import ToolRegistry

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


# ── A control plane, implementing the documented contract ─────────────────────


class _ControlPlane(BaseHTTPRequestHandler):
    """The three endpoints `Self-hosting the cloud side` documents.

    Requests are recorded on the server object so tests can assert on what the
    runner actually sent, which is the part that matters for a contract test.
    """

    def log_message(self, *args) -> None:  # noqa: D102 - silence stderr logging
        pass

    # -- helpers ------------------------------------------------------------

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _record(self, kind: str, body: dict | None = None) -> None:
        self.server.calls.append(  # type: ignore[attr-defined]
            {
                "kind": kind,
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "runner_id": self.headers.get("X-Runner-ID"),
                "body": body,
            }
        )

    # -- routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.endswith("/api/v1/users/me"):
            self._record("verify_auth")
            if self.headers.get("Authorization") != "Bearer test-token":
                self._json(401, {"detail": "unauthorised"})
                return
            self._json(200, {"email": "runner@example.com", "uid": "u1"})
            return
        self._json(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self._read_json()

        if self.path.endswith("/api/v1/cloud/thoughts"):
            self._record("push_thought", body)
            self._json(201, {"message_id": "msg-1", "cluster_id": "c-1", "cluster_name": "Work"})
            return

        if self.path.endswith("/api/v1/runner/agent/turn"):
            self._record("agent_turn", body)
            turn = self.server.turns.pop(0)  # type: ignore[attr-defined]
            self._json(200, turn)
            return

        self._json(404, {"detail": "not found"})


@pytest.fixture
def control_plane():
    """A live control plane on a free port, with a scripted set of turns."""
    server = HTTPServer(("127.0.0.1", 0), _ControlPlane)
    server.calls = []  # type: ignore[attr-defined]
    server.turns = []  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]

    yield server

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


# ── Detached: no credentials, no network ──────────────────────────────────────


class TestDetached:
    def test_a_full_agentic_run_needs_nothing_external(self, tmp_path: Path):
        """The whole product, with no account and no remote host configured."""
        from runner.demo import _script, _workspace, run_demo

        documents = _workspace(tmp_path)
        result = run_demo(documents, _script(documents))

        assert result.iterations == 3
        assert [c.tool for c in result.tool_calls] == ["explorer", "document_reader"]
        assert "412.000" in result.response

    def test_the_local_backend_reports_itself_as_local(self):
        """`is_local` is what the Phase 1 perimeter will read; it must be true here."""
        assert EchoBackend().capabilities.is_local is True

    def test_the_vault_survives_without_a_runner(self, tmp_path: Path):
        """Walking away leaves readable markdown behind, not a proprietary store."""
        from runner.brain.manager import BrainManager

        brain = BrainManager(tmp_path / "vault")
        brain.create(title="Matter 2026-118", content="Call the client", tags=["work"])
        brain.close()

        files = list((tmp_path / "vault").rglob("*.md"))
        assert len(files) == 1
        assert "Call the client" in files[0].read_text(encoding="utf-8")

    def test_a_note_is_self_describing_without_the_index(self, tmp_path: Path):
        """The real portability test: delete the database, keep everything.

        This used to fail. Markdown files held the body alone, so a vault without
        its SQLite index was a folder of anonymous text. Frontmatter closed it —
        the check is that a third-party YAML parser, which knows nothing about
        this project, recovers the note.
        """
        import yaml

        from runner.brain.manager import BrainManager

        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        note = brain.create(title="Matter 2026-118", content="Call the client", tags=["work"])
        brain.mark_pending(note.id)
        brain.close()

        # Everything the runner owns, gone.
        (vault / ".akaion" / "index.db").unlink()

        text = next(vault.rglob("*.md")).read_text(encoding="utf-8")
        _, raw, body = text.split("---", 2)
        recovered = yaml.safe_load(raw)

        assert recovered["title"] == "Matter 2026-118"
        assert recovered["tags"] == ["work"]
        assert recovered["sync"] == "pending_sync"
        assert recovered["id"] == note.id
        assert body.strip() == "Call the client"

    def test_a_vault_whose_index_is_lost_is_rebuilt_unchanged(self, tmp_path: Path):
        """Delete the index, reopen the vault, and nothing that made it findable is gone.

        The test above proves the metadata is *in* the files. This one proves the
        runner reads it back: before, a reopened vault without `index.db` listed
        no notes at all, because nothing went files → index (#6).
        """
        from runner.brain.manager import BrainManager

        vault = tmp_path / "vault"
        brain = BrainManager(vault)
        first = brain.create(title="Matter 2026-118", content="Call the client", tags=["work"])
        second = brain.create(title="Groceries", content="Oat milk", tags=["home", "list"])
        brain.mark_pending(first.id)
        brain.mark_synced(second.id, "msg-1", "cluster-7", "Errands")
        before = {n.id: n for n in brain.list()}
        brain.close()

        (vault / ".akaion" / "index.db").unlink()

        rebuilt = BrainManager(vault)
        after = {n.id: n for n in rebuilt.list()}
        assert after.keys() == before.keys()
        for note_id, note in before.items():
            again = after[note_id]
            assert (again.title, again.content, again.tags) == (note.title, note.content, note.tags)
            assert (again.sync_status, again.cot_message_id, again.cot_cluster_id) == (
                note.sync_status,
                note.cot_message_id,
                note.cot_cluster_id,
            )
            assert (again.created_at, again.updated_at) == (note.created_at, note.updated_at)
        assert [n.id for n in rebuilt.search("client")] == [first.id]
        rebuilt.close()

    def test_sync_without_credentials_is_a_silent_no_op(self, tmp_path: Path):
        """Not a crash, and above all not a `Bearer None` request."""
        from unittest.mock import MagicMock

        from runner.brain.manager import BrainManager
        from runner.sync.engine import SyncEngine

        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="n", content="c", tags=[])
        brain.mark_pending(note.id)

        auth = MagicMock()
        auth.get_firebase_token.return_value = None

        # An unreachable URL: if the engine tried to connect, this would hang or raise.
        result = SyncEngine(brain, "http://127.0.0.1:1", auth).push_pending()
        brain.close()

        assert result.get("error") == "not_authenticated"
        assert result.get("synced", 0) == 0


# ── Attached: the documented three-endpoint contract ──────────────────────────


class TestAttachedAuth:
    def test_a_valid_token_verifies(self, control_plane):
        client = MainBackendClient(api_key="test-token", base_url=control_plane.base_url)

        assert client.verify_auth() is True
        assert control_plane.calls[0]["kind"] == "verify_auth"
        assert control_plane.calls[0]["auth"] == "Bearer test-token"

    def test_a_rejected_token_does_not_raise(self, control_plane):
        """A bad token is an answer, not an exception — the daemon keeps running."""
        client = MainBackendClient(api_key="wrong", base_url=control_plane.base_url)

        assert client.verify_auth() is False


class TestAttachedSync:
    def test_a_pending_note_is_pushed_and_marked_synced(self, control_plane, tmp_path: Path):
        from unittest.mock import MagicMock

        from runner.brain.manager import BrainManager
        from runner.brain.models import SYNC_SYNCED
        from runner.sync.engine import SyncEngine

        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="Q1 report", content="Revenue up", tags=["work", "q1"])
        brain.mark_pending(note.id)

        auth = MagicMock()
        auth.get_firebase_token.return_value = "test-token"

        result = SyncEngine(brain, control_plane.base_url, auth).push_pending()
        stored = brain.get(note.id)
        brain.close()

        assert result["synced"] == 1
        assert result["errors"] == 0
        assert stored.sync_status == SYNC_SYNCED
        assert stored.cot_message_id == "msg-1"

    def test_the_payload_matches_the_documented_contract(self, control_plane, tmp_path: Path):
        from unittest.mock import MagicMock

        from runner.brain.manager import BrainManager
        from runner.sync.engine import SyncEngine

        brain = BrainManager(tmp_path / "vault")
        note = brain.create(title="Title", content="Body text", tags=["alpha"])
        brain.mark_pending(note.id)

        auth = MagicMock()
        auth.get_firebase_token.return_value = "test-token"
        SyncEngine(brain, control_plane.base_url, auth).push_pending()
        brain.close()

        push = next(c for c in control_plane.calls if c["kind"] == "push_thought")
        assert push["auth"] == "Bearer test-token"
        assert push["body"]["content"] == "# Title\n\nBody text"
        assert push["body"]["metadata"]["local_id"] == note.id
        assert push["body"]["metadata"]["source"] == "local_runner"
        assert push["body"]["hint_tags"] == ["alpha"]

    def test_only_pending_notes_are_sent(self, control_plane, tmp_path: Path):
        """Push, never pull, and never everything: local_only stays local."""
        from unittest.mock import MagicMock

        from runner.brain.manager import BrainManager
        from runner.sync.engine import SyncEngine

        brain = BrainManager(tmp_path / "vault")
        brain.create(title="private", content="stays here", tags=[])
        pending = brain.create(title="shared", content="goes up", tags=[])
        brain.mark_pending(pending.id)

        auth = MagicMock()
        auth.get_firebase_token.return_value = "test-token"
        result = SyncEngine(brain, control_plane.base_url, auth).push_pending()
        brain.close()

        pushes = [c for c in control_plane.calls if c["kind"] == "push_thought"]
        assert result["synced"] == 1
        assert len(pushes) == 1
        assert "goes up" in pushes[0]["body"]["content"]
        assert all("stays here" not in p["body"]["content"] for p in pushes)


class TestAttachedAgentTurn:
    """A full agentic run driven by a remote control plane, tools running locally."""

    def _config(self, workspace: Path) -> dict:
        return {
            "tools": {"enabled": ["filesystem", "document_reader", "explorer"]},
            "permissions": {
                "filesystem": {
                    "allowed_paths": [str(workspace)],
                    "denied_paths": [],
                    "max_file_size_mb": 10,
                },
                "shell": {"enabled": False, "allowed_commands": []},
            },
        }

    def test_the_plane_plans_and_the_runner_executes(self, control_plane, tmp_path: Path):
        report = tmp_path / "report.txt"
        report.write_text("Revenue: 412000\n", encoding="utf-8")

        control_plane.turns = [
            {
                "content": [
                    {"type": "text", "text": "Reading the report."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "document_reader",
                        "input": {"path": str(report)},
                    },
                ],
                "stop_reason": "tool_use",
            },
            {
                "content": [{"type": "text", "text": "Revenue was 412000."}],
                "stop_reason": "end_turn",
            },
        ]

        config = self._config(tmp_path)
        client = AIBackendClient(
            api_key="test-token", runner_id="runner-e2e", base_url=control_plane.base_url
        )

        result = AgentLoop(
            AkaionBackend(client=client),
            RegistryToolExecutor(ToolRegistry(config)),
            PermissionGate(PermissionManager(config)),
        ).run("Summarise the report", {"workspace": str(tmp_path)})

        assert result.iterations == 2
        assert result.response == "Revenue was 412000."
        assert result.tool_calls[0].tool == "document_reader"
        assert result.tool_calls[0].error is False
        # The tool really read the file: this is local execution, not a stub.
        assert "412000" in str(result.tool_calls[0].result)

    def test_the_transcript_is_sent_back_on_the_next_turn(self, control_plane, tmp_path: Path):
        """Documented sovereignty behaviour: tool results cross the perimeter.

        This is not a bug being asserted as correct — it is the known Phase 0
        property recorded in the README, SECURITY.md and the architecture page.
        The test exists so that when Phase 1 changes it, the change is visible.
        """
        secret = tmp_path / "secret.txt"
        secret.write_text("PRIVILEGED CLIENT MATTER\n", encoding="utf-8")

        control_plane.turns = [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "document_reader",
                        "input": {"path": str(secret)},
                    }
                ],
                "stop_reason": "tool_use",
            },
            {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
        ]

        config = self._config(tmp_path)
        client = AIBackendClient(
            api_key="test-token", runner_id="runner-e2e", base_url=control_plane.base_url
        )
        AgentLoop(
            AkaionBackend(client=client),
            RegistryToolExecutor(ToolRegistry(config)),
            PermissionGate(PermissionManager(config)),
        ).run("read it")

        turns = [c for c in control_plane.calls if c["kind"] == "agent_turn"]
        assert len(turns) == 2
        assert "PRIVILEGED CLIENT MATTER" in json.dumps(turns[1]["body"]["messages"]), (
            "the file's contents reached the control plane — the Phase 0 behaviour "
            "documented in SECURITY.md. Phase 1's perimeter is what changes this."
        )

    def test_policy_denials_happen_locally_and_are_reported_upstream(
        self, control_plane, tmp_path: Path
    ):
        """The plane may ask for anything; the perimeter is what refuses."""
        control_plane.turns = [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "filesystem",
                        "input": {"operation": "read", "path": "/etc/passwd"},
                    }
                ],
                "stop_reason": "tool_use",
            },
            {"content": [{"type": "text", "text": "understood"}], "stop_reason": "end_turn"},
        ]

        config = self._config(tmp_path)
        client = AIBackendClient(
            api_key="test-token", runner_id="runner-e2e", base_url=control_plane.base_url
        )
        result = AgentLoop(
            AkaionBackend(client=client),
            RegistryToolExecutor(ToolRegistry(config)),
            PermissionGate(PermissionManager(config)),
        ).run("read /etc/passwd")

        assert result.tool_calls[0].error is True
        assert result.tool_calls[0].result == {"error": "Permission denied for tool: filesystem"}

        # The denial is reported back, and no file content went with it.
        turns = [c for c in control_plane.calls if c["kind"] == "agent_turn"]
        second = json.dumps(turns[1]["body"]["messages"])
        assert "Permission denied" in second
        assert "root:" not in second

    def test_the_runner_identifies_itself(self, control_plane, tmp_path: Path):
        control_plane.turns = [
            {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        ]
        client = AIBackendClient(
            api_key="test-token", runner_id="runner-abc", base_url=control_plane.base_url
        )

        AgentLoop(
            AkaionBackend(client=client),
            RegistryToolExecutor(ToolRegistry({"tools": {"enabled": []}})),
            PermissionGate(None),
        ).run("hello")

        turn = next(c for c in control_plane.calls if c["kind"] == "agent_turn")
        assert turn["body"]["runner_id"] == "runner-abc"
        assert turn["runner_id"] == "runner-abc"


class TestAttachedFailure:
    def test_an_unreachable_plane_degrades_instead_of_crashing(self, tmp_path: Path):
        """A laptop that loses Wi-Fi mid-task returns what it has."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]

        client = AIBackendClient(
            api_key="test-token", runner_id="r", base_url=f"http://127.0.0.1:{dead_port}"
        )

        result = AgentLoop(
            AkaionBackend(client=client),
            RegistryToolExecutor(ToolRegistry({"tools": {"enabled": []}})),
            PermissionGate(None),
        ).run("do something")

        assert result.response == ""
        assert result.tool_calls == ()
        assert result.iterations == 1


# ── The bug that only appears in the packaged app ────────────────────────────


def test_a_relative_log_path_resolves_against_the_runner_home_not_the_cwd(tmp_path, monkeypatch):
    """Launched from the Dock, the working directory is `/`.

    The daemon used to create `logs/` relative to wherever it was started, which
    works in a terminal and dies with `[Errno 30] Read-only file system: 'logs'`
    when macOS launches the app — invisible in development, fatal in the bundle.
    """
    from runner.main import RunnerDaemon

    monkeypatch.setenv("ANNONA_HOME", str(tmp_path / "home"))
    resolved = RunnerDaemon._resolve_log_file("logs/annona.log")

    assert resolved.is_absolute()
    assert str(resolved).startswith(str(tmp_path / "home"))


def test_an_absolute_log_path_is_left_alone(tmp_path):
    from runner.main import RunnerDaemon

    target = tmp_path / "somewhere" / "annona.log"
    assert RunnerDaemon._resolve_log_file(str(target)) == target


def test_the_daemon_starts_even_when_the_log_directory_cannot_be_created(tmp_path, monkeypatch):
    """A daemon that cannot write a log still has work to do.

    Console logging survives, the file sink is skipped, and the reason is said
    out loud rather than taking the process down.
    """
    from runner.main import RunnerDaemon

    monkeypatch.setenv("ANNONA_HOME", "/dev/null/nope")
    daemon = RunnerDaemon.__new__(RunnerDaemon)
    daemon.config = {"logging": {"level": "INFO", "file": "logs/annona.log"}}
    daemon.dev_mode = False

    daemon._setup_logging()  # must not raise
