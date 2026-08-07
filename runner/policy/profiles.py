"""Starting policies a person can choose between, and what choosing costs them.

The default policy is one document, and it makes one decision for everybody:
nothing leaves this machine. That is the right default and the wrong *only*
option, because the two questions a new install actually raises —

    "can I use a frontier model for the harmless things?"
    "which folders may it read?"

— are answered today by opening ``policy.yaml`` in an editor and understanding a
schema before having run anything. Most people will not, so most installs run
the default forever, including the ones for whom it is wrong. A choice offered
once, in words, is a policy somebody has actually decided.

Each profile states its **consequence** rather than its settings. "Adds a
frontier substrate capped at public" is a description of YAML; "material this
machine classified as internal still never leaves, and internal is the floor for
anything unrecognised" is what the person is agreeing to. The second sentence is
the one that belongs in a chooser, and it is the one that has to stay true when
the profile changes.

Shared by the CLI (``annona setup``) and the daemon (``POST /api/kernel/policy``)
so the desktop window and the terminal cannot drift into offering different
things. This module builds *documents*; it validates nothing and writes nothing,
which is why it can live at L2 without reaching an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runner.policy.loader import default_policy_document

__all__ = [
    "FRONTIER_PROVIDERS",
    "PROFILES",
    "FrontierProvider",
    "Profile",
    "build_policy_document",
    "get_profile",
]


@dataclass(frozen=True)
class Profile:
    """One starting policy, described by what it means rather than what it sets."""

    id: str
    title: str
    summary: str
    """One line, for a list or a card."""

    consequence: str
    """What this profile means for material leaving the machine. The sentence a
    person is agreeing to, and the one that must be re-read whenever the
    document below changes."""

    needs_frontier: bool = False
    recommended: bool = False


PROFILES: tuple[Profile, ...] = (
    Profile(
        id="local-only",
        title="Local only",
        summary="One local model. Nothing is registered that could send material anywhere.",
        consequence=(
            "Nothing leaves this machine, ever. If the local model is down, work is "
            "held rather than sent elsewhere — you get a refusal, not a quiet fallback."
        ),
        recommended=True,
    ),
    Profile(
        id="frontier-for-public",
        title="Local, plus a frontier model for public material",
        summary="Adds a hosted model capped at the `public` class.",
        consequence=(
            "The hosted provider can only ever see material classified public. "
            "Unrecognised material is classified internal, not public, so in practice "
            "nothing reaches the provider until you deliberately mark something public "
            "— and restricted material is refused even if a rule names the provider."
        ),
        needs_frontier=True,
    ),
    Profile(
        id="read-nothing",
        title="Local only, and no file access",
        summary="One local model, and every file-reading tool switched off.",
        consequence=(
            "The kernel answers from the conversation alone. No tool may open a file, "
            "so nothing on disk can reach a model even by accident — useful on a shared "
            "or borrowed machine, and the profile to pick when in doubt."
        ),
    ),
)


@dataclass(frozen=True)
class FrontierProvider:
    """A hosted provider, as the four facts a substrate needs.

    The key's *name* is here; the key never is. A policy file is the document an
    operator hands an auditor, and a secret in it is a secret in a git history.
    """

    id: str
    title: str
    kind: str
    model: str
    api_key_env: str
    jurisdiction: str
    endpoint: str = ""
    quality: int = 95
    cost_per_mtok: float = 10.0


FRONTIER_PROVIDERS: tuple[FrontierProvider, ...] = (
    FrontierProvider(
        id="anthropic",
        title="Anthropic (Claude)",
        kind="anthropic",
        model="claude-opus-5",
        api_key_env="ANTHROPIC_API_KEY",
        jurisdiction="us",
        quality=98,
        cost_per_mtok=15.0,
    ),
    FrontierProvider(
        id="openai",
        title="OpenAI",
        kind="openai-compatible",
        model="gpt-4o",
        api_key_env="OPENAI_API_KEY",
        endpoint="https://api.openai.com/v1",
        jurisdiction="us",
        quality=95,
        cost_per_mtok=10.0,
    ),
    FrontierProvider(
        id="mistral",
        title="Mistral (EU)",
        kind="openai-compatible",
        model="mistral-large-latest",
        api_key_env="MISTRAL_API_KEY",
        endpoint="https://api.mistral.ai/v1",
        jurisdiction="eu",
        quality=88,
        cost_per_mtok=6.0,
    ),
    FrontierProvider(
        id="custom",
        title="Something else that speaks /v1/chat/completions",
        kind="openai-compatible",
        model="",
        api_key_env="ANNONA_SUBSTRATE_KEY",
        endpoint="",
        jurisdiction="world",
        quality=90,
        cost_per_mtok=8.0,
    ),
)


@dataclass
class FrontierChoice:
    """A provider, with whatever the person changed about it."""

    provider: FrontierProvider
    model: str = ""
    endpoint: str = ""
    api_key_env: str = ""
    jurisdiction: str = ""
    substrate_id: str = "frontier"
    extra: dict[str, Any] = field(default_factory=dict)

    def as_substrate(self) -> dict[str, Any]:
        substrate: dict[str, Any] = {
            "id": self.substrate_id,
            "kind": self.provider.kind,
            "model": self.model or self.provider.model,
            "api_key_env": self.api_key_env or self.provider.api_key_env,
            "jurisdiction": self.jurisdiction or self.provider.jurisdiction,
            # The field the whole product turns on. A profile that offers a
            # frontier model and does not cap it is not a profile, it is a
            # default with extra steps.
            "max_class": "public",
            "tools": True,
            "quality": self.provider.quality,
            "cost_per_mtok": self.provider.cost_per_mtok,
            "probe": False,
        }
        endpoint = self.endpoint or self.provider.endpoint
        if endpoint:
            substrate["endpoint"] = endpoint
        substrate.update(self.extra)
        return substrate


def get_profile(profile_id: str) -> Profile:
    for profile in PROFILES:
        if profile.id == profile_id:
            return profile
    known = ", ".join(p.id for p in PROFILES)
    raise ValueError(f"unknown policy profile {profile_id!r} — known: {known}")


def build_policy_document(
    profile_id: str = "local-only",
    *,
    local_endpoint: str = "http://localhost:11434",
    local_model: str = "qwen2.5:14b",
    frontier: FrontierChoice | None = None,
    readable_paths: list[str] | None = None,
) -> dict[str, Any]:
    """The chosen profile, as a policy document.

    Every profile starts from :func:`default_policy_document` and *removes* or
    *adds* deliberately, so the strict parts — the deny list, the restricted
    classes, internal as the floor — are the same document in every profile and
    cannot be dropped by a profile that forgot to include them.
    """
    profile = get_profile(profile_id)
    doc = default_policy_document(local_endpoint=local_endpoint, local_model=local_model)

    if readable_paths is not None:
        # Same paths for every reading tool: the distinction between them is
        # what they do, not where they may look, and a chooser that offered
        # three separate path lists would be a schema editor with a wizard
        # around it.
        doc["tools"]["allow"] = {tool: list(readable_paths) for tool in doc["tools"]["allow"]}

    if profile.id == "read-nothing":
        # An empty allow-list is a refusal, not an omission: `tools` is
        # default-deny, and a tool with no paths does not run.
        doc["tools"]["allow"] = {tool: [] for tool in doc["tools"]["allow"]}

    if profile.id == "frontier-for-public" and frontier is not None:
        substrate = frontier.as_substrate()
        doc["substrates"].append(substrate)
        for rule in doc["rules"]:
            if rule.get("match", {}).get("class") == "public":
                # Quality, because the reason to reach a hosted model at all is
                # that it is better; the class ceiling is what keeps that from
                # being expensive in the way that matters.
                rule["allow"] = [substrate["id"], "local-gpu"]
                rule["prefer"] = "quality"

    return doc
