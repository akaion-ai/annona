"""Pseudonymisation: the fourth answer, and the ways it must not be trusted.

Redaction is the most dangerous feature in the project, because it is the one
that turns a refusal into a crossing. These tests are written from that angle:
they assert that the redacted payload is reclassified rather than believed, that
a redactor outage stops the step instead of quietly disabling the control, and
that the mapping never appears anywhere it could leak.
"""

from __future__ import annotations

import httpx
import pytest

from runner.agent.loop import AgentLoop
from runner.capability.redactors.rizzo_pii import DEFAULT_LABEL_CLASSES, RizzoPiiRedactor
from runner.kernel.errors import BackendUnavailableError, PlacementHeldError, PolicyError
from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    SensitivityClass,
    ToolCall,
)
from runner.policy.loader import parse_policy
from runner.policy.redaction import (
    Redaction,
    RedactionPolicy,
    class_for_labels,
    restore,
    unresolved_placeholders,
)
from runner.services.enforcement import Enforcement

pytestmark = pytest.mark.unit

RESTRICTED = SensitivityClass.RESTRICTED
INTERNAL = SensitivityClass.INTERNAL
PUBLIC = SensitivityClass.PUBLIC

REAL = "Il Sig. Mario Rossi, C.F. RSSMRA85T10A562S, chiede una proroga."
REDACTED = "Il Sig. [FULLNAME_1], C.F. [CF_1], chiede una proroga."
MAPPING = {"[FULLNAME_1]": "Mario Rossi", "[CF_1]": "RSSMRA85T10A562S"}


# ── Putting the values back ───────────────────────────────────────────────────


def test_restore_is_the_inverse_of_the_mapping():
    assert restore(REDACTED, MAPPING) == REAL


def test_restore_is_a_pure_local_function():
    """It has to work with the network on fire: an answer nobody can read is a
    failure even when every control behaved correctly."""
    assert restore("[CF_1]", {"[CF_1]": "X"}) == "X"
    assert restore("nothing to do", {}) == "nothing to do"


def test_longer_placeholders_win_so_indices_do_not_collide():
    """`[CF_1]` must not eat the prefix of `[CF_11]`."""
    text = "primo [CF_1], undicesimo [CF_11]"
    mapping = {"[CF_1]": "AAA", "[CF_11]": "BBB"}
    assert restore(text, mapping) == "primo AAA, undicesimo BBB"


def test_a_placeholder_the_model_invented_is_named_not_shipped():
    """Models hallucinate tokens that look like placeholders. Catching that is
    the difference between an answer with a name in it and an answer with
    `[FULLNAME_9]` in it."""
    assert unresolved_placeholders("ciao [FULLNAME_9] e [CF_1]", MAPPING) == ("[FULLNAME_9]",)
    assert unresolved_placeholders(REDACTED, MAPPING) == ()


# ── Translating a detector's vocabulary into classes ──────────────────────────


def test_the_class_is_the_maximum_over_what_was_found():
    policy = RedactionPolicy(
        provider="rizzo-pii",
        labels={"FULLNAME": INTERNAL, "CF": RESTRICTED},
    )
    assert class_for_labels({"FULLNAME": 3}, policy) is INTERNAL
    assert class_for_labels({"FULLNAME": 3, "CF": 1}, policy) is RESTRICTED


def test_an_unmapped_label_is_not_treated_as_harmless():
    """A detector naming something it recognised is evidence, not noise."""
    policy = RedactionPolicy(provider="x", labels={}, default_label_class=INTERNAL)
    assert class_for_labels({"SOMETHING_NEW": 1}, policy) is INTERNAL


def test_zero_counts_do_not_raise_the_class():
    policy = RedactionPolicy(provider="x", labels={"CF": RESTRICTED})
    assert class_for_labels({"CF": 0}, policy) is PUBLIC


def test_the_shipped_label_map_covers_the_italian_legal_identifiers():
    """The five rizzo-pii calls out as unique to it must be the strict ones."""
    for label in ("CF", "PIVA", "CATASTO", "DOCID", "ID_DOC"):
        assert DEFAULT_LABEL_CLASSES[label] == "restricted"


# ── The rizzo-pii adapter ─────────────────────────────────────────────────────


class FakeHTTP:
    def __init__(self, payload=None, status=200, raises=None):
        self.payload = payload
        self.status = status
        self.raises = raises
        self.sent = None
        self.url = ""

    def post(self, url, json=None):
        if self.raises:
            raise self.raises
        self.url, self.sent = url, json
        return httpx.Response(self.status, json=self.payload, request=httpx.Request("POST", url))

    def get(self, url):
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json={}, request=httpx.Request("GET", url))


ANALYZE_OK = {
    "anonymized_text": REDACTED,
    "mapping": MAPPING,
    "by_label": {"FULLNAME": 1, "CF": 1},
    "n_entities": 2,
}


def test_it_posts_the_text_to_the_analyze_contract():
    http = FakeHTTP(ANALYZE_OK)
    RizzoPiiRedactor(client=http).analyse(REAL)

    assert http.url == "http://127.0.0.1:5005/analyze"
    # include_mapping is sent explicitly rather than relied on as a server
    # default: whether an answer can be re-identified afterwards is the caller's
    # decision, and the server can be started either way.
    assert http.sent == {"text": REAL, "include_mapping": True}


def test_excluded_tags_are_sent_per_request_when_a_deployment_asks():
    http = FakeHTTP(ANALYZE_OK)
    RizzoPiiRedactor(client=http, exclude_tags=["CITY", "DATE"]).analyse(REAL)

    assert http.sent["exclude_tags"] == ["CITY", "DATE"]


def test_a_server_without_a_mapping_is_reported_rather_than_trusted():
    """The reply would come back full of placeholders nobody can resolve."""
    body = {**ANALYZE_OK, "mapping": {}, "mapping_enabled": False}

    with pytest.raises(BackendUnavailableError, match="mapping disabled"):
        RizzoPiiRedactor(client=FakeHTTP(body)).analyse(REAL)


def test_definitive_anonymisation_is_available_on_purpose():
    """``keep_mapping=False`` asks the server not to build a dictionary at all."""
    http = FakeHTTP({**ANALYZE_OK, "mapping": {}, "mapping_enabled": False})
    redaction = RizzoPiiRedactor(client=http, keep_mapping=False).analyse(REAL)

    assert http.sent["include_mapping"] is False
    assert redaction.mapping == {}
    assert redaction.text == REDACTED


def test_it_returns_the_redacted_text_the_mapping_and_the_labels():
    redaction = RizzoPiiRedactor(client=FakeHTTP(ANALYZE_OK)).analyse(REAL)

    assert redaction.text == REDACTED
    assert redaction.mapping == MAPPING
    assert redaction.labels == {"FULLNAME": 1, "CF": 1}
    assert redaction.count == 2
    assert redaction.changed


def test_empty_text_needs_no_round_trip():
    http = FakeHTTP(ANALYZE_OK)
    assert RizzoPiiRedactor(client=http).analyse("   ").text == "   "
    assert http.sent is None


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"raises": httpx.ConnectError("refused")}, "unreachable"),
        ({"payload": {}, "status": 500}, "returned 500"),
        ({"payload": {"error": "text too long"}}, "refused the text"),
        ({"payload": {"mapping": {}}}, "no anonymized_text"),
    ],
)
def test_every_failure_raises_rather_than_returning_the_original(kwargs, match):
    """The one behaviour that would be catastrophic: returning the text
    unchanged on error. The caller would send the material believing it was
    cleaned, which is worse than not having a redactor at all."""
    with pytest.raises(BackendUnavailableError, match=match):
        RizzoPiiRedactor(client=FakeHTTP(**kwargs)).analyse(REAL)


# ── The policy ────────────────────────────────────────────────────────────────


BASE = {
    "version": 1,
    "default": "deny",
    "classes": {
        "restricted": {"patterns": [r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]"]},
        "internal": {"paths": ["/mnt/work/**"]},
        "public": {"default": True},
    },
    "substrates": [
        {"id": "local-gpu", "kind": "echo", "jurisdiction": "on-prem", "max_class": "restricted"},
        {
            "id": "frontier",
            "kind": "echo",
            "jurisdiction": "us",
            "max_class": "public",
            "quality": 99,
        },
    ],
    "rules": [
        {
            "id": "R-restricted",
            "match": {"class": "restricted"},
            "allow": ["local-gpu"],
            "on_unavailable": "redact",
        },
        {
            "id": "R-internal",
            "match": {"class": "internal"},
            "allow": ["local-gpu"],
            "on_unavailable": "redact",
        },
        {
            "id": "R-public",
            "match": {"class": "public"},
            "allow": ["frontier", "local-gpu"],
            "prefer": "quality",
        },
    ],
    "redaction": {
        "provider": "rizzo-pii",
        "endpoint": "http://127.0.0.1:5005",
        "labels": {"CF": "restricted", "FULLNAME": "internal"},
    },
    # Redaction is off until a policy names the classes that may take it.
    "egress": {"redact": {"allowed_for": ["internal"]}},
}


def test_asking_for_redaction_without_a_provider_is_refused():
    document = {**BASE, "redaction": {}}
    with pytest.raises(PolicyError, match="asks for redaction"):
        parse_policy(document)


def test_an_unknown_on_error_setting_is_refused():
    document = {**BASE, "redaction": {**BASE["redaction"], "on_error": "retry"}}
    with pytest.raises(PolicyError, match="on_error must be hold or ignore"):
        parse_policy(document)


def test_redaction_defaults_to_failing_closed():
    assert parse_policy(BASE).redaction.fails_closed is True


# ── End to end, through the router ────────────────────────────────────────────


class FakeRedactor:
    name = "fake-pii"

    def __init__(self, redaction: Redaction | None = None, raises=None):
        self.redaction = redaction or Redaction(text=REDACTED, mapping=MAPPING, labels={"CF": 1})
        self.raises = raises
        self.calls = 0

    def analyse(self, text: str) -> Redaction:
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.redaction


class Wiretap:
    def __init__(self, name="frontier", reply="the deadline is in March"):
        self._name, self._reply = name, reply
        self.received: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(native_tools=True, is_local=False, context_window=200_000)

    def complete(self, request: CompletionRequest) -> Completion:
        self.received.append(
            "\n".join(str(getattr(b, "content", b)) for t in request.transcript for b in t.blocks)
        )
        return Completion(text_parts=(self._reply,), stop_reason="end_turn")


def build(tmp_path, *, redactor, frontier=None, local_down=True, klass=INTERNAL):
    """A perimeter holding internal material with the local GPU unreachable.

    The situation redaction exists for: something sensitive to answer, and the
    only substrate allowed to see it is down.
    """
    frontier = frontier or Wiretap()
    enforcement = Enforcement.for_run(
        policy=parse_policy(BASE),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={"local-gpu": Wiretap("local-gpu"), "frontier": frontier},
        redactor=redactor,
        probe=False,
        fsync=False,
    )
    if local_down:
        enforcement.registry.mark_down("local-gpu", "simulated outage")
    enforcement.working_set.observe("/mnt/clients/BG-114.pdf", klass)
    return enforcement, frontier


def test_a_restricted_payload_crosses_only_after_its_identifiers_are_replaced(tmp_path):
    """The whole feature in one test.

    The local GPU is down, so a restricted step would be held. With a redactor
    the identifiers are replaced on the machine, the redacted text is
    reclassified as public, and *that* is what reaches the frontier model.
    """
    redactor = FakeRedactor()
    enforcement, frontier = build(tmp_path, redactor=redactor)

    completion = enforcement.backend().complete(
        CompletionRequest(system="", transcript=(), tools=())
    )

    assert redactor.calls == 1
    assert frontier.received == [REDACTED]
    assert "RSSMRA85T10A562S" not in frontier.received[0]
    assert "Mario Rossi" not in frontier.received[0]
    assert completion.text == "the deadline is in March"

    entry = enforcement.ledger.entries()[-1]
    assert entry.outcome == "redacted"
    assert entry.detail["redacted"] == {"CF": 1}
    assert "Mario Rossi" not in enforcement.ledger.path.read_text()


def test_the_answer_is_re_identified_locally(tmp_path):
    """Placeholders come back as the real values, here and nowhere else."""
    redactor = FakeRedactor()
    enforcement, _ = build(
        tmp_path,
        redactor=redactor,
        frontier=Wiretap(reply="Il Sig. [FULLNAME_1] ha tempo fino al 15 marzo."),
    )

    completion = enforcement.backend().complete(CompletionRequest(system="", transcript=()))
    assert completion.text == "Il Sig. Mario Rossi ha tempo fino al 15 marzo."


def test_placeholders_inside_tool_arguments_are_re_identified_too(tmp_path):
    """A model that answers with a tool call must not be handed a token to act on."""

    class CallingFrontier(Wiretap):
        def complete(self, request):
            self.received.append("x")
            return Completion(
                tool_calls=(ToolCall(id="1", name="lookup", arguments={"who": "[FULLNAME_1]"}),),
                stop_reason="tool_use",
            )

    enforcement, _ = build(tmp_path, redactor=FakeRedactor(), frontier=CallingFrontier())
    completion = enforcement.backend().complete(CompletionRequest(system="", transcript=()))

    assert completion.tool_calls[0].arguments == {"who": "Mario Rossi"}


def test_a_redaction_that_leaves_an_identifier_behind_is_held(tmp_path):
    """The redactor is the instrument; the perimeter stays the authority.

    Output that still matches a restricted pattern is treated as freshly
    arrived material, not as a promise that was kept.
    """
    leaky = FakeRedactor(
        Redaction(
            text="Il Sig. [FULLNAME_1], C.F. RSSMRA85T10A562S",  # the model missed one
            mapping={"[FULLNAME_1]": "Mario Rossi"},
            labels={"FULLNAME": 1},
        )
    )
    enforcement, frontier = build(tmp_path, redactor=leaky)

    with pytest.raises(PlacementHeldError, match="still restricted"):
        enforcement.backend().complete(CompletionRequest(system="", transcript=()))

    assert frontier.received == []


def test_a_canary_surviving_redaction_is_held(tmp_path):
    document = {**BASE, "egress": {"canaries": ["ANNONA-CANARY-7f3a91"]}}
    enforcement = Enforcement.for_run(
        policy=parse_policy(document),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={"local-gpu": Wiretap("local-gpu"), "frontier": Wiretap()},
        redactor=FakeRedactor(
            Redaction(text="cleaned but ANNONA-CANARY-7f3a91 remains", mapping={"[CF_1]": "x"})
        ),
        probe=False,
        fsync=False,
    )
    enforcement.registry.mark_down("local-gpu", "outage")
    enforcement.working_set.observe("seed", RESTRICTED)

    with pytest.raises(PlacementHeldError):
        enforcement.backend().complete(CompletionRequest(system="", transcript=()))


def test_a_redactor_outage_holds_the_step_by_default(tmp_path):
    """Fail closed: no redactor, no crossing. The alternative is a control that
    disappears exactly when the machine is under stress."""
    enforcement, frontier = build(
        tmp_path,
        redactor=FakeRedactor(raises=BackendUnavailableError("rizzo-pii is not running")),
    )

    with pytest.raises(PlacementHeldError, match="redactor is unavailable"):
        enforcement.backend().complete(CompletionRequest(system="", transcript=()))

    assert frontier.received == []


def test_with_on_error_ignore_the_outage_is_not_silently_swallowed(tmp_path):
    """`ignore` means "do not hold" — it does not mean "pretend it worked"."""
    document = {**BASE, "redaction": {**BASE["redaction"], "on_error": "ignore"}}
    enforcement = Enforcement.for_run(
        policy=parse_policy(document),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={"local-gpu": Wiretap("local-gpu"), "frontier": Wiretap()},
        redactor=FakeRedactor(raises=BackendUnavailableError("down")),
        probe=False,
        fsync=False,
    )
    enforcement.registry.mark_down("local-gpu", "outage")
    enforcement.working_set.observe("seed", RESTRICTED)

    with pytest.raises(BackendUnavailableError):
        enforcement.backend().complete(CompletionRequest(system="", transcript=()))


def test_the_run_still_works_through_the_real_loop(tmp_path):
    """Nothing about redaction is visible to the agent loop."""

    class Tools:
        def specs(self):
            return ()

        def invoke(self, call):  # pragma: no cover - no tools in this run
            raise AssertionError("no tools expected")

    enforcement, frontier = build(tmp_path, redactor=FakeRedactor())

    loop = AgentLoop(enforcement.backend(), enforcement.executor(Tools()), enforcement.gate())
    result = loop.run("riassumi la pratica")

    assert result.response == "the deadline is in March"
    assert frontier.received == [REDACTED]
