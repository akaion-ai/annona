"""The link acceptance run, against a Studio on a real socket.

`deploy/verify_link.py` is what an operator runs on a new machine against the
real Studio. This runs it, unchanged, against an in-process Studio that speaks
the same HTTP contract as `runner_link_router.py` in akaion-app-backend-ai —
enroll, heartbeat, claim, result, jobs, cancel, revoke — with the machine side
fully real: the perimeter, the ledger, the inbox, and the offline echo model.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "/api/v1/runner/link"
ORG = "b35b1b36-657f-4652-b18d-53a5fd29e042"
TOKEN = "studio-session"


class FakeStudio:
    """State of one organization's machines and jobs. Mirrors the router's rules."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.codes: set[str] = set()
        self.runners: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.results: list[dict] = []  # every body a runner sent, for the leak check

    def handle(self, method: str, path: str, headers, body: dict) -> tuple[int, object]:
        auth = headers.get("Authorization", "")
        with self.lock:
            # ── runner side ──
            if path == "/enroll":
                if body.get("code") not in self.codes:
                    return 401, {"detail": "invalid code"}
                self.codes.discard(body["code"])
                rid, secret = str(uuid.uuid4()), secrets.token_urlsafe(16)
                self.runners[rid] = {
                    "id": rid,
                    "name": body["name"],
                    "secret": secret,
                    "revoked": False,
                    "beat": 0.0,
                    "substrates": [],
                }
                return 200, {"runner_id": rid, "secret": secret, "organization": {"name": "Akaion"}}
            if auth.startswith("Runner "):
                rid, _, secret = auth[7:].partition(".")
                runner = self.runners.get(rid)
                if not runner or runner["revoked"] or runner["secret"] != secret:
                    return 401, {"detail": "Invalid runner credential"}
                if path == "/heartbeat":
                    runner.update(beat=time.monotonic(), substrates=body.get("substrates", []))
                    return 200, {
                        "cancel": [
                            j
                            for j, v in self.jobs.items()
                            if v["runner_id"] == rid
                            and v["cancel_requested"]
                            and v["status"] == "running"
                        ]
                    }
                if path == "/claim":
                    for jid in self.order:
                        job = self.jobs[jid]
                        if job["runner_id"] == rid and job["status"] == "queued":
                            job.update(status="running", lease_id=str(uuid.uuid4()))
                            return 200, {
                                "job": {
                                    "id": jid,
                                    "lease_id": job["lease_id"],
                                    "instruction": job["instruction"],
                                    "title": job["title"],
                                    "skill": None,
                                    "kind": "run",
                                    "requested_by": {
                                        "email": "admin@akaion.com",
                                        "role": "owner",
                                        "organization_id": ORG,
                                    },
                                }
                            }
                    return 200, {"job": None}
                if path.endswith("/result"):
                    job = self.jobs[path.split("/")[2]]
                    if body.get("lease_id") != job.get("lease_id") or job["status"] != "running":
                        return 409, {"detail": "No live lease on this job"}
                    self.results.append(body)
                    job.update(
                        status=body["status"],
                        result=body,
                        error=body.get("error"),
                        response=body.get("response") if body["status"] == "completed" else None,
                    )
                    return 200, {"id": job["id"], "status": job["status"]}
                return 404, {}
            # ── person side ──
            if auth != f"Bearer {TOKEN}" or headers.get("X-Organization-Id") != ORG:
                return 401, {"detail": "not a member"}
            if method == "POST" and path == "/enrollments":
                code = "ann_enr_" + secrets.token_urlsafe(16)
                self.codes.add(code)
                return 201, {"code": code}
            if method == "GET" and path == "/runners":
                return 200, [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "substrates": r["substrates"],
                        "online": not r["revoked"] and time.monotonic() - r["beat"] < 30,
                    }
                    for r in self.runners.values()
                ]
            if method == "DELETE" and path.startswith("/runners/"):
                self.runners[path.split("/")[2]]["revoked"] = True
                return 200, {}
            if method == "POST" and path == "/jobs":
                jid = str(uuid.uuid4())
                self.jobs[jid] = {
                    "id": jid,
                    "runner_id": body["runner_id"],
                    "status": "queued",
                    "instruction": body["instruction"],
                    "title": body.get("title"),
                    "cancel_requested": False,
                    "response": None,
                    "result": None,
                    "error": None,
                }
                self.order.append(jid)
                return 201, self.jobs[jid]
            if path.startswith("/jobs/"):
                job = self.jobs[path.split("/")[2]]
                if path.endswith("/cancel"):
                    if job["status"] == "queued":
                        job["status"] = "cancelled"
                    job["cancel_requested"] = True
                return 200, job
            return 404, {}


@pytest.fixture
def studio():
    state = FakeStudio()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            code, payload = state.handle(
                self.command, self.path.removeprefix(PREFIX), self.headers, body
            )
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _serve  # noqa: N815

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _load():
    spec = importlib.util.spec_from_file_location("verify_link", ROOT / "deploy" / "verify_link.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_link"] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


def test_the_whole_round_trip_passes_against_a_studio_that_keeps_its_contract(
    studio, tmp_path, monkeypatch
):
    state, endpoint = studio
    verify_link = _load()
    verify_link._isolate(tmp_path)
    verify_link._prepare("echo", "")

    report = verify_link.verify(
        verify_link.Studio(endpoint, ORG, TOKEN), endpoint, name="ci", timeout=30
    )

    assert [name for name, *_ in report.checks] == [
        "enroll",
        "cancel",
        "online",
        "released",
        "withheld",
        "revoked",
        "ledger",
    ]
    assert report.ok, report.checks
    assert verify_link.FISCAL_CODE not in json.dumps(state.results), "the CF never left"
    assert all(r["revoked"] for r in state.runners.values()), "the throwaway runner is revoked"


def test_a_studio_that_refuses_the_session_fails_the_run_and_says_why(
    studio, tmp_path, monkeypatch
):
    _, endpoint = studio
    verify_link = _load()
    verify_link._isolate(tmp_path)
    verify_link._prepare("echo", "")

    report = verify_link.verify(
        verify_link.Studio(endpoint, ORG, "expired"), endpoint, name="ci", timeout=5
    )

    assert not report.ok
    assert "401" in report.checks[-1][2]


def test_main_needs_a_token(monkeypatch):
    monkeypatch.delenv("ANNONA_STUDIO_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        _load().main(["--org", ORG])
