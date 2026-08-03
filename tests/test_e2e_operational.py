"""End-to-end tests for the three surfaces the shipped app actually has.

`test_e2e_topologies.py` proves the *contract* — that the runner speaks the
documented three endpoints correctly. This file proves the *product*: it drives
the same HTTP API the desktop window calls, in the same order a person clicks,
against a cloud that is a real socket rather than a mock.

Three surfaces, matching the three remaining nav entries:

**Notes** — the vault through `/api/brain/*`. Create, read, edit, search,
delete, and the invariant that a note is a markdown file on disk, not a row
hidden inside SQLite.

**Sync** — `/api/sync/*`. A note is local until marked, marked until pushed,
and the counters the Sync screen renders move the way the screen claims. The
failure paths matter more than the happy one: a cloud that 500s, and a runner
with no credentials, must both leave the note recoverable instead of lost.

**Tasks** — a task executed by the daemon lands in the vault as a note, and that
note can then be pushed to the cloud. Task → note → cloud is the only chain in
this product that crosses all three surfaces, and nothing else tests it whole.

The cloud here is an in-process HTTP server on a free port. That is deliberate:
a `unittest.mock` for the sync engine would still pass if the engine stopped
sending a body at all.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from runner.auth import AuthManager
from runner.brain.manager import BrainManager
from runner.local_api import create_app
from runner.sync.engine import SyncEngine

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


# ── A cloud that records what it was sent ─────────────────────────────────────


class _Cloud(BaseHTTPRequestHandler):
    """The COT ingest endpoint, plus a switch to make it fail on purpose."""

    def log_message(self, *args) -> None:  # noqa: D102 - silence stderr logging
        pass

    def _json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if not self.path.endswith("/api/v1/cloud/thoughts"):
            self._json(404, {"detail": "not found"})
            return

        self.server.calls.append(  # type: ignore[attr-defined]
            {"auth": self.headers.get("Authorization"), "body": body}
        )

        status = self.server.next_status  # type: ignore[attr-defined]
        if status >= 400:
            self._json(status, {"detail": "the cloud is having a day"})
            return

        n = len(self.server.calls)  # type: ignore[attr-defined]
        self._json(201, {"message_id": f"msg-{n}", "cluster_id": "c-1", "cluster_name": "Work"})


@pytest.fixture
def cloud():
    """A live cloud on a free port. `next_status` scripts the failure tests."""
    server = HTTPServer(("127.0.0.1", 0), _Cloud)
    server.calls = []  # type: ignore[attr-defined]
    server.next_status = 201  # type: ignore[attr-defined]
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server

    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


# ── The app, wired the way the daemon wires it ────────────────────────────────


def _vault(tmp_path: Path) -> BrainManager:
    brain_dir = tmp_path / "vault"
    brain_dir.mkdir(parents=True, exist_ok=True)
    return BrainManager(brain_dir)


def _signed_in_auth(tmp_path: Path) -> AuthManager:
    auth = AuthManager(config_dir=tmp_path / ".annona")
    auth.save_credentials(
        firebase_token="test-token",
        refresh_token="test-refresh",
        expires_in=3600,
        email="operator@example.com",
    )
    return auth


@pytest.fixture
def signed_in(tmp_path: Path, cloud):
    """The everyday case: a vault, a signed-in operator, a reachable cloud."""
    brain = _vault(tmp_path)
    auth = _signed_in_auth(tmp_path)
    sync = SyncEngine(brain=brain, cot_url=cloud.base_url, auth=auth)
    with TestClient(create_app(brain, sync, auth, cloud_enabled=True)) as client:
        yield client, brain, cloud
    brain.close()


@pytest.fixture
def signed_out(tmp_path: Path, cloud):
    """The same app with no credentials on disk."""
    brain = _vault(tmp_path)
    auth = AuthManager(config_dir=tmp_path / ".annona-empty")
    sync = SyncEngine(brain=brain, cot_url=cloud.base_url, auth=auth)
    with TestClient(create_app(brain, sync, auth, cloud_enabled=False)) as client:
        yield client, brain, cloud
    brain.close()


def _create(client: TestClient, title: str, content: str = "", tags: list[str] | None = None):
    resp = client.post(
        "/api/brain/notes", json={"title": title, "content": content, "tags": tags or []}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Notes ─────────────────────────────────────────────────────────────────────


class TestNotes:
    def test_a_new_note_starts_local_only(self, signed_in):
        client, _, cloud = signed_in

        note = _create(client, "Quarterly numbers", "Revenue is up.", ["work"])

        assert note["sync_status"] == "local_only"
        assert note["cot_message_id"] is None
        assert cloud.calls == [], "creating a note must not talk to the cloud"

    def test_the_note_is_a_markdown_file_on_disk(self, signed_in, tmp_path):
        client, brain, _ = signed_in

        note = _create(client, "Portable", "This body must survive the index.")

        matches = list(Path(brain.brain_dir).rglob(f"*{note['id']}*.md"))
        assert matches, f"no markdown file for {note['id']} under {brain.brain_dir}"
        assert "This body must survive the index." in matches[0].read_text(encoding="utf-8")

    def test_a_created_note_is_listed_and_fetchable(self, signed_in):
        client, _, _ = signed_in
        note = _create(client, "Findable")

        listed = client.get("/api/brain/notes").json()
        assert [n["id"] for n in listed] == [note["id"]]

        fetched = client.get(f"/api/brain/notes/{note['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["title"] == "Findable"

    def test_an_edit_persists(self, signed_in):
        client, _, _ = signed_in
        note = _create(client, "Draft", "first pass", ["draft"])

        patched = client.patch(
            f"/api/brain/notes/{note['id']}",
            json={"title": "Final", "content": "second pass", "tags": ["done"]},
        )
        assert patched.status_code == 200

        reread = client.get(f"/api/brain/notes/{note['id']}").json()
        assert reread["title"] == "Final"
        assert reread["content"] == "second pass"
        assert reread["tags"] == ["done"]

    def test_search_finds_a_note_by_its_body(self, signed_in):
        client, _, _ = signed_in
        _create(client, "Meeting", "We agreed on the pergolato budget.")
        _create(client, "Unrelated", "Nothing to see here.")

        hits = client.get("/api/brain/search", params={"q": "pergolato"}).json()
        assert [h["title"] for h in hits] == ["Meeting"]

    @pytest.mark.parametrize(
        "query",
        [
            "end-to-end",  # `-` is FTS5's NOT: raised "no such column: to"
            "set-a-goal",
            'the "quoted" one',  # an unbalanced quote is a syntax error
            "budget:2026",  # `:` is FTS5's column filter
            "NEAR",  # a bare operator keyword
            "wildcard*",
            "(unbalanced",
            "operator@example.com",
        ],
    )
    def test_punctuation_in_the_search_box_is_not_a_500(self, signed_in, query):
        """Whatever gets typed, the answer is results or none — never a crash."""
        client, _, _ = signed_in
        _create(client, "Roadmap", "Our end-to-end plan for set-a-goal, budget:2026.")

        resp = client.get("/api/brain/search", params={"q": query})

        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json(), list)

    def test_a_hyphenated_word_still_finds_the_note(self, signed_in):
        client, _, _ = signed_in
        _create(client, "Roadmap", "Our end-to-end plan.")
        _create(client, "Other", "Nothing relevant.")

        hits = client.get("/api/brain/search", params={"q": "end-to-end"}).json()

        assert [h["title"] for h in hits] == ["Roadmap"]

    def test_two_words_mean_notes_containing_both(self, signed_in):
        client, _, _ = signed_in
        _create(client, "Both", "The budget and the roadmap.")
        _create(client, "One", "The budget only.")

        hits = client.get("/api/brain/search", params={"q": "budget roadmap"}).json()

        assert [h["title"] for h in hits] == ["Both"]

    def test_a_query_of_pure_punctuation_returns_nothing(self, signed_in):
        client, _, _ = signed_in
        _create(client, "Something", "Anything.")

        assert client.get("/api/brain/search", params={"q": "---"}).json() == []

    def test_a_deleted_note_is_gone_from_the_api_and_the_disk(self, signed_in):
        client, brain, _ = signed_in
        note = _create(client, "Temporary")

        assert client.delete(f"/api/brain/notes/{note['id']}").status_code == 204
        assert client.get(f"/api/brain/notes/{note['id']}").status_code == 404
        assert not list(Path(brain.brain_dir).rglob(f"*{note['id']}*.md"))

    def test_an_unknown_id_is_a_404_not_a_500(self, signed_in):
        client, _, _ = signed_in
        assert client.get("/api/brain/notes/does-not-exist").status_code == 404
        assert client.patch("/api/brain/notes/nope", json={"title": "x"}).status_code == 404
        assert client.delete("/api/brain/notes/nope").status_code == 404


# ── Sync ──────────────────────────────────────────────────────────────────────


class TestSync:
    def test_marking_a_note_moves_the_counters_the_sync_screen_renders(self, signed_in):
        client, _, _ = signed_in
        note = _create(client, "To publish")

        before = client.get("/api/sync/status").json()
        assert (before["local_only"], before["pending"], before["synced"]) == (1, 0, 0)

        marked = client.post(f"/api/brain/notes/{note['id']}/mark-sync")
        assert marked.status_code == 200
        assert marked.json()["sync_status"] == "pending_sync"

        after = client.get("/api/sync/status").json()
        assert (after["local_only"], after["pending"], after["synced"]) == (0, 1, 0)

    def test_a_push_sends_the_documented_payload(self, signed_in):
        client, _, cloud = signed_in
        note = _create(client, "Board update", "Numbers attached.", ["work", "board"])
        client.post(f"/api/brain/notes/{note['id']}/mark-sync")

        assert client.post("/api/sync/push").json() == {"synced": 1, "errors": 0}

        assert len(cloud.calls) == 1
        call = cloud.calls[0]
        assert call["auth"] == "Bearer test-token"
        assert call["body"]["content"] == "# Board update\n\nNumbers attached."
        assert call["body"]["metadata"]["local_id"] == note["id"]
        assert call["body"]["metadata"]["source"] == "local_runner"
        assert call["body"]["hint_tags"] == ["work", "board"]
        assert call["body"]["enable_embeddings"] is True

    def test_a_pushed_note_carries_the_cloud_ids_back(self, signed_in):
        client, _, _ = signed_in
        note = _create(client, "Round trip")
        client.post(f"/api/brain/notes/{note['id']}/mark-sync")
        client.post("/api/sync/push")

        synced = client.get(f"/api/brain/notes/{note['id']}").json()
        assert synced["sync_status"] == "synced"
        assert synced["cot_message_id"] == "msg-1"
        assert synced["cot_cluster_name"] == "Work"
        assert synced["synced_at"] is not None

        status = client.get("/api/sync/status").json()
        assert (status["pending"], status["synced"], status["errors"]) == (0, 1, 0)
        assert status["last_push"] is not None

    def test_an_unmarked_note_is_never_sent(self, signed_in):
        client, _, cloud = signed_in
        _create(client, "Private thought", "Stays here.")
        marked = _create(client, "Shared thought")
        client.post(f"/api/brain/notes/{marked['id']}/mark-sync")

        client.post("/api/sync/push")

        assert len(cloud.calls) == 1
        assert cloud.calls[0]["body"]["metadata"]["local_id"] == marked["id"]

    def test_pushing_with_nothing_pending_is_a_no_op(self, signed_in):
        client, _, cloud = signed_in
        _create(client, "Local forever")

        assert client.post("/api/sync/push").json() == {"synced": 0, "errors": 0}
        assert cloud.calls == []

    def test_a_single_note_can_be_pushed_by_id_without_marking_it(self, signed_in):
        client, _, cloud = signed_in
        note = _create(client, "Send this one now")

        resp = client.post(f"/api/sync/push/{note['id']}")
        assert resp.status_code == 200
        assert resp.json()["sync_status"] == "synced"
        assert len(cloud.calls) == 1

    def test_a_cloud_error_leaves_the_note_recoverable(self, signed_in):
        client, _, cloud = signed_in
        cloud.next_status = 500
        note = _create(client, "Will fail", "Work that must not be lost.", ["important"])
        client.post(f"/api/brain/notes/{note['id']}/mark-sync")

        assert client.post("/api/sync/push").json() == {"synced": 0, "errors": 1}

        failed = client.get(f"/api/brain/notes/{note['id']}").json()
        assert failed["sync_status"] == "sync_error"
        assert "500" in failed["sync_error"]
        assert failed["content"] == "Work that must not be lost."
        assert failed["tags"] == ["important"]
        assert client.get("/api/sync/status").json()["errors"] == 1

        # And the operator can retry once the cloud is back.
        cloud.next_status = 201
        client.post(f"/api/brain/notes/{note['id']}/mark-sync")
        assert client.post("/api/sync/push").json() == {"synced": 1, "errors": 0}
        assert client.get(f"/api/brain/notes/{note['id']}").json()["sync_status"] == "synced"

    def test_pushing_without_credentials_never_sends_bearer_none(self, signed_out):
        client, _, cloud = signed_out
        note = _create(client, "No account here")
        client.post(f"/api/brain/notes/{note['id']}/mark-sync")

        assert client.post("/api/sync/push").json() == {
            "synced": 0,
            "errors": 0,
            "error": "not_authenticated",
        }
        assert cloud.calls == [], "a signed-out runner must not open a request at all"

        # The note stays pending, so signing in later still publishes it.
        assert client.get(f"/api/brain/notes/{note['id']}").json()["sync_status"] == "pending_sync"
        assert client.get("/api/sync/status").json()["pending"] == 1

    def test_the_app_reports_which_mode_it_is_in(self, signed_in, signed_out):
        online, _, _ = signed_in
        assert online.get("/api/runner/mode").json()["mode"] == "cloud"
        assert online.get("/api/auth/status").json()["authenticated"] is True

        offline, _, _ = signed_out
        assert offline.get("/api/runner/mode").json()["mode"] == "local"
        assert offline.get("/api/auth/status").json()["authenticated"] is False


# ── Tasks: the chain that crosses all three surfaces ──────────────────────────


def _echo_config(tmp_path: Path, workspace: Path) -> dict[str, Any]:
    """A daemon config that runs offline: scripted model, one permitted folder."""
    return {
        "tools": {"enabled": ["filesystem", "document_reader", "explorer"]},
        "permissions": {
            "filesystem": {
                "allowed_paths": [str(workspace)],
                "denied_paths": [],
                "max_file_size_mb": 10,
            },
            "shell": {"enabled": False, "allowed_commands": [], "denied_commands": []},
            "network": {"enabled": False},
        },
        "ai": {
            "provider": "echo",
            "echo": {
                "script": [
                    {
                        "text": "Reading the file you asked about.",
                        "tools": [
                            {
                                "name": "filesystem",
                                "arguments": {
                                    "operation": "read",
                                    "path": str(workspace / "report.txt"),
                                },
                            }
                        ],
                    },
                    {"text": "The report says revenue grew 12%."},
                ]
            },
        },
        "runner": {"capture_to_brain": True},
        "logging": {"level": "WARNING", "file": str(tmp_path / "annona.log")},
        "cloud": {"enabled": False},
    }


class TestTasks:
    @pytest.fixture
    def daemon(self, tmp_path: Path, cloud, monkeypatch):
        """A real daemon over a real vault, pointed at the fake cloud."""
        from runner.main import RunnerDaemon

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "report.txt").write_text("Revenue grew 12% year on year.", encoding="utf-8")

        monkeypatch.setenv("AKAION_CAPTURE_TO_BRAIN", "1")

        d = RunnerDaemon(
            config=_echo_config(tmp_path, workspace),
            brain_dir=tmp_path / "vault",
        )
        # The daemon resolves the real COT URL at construction; point it here.
        d.sync = SyncEngine(brain=d.brain, cot_url=cloud.base_url, auth=d.auth_manager)
        yield d
        d.brain.close()

    def test_a_task_runs_offline_and_lands_in_the_vault(self, daemon):
        result = daemon.execute_once("What does the report say?")

        assert result["type"] == "command_result"
        assert daemon.tasks_executed == 1

        notes = daemon.brain.list()
        assert len(notes) == 1, "the task should have been captured as exactly one note"
        assert notes[0].sync_status == "local_only", "a captured task must not auto-publish"

    def test_the_captured_note_is_visible_through_the_api_the_ui_calls(self, daemon, tmp_path):
        daemon.execute_once("What does the report say?")

        auth = _signed_in_auth(tmp_path)
        with TestClient(create_app(daemon.brain, daemon.sync, auth, cloud_enabled=True)) as client:
            notes = client.get("/api/brain/notes").json()
            assert len(notes) == 1
            assert "task" in " ".join(notes[0]["tags"])

    def test_task_then_note_then_cloud(self, daemon, cloud, tmp_path):
        """The whole chain: run a task, find it in the vault, publish it."""
        daemon.execute_once("What does the report say?")

        # The daemon ran the task with no credentials at all. Signing in is a
        # separate step, and it is what turns a captured task into a publishable
        # one — so the engine that pushes is built from the signed-in auth, not
        # from the one the task ran under.
        auth = _signed_in_auth(tmp_path)
        sync = SyncEngine(brain=daemon.brain, cot_url=cloud.base_url, auth=auth)
        with TestClient(create_app(daemon.brain, sync, auth, cloud_enabled=True)) as client:
            note_id = client.get("/api/brain/notes").json()[0]["id"]

            client.post(f"/api/brain/notes/{note_id}/mark-sync")
            assert client.post("/api/sync/push").json() == {"synced": 1, "errors": 0}

            published = client.get(f"/api/brain/notes/{note_id}").json()
            assert published["sync_status"] == "synced"
            assert published["cot_message_id"] == "msg-1"

        assert len(cloud.calls) == 1
        assert cloud.calls[0]["body"]["metadata"]["source"] == "local_runner"

    def test_the_same_task_is_not_captured_twice(self, daemon):
        daemon._execute_task(
            {"id": "task-1", "type": "command", "payload": {"command": "read the report"}}
        )
        # The echo script is exhausted after one run; what matters is that a
        # second capture of the same id does not duplicate the note.
        from runner.brain.capture import capture_task_as_note

        capture_task_as_note(daemon.brain, {"id": "task-1", "type": "command"}, {"success": True})

        assert len(daemon.brain.list()) == 1

    def test_a_signed_out_task_says_so_instead_of_raising_attributeerror(
        self, tmp_path, monkeypatch
    ):
        """The first thing a fresh install does, with the shipped defaults.

        `ai.provider` defaults to `akaion`, and with no credentials the client
        is None. The agentic loop used to hand that None to the backend adapter
        and die with

            'NoneType' object has no attribute 'runner_agent_turn'

        which tells a new user nothing about the fact that they are not signed
        in. The same crash had already happened once under the name
        `runner_execute`; a test is how it stops coming back.
        """
        from runner.kernel.errors import ConfigurationError
        from runner.main import RunnerDaemon

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "report.txt").write_text("anything", encoding="utf-8")

        config = _echo_config(tmp_path, workspace)
        config["ai"] = {"provider": "akaion", "temperature": 0.7, "max_tokens": 4000}

        monkeypatch.delenv("AKAION_API_KEY", raising=False)

        d = RunnerDaemon(config=config, brain_dir=tmp_path / "vault-signed-out")
        try:
            with pytest.raises(ConfigurationError) as excinfo:
                d.execute_once("Summarise the report")

            message = str(excinfo.value)
            assert "annona login" in message, "the error must say how to fix it"
            assert "Nothing was sent anywhere" in message
        finally:
            d.brain.close()

    def test_capture_can_be_switched_off(self, tmp_path, cloud, monkeypatch):
        from runner.main import RunnerDaemon

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "report.txt").write_text("irrelevant", encoding="utf-8")

        monkeypatch.setenv("AKAION_CAPTURE_TO_BRAIN", "0")
        d = RunnerDaemon(
            config=_echo_config(tmp_path, workspace),
            brain_dir=tmp_path / "vault-quiet",
        )
        try:
            d.execute_once("What does the report say?")
            assert d.brain.list() == [], "capture disabled must leave the vault untouched"
        finally:
            d.brain.close()
