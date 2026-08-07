"""Tests for the kernel's HTTP surface (runner/kernel_api.py).

The reads exist so a person can watch the perimeter work; the write exists so
they can make it work. What is asserted here is mostly about honesty under
absence and failure — no policy, a broken policy, no executor, a run that is not
enforced — because those are the states where a status page is tempted to draw
something reassuring.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runner.kernel_api import kernel_router

# ── Fixtures ──────────────────────────────────────────────────────────────────


POLICY = """
version: 1
default: deny

classes:
  restricted:
    paths: ["~/clienti/**"]
    patterns: ['-----BEGIN [A-Z ]*PRIVATE KEY-----']
  internal:
    paths: ["~/**"]
  public:
    default: true

substrates:
  - id: local-gpu
    kind: ollama
    endpoint: http://localhost:11434
    model: qwen2.5:3b
    jurisdiction: on-prem
    max_class: restricted
    tools: true
  - id: frontier
    kind: echo
    jurisdiction: us
    max_class: public

rules:
  - match: {class: restricted}
    allow: [local-gpu]
    on_unavailable: hold
  - match: {class: internal}
    allow: [local-gpu]
    on_unavailable: hold
  - match: {class: public}
    allow: [frontier, local-gpu]
    on_unavailable: hold

tools:
  allow:
    read_file: ["~/work/**"]
  deny_paths: ["~/.ssh/**"]
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """An Annona home with nothing in it yet."""
    h = tmp_path / "annona-home"
    h.mkdir()
    monkeypatch.setenv("ANNONA_HOME", str(h))
    return h


def client_for(executor=None) -> TestClient:
    app = FastAPI()
    app.include_router(kernel_router(executor))
    return TestClient(app)


def write_policy(home: Path, text: str = POLICY) -> Path:
    target = home / "policy.yaml"
    target.write_text(text)
    return target


def write_ledger(home: Path, entries: list[dict]) -> Path:
    """A ledger written directly, hashes and all, via the real writer."""
    from runner.audit.ledger import Ledger
    from runner.kernel.types import SensitivityClass

    ledger = Ledger(home / "ledger.jsonl", run_id="run-test", fsync=False)
    for e in entries:
        ledger.record(**{**e, "klass": SensitivityClass.parse(e["klass"])})
    return ledger.path


# ── Absence ───────────────────────────────────────────────────────────────────


def test_policy_missing_says_how_to_create_one(home):
    r = client_for().get("/api/kernel/policy")
    assert r.status_code == 404
    assert "annona policy init" in r.json()["detail"]


def test_status_without_policy_is_not_enforcing(home):
    body = client_for().get("/api/kernel/status").json()
    assert body["enforcing"] is False
    assert body["reason"] == "no policy file"


def test_ledger_missing_is_empty_not_an_error(home):
    body = client_for().get("/api/kernel/ledger").json()
    assert body == {"path": str(home / "ledger.jsonl"), "total": 0, "entries": []}


def test_verify_on_missing_ledger_is_ok_and_says_it_is_empty(home):
    body = client_for().get("/api/kernel/ledger/verify").json()
    assert body["ok"] is True
    assert body["empty"] is True


def test_broken_policy_is_422_and_not_reported_as_missing(home):
    # `default: allow` is the one thing the loader must never accept.
    write_policy(home, POLICY.replace("default: deny", "default: allow"))
    r = client_for().get("/api/kernel/policy")
    assert r.status_code == 422
    body = client_for().get("/api/kernel/status").json()
    assert body["enforcing"] is False
    assert body["reason"]  # carries the loader's complaint, not a generic string


# ── Reads ─────────────────────────────────────────────────────────────────────


def test_policy_is_returned_as_the_runtime_understands_it(home):
    write_policy(home)
    body = client_for().get("/api/kernel/policy").json()

    assert body["default_class"] == "public"
    assert [s["id"] for s in body["substrates"]] == ["local-gpu", "frontier"]

    frontier = next(s for s in body["substrates"] if s["id"] == "frontier")
    assert frontier["max_class"] == "public"
    assert frontier["jurisdiction"] == "us"

    assert body["tools"]["allow"]["read_file"] == ["~/work/**"]
    assert body["tools"]["deny_paths"] == ["~/.ssh/**"]
    assert [r["class"] for r in body["rules"]] == ["restricted", "internal", "public"]

    # A class carries what earns it, so the window can answer "why restricted?"
    restricted = next(c for c in body["classes"] if c["label"] == "restricted")
    assert restricted["paths"] == ["~/clienti/**"]
    assert restricted["patterns"] == ["-----BEGIN [A-Z ]*PRIVATE KEY-----"]


def test_substrates_without_probing_do_not_touch_the_network(home):
    write_policy(home)
    body = client_for().get("/api/kernel/substrates?probe=false").json()
    assert body["probed"] is False
    assert {s["id"] for s in body["substrates"]} == {"local-gpu", "frontier"}


def test_ledger_returns_refusals_alongside_everything_else(home):
    write_policy(home)
    write_ledger(
        home,
        [
            {
                "step_id": "s1",
                "kind": "inference",
                "outcome": "placed",
                "klass": "public",
                "substrate": "frontier",
            },
            {
                "step_id": "s2",
                "kind": "inference",
                "outcome": "held",
                "klass": "restricted",
                "detail": {"reason": "local-gpu unhealthy"},
            },
        ],
    )

    body = client_for().get("/api/kernel/ledger").json()
    assert body["total"] == 2
    assert [e["outcome"] for e in body["entries"]] == ["placed", "held"]

    only_held = client_for().get("/api/kernel/ledger?held=true").json()
    assert [e["step_id"] for e in only_held["entries"]] == ["s2"]
    assert only_held["entries"][0]["detail"]["reason"] == "local-gpu unhealthy"
    # The total stays the total: narrowing the view must not restate the history.
    assert only_held["total"] == 2


def test_verify_reports_a_tampered_chain(home):
    write_policy(home)
    path = write_ledger(
        home,
        [
            {"step_id": "s1", "kind": "inference", "outcome": "placed", "klass": "public"},
            {"step_id": "s2", "kind": "inference", "outcome": "held", "klass": "restricted"},
        ],
    )

    assert client_for().get("/api/kernel/ledger/verify").json()["ok"] is True

    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["outcome"] = "placed-after-the-fact"
    lines[0] = json.dumps(first)
    path.write_text("\n".join(lines) + "\n")

    body = client_for().get("/api/kernel/ledger/verify").json()
    assert body["ok"] is False
    assert body["problem"]


# ── Ask ───────────────────────────────────────────────────────────────────────


class _Executor:
    """The daemon's executor, reduced to what `ask` uses."""

    def __init__(self, result=None, raises=None):
        self.tools = object()
        self.permissions = object()
        self.seen: dict = {}
        outer = self

        class _Client:
            def reason_and_execute(self, **kwargs):
                outer.seen = kwargs
                if raises is not None:
                    raise raises
                return result

        self.ai_client = _Client()


def test_ask_without_an_executor_is_503(home):
    write_policy(home)
    r = client_for(None).post("/api/kernel/ask", json={"prompt": "hello"})
    assert r.status_code == 503


def test_ask_returns_the_answer_and_the_decisions_it_caused(home):
    write_policy(home)
    write_ledger(
        home, [{"step_id": "old", "kind": "inference", "outcome": "placed", "klass": "public"}]
    )

    executor = _Executor(
        result={
            "response": "done",
            "iterations": 2,
            "tool_calls": [],
            "placement": {"class": "restricted", "outcome": "placed", "substrate": "local-gpu"},
        }
    )

    # The run itself appends to the ledger; simulate that by recording during it.
    original = executor.ai_client.reason_and_execute

    def recording(**kwargs):
        write_ledger(
            home,
            [{"step_id": "new", "kind": "inference", "outcome": "placed", "klass": "restricted"}],
        )
        return original(**kwargs)

    executor.ai_client.reason_and_execute = recording

    body = client_for(executor).post("/api/kernel/ask", json={"prompt": "riassumi"}).json()

    assert body["response"] == "done"
    assert body["enforced"] is True
    assert body["placement"]["substrate"] == "local-gpu"
    # Only what this request caused — the pre-existing entry must not reappear.
    assert [d["step_id"] for d in body["decisions"]] == ["new"]


def test_ask_says_when_a_run_was_not_enforced(home):
    """A legacy run has no placement. The API must not invent one."""
    executor = _Executor(result={"response": "hi", "iterations": 1, "tool_calls": []})
    body = client_for(executor).post("/api/kernel/ask", json={"prompt": "ciao"}).json()

    assert body["enforced"] is False
    assert body["placement"] is None


def test_ask_reports_a_perimeter_that_could_not_be_assembled_as_409(home):
    from runner.kernel.errors import ConfigurationError

    executor = _Executor(raises=ConfigurationError("substrate 'frontier' needs ANTHROPIC_API_KEY"))
    r = client_for(executor).post("/api/kernel/ask", json={"prompt": "ciao"})

    assert r.status_code == 409
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_ask_rejects_an_empty_prompt(home):
    executor = _Executor(result={"response": "", "iterations": 0, "tool_calls": []})
    assert client_for(executor).post("/api/kernel/ask", json={"prompt": ""}).status_code == 422


def test_ask_passes_the_iteration_budget_through(home):
    executor = _Executor(result={"response": "", "iterations": 1, "tool_calls": []})
    client_for(executor).post("/api/kernel/ask", json={"prompt": "x", "max_iterations": 3})
    assert executor.seen["max_iterations"] == 3


# ── Onboarding: the first policy, and only the first ──────────────────────────
#
# The window could read the perimeter and never create one, so a .dmg install
# had to open a terminal before it could answer anything. These routes close
# that, without becoming a policy editor reachable by every page in the browser.


def test_profiles_are_offered_with_their_consequences(home, monkeypatch):
    from runner import cli_setup

    monkeypatch.setattr(
        cli_setup,
        "probe_runtime",
        lambda *a, **k: cli_setup.RuntimeProbe("x", True, ("qwen2.5:3b",)),
    )

    body = client_for().get("/api/kernel/profiles").json()

    assert body["configured"] is False
    assert body["suggested_model"] == "qwen2.5:3b"
    assert all(p["consequence"] for p in body["profiles"])


def test_creating_the_first_policy_makes_the_daemon_enforce(home, monkeypatch):
    from runner import cli_setup

    monkeypatch.setattr(
        cli_setup,
        "probe_runtime",
        lambda *a, **k: cli_setup.RuntimeProbe("x", True, ("qwen2.5:3b",)),
    )
    client = client_for()

    assert client.get("/api/kernel/status").json()["enforcing"] is False

    created = client.post("/api/kernel/policy", json={"profile": "local-only"})

    assert created.status_code == 201
    assert client.get("/api/kernel/status").json()["enforcing"] is True


def test_it_refuses_to_touch_a_policy_that_already_exists(home):
    """The whole safety property: ungoverned → governed, never sideways."""
    write_policy(home)
    before = (home / "policy.yaml").read_text()

    response = client_for().post("/api/kernel/policy", json={"profile": "read-nothing"})

    assert response.status_code == 409
    assert (home / "policy.yaml").read_text() == before


def test_an_unknown_profile_is_refused(home):
    response = client_for().post("/api/kernel/policy", json={"profile": "trust-me"})
    assert response.status_code == 422


def test_the_frontier_profile_needs_a_provider(home):
    response = client_for().post("/api/kernel/policy", json={"profile": "frontier-for-public"})
    assert response.status_code == 422


def test_a_created_frontier_policy_caps_the_provider_at_public(home, monkeypatch):
    from runner import cli_setup

    monkeypatch.setattr(
        cli_setup,
        "probe_runtime",
        lambda *a, **k: cli_setup.RuntimeProbe("x", True, ("qwen2.5:3b",)),
    )
    client = client_for()

    response = client.post(
        "/api/kernel/policy",
        json={"profile": "frontier-for-public", "provider": "anthropic"},
    )
    assert response.status_code == 201

    substrates = client.get("/api/kernel/policy").json()["substrates"]
    hosted = next(s for s in substrates if s["id"] == "frontier")
    assert hosted["max_class"] == "public"


def test_the_created_policy_never_contains_a_key(home, monkeypatch):
    """There is no field on the request that could carry one, and this pins it."""
    from runner import cli_setup

    monkeypatch.setattr(
        cli_setup,
        "probe_runtime",
        lambda *a, **k: cli_setup.RuntimeProbe("x", True, ("qwen2.5:3b",)),
    )

    client_for().post(
        "/api/kernel/policy",
        json={
            "profile": "frontier-for-public",
            "provider": "anthropic",
            "api_key_env": "MY_KEY",
        },
    )

    written = (home / "policy.yaml").read_text()
    assert "MY_KEY" in written
    assert "sk-" not in written


# ── Editing an existing policy ────────────────────────────────────────────────
#
# The write side exists because a perimeter nobody adjusts stops describing what
# anybody wants. What makes it safe is not the UI: it is that nothing invalid
# reaches disk, nothing is lost, and nothing is quiet.


def _valid_yaml(home: Path) -> str:
    write_policy(home)
    return (home / "policy.yaml").read_text()


def test_the_source_is_offered_with_the_digest_to_edit_against(home):
    write_policy(home)
    body = client_for().get("/api/kernel/policy/source").json()
    assert "substrates:" in body["text"]
    assert len(body["digest"]) == 64


def test_a_valid_replacement_is_written(home):
    text = _valid_yaml(home).replace("local-gpu", "local-box")
    response = client_for().put("/api/kernel/policy", json={"yaml": text})

    assert response.status_code == 200
    assert "local-box" in (home / "policy.yaml").read_text()


def test_an_invalid_policy_never_reaches_disk(home):
    """A daemon whose policy does not load stops enforcing. This is the guard."""
    before = _valid_yaml(home)

    response = client_for().put(
        "/api/kernel/policy",
        json={"yaml": "version: 1\nsubstrates: []\nrules: []\n"},
    )

    assert response.status_code == 422
    assert (home / "policy.yaml").read_text() == before


def test_malformed_yaml_is_refused_with_the_parser_error(home):
    _valid_yaml(home)
    response = client_for().put("/api/kernel/policy", json={"yaml": "substrates: [oops\n"})
    assert response.status_code == 422
    assert "not valid YAML" in response.json()["detail"]


def test_the_previous_policy_is_always_kept(home):
    before = _valid_yaml(home)
    client_for().put("/api/kernel/policy", json={"yaml": before.replace("local-gpu", "box")})

    backups = list(home.glob("policy.yaml.bak-*"))
    assert backups, "the replaced policy was not kept"
    assert backups[0].read_text() == before


def test_every_replacement_lands_in_the_ledger(home):
    """Widen, run, narrow back — and there are three entries saying so."""
    before = _valid_yaml(home)
    client = client_for()

    client.put("/api/kernel/policy", json={"yaml": before.replace("local-gpu", "box")})

    entries = client.get("/api/kernel/ledger").json()["entries"]
    change = next(e for e in entries if e["kind"] == "policy")
    assert change["outcome"] == "replaced"
    assert change["detail"]["before_digest"] != change["detail"]["after_digest"]
    # Digests, not contents: the ledger's rule everywhere else.
    assert "local-gpu" not in json.dumps(change["detail"])


def test_a_stale_edit_is_refused_rather_than_applied(home):
    """The file is editable from a terminal too."""
    before = _valid_yaml(home)
    stale = __import__("runner.audit.ledger", fromlist=["digest"]).digest(before)

    # Somebody else changes it in the meantime.
    (home / "policy.yaml").write_text(before.replace("local-gpu", "box"))

    response = client_for().put(
        "/api/kernel/policy",
        json={"yaml": before, "expected_digest": stale},
    )

    assert response.status_code == 409
    assert "box" in (home / "policy.yaml").read_text()


def test_a_structured_save_keeps_the_explanatory_header(home):
    """The header is the only documentation of the schema most people will read."""
    target = home / "policy.yaml"
    target.write_text("# keep me — this explains the whole file\n" + POLICY)

    document = client_for().get("/api/kernel/policy/source").json()
    import yaml as yaml_lib

    parsed = yaml_lib.safe_load(document["text"])
    response = client_for().put("/api/kernel/policy", json={"document": parsed})

    assert response.status_code == 200
    assert "# keep me" in target.read_text()


def test_comments_inside_the_body_are_reported_not_silently_dropped(home):
    target = home / "policy.yaml"
    target.write_text(POLICY.replace("substrates:", "# a note from an operator\nsubstrates:"))

    body = client_for().get("/api/kernel/policy/source").json()

    assert body["body_has_comments"] is True


def test_a_paired_app_may_run_steps_but_not_change_what_is_permitted(home):
    """Pairing is a grant to execute, not to govern.

    The middleware lets a paired origin reach every /api/ route, which is right
    for `ask`. An origin that could also widen the perimeter and then run under
    it would hold exactly the permissions the perimeter exists to withhold, and
    nobody was asked about that when they pasted a token.
    """
    from runner.pairing import PairedOriginMiddleware, Pairing

    CLOUD = "https://app.akaion.com"
    text = _valid_yaml(home)
    pairing = Pairing.create(home / "pairing.json", origins=(CLOUD,))

    app = FastAPI()
    app.add_middleware(PairedOriginMiddleware, pairing=pairing)
    app.include_router(kernel_router(None))
    client = TestClient(app)

    headers = {"Origin": CLOUD, "x-annona-token": pairing.token}
    response = client.put("/api/kernel/policy", json={"yaml": text}, headers=headers)

    assert response.status_code == 403
    # And it can still read, which is what pairing was for.
    assert client.get("/api/kernel/policy", headers=headers).status_code == 200


def test_the_window_on_this_machine_may_still_change_it(home):
    _valid_yaml(home)
    text = (home / "policy.yaml").read_text().replace("local-gpu", "box")

    response = client_for().put(
        "/api/kernel/policy",
        json={"yaml": text},
        headers={"Origin": "http://127.0.0.1:7070"},
    )

    assert response.status_code == 200
