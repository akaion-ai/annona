"""More than one person: a proven subject, rules per group, memory per group.

The acceptance of docs/design/multi-user.md (U1–U3), one test each:

- a token from any OIDC issuer — Akaion's included, as a preset — proves a subject;
  a wrong audience, issuer, key or an expired token does not;
- proxy headers without the proxy's secret are refused, never downgraded;
- anonymous requests are refused when identity is required, and recorded;
- the subject is in every ledger entry, inside the hash chain;
- rules match on group, ``${subject}`` gives each person a folder, and a memory
  folder of another group is never retrieved.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from runner.audit.ledger import Ledger, read_entries, verify_file
from runner.kernel.errors import PolicyError
from runner.kernel.types import SensitivityClass, Subject, ToolCall
from runner.kernel_api import _entry_json
from runner.memory.index import MemoryIndex, roots
from runner.policy.loader import AKAION_JWKS, parse_policy
from runner.services.enforcement import Enforcement, MemoryScope
from runner.services.identity import PROXY_SECRET_HEADER, IdentityError, authenticate
from tests.test_enforcement import policy_document
from tests.test_kernel_api import POLICY, _Executor, client_for, home, write_policy  # noqa: F401
from tests.test_memory import fake_embed, fake_facts, read_text

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER = "https://login.example.com/tenant/v2.0"
JWKS = "https://login.example.com/keys"


def token(key=KEY, **claims):
    body = {
        "iss": ISSUER,
        "aud": "api://annona",
        "exp": int(time.time()) + 600,
        "email": "anna@acme.example",
        "groups": ["sales"],
        **claims,
    }
    return jwt.encode(body, key, algorithm="RS256")


def keys(url, _token):
    """The issuer's published key, without the network."""
    assert url in (JWKS, AKAION_JWKS)
    return KEY.public_key()


def identity_policy(tmp_path, **identity):
    doc = policy_document(tmp_path)
    doc["identity"] = identity or {
        "required": True,
        "providers": [
            {"kind": "jwt", "issuer": ISSUER, "audience": "api://annona", "jwks_url": JWKS},
            {"kind": "proxy", "secret_env": "ANNONA_PROXY_SECRET"},
        ],
    }
    return parse_policy(doc)


def bearer(value):
    return {"authorization": f"Bearer {value}"}


# ── U1 · who asked ───────────────────────────────────────────────────────────


def test_a_token_from_the_company_idp_proves_a_subject_and_its_groups(tmp_path):
    policy = identity_policy(tmp_path)
    subject = authenticate(policy.identity, bearer(token()), key_for=keys)
    assert subject == Subject("anna@acme.example", ("sales",), f"jwt:{ISSUER}")


@pytest.mark.parametrize(
    "bad",
    [
        token(aud="api://someone-else"),
        token(iss="https://evil.example"),
        token(exp=int(time.time()) - 3600),
        token(key=OTHER_KEY),
        token(email_verified=False),
        jwt.encode(
            {"iss": ISSUER, "aud": "api://annona", "exp": int(time.time()) + 600, "email": "x@y"},
            "shared-secret-is-not-accepted-by-this-verifier",
            algorithm="HS256",
        ),
    ],
    ids=["audience", "issuer", "expired", "wrong-key", "unverified-email", "hs256"],
)
def test_a_token_the_idp_did_not_issue_for_us_is_refused(tmp_path, bad):
    policy = identity_policy(tmp_path)
    with pytest.raises(IdentityError, match="no provider accepted"):
        authenticate(policy.identity, bearer(bad), key_for=keys)


def test_akaion_is_a_preset_of_the_same_verifier_not_a_special_case(tmp_path):
    policy = identity_policy(
        tmp_path, providers=[{"preset": "akaion", "project": "akaion-prod-eu"}]
    )
    (provider,) = policy.identity.providers
    assert (provider.kind, provider.issuer, provider.audience, provider.jwks_url) == (
        "jwt",
        "https://securetoken.google.com/akaion-prod-eu",
        "akaion-prod-eu",
        AKAION_JWKS,
    )
    firebase = token(
        iss="https://securetoken.google.com/akaion-prod-eu",
        aud="akaion-prod-eu",
        email_verified=True,
        groups=None,
    )
    subject = authenticate(policy.identity, bearer(firebase), key_for=keys)
    assert subject.id == "anna@acme.example" and subject.groups == ()


def test_an_incomplete_provider_is_a_policy_error(tmp_path):
    with pytest.raises(PolicyError, match="needs project"):
        identity_policy(tmp_path, providers=[{"preset": "akaion"}])
    with pytest.raises(PolicyError, match="needs secret_env"):
        identity_policy(tmp_path, providers=[{"kind": "proxy"}])
    with pytest.raises(PolicyError, match="no provider"):
        identity_policy(tmp_path, required=True)


def test_proxy_headers_count_only_with_the_proxy_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNONA_PROXY_SECRET", "s3cret")
    policy = identity_policy(tmp_path)
    forwarded = {"x-forwarded-email": "luca@acme.example", "x-forwarded-groups": "legal, admins"}

    with pytest.raises(IdentityError, match="without the proxy's secret"):
        authenticate(policy.identity, forwarded)
    with pytest.raises(IdentityError):
        authenticate(policy.identity, {**forwarded, PROXY_SECRET_HEADER.lower(): "guess"})

    subject = authenticate(policy.identity, {**forwarded, PROXY_SECRET_HEADER.lower(): "s3cret"})
    assert subject == Subject("luca@acme.example", ("legal", "admins"), "proxy")


def test_anonymous_only_where_identity_is_optional(tmp_path):
    with pytest.raises(IdentityError, match="requires identity"):
        authenticate(identity_policy(tmp_path).identity, {})
    optional = identity_policy(tmp_path, providers=[{"kind": "proxy", "secret_env": "X"}])
    assert authenticate(optional.identity, {}).anonymous


def test_ask_refuses_an_anonymous_request_and_records_it(home):  # noqa: F811
    write_policy(
        home,
        POLICY
        + "\nidentity:\n  required: true\n  providers:\n"
        + "    - {kind: proxy, secret_env: ANNONA_PROXY_SECRET}\n",
    )
    executor = _Executor(result={"response": "never"})

    r = client_for(executor).post("/api/kernel/ask", json={"prompt": "hello"})

    assert r.status_code == 401
    assert executor.seen == {}, "the run never started"
    (entry,) = (json.loads(line) for line in (home / "ledger.jsonl").read_text().splitlines())
    assert (entry["kind"], entry["outcome"]) == ("identity", "refused")


# ── U1 · the subject is part of the record ───────────────────────────────────


def test_every_entry_carries_the_subject_inside_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    Ledger(path, fsync=False).record("inference", outcome="placed", klass=SensitivityClass.PUBLIC)
    anna = Subject("anna@acme.example", ("sales",))
    Ledger(path, fsync=False, subject=anna).record(
        "inference", outcome="placed", klass=SensitivityClass.RESTRICTED
    )

    rows = [_entry_json(e) for e in read_entries(path)]
    assert [(r["subject"], r["groups"]) for r in rows] == [("", []), (anna.id, ["sales"])]

    lines = path.read_text().splitlines()
    assert "subject" not in json.loads(lines[0]), "anonymous entries hash as they always did"
    assert json.loads(lines[1])["subject"] == "anna@acme.example"
    assert verify_file(path).ok

    path.write_text(lines[0] + "\n" + lines[1].replace("anna@", "marco@") + "\n")
    result = verify_file(path)
    assert not result.ok and "altered" in result.problem


# ── U2 · rules per group, a folder per person ────────────────────────────────


def grouped_policy(tmp_path):
    doc = policy_document(tmp_path)
    doc["groups"] = {"sales": ["anna@acme.example"], "legal": ["*@legal.acme.example"]}
    # A rule of its own for sales; everyone else falls through to R-restricted.
    doc["rules"].insert(
        0,
        {
            "id": "R-sales",
            "match": {"class": "restricted", "group": "sales"},
            "allow": ["local-gpu"],
            "prefer": "quality",
        },
    )
    doc["tools"]["allow"]["document_reader"] = [
        f"{tmp_path}/shared/**",
        f"{tmp_path}/people/${{subject}}/**",
    ]
    return parse_policy(doc)


def test_a_rule_for_a_group_applies_only_to_its_members(tmp_path):
    policy = grouped_policy(tmp_path)
    anna = policy.for_subject(Subject("anna@acme.example"))
    luca = policy.for_subject(Subject("luca@legal.acme.example"))

    assert policy.groups_of(Subject("luca@legal.acme.example")) == ("legal",)
    assert anna.rule_for(SensitivityClass.RESTRICTED).id == "R-sales"
    assert luca.rule_for(SensitivityClass.RESTRICTED).id == "R-restricted"


def test_subject_in_an_allow_list_is_one_folder_per_person_never_everyones(tmp_path):
    policy = grouped_policy(tmp_path)

    def may_read(subject, path):
        return policy.for_subject(subject).tools.permits_path("document_reader", path)[0]

    anna = Subject("anna@acme.example")
    assert may_read(anna, f"{tmp_path}/people/anna@acme.example/cv.pdf")
    assert not may_read(anna, f"{tmp_path}/people/marco@acme.example/cv.pdf")
    assert may_read(anna, f"{tmp_path}/shared/listino.pdf")
    # Nobody, or a name that is not one path segment, gets no personal folder at all.
    for who in (Subject(), Subject("*"), Subject(".."), Subject("a/../marco@acme.example")):
        assert not may_read(who, f"{tmp_path}/people/marco@acme.example/cv.pdf"), who.id
        assert may_read(who, f"{tmp_path}/shared/listino.pdf")


# ── U3 · memory folders per group ────────────────────────────────────────────


def two_memories(tmp_path):
    sales = tmp_path / "Memoria-Sales"
    legal = tmp_path / "Memoria-Legal"
    sales.mkdir()
    legal.mkdir()
    (sales / "verbale.md").write_text(
        "Nordika è partner diretto sulla piattaforma.\n\nClausola 7.3 dell'NDA: avvisare prima.",
        encoding="utf-8",
    )
    (legal / "parere.md").write_text("Nordika: contratto quadro in revisione.", encoding="utf-8")
    index = MemoryIndex(tmp_path / "index.sqlite")
    index.build(
        [f"{sales}/**", f"{legal}/**"],
        fake_embed,
        read_text,
        model="fake",
        endpoint="-",
        facts=fake_facts,
    )
    return index, sales, legal


def test_a_folder_of_another_group_is_never_retrieved_scored_or_followed(tmp_path):
    index, sales, legal = two_memories(tmp_path)
    doc = policy_document(tmp_path)
    doc["groups"] = {"sales": ["anna@acme.example"]}
    doc["memory"] = {
        "embed_with": "local-gpu",
        "folders": [{"path": f"{sales}/**", "groups": ["sales"]}, f"{legal}/**"],
    }
    doc["substrates"][0]["kind"] = "ollama"
    policy = parse_policy(doc)

    anna = roots(policy.for_subject(Subject("anna@acme.example")).memory.folders)
    luca = roots(policy.for_subject(Subject("luca@acme.example")).memory.folders)
    assert len(anna) == 2 and luca == roots([f"{legal}/**"])

    query = "Nordika partner contratto"
    assert {h.path for h in index.search(query, fake_embed, within=anna)} == {
        str((sales / "verbale.md").resolve()),
        str((legal / "parere.md").resolve()),
    }
    hits = index.search(query, fake_embed, within=luca)
    assert [h.path for h in hits] == [str((legal / "parere.md").resolve())]
    # The graph too: the second hop to the NDA lives in the sales minutes only.
    relations = {f.relation for f in index.facts_about("Nordika Mobility", within=anna)}
    assert relations == {"partner_of", "bound_by"}
    facts = index.facts_about("Nordika Mobility", within=luca)
    assert facts and {f.path for f in facts} == {str((legal / "parere.md").resolve())}


def test_the_scope_is_the_perimeters_to_set_never_the_models(tmp_path):
    class Recorder:
        def specs(self):
            return ()

        def invoke(self, call):
            self.call = call
            return call

    inner = Recorder()
    scoped = MemoryScope(inner, ("/allowed",))
    scoped.invoke(ToolCall("1", "memory_search", {"query": "x", "within": ["/"]}))
    assert inner.call.arguments["within"] == ["/allowed"]
    scoped.invoke(ToolCall("2", "document_reader", {"path": "/a"}))
    assert "within" not in inner.call.arguments


def test_for_run_narrows_the_policy_and_stamps_the_ledger(tmp_path):
    enforcement = Enforcement.for_run(
        policy=grouped_policy(tmp_path),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={},
        probe=False,
        fsync=False,
        subject=Subject("anna@acme.example", via="proxy"),
    )
    assert enforcement.policy.rule_for(SensitivityClass.RESTRICTED).id == "R-sales"
    enforcement.ledger.record("run", outcome="started", klass=SensitivityClass.PUBLIC)
    entry = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    assert (entry["subject"], entry["groups"]) == ("anna@acme.example", ["sales"])


# ── whoami: what the window shows is what the perimeter verified ─────────────


def test_whoami_reports_the_verified_subject_not_the_windows_belief(home, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ANNONA_PROXY_SECRET", "s3cret")
    write_policy(
        home,
        POLICY
        + "\nidentity:\n  required: true\n  providers:\n"
        + "    - {kind: proxy, secret_env: ANNONA_PROXY_SECRET}\n"
        + "    - {preset: akaion, project: akaion-prod-eu}\n"
        + "groups:\n  sales: ['*@acme.example']\n",
    )
    client = client_for(None)

    nobody = client.get("/api/kernel/identity").json()
    assert (nobody["required"], nobody["you"], nobody["problem"]) == (True, None, "")
    assert [p["label"] for p in nobody["providers"]] == ["your organisation's sign-in", "Akaion"]
    assert [p["signin"] for p in nobody["providers"]] == ["", "akaion"]

    anna = client.get(
        "/api/kernel/identity",
        headers={"X-Forwarded-Email": "anna@acme.example", PROXY_SECRET_HEADER: "s3cret"},
    ).json()["you"]
    assert (anna["id"], anna["groups"]) == ("anna@acme.example", ["sales"])

    forged = client.get("/api/kernel/identity", headers={"Authorization": "Bearer abc.def.ghi"})
    assert forged.status_code == 200 and forged.json()["you"] is None
    assert "no provider accepted" in forged.json()["problem"]
