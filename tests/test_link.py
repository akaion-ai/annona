"""The link to Agents Studio (ADR 0006), against a fake control plane.

The fake speaks the wire contract in the ADR through ``httpx.MockTransport``,
so these tests pin the runner's half of it: what is sent, what is refused, and
above all what never leaves — the answer to a run that read restricted material.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import httpx
import pytest
import yaml

from runner.audit.ledger import Ledger, read_entries, verify_file
from runner.kernel.types import SensitivityClass
from runner.link import (
    API_PREFIX,
    LinkClient,
    LinkConfig,
    LinkError,
    LinkRevokedError,
    LinkWorker,
    check_endpoint,
    enroll,
    release_ceiling,
    release_decision,
)
from runner.policy.loader import default_policy_document, parse_policy

FISCAL_CODE = "RSSMRA85T10A562S"
ENDPOINT = "https://studio.test"
ON_PREM = "https://studio.intranet.example"
JOB = {
    "id": "job-1",
    "lease_id": "lease-1",
    "title": "Offerta probe card",
    "instruction": "Riassumi la specifica",
    "requested_by": {
        "email": "ada@technoprobe.example",
        "role": "member",
        "organization_id": "o-1",
    },
}


def policy_doc(release: str | None = "internal", **endpoints: str) -> dict:
    """``endpoints`` maps a url to its ceiling, e.g. ``policy_doc(**{ON_PREM: "restricted"})``."""
    doc = json.loads(json.dumps(default_policy_document()))
    if release is not None:
        doc["link"] = {"release": release}
    if endpoints:
        doc.setdefault("link", {})["endpoints"] = [
            {"url": url, "release": ceiling} for url, ceiling in endpoints.items()
        ]
    return doc


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy_doc()), encoding="utf-8")
    return path


def config(endpoint: str = ENDPOINT) -> LinkConfig:
    return LinkConfig(endpoint=endpoint, runner_id="r-1", secret="s3cret", name="dgx1")


def enforced(response: str, klass: str = "internal", sealed: str = ""):
    """A stand-in for reason_and_execute on the enforced path."""

    def run(instruction, cancelled, skill=None):
        return {
            "response": response,
            "placement": {
                "class": klass,
                "outcome": "placed",
                "substrate": "local-gpu",
                "reason": "r",
            },
            "sealed": sealed,
        }

    return run


def worker(policy_file: Path, run, handler=None, endpoint: str = ENDPOINT, **kwargs) -> LinkWorker:
    transport = httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={})))
    client = LinkClient(config(endpoint), client=httpx.Client(transport=transport))
    return LinkWorker(
        client, run, policy_file=policy_file, poll_seconds=0.01, heartbeat_seconds=0.01, **kwargs
    )


# ── Transport ────────────────────────────────────────────────────────────────


def test_only_https_leaves_this_machine():
    assert (
        check_endpoint("https://api.prod.akaion.com/api/service4/")
        == "https://api.prod.akaion.com/api/service4"
    )
    assert check_endpoint("http://127.0.0.1:8084") == "http://127.0.0.1:8084"
    for bad in ("http://studio.example.com", "ftp://x", "studio.example.com"):
        with pytest.raises(LinkError):
            check_endpoint(bad)


def test_the_secret_is_written_0600_and_a_readable_one_is_refused(tmp_path: Path):
    path = config().save(tmp_path / "link.json")
    assert path.stat().st_mode & 0o777 == 0o600
    assert LinkConfig.load(path) == config()

    os.chmod(path, 0o644)
    with pytest.raises(LinkError, match="0600"):
        LinkConfig.load(path)


def test_enrollment_trades_a_code_for_the_runners_own_credential():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "runner_id": "r-9",
                "secret": "fresh",
                "organization": {"id": "o-1", "name": "Technoprobe"},
            },
        )

    cfg = enroll(
        ENDPOINT,
        " ann_enr_abc ",
        "dgx1",
        version="0.1.0",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert seen["path"] == f"{API_PREFIX}/enroll"
    assert seen["auth"] is None, "enrollment must not carry any other credential"
    assert seen["body"]["code"] == "ann_enr_abc"
    assert len(seen["body"]["machine_id"]) == 32
    assert (cfg.runner_id, cfg.secret, cfg.organization) == ("r-9", "fresh", "Technoprobe")


def test_every_runner_call_carries_the_runner_credential_and_nothing_else():
    headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"job": None, "cancel": []})

    client = LinkClient(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.claim()
    client.heartbeat({})
    assert headers == ["Runner r-1.s3cret"] * 2


def test_a_401_means_revoked():
    client = LinkClient(
        config(),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"detail": "nope"}))
        ),
    )
    with pytest.raises(LinkRevokedError):
        client.claim()


# ── What may go back ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("release", "run_class", "sealed", "response", "released"),
    [
        ("internal", SensitivityClass.INTERNAL, "", "Pitch 40 µm, 12k pad.", True),
        ("internal", SensitivityClass.PUBLIC, "", "ok", True),
        (None, SensitivityClass.PUBLIC, "", "ok", False),  # no link section → metadata only
        ("internal", SensitivityClass.RESTRICTED, "", "ok", False),  # read restricted
        ("internal", SensitivityClass.INTERNAL, "Progetto Falcon", "ok", False),  # sealed
        (
            "internal",
            SensitivityClass.INTERNAL,
            "",
            f"Il cliente è {FISCAL_CODE}",
            False,
        ),  # answer itself
        ("public", SensitivityClass.INTERNAL, "", "ok", False),  # ceiling lowered
    ],
)
def test_release_decision(release, run_class, sealed, response, released):
    policy = parse_policy(policy_doc(release))
    ok, why = release_decision(
        policy, endpoint=ENDPOINT, run_class=run_class, sealed=sealed, response=response
    )
    assert ok is released, why
    assert why


# ── A higher ceiling bound to one endpoint (ADR 0007) ───────────────────────


def test_the_named_endpoint_gets_its_own_ceiling_and_every_other_gets_link_release():
    policy = parse_policy(policy_doc("internal", **{ON_PREM: "restricted"}))

    assert release_ceiling(policy, ON_PREM) == (
        SensitivityClass.RESTRICTED,
        f"link.release for {ON_PREM}",
    )
    # The same policy on a laptop re-enrolled to another Studio: the base ceiling.
    assert release_ceiling(policy, ENDPOINT) == (SensitivityClass.INTERNAL, "link.release")
    # A near miss is another endpoint, and falls back — the safe way to be wrong.
    assert release_ceiling(policy, ON_PREM + ":8443")[0] is SensitivityClass.INTERNAL
    assert release_ceiling(policy, ON_PREM + "/studio")[0] is SensitivityClass.INTERNAL


def test_endpoints_match_across_case_and_trailing_slash():
    policy = parse_policy(
        policy_doc("public", **{"https://Studio.Intranet.EXAMPLE/": "restricted"})
    )
    assert release_ceiling(policy, ON_PREM)[0] is SensitivityClass.RESTRICTED
    assert release_ceiling(policy, "https://STUDIO.intranet.example/")[0] is (
        SensitivityClass.RESTRICTED
    )


def test_an_endpoints_only_policy_sends_nothing_to_a_studio_it_does_not_name():
    policy = parse_policy(policy_doc(None, **{ON_PREM: "internal"}))
    ok, why = release_decision(
        policy, endpoint=ENDPOINT, run_class=SensitivityClass.PUBLIC, sealed="", response="ok"
    )
    assert not ok and "no link.release" in why


def test_restricted_goes_to_the_studio_in_the_building_and_nowhere_else(tmp_path: Path):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy_doc("internal", **{ON_PREM: "restricted"})))

    body = worker(path, enforced("NDA", klass="restricted"), endpoint=ON_PREM).run_job(JOB)
    assert body["status"] == "completed"
    assert body["release"] == f"within link.release for {ON_PREM} (restricted)"

    body = worker(path, enforced("NDA", klass="restricted")).run_job({**JOB, "id": "job-2"})
    assert body["status"] == "withheld"
    assert "link.release permits up to internal" in body["release"]


def test_sealed_material_never_leaves_even_for_the_studio_in_the_building():
    policy = parse_policy(policy_doc("internal", **{ON_PREM: "restricted"}))
    ok, why = release_decision(
        policy,
        endpoint=ON_PREM,
        run_class=SensitivityClass.INTERNAL,
        sealed="Progetto Falcon",
        response="ok",
    )
    assert not ok and "sealed material never leaves" in why


def test_a_released_answer_goes_back_with_who_asked_in_the_ledger(policy_file: Path):
    body = worker(policy_file, enforced("Pitch 40 µm.")).run_job(JOB)

    assert body["status"] == "completed"
    assert body["response"] == "Pitch 40 µm."
    assert body["lease_id"] == "lease-1"
    assert body["placement"] == {"class": "internal", "outcome": "placed", "substrate": "local-gpu"}

    ledger = policy_file.parent / "ledger.jsonl"
    link = [e for e in read_entries(ledger) if e.kind == "link"]
    assert [e.outcome for e in link] == ["received", "released"]
    assert link[0].detail["requested_by"]["email"] == "ada@technoprobe.example"
    assert verify_file(ledger).ok


def test_the_heartbeat_says_what_machine_it_is_and_nothing_about_its_people(policy_file: Path):
    host = worker(policy_file, enforced("x")).report()["host"]
    assert set(host) == {"os", "arch", "chip", "accelerator", "model_memory_gb"}
    assert Path.home().name not in json.dumps(host)


def test_a_run_the_perimeter_held_is_not_reported_as_completed(policy_file: Path):
    """Studio showed a green "completed" over an empty answer (#38, over the link)."""

    def held(instruction, cancelled, skill=None):
        return {
            "response": "",
            "placement": {
                "class": "restricted",
                "outcome": "held",
                "substrate": "",
                "reason": "no permitted substrate is available",
            },
            "sealed": "",
        }

    body = worker(policy_file, held).run_job(JOB)

    assert body["status"] == "failed"
    assert body["error"] == "held by this machine's policy: no permitted substrate is available"
    assert "response" not in body
    outcomes = [
        e.outcome for e in read_entries(policy_file.parent / "ledger.jsonl") if e.kind == "link"
    ]
    assert outcomes == ["received", "held"]
    assert not (policy_file.parent / "link" / "inbox").exists(), "nothing ran, nothing to keep"


def test_a_restricted_run_is_withheld_and_its_answer_never_leaves(policy_file: Path):
    secret_answer = f"La pratica di {FISCAL_CODE} scade il 15 marzo"
    body = worker(policy_file, enforced(secret_answer, klass="restricted")).run_job(JOB)

    assert body["status"] == "withheld"
    assert "response" not in body
    assert FISCAL_CODE not in json.dumps(body)
    assert all(
        "reason" not in d for d in body["decisions"]
    ), "reasons name material; withheld sends shape only"
    outcomes = [
        e.outcome for e in read_entries(policy_file.parent / "ledger.jsonl") if e.kind == "link"
    ]
    assert outcomes == ["received", "withheld"]

    kept = policy_file.parent / "link" / "inbox" / "job-1.json"
    assert kept.stat().st_mode & 0o777 == 0o600, "the answer stays here, readable by this user only"
    record = json.loads(kept.read_text(encoding="utf-8"))
    assert record["response"] == secret_answer
    assert record["requested_by"]["email"] == "ada@technoprobe.example"


def with_skills(policy_file: Path, *names: str) -> Path:
    doc = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    doc["skills"] = list(names)
    doc.setdefault("tools", {}).setdefault("allow", {})["skill"] = ["/**"]
    policy_file.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return policy_file


def test_the_heartbeat_offers_enabled_skills_and_allowed_tools_only(policy_file: Path):
    report = worker(with_skills(policy_file, "second-opinion"), enforced("x")).report()
    assert [s["name"] for s in report["skills"]] == ["second-opinion"]
    assert set(report["skills"][0]) == {"name", "description", "pins"}, "never the body"
    assert report["tools"] == sorted(report["tools"]) and "shell" not in report["tools"]


def test_a_named_skill_is_handed_to_the_run_and_reported_back(policy_file: Path):
    seen = []

    def run(instruction, cancelled, skill=None):
        seen.append(skill)
        return enforced("ok")(instruction, cancelled)

    body = worker(with_skills(policy_file, "second-opinion"), run).run_job(
        {**JOB, "skill": "second-opinion"}
    )
    assert seen == ["second-opinion"]
    assert body["status"] == "completed"
    assert "skill" not in body, "the fake run never loaded it, so it is not reported"

    def loads(instruction, cancelled, skill=None):
        Ledger(policy_file.parent / "ledger.jsonl").record(
            "skill",
            outcome="cleared",
            klass=SensitivityClass.PUBLIC,
            substrate="local",
            detail={"skill": skill},
        )
        return enforced("ok")(instruction, cancelled)

    body = worker(policy_file, loads).run_job({**JOB, "id": "job-2", "skill": "second-opinion"})
    assert body["skill"] == "second-opinion"


def test_skills_are_not_offered_when_the_skill_tool_is_not_allowed(policy_file: Path):
    doc = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    doc["skills"] = ["second-opinion"]
    policy_file.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert (
        worker(policy_file, enforced("x")).report()["skills"] == []
    ), "the gate would refuse every load; offering the skill promises work that cannot happen"


def test_a_skill_the_policy_does_not_enable_fails_without_running(policy_file: Path):
    called = []
    body = worker(policy_file, lambda i, c, s=None: called.append(i)).run_job(
        {**JOB, "skill": "second-opinion"}
    )
    assert body["status"] == "failed" and "not enabled" in body["error"]
    assert not called


# ── Installs Studio may ask for (ADR 0008) ───────────────────────────────────

CATALOG_URL = "https://akaion-ai.github.io/annona/catalog/index.json"
PUBLISHED = Path(__file__).resolve().parent.parent / "docs" / "catalog"
INSTALL = {**JOB, "id": "job-i", "kind": "install_skill", "skill": "rfq-triage"}


def with_catalog(policy_file: Path, *enable: str) -> Path:
    doc = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    doc["skill_catalogs"] = [{"name": "akaion", "url": CATALOG_URL, "enable": list(enable)}]
    policy_file.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return policy_file


def catalog_worker(policy_file: Path, run=None, *, down: bool = False) -> LinkWorker:
    """A worker whose catalog is the one this repository publishes."""

    def publish(request: httpx.Request) -> httpx.Response:
        path = PUBLISHED / request.url.path.rsplit("/", 1)[-1]
        if down or not path.is_file():
            return httpx.Response(503 if down else 404)
        return httpx.Response(200, content=path.read_bytes())

    catalog = httpx.Client(transport=httpx.MockTransport(publish))
    return worker(policy_file, run or enforced("x"), catalog_client=catalog)


def test_the_heartbeat_offers_pre_approved_skills_not_yet_installed(policy_file: Path):
    w = catalog_worker(with_catalog(policy_file, "rfq-triage", "eight-d", "not-published"))
    offered = w.report()["installable"]
    assert [(s["name"], s["catalog"], s["pins"]) for s in offered] == [
        ("rfq-triage", "akaion", "local"),
        ("eight-d", "akaion", "local"),
    ]
    assert set(offered[0]) == {"name", "version", "description", "pins", "catalog"}

    assert w.run_job(INSTALL)["status"] == "completed"
    assert [s["name"] for s in w.report()["installable"]] == ["eight-d"]


def test_an_unreachable_catalog_does_not_break_the_heartbeat(policy_file: Path):
    report = catalog_worker(with_catalog(policy_file, "rfq-triage"), down=True).report()
    assert report["installable"] == []
    assert report["substrates"], "the rest of the heartbeat is still there"


def test_an_install_job_fetches_a_pre_approved_skill_without_running_a_model(policy_file: Path):
    called = []
    w = catalog_worker(with_catalog(policy_file, "rfq-triage"), lambda *a: called.append(a))
    body = w.run_job(INSTALL)

    index = json.loads((PUBLISHED / "index.json").read_text(encoding="utf-8"))
    entry = next(s for s in index["skills"] if s["name"] == "rfq-triage")
    sha, version = entry["sha256"], entry["version"]
    assert not called
    assert body == {
        "lease_id": "lease-1",
        "status": "completed",
        "response": f"Installed rfq-triage {version} from akaion (sha256 {sha[:12]}…); pinned local; "
        "enabled by skill_catalogs.enable",
        "skill": "rfq-triage",
    }

    from runner.skills.loader import discover_skills

    assert discover_skills()["rfq-triage"].pins_local
    (entry,) = list(read_entries(policy_file.parent / "ledger.jsonl"))
    assert (entry.kind, entry.outcome) == ("skill_install", "installed")
    assert entry.detail["catalog"] == "akaion" and entry.detail["sha256"] == sha
    assert entry.detail["requested_by"]["email"] == "ada@technoprobe.example"

    again = w.run_job({**INSTALL, "id": "job-j"})
    assert again["status"] == "failed" and "already installed" in again["error"]


def test_an_install_job_for_a_skill_the_policy_does_not_pre_approve_is_refused(policy_file: Path):
    w = catalog_worker(with_catalog(policy_file, "eight-d"))
    body = w.run_job(INSTALL)
    assert body == {
        "lease_id": "lease-1",
        "status": "failed",
        "error": "skill 'rfq-triage' is not pre-approved by this machine's policy",
    }
    (entry,) = list(read_entries(policy_file.parent / "ledger.jsonl"))
    assert (entry.kind, entry.outcome) == ("skill_install", "refused")
    assert entry.detail["requested_by"]["email"] == "ada@technoprobe.example"

    from runner.skills.loader import discover_skills

    assert "rfq-triage" not in discover_skills()


def test_an_unknown_job_kind_is_refused(policy_file: Path):
    body = worker(policy_file, enforced("x")).run_job({**JOB, "kind": "shell"})
    assert body["status"] == "failed" and "shell" in body["error"]


def test_no_policy_means_no_remote_work(tmp_path: Path):
    called = []
    body = worker(tmp_path / "missing.yaml", lambda i, c, s=None: called.append(i)).run_job(JOB)
    assert body["status"] == "failed"
    assert not called, "an instruction from the network must not run without a perimeter"


def test_an_unenforced_run_is_never_released(policy_file: Path):
    body = worker(policy_file, lambda i, c, s=None: {"response": "legacy answer"}).run_job(JOB)
    assert body["status"] == "failed"
    assert "response" not in body


def test_a_failure_reports_its_type_not_its_text(policy_file: Path):
    def boom(i, c):
        raise RuntimeError(f"could not parse {FISCAL_CODE}.pdf")

    body = worker(policy_file, boom).run_job(JOB)
    assert body["status"] == "failed"
    assert FISCAL_CODE not in json.dumps(body)


# ── The loop ─────────────────────────────────────────────────────────────────


def test_serve_claims_runs_reports_and_stops_on_revocation(policy_file: Path):
    posted = []
    claims = iter([{"job": JOB}, {"job": None}])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/claim"):
            try:
                return httpx.Response(200, json=next(claims))
            except StopIteration:
                return httpx.Response(401, json={"detail": "revoked"})
        if path.endswith("/result"):
            posted.append(json.loads(request.content))
        return httpx.Response(200, json={"cancel": []})

    w = worker(policy_file, enforced("ok"), handler)
    with pytest.raises(LinkRevokedError):
        w.serve(threading.Event())

    assert len(posted) == 1
    assert posted[0]["status"] == "completed"


def test_a_job_cancelled_in_studio_reports_cancelled(policy_file: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cancel": ["job-1"]})

    w = worker(policy_file, None, handler)

    def run(instruction, cancelled, skill=None):
        w.beat_once()  # the heartbeat lands mid-run
        assert cancelled()
        return enforced("partial")(instruction, cancelled)

    w._run = run
    assert w.run_job(JOB)["status"] == "cancelled"


def test_the_heartbeat_names_substrates_but_never_endpoints(policy_file: Path):
    report = worker(policy_file, enforced("ok")).report()
    assert report["policy_digest"]
    assert report["substrates"][0]["id"] == "local-gpu"
    assert "endpoint" not in json.dumps(report)


def test_enroll_adds_link_release_keeping_the_operators_comments(tmp_path, monkeypatch):
    from runner.cli_link import _ensure_link_section

    path = tmp_path / "policy.yaml"
    path.write_text(
        "# mine, keep me\n" + yaml.safe_dump(policy_doc(release=None)), encoding="utf-8"
    )
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path))

    with pytest.raises(ValueError):
        _ensure_link_section("top-secret")
    assert "link:" not in path.read_text(), "a bad value must not be written"

    assert _ensure_link_section("internal").link.release is SensitivityClass.INTERNAL
    text = path.read_text()
    assert text.startswith("# mine, keep me")
    assert (
        _ensure_link_section("public").link.release is SensitivityClass.INTERNAL
    ), "an existing ceiling is never overwritten"


def test_enroll_keeps_an_endpoints_only_link_section(tmp_path, monkeypatch):
    from runner.cli_link import _ensure_link_section

    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy_doc(None, **{ON_PREM: "restricted"})), encoding="utf-8")
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path))

    policy = _ensure_link_section("internal")
    assert policy.link.release is None, "nothing to unnamed Studios was a decision"
    assert path.read_text().count("link:") == 1


def test_a_bad_enrollment_code_is_not_reported_as_a_revocation():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(LinkError, match="enrollment code") as info:
        enroll(ENDPOINT, "ann_enr_wrong", "dgx1", version="0.1.0", client=client)
    assert not isinstance(info.value, LinkRevokedError)


def test_a_refused_result_is_not_retried(policy_file: Path):
    posts = []
    claims = iter([{"job": JOB}])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            try:
                return httpx.Response(200, json=next(claims))
            except StopIteration:
                return httpx.Response(401)
        if request.url.path.endswith("/result"):
            posts.append(1)
            return httpx.Response(409, json={"detail": "No live lease on this job"})
        return httpx.Response(200, json={"cancel": []})

    with pytest.raises(LinkRevokedError):
        worker(policy_file, enforced("ok"), handler).serve(threading.Event())
    assert len(posts) == 1, "a 409 is final; retrying it only adds noise"
