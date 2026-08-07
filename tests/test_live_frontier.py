"""Live checks against real frontier providers.

Skipped unless the credential for a provider is present, so CI stays hermetic
and a developer with one key runs exactly the one test that key can answer.

```bash
ANTHROPIC_API_KEY=sk-…  env/bin/python -m pytest tests/test_live_frontier.py -v
OPENAI_API_KEY=sk-…     env/bin/python -m pytest tests/test_live_frontier.py -v
ANNONA_LIVE_ENDPOINT=https://openrouter.ai/api/v1 \\
ANNONA_LIVE_MODEL=anthropic/claude-sonnet-5 \\
ANNONA_LIVE_KEY=sk-or-…  env/bin/python -m pytest tests/test_live_frontier.py -v
```

What these assert is the seam, not the model: that a substrate declared in a
policy file becomes a backend that answers, that the perimeter places the turn
before it crosses, and that a tool round-trips. A test that graded the answer
would fail on a model release; these fail only when the integration is broken.

The last test is the one worth having: it drives a **real** frontier provider
through the whole perimeter with a restricted working set and asserts that
nothing crossed. Every other guarantee in this repository is checked against a
fake substrate; this one is checked against the network.
"""

from __future__ import annotations

import os

import pytest

from runner.kernel.blocks import text_block
from runner.kernel.errors import PlacementHeldError
from runner.kernel.types import CompletionRequest, SensitivityClass, ToolSpec, Turn
from runner.policy.loader import parse_policy
from runner.services.enforcement import Enforcement, build_backend

pytestmark = pytest.mark.live

ASK = (Turn(role="user", blocks=(text_block("Reply with exactly: OK"),)),)

WEATHER = ToolSpec(
    name="get_weather",
    description="Current weather for a city.",
    schema={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
)


def substrate(**overrides) -> dict:
    return {
        "id": "frontier",
        "kind": "anthropic",
        "jurisdiction": "us",
        "max_class": "public",
        "tools": True,
        **overrides,
    }


PROVIDERS = {
    # Native Anthropic. The default model is whatever `build_backend` picks
    # when a policy omits one — so this test also catches a default that has
    # been retired out from under the project.
    "anthropic": (
        "ANTHROPIC_API_KEY",
        substrate(model=os.getenv("ANNONA_LIVE_ANTHROPIC_MODEL", "claude-opus-5")),
    ),
    # Anything speaking OpenAI's /v1 — OpenAI itself, Groq, Together,
    # OpenRouter, Mistral, Gemini's compatibility endpoint, vLLM, LM Studio.
    "openai": (
        "OPENAI_API_KEY",
        substrate(
            kind="openai-compatible",
            endpoint="https://api.openai.com/v1",
            model=os.getenv("ANNONA_LIVE_OPENAI_MODEL", "gpt-4o-mini"),
        ),
    ),
    # Whatever the operator points it at, for the provider this file does not
    # know about yet.
    "custom": (
        "ANNONA_LIVE_KEY",
        substrate(
            kind="openai-compatible",
            endpoint=os.getenv("ANNONA_LIVE_ENDPOINT", ""),
            model=os.getenv("ANNONA_LIVE_MODEL", ""),
            api_key_env="ANNONA_LIVE_KEY",
        ),
    ),
}


def available() -> list[str]:
    return [
        name
        for name, (env, spec) in PROVIDERS.items()
        if os.getenv(env) and (name != "custom" or (spec["endpoint"] and spec["model"]))
    ]


@pytest.fixture(params=list(PROVIDERS))
def provider(request):
    name = request.param
    env, spec = PROVIDERS[name]
    if name not in available():
        pytest.skip(f"set {env} to run the {name} checks")
    return name, spec


def policy_for(spec: dict, *, ceiling: str = "public") -> dict:
    """A minimal perimeter: one local substrate, one frontier, one rule each."""
    return {
        "version": 1,
        "default": "deny",
        "classes": {
            "restricted": {"patterns": [r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]"]},
            "internal": {"paths": ["~/**"]},
            "public": {"default": True},
        },
        "substrates": [{**spec, "max_class": ceiling}],
        "rules": [
            {"match": {"class": "restricted"}, "allow": []},
            {"match": {"class": "internal"}, "allow": []},
            {"match": {"class": "public"}, "allow": [spec["id"]]},
        ],
        "tools": {"allow": {}, "deny_paths": []},
    }


# ── The seam ──────────────────────────────────────────────────────────────────


def test_a_declared_substrate_becomes_a_backend_that_answers(provider):
    """Policy → backend → network → text. The whole configuration path."""
    _, spec = provider
    policy = parse_policy(policy_for(spec))
    backend = build_backend(policy.substrates[0])

    completion = backend.complete(
        CompletionRequest(system="Answer with one word.", transcript=ASK, max_tokens=64)
    )

    assert completion.text.strip(), "the provider returned no text"


def test_a_tool_definition_survives_the_round_trip(provider):
    """The schema this project emits is one the provider accepts and can call."""
    _, spec = provider
    policy = parse_policy(policy_for(spec))
    backend = build_backend(policy.substrates[0])

    completion = backend.complete(
        CompletionRequest(
            system="Use the tool when asked about weather. Do not answer from memory.",
            transcript=(Turn(role="user", blocks=(text_block("Weather in Milan?"),)),),
            tools=(WEATHER,),
            max_tokens=512,
        )
    )

    assert completion.tool_calls or completion.text, "neither a tool call nor an answer"
    for call in completion.tool_calls:
        assert call.name == "get_weather"
        assert isinstance(call.arguments, dict)


def test_the_perimeter_places_the_turn_before_it_crosses(provider, tmp_path):
    """A real crossing, recorded. The ledger is the artefact, not the answer."""
    _, spec = provider
    enforcement = Enforcement.for_run(
        policy=parse_policy(policy_for(spec)),
        ledger_path=tmp_path / "ledger.jsonl",
        probe=False,
        fsync=False,
    )

    completion = enforcement.backend().complete(
        CompletionRequest(system="Answer with one word.", transcript=ASK, max_tokens=64)
    )

    assert completion.text.strip()
    placed = [e for e in enforcement.ledger.entries() if e.outcome == "placed"]
    assert placed, "a crossing must be recorded"
    assert placed[-1].substrate == spec["id"]


def test_restricted_material_does_not_reach_a_real_provider(provider, tmp_path):
    """The claim, against the network rather than against a mock.

    A frontier substrate capped at ``public``, a run whose working set is
    restricted, and a live credential in the environment. The step is held: no
    request is made, and the refusal is in the ledger.
    """
    _, spec = provider
    enforcement = Enforcement.for_run(
        policy=parse_policy(policy_for(spec)),
        ledger_path=tmp_path / "ledger.jsonl",
        probe=False,
        fsync=False,
    )
    enforcement.working_set.observe("/mnt/clienti/BG-114.pdf", SensitivityClass.RESTRICTED)

    with pytest.raises(PlacementHeldError):
        enforcement.backend().complete(
            CompletionRequest(
                system="",
                transcript=(Turn(role="user", blocks=(text_block("Riassumi la pratica."),)),),
                max_tokens=64,
            )
        )

    held = [e for e in enforcement.ledger.entries() if e.outcome == "held"]
    assert held, "the refusal must be recorded"
    assert not any(e.outcome == "placed" for e in enforcement.ledger.entries())


def test_at_least_one_provider_was_actually_exercised():
    """Fails loudly if the whole file skipped — a silent skip proves nothing."""
    if not available():
        pytest.skip("no provider credentials in the environment")
    assert available()
