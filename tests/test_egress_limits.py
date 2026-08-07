"""What redaction may not do, and the material no transformation releases.

These tests exist because of a specific failure that was reachable in this
codebase until it was written down. A studio configures ``on_unavailable:
redact`` so the good model can be used when the local GPU is down. An M&A
memorandum is restricted — it carries a codice fiscale. The local GPU dies. The
redactor replaces the tax code, the name and the two company names, the redacted
text is reclassified as *public* because the pattern that made it restricted is
the pattern that was just removed, and this crosses to a US frontier API::

    Progetto Falcon — memorandum riservato.
    Il nostro cliente [ORG_1] ha incaricato lo studio di assistere
    l'acquisizione del 70% di [ORG_2] per [AMOUNT_1]. Signing entro [DATE_1].

Nothing personal remains. The entire secret is intact: a firm is advising on an
acquisition of that size on that timetable, and the provider knows which firm is
asking because the firm is holding the account. Two facts and a newspaper name
the target.

The lesson is not "the redactor was bad". The redactor was perfect. The lesson
is that **class is a statement about identifiers and sensitivity is not**, and
so a mechanism that removes identifiers must never be able to grant permission
on its own. Three controls follow, and each has a test here.
"""

from __future__ import annotations

import pytest

from runner.kernel.blocks import text_block
from runner.kernel.errors import PlacementHeldError, PolicyError
from runner.kernel.types import (
    Completion,
    CompletionRequest,
    Requirement,
    SensitivityClass,
    Turn,
)
from runner.policy.loader import parse_policy
from runner.policy.redaction import Redaction
from runner.services.enforcement import Enforcement

MEMO = (
    "Progetto Falcon — memorandum riservato.\n"
    "Il nostro cliente Brembo S.p.A. ha incaricato lo studio di assistere "
    "l'acquisizione del 70% di Marposs S.p.A. per 480 milioni di euro. "
    "Referente: Mario Rossi, CF RSSMRA85T10A562S.\n"
    "Signing previsto entro il 30/09/2026."
)

BASE = {
    "version": 1,
    "default": "deny",
    "classes": {
        "restricted": {"patterns": [r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]"]},
        "internal": {"paths": ["/mnt/studio/**"]},
        "public": {"default": True},
    },
    "substrates": [
        {"id": "local-gpu", "kind": "echo", "jurisdiction": "on-prem", "max_class": "restricted"},
        {"id": "frontier", "kind": "echo", "jurisdiction": "us", "max_class": "public"},
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
        {"id": "R-public", "match": {"class": "public"}, "allow": ["frontier", "local-gpu"]},
    ],
    "redaction": {"provider": "rizzo-pii", "labels": {"CF": "restricted", "FULLNAME": "internal"}},
    "egress": {"brief": {"produced_by": "local-gpu"}, "redact": {"allowed_for": ["internal"]}},
}


class PerfectRedactor:
    """A redactor that misses nothing — which is exactly the point."""

    name = "rizzo-pii:test"

    def analyse(self, text: str) -> Redaction:
        mapping = {
            "[ORG_1]": "Brembo S.p.A.",
            "[ORG_2]": "Marposs S.p.A.",
            "[FULLNAME_1]": "Mario Rossi",
            "[CF_1]": "RSSMRA85T10A562S",
            "[AMOUNT_1]": "480 milioni di euro",
            "[DATE_1]": "30/09/2026",
        }
        out = text
        for placeholder, value in mapping.items():
            out = out.replace(value, placeholder)
        return Redaction(
            text=out,
            mapping=mapping,
            labels={"ORG": 2, "FULLNAME": 1, "CF": 1, "AMOUNT": 1, "DATE": 1},
        )


class Spy:
    """A substrate that records what it was actually sent."""

    capabilities = None

    def __init__(self, name: str) -> None:
        self.name = name
        self.received: list[str] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.received.append(
            " ".join(str(getattr(b, "content", b)) for t in request.transcript for b in t.blocks)
        )
        return Completion(text_parts=("ok",))


def perimeter(tmp_path, document, *, touched=None):
    spies = {"local-gpu": Spy("local-gpu"), "frontier": Spy("frontier")}
    enforcement = Enforcement.for_run(
        policy=parse_policy(document),
        ledger_path=tmp_path / "ledger.jsonl",
        backends=spies,
        redactor=PerfectRedactor(),
        probe=False,
        fsync=False,
    )
    # The situation the escape hatches exist for: the only substrate allowed to
    # see this material is down.
    enforcement.registry.mark_down("local-gpu", "the GPU died at 14:02")
    if touched:
        enforcement.working_set.observe(*touched)
    return enforcement, spies


def ask(enforcement, text=MEMO):
    return enforcement.backend().complete(
        CompletionRequest(
            system="Sei un assistente legale.",
            transcript=(Turn(role="user", blocks=(text_block(text),)),),
        )
    )


# ── 1 · Redaction is opt-in, per class ────────────────────────────────────────


def test_redaction_is_not_available_until_a_policy_names_the_classes(tmp_path):
    """Off by default. A brief is a paragraph; a redaction is the whole document."""
    document = {**BASE, "egress": {"brief": {"produced_by": "local-gpu"}}}
    enforcement, spies = perimeter(
        tmp_path, document, touched=("/mnt/studio/nota.txt", SensitivityClass.INTERNAL)
    )

    with pytest.raises(PlacementHeldError, match="redaction is not permitted"):
        ask(enforcement, "Nota interna: quali termini si applicano?")

    assert spies["frontier"].received == []


def test_restricted_material_is_never_redacted_out(tmp_path):
    """The failure this file was written for.

    The memorandum is restricted, the policy asks for redaction, the redactor
    would have done its job perfectly — and the step is held, because removing
    the identifiers does not change what the text is about.
    """
    enforcement, spies = perimeter(tmp_path, BASE)

    with pytest.raises(PlacementHeldError) as held:
        ask(enforcement)

    assert "redaction is not permitted for class restricted" in str(held.value)
    assert spies["frontier"].received == [], "nothing reached the frontier model"
    assert "Falcon" not in enforcement.ledger.path.read_text()


def test_declaring_restricted_redactable_is_possible_and_explicit(tmp_path):
    """The judgement the product must not make on the operator's behalf.

    Most restricted material is restricted *because of* its identifiers — the
    letter carrying a codice fiscale is the case redaction exists for — and no
    classifier tells that apart from a memorandum that is restricted because of
    what it is about. Refusing the class outright would delete the feature
    rather than secure it, so both readings are available in one line and the
    policy file records which was chosen.
    """
    policy = parse_policy(
        {**BASE, "egress": {"redact": {"allowed_for": ["internal", "restricted"]}}}
    )

    assert policy.egress.permits_redaction(SensitivityClass.RESTRICTED)
    # A brief still may not: that list has always meant something stricter, and
    # a summary is produced by a model rather than by substitution.
    with pytest.raises(PolicyError, match="may not include 'restricted'"):
        parse_policy({**BASE, "egress": {"brief": {"allowed_for": ["restricted"]}}})


def test_the_seal_holds_even_when_everything_is_redactable(tmp_path):
    """The working deployment, with the matter named.

    Redaction is permitted for restricted material; the memorandum still does
    not move, because a seal is not a statement about identifiers.
    """
    document = {
        **BASE,
        "egress": {
            "redact": {"allowed_for": ["internal", "restricted"]},
            "sealed": {"patterns": [r"Progetto\s+\w+"]},
        },
    }
    enforcement, spies = perimeter(tmp_path, document)

    with pytest.raises(PlacementHeldError, match="sealed"):
        ask(enforcement)

    assert spies["frontier"].received == []


def test_an_ordinary_letter_is_exactly_what_redaction_is_for(tmp_path):
    """And it works: restricted by its identifiers, and they are what leave.

    This is the case the strict reading refuses and the working reading serves.
    The text that crosses is recorded verbatim so a person can check the
    judgement afterwards instead of trusting it.
    """
    document = {**BASE, "egress": {"redact": {"allowed_for": ["internal", "restricted"]}}}
    enforcement, spies = perimeter(tmp_path, document)

    enforcement.backend().complete(
        CompletionRequest(
            system="",
            transcript=(
                Turn(
                    role="user",
                    blocks=(
                        text_block("Il Sig. Mario Rossi, CF RSSMRA85T10A562S, chiede una proroga."),
                    ),
                ),
            ),
        )
    )

    crossed = spies["frontier"].received[0]
    assert "RSSMRA85T10A562S" not in crossed
    assert "[CF_1]" in crossed
    assert "chiede una proroga" in crossed


def test_internal_material_still_takes_the_route_it_was_given(tmp_path):
    """The feature is not broken: this is what redaction is *for*.

    A working note whose sensitivity is the names in it. The policy opted in for
    that class, the identifiers go, and the redacted question reaches the model
    that can answer it.
    """
    enforcement, spies = perimeter(tmp_path, BASE)
    enforcement.working_set.observe("/mnt/studio/nota.txt", SensitivityClass.INTERNAL)

    ask(enforcement, "Il Sig. Mario Rossi chiede una proroga: quali termini si applicano?")

    assert spies["frontier"].received, "the redacted question should have crossed"
    crossed = spies["frontier"].received[0]
    assert "Mario Rossi" not in crossed
    assert "[FULLNAME_1]" in crossed
    assert enforcement.ledger.entries()[-1].outcome == "redacted"


# ── 2 · Sealed matter: no transformation releases it ──────────────────────────

SEALED = {
    **BASE,
    "egress": {
        "brief": {"produced_by": "local-gpu", "allowed_for": ["internal"]},
        "redact": {"allowed_for": ["internal"]},
        "sealed": {"patterns": [r"Progetto\s+\w+", r"memorandum riservato"]},
    },
}


def test_sealed_matter_is_not_redacted(tmp_path):
    enforcement, spies = perimeter(tmp_path, SEALED)
    enforcement.working_set.observe("/mnt/studio/falcon.docx", SensitivityClass.INTERNAL)

    with pytest.raises(PlacementHeldError, match="sealed"):
        ask(enforcement)

    assert spies["frontier"].received == []


def test_sealed_matter_is_not_briefed_either(tmp_path):
    """Because a summary of a deal is still the deal.

    This is the control that does not depend on a detector being good: it does
    not look for identifiers at all, it recognises the matter.
    """
    document = {
        **SEALED,
        "rules": [
            {**rule, "on_unavailable": "brief"} if rule["id"] != "R-public" else rule
            for rule in SEALED["rules"]
        ],
    }
    enforcement, spies = perimeter(tmp_path, document)
    enforcement.working_set.observe("/mnt/studio/falcon.docx", SensitivityClass.INTERNAL)

    with pytest.raises(PlacementHeldError, match="sealed"):
        ask(enforcement)

    assert spies["frontier"].received == []


def test_a_seal_survives_the_turn_that_mentioned_it(tmp_path):
    """Monotone, like the working set.

    Turn one quotes the codename; turn two says "and the timetable?". A control
    that only inspects the current payload would let the second turn out, and
    the second turn is about the same deal.
    """
    enforcement, spies = perimeter(tmp_path, SEALED)

    with pytest.raises(PlacementHeldError):
        ask(enforcement)

    with pytest.raises(PlacementHeldError, match="sealed"):
        ask(enforcement, "E la tempistica del signing?")

    assert spies["frontier"].received == []


def test_a_sealed_path_seals_the_run_even_without_the_words(tmp_path):
    """Naming the file is enough; the text never has to say what it is."""
    document = {
        **BASE,
        "egress": {
            "redact": {"allowed_for": ["internal"]},
            "sealed": {"paths": ["/mnt/studio/mna/**"]},
        },
    }
    enforcement, spies = perimeter(tmp_path, document)
    enforcement.working_set.observe("/mnt/studio/nota.txt", SensitivityClass.INTERNAL)

    with pytest.raises(PlacementHeldError, match="sealed"):
        ask(enforcement, "Riassumi /mnt/studio/mna/term-sheet.docx per il socio.")

    assert spies["frontier"].received == []


def test_sealing_nothing_changes_nothing(tmp_path):
    """A policy with no seal behaves exactly as before — no cost for not using it."""
    enforcement, spies = perimeter(tmp_path, BASE)
    enforcement.working_set.observe("/mnt/studio/nota.txt", SensitivityClass.INTERNAL)

    ask(enforcement, "Il Sig. Mario Rossi chiede una proroga.")

    assert spies["frontier"].received


# ── 3 · The floor ─────────────────────────────────────────────────────────────


def test_the_floor_stops_redaction_from_laundering_the_class(tmp_path):
    """With a floor of internal, redacted material stays internal.

    The stricter reading, for a deployment that wants it: a frontier substrate
    then has to be declared willing to hold internal material before redacted
    work can run there. That is a sentence in a policy file somebody can be
    asked about, which is the difference between a decision and an accident.
    """
    document = {
        **BASE,
        "redaction": {**BASE["redaction"], "floor": "internal"},
    }
    enforcement, spies = perimeter(tmp_path, document)
    enforcement.working_set.observe("/mnt/studio/nota.txt", SensitivityClass.INTERNAL)

    with pytest.raises(PlacementHeldError):
        ask(enforcement, "Il Sig. Mario Rossi chiede una proroga.")

    assert spies["frontier"].received == []


def test_the_sealed_requirement_reaches_the_engine(tmp_path):
    """Unit-level: the engine refuses both hatches when the step is sealed."""
    from runner.placement.engine import PlacementDecisionEngine
    from runner.placement.registry import SubstrateRegistry

    policy = parse_policy(SEALED)
    engine = PlacementDecisionEngine(
        policy, SubstrateRegistry.from_substrates(policy.substrates, prober=None)
    )
    engine._registry.mark_down("local-gpu", "down")  # noqa: SLF001 — the point of the test

    sealed = engine.place(SensitivityClass.INTERNAL, Requirement(sealed=True))
    open_matter = engine.place(SensitivityClass.INTERNAL, Requirement())

    assert sealed.outcome == "held"
    assert open_matter.outcome == "redacted"
