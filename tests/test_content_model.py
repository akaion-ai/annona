"""A learned content model on top of the policy's paths and patterns.

Three properties: it is consulted wherever content is (one seam covers the
router, the gate and the tracker); it can only raise a class; and a judge that
fails counts as restricted.
"""

from __future__ import annotations

import random

import httpx
import pytest

from runner.agent.loop import AgentLoop
from runner.capability.classifiers.systemone import SystemOneJudge, conformal_threshold
from runner.kernel.types import Completion, SensitivityClass
from runner.policy.classifier import PolicyClassifier
from runner.policy.loader import parse_policy
from runner.services.enforcement import Enforcement
from tests.test_enforcement import ScriptedSubstrate, policy_document

OFFER = "Offriamo 1.200 schede a 184 EUR cadauna, sconto 18% riservato al vostro gruppo."


class Keyword:
    """A content model that calls anything mentioning a discount restricted."""

    def __init__(self):
        self.seen: list[str] = []

    def classify(self, text: str) -> SensitivityClass:
        self.seen.append(text)
        return SensitivityClass.RESTRICTED if "sconto" in text else SensitivityClass.PUBLIC


class Broken:
    def classify(self, text: str) -> SensitivityClass:
        raise RuntimeError("judge is down")


class SaysPublic:
    def classify(self, text: str) -> SensitivityClass:
        return SensitivityClass.PUBLIC


def test_the_model_raises_a_class_the_patterns_cannot_see(tmp_path):
    policy = parse_policy(policy_document(tmp_path))
    assert PolicyClassifier(policy).classify_text(OFFER) is SensitivityClass.PUBLIC
    assert PolicyClassifier(policy, Keyword()).classify_text(OFFER) is SensitivityClass.RESTRICTED


def test_the_model_never_lowers_what_the_patterns_found(tmp_path):
    policy = parse_policy(policy_document(tmp_path))
    tax_code = "cliente RSSMRA85T10A562S"
    assert PolicyClassifier(policy, SaysPublic()).classify_text(tax_code) is (
        SensitivityClass.RESTRICTED
    )


def test_a_failing_judge_fails_upward(tmp_path):
    policy = parse_policy(policy_document(tmp_path))
    assert PolicyClassifier(policy, Broken()).classify_text("hello") is SensitivityClass.RESTRICTED


def test_an_offer_with_no_identifier_stays_on_prem_through_the_loop(tmp_path):
    """End to end: the prompt itself is judged before it is placed."""
    frontier = ScriptedSubstrate("frontier")
    local = ScriptedSubstrate("local-gpu", [Completion(text_parts=("local",))], local=True)
    enforcement = Enforcement.for_run(
        policy=parse_policy(policy_document(tmp_path)),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={"local-gpu": local, "eu-cluster": ScriptedSubstrate("eu"), "frontier": frontier},
        probe=False,
        fsync=False,
        content_model=Keyword(),
    )
    loop = AgentLoop(enforcement.backend(), enforcement.executor(_NoTools()), enforcement.gate())
    loop.run(f"rewrite this for the customer: {OFFER}")

    assert frontier.calls == 0
    assert local.calls == 1
    assert enforcement.klass is SensitivityClass.RESTRICTED


class _NoTools:
    def specs(self):
        return ()

    def invoke(self, call):  # pragma: no cover - never called
        raise AssertionError


# ── The systemone adapter ─────────────────────────────────────────────────────


def judge_answering(probabilities, *, tau=0.3, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"answers": {"sensitivity": {"type": "choice", "probabilities": probabilities}}}
        return httpx.Response(status, json=body)

    return SystemOneJudge(tau=tau, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_strict_mass_includes_what_the_judge_would_not_assign():
    # 0.2 restricted + 0.15 on an option it did not name ("cannot tell") ≥ 0.3
    judge = judge_answering({"public": 0.6, "internal": 0.05, "restricted": 0.2})
    assert judge.classify("x") is SensitivityClass.RESTRICTED


def test_below_the_threshold_the_class_follows_the_rest_of_the_mass():
    assert (
        judge_answering({"public": 0.9, "internal": 0.08, "restricted": 0.02}).classify("x")
        is SensitivityClass.PUBLIC
    )
    assert (
        judge_answering({"public": 0.3, "internal": 0.68, "restricted": 0.02}).classify("x")
        is SensitivityClass.INTERNAL
    )


def test_an_unreachable_judge_raises_so_the_classifier_can_fail_upward():
    judge = judge_answering({}, status=503)
    with pytest.raises(Exception):  # noqa: B017 - any failure is the contract
        judge.classify("x")


def test_the_conformal_threshold_bounds_misses_empirically():
    """Over many calibration draws, a fresh restricted text is missed at most alpha of the time."""
    rng = random.Random(0)
    alpha, misses, trials = 0.05, 0, 4000
    for _ in range(trials):
        calibration = [rng.betavariate(2, 5) for _ in range(99)]  # a poorly calibrated judge
        tau = conformal_threshold(calibration, alpha)
        misses += rng.betavariate(2, 5) < tau
    assert misses / trials <= alpha + 0.01


def test_too_few_calibration_texts_cannot_promise_alpha():
    with pytest.raises(ValueError, match="cannot support"):
        conformal_threshold([0.9] * 10, 0.01)
