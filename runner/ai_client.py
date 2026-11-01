"""AI client facade — the composition root for agentic runs.

This module is where provider clients are *constructed*: credentials, the
environment and the `_init_*` seams live here. It then hands the constructed
client to an L1 adapter and delegates the run to the single loop in
:mod:`runner.agent.loop`.

Before Phase 0 this file also *contained* the loop — three times over, one copy
per provider, of which only two supported tool use. Those copies are gone; the
behaviour they implemented is preserved in one place. See
``docs/adr/0002-unify-the-agentic-loop.md``.

Layer note: this is the outermost layer, so it is the one place allowed to know
about every other. It wires ports to adapters and does nothing else — no control
flow, no provider protocol details.

Supported providers:

===============  ===============================  ===============
``provider``     Path                             Tool use
===============  ===============================  ===============
``akaion``       control-plane proxy (L1)         yes
``anthropic``    Messages API (L1)                yes
``echo``         scripted, offline (L1)           yes
``openai``       chat completion fallback         no (Phase 2)
``google``       chat completion fallback         no (Phase 2)
``local``        chat completion fallback         no (Phase 2)
===============  ===============================  ===============

``openai`` and ``google`` still reach a model without being able to call tools.
``local`` no longer does: it runs a real model on the machine, with tools.
"""

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from runner.agent.loop import AgentLoop
from runner.capability.backends import (
    AkaionBackend,
    AnthropicBackend,
    EchoBackend,
    OllamaBackend,
)
from runner.capability.backends.echo import script_from_config
from runner.capability.tooling import PermissionGate, RegistryToolExecutor
from runner.kernel.errors import ConfigurationError
from runner.kernel.ports import InferenceBackend
from runner.kernel.types import Attachment, Subject, ToolCall

from .cloud_client import AIBackendClient

#: What an operator is told when a run is about to go down the legacy path. One
#: string, so the daemon's banner and the per-process log line cannot drift.
LEGACY_PERIMETER_WARNING = (
    "This installation is unenforced: there is no policy, so tools are gated by the "
    "allow-by-default permission manager and the configured provider serves every "
    "turn. Run `annona policy init` to put it behind the perimeter."
)

_legacy_warned = False


def perimeter_mode(config: Any) -> tuple[bool, Path]:
    """Whether runs under ``config`` are enforced, and the policy file they read.

    The single answer to "which of the two wirings is this installation on",
    shared by :meth:`AIClient._build_enforcement` and anything that has to tell
    an operator — so the answer a person reads is the one a run acts on.
    """
    from runner.services.enforcement import policy_path

    perimeter = (config.get("perimeter") or {}) if isinstance(config, dict) else {}
    configured = perimeter.get("enabled")
    path = Path(perimeter.get("policy") or policy_path())

    if configured is False:
        return False, path
    return bool(configured) or path.exists(), path


class AIClient:
    """Client AI che supporta multiple providers"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.ai_config = config.get("ai", {})

        self.provider = self.ai_config.get("provider", "akaion")
        self.model = self.ai_config.get("model", "gpt-4")
        self.temperature = self.ai_config.get("temperature", 0.7)
        self.max_tokens = self.ai_config.get("max_tokens", 4000)

        # Client typed as Any to avoid complex union types
        self.client: Any = None

        # Set up the provider
        self._init_provider()

    def _init_provider(self):
        """Construct the provider client."""
        if self.provider == "akaion":
            self._init_akaion()
        elif self.provider == "openai":
            self._init_openai()
        elif self.provider == "anthropic":
            self._init_anthropic()
        elif self.provider == "google":
            self._init_google()
        elif self.provider == "local":
            self._init_local()
        elif self.provider == "echo":
            self._init_echo()
        else:
            raise ValueError(f"Unknown AI provider: {self.provider}")

    def _init_echo(self):
        """Initialise the offline scripted provider.

        There is nothing to construct: no client, no credentials, no network. See
        :mod:`runner.capability.backends.echo` for why this ships in the product
        rather than only in the tests.
        """
        self.client = None
        logger.info("Using offline echo backend (no network, no credentials)")

    def _init_akaion(self):
        """Akaion control plane. Local-first: with no credentials it stays inactive
        (client=None) ma la classe è costruibile — il check duro avviene solo
        only when a completion is actually attempted."""
        from .auth import AuthManager

        auth = AuthManager()

        api_key = auth.get_api_key()
        if not api_key:
            logger.info("Akaion AI client disabled — no API key (local-only mode)")
            self.client = None
            return

        self.client = AIBackendClient(api_key=api_key, runner_id=auth.get_runner_id())
        logger.info("Using Akaion AI Backend")

    def _init_openai(self):
        """Construct the OpenAI client."""
        from openai import OpenAI

        api_key = self.ai_config.get("openai", {}).get("api_key") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OpenAI API key not configured")

        self.client = OpenAI(api_key=api_key)
        logger.info("Using OpenAI")

    def _init_anthropic(self):
        """Construct the Anthropic client."""
        from anthropic import Anthropic

        api_key = self.ai_config.get("anthropic", {}).get("api_key") or os.getenv(
            "ANTHROPIC_API_KEY"
        )
        if not api_key:
            raise ValueError("Anthropic API key not configured")

        self.client = Anthropic(api_key=api_key)
        logger.info("Using Anthropic Claude")

    def _init_google(self):
        """Construct the Google client."""
        try:
            import google.generativeai as genai

            api_key = self.ai_config.get("google", {}).get("api_key") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("Google API key not configured")

            genai.configure(api_key=api_key)  # type: ignore
            self.client = genai
            logger.info("Using Google Gemini")
        except ImportError:
            raise ValueError("google-generativeai package not installed")

    def _init_local(self):
        """Construct the local Ollama client."""
        import httpx

        endpoint = self.ai_config.get("local", {}).get("endpoint", "http://localhost:11434")
        self.client = httpx.Client(base_url=endpoint)
        logger.info(f"Using Local LLM at {endpoint}")

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Run a chat completion

        Args:
            messages: Lista di messaggi [{role: "user/assistant/system", content: "..."}]
            temperature: Override temperature
            max_tokens: Override max_tokens

        Returns:
            The model's reply
        """
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens

        if self.provider == "akaion":
            return self._chat_akaion(messages, temp, max_tok)
        elif self.provider == "openai":
            return self._chat_openai(messages, temp, max_tok)
        elif self.provider == "anthropic":
            return self._chat_anthropic(messages, temp, max_tok)
        elif self.provider == "google":
            return self._chat_google(messages, temp, max_tok)
        elif self.provider == "local":
            return self._chat_local(messages, temp, max_tok)
        else:
            return ""

    def _chat_akaion(self, messages: List[Dict[str, str]], temp: float, max_tok: int) -> str:
        """Chat con Akaion AI"""
        result = self.client.chat_completion(  # type: ignore
            messages=messages, model=self.model, temperature=temp
        )
        return result.get("message", {}).get("content", "") if result else ""

    def _chat_openai(self, messages: List[Dict[str, str]], temp: float, max_tok: int) -> str:
        """Chat con OpenAI"""
        response = self.client.chat.completions.create(  # type: ignore
            model=self.model,
            messages=messages,  # type: ignore
            temperature=temp,
            max_tokens=max_tok,
        )
        return response.choices[0].message.content or ""

    def _chat_anthropic(self, messages: List[Dict[str, str]], temp: float, max_tok: int) -> str:
        """Chat con Anthropic"""
        # Anthropic takes the system message separately
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
        user_messages = [m for m in messages if m["role"] != "system"]

        response = self.client.messages.create(  # type: ignore
            model=self.model,
            system=system_msg or "",
            messages=user_messages,  # type: ignore
            temperature=temp,
            max_tokens=max_tok,
        )
        # Handle different content block types
        if response.content and len(response.content) > 0:
            block = response.content[0]
            if hasattr(block, "text"):
                return block.text  # type: ignore
        return ""

    def _chat_google(self, messages: List[Dict[str, str]], temp: float, max_tok: int) -> str:
        """Chat con Google"""
        model = self.client.GenerativeModel(self.model)  # type: ignore

        # Converti messages in formato Google
        chat = model.start_chat(history=[])  # type: ignore
        for msg in messages[:-1]:  # everything but the last
            chat.send_message(msg["content"])  # type: ignore

        # Invia ultimo messaggio
        response = chat.send_message(messages[-1]["content"])  # type: ignore
        return response.text or ""  # type: ignore

    def _chat_local(self, messages: List[Dict[str, str]], temp: float, max_tok: int) -> str:
        """Chat through a local Ollama endpoint."""
        response = self.client.post(  # type: ignore
            "/api/chat",
            json={"model": self.model, "messages": messages, "temperature": temp, "stream": False},
        )
        return response.json().get("message", {}).get("content", "")

    def build_backend(self) -> Optional[InferenceBackend]:
        """Wrap the constructed provider client in an L1 inference adapter.

        Returns ``None`` for providers that have no adapter yet, which routes the
        run to the tool-less chat fallback — the behaviour those providers have
        always had.

        Built per run rather than cached at construction: the runner id is
        assigned during cloud registration, which can complete after this object
        exists.
        """
        if self.provider == "akaion":
            # `_init_akaion` leaves the client None when there are no
            # credentials, which is correct — the class must stay constructible
            # on a machine that has never signed in. What was missing is this
            # check. The old `execute_command` had it; the agentic loop, which replaced
            # that path, did not, so the documented
            #
            #     annona run --once --task "..."
            #
            # died on a fresh install with
            #
            #     'NoneType' object has no attribute 'runner_agent_turn'
            #
            # An AttributeError is not an answer to "you are not signed in".
            if self.client is None:
                raise ConfigurationError(
                    "the Akaion provider needs credentials: run `annona login`, or set "
                    "ai.provider to a local runtime (`local` for Ollama, `echo` for the "
                    "offline scripted backend). Nothing was sent anywhere."
                )
            return AkaionBackend(client=self.client)

        if self.provider == "anthropic":
            return AnthropicBackend(client=self.client, model=self.model)

        if self.provider == "local":
            local = self.ai_config.get("local", {})
            return OllamaBackend(
                model=self.model,
                endpoint=local.get("endpoint", "http://localhost:11434"),
            )

        if self.provider == "echo":
            return EchoBackend(
                script=script_from_config(self.ai_config.get("echo", {}).get("script"))
            )

        return None

    def reason_and_execute(
        self,
        prompt: str,
        context: Dict[str, Any],
        tools,
        permissions,
        max_iterations: int = 10,
        attachments: Sequence[Attachment] = (),
        prefetch: Sequence[ToolCall] = (),
        prefer_quality: bool = False,
        cancel: Callable[[], bool] | None = None,
        subject: Subject | None = None,
    ) -> Any:
        """Run an agentic task: reason, call tools, repeat until done.

        Delegates to the single loop in :mod:`runner.agent.loop`. This method
        only wires ports to adapters — the loop decides what happens next, and
        the L1 adapter knows how to talk to the provider.

        Two wirings are possible, and which one is used is a deployment
        decision rather than a code path the caller picks:

        **Enforced.** A policy exists (``~/.annona/policy.yaml``, or
        ``perimeter.enabled: true`` in the config). Placement, default-deny
        clearance and the ledger are in the loop, and the ``ai.provider``
        setting is ignored — the policy decides where each turn runs.

        **Legacy.** No policy. The configured provider serves every turn and
        the allow-by-default permission manager gates tools, which is what an
        existing installation has always done.

        Returns:
            ``{"response": str, "iterations": int, "tool_calls": [...]}`` — the
            same shape this method has always returned, plus ``placement`` and
            ``ledger`` keys when the perimeter is enforcing.
        """
        enforcement = self._build_enforcement(subject)
        if enforcement is not None:
            return self._reason_enforced(
                enforcement,
                prompt,
                context,
                tools,
                max_iterations,
                attachments,
                prefetch,
                prefer_quality,
                cancel,
            )

        backend = self.build_backend()

        if backend is None:
            # openai / google / local: reach a model, cannot call tools.
            return self._agentic_loop_generic(prompt, context, tools)

        loop = AgentLoop(
            backend,
            RegistryToolExecutor(tools),
            PermissionGate(permissions),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        return loop.run(
            prompt, context, max_iterations, attachments, prefetch, cancel=cancel
        ).to_dict()

    # ── The enforced path ─────────────────────────────────────────────────────

    def _build_enforcement(self, subject: Subject | None = None):
        """Assemble the perimeter for this run, or ``None`` to stay legacy.

        Absence of a policy file is not treated as "enforce with defaults":
        turning a working installation into a default-deny one because a file
        appeared elsewhere would be a hostile upgrade. It is turned on by
        writing a policy (``annona init``) or by asking for it in the config.

        That argument holds for an installation that already runs, not for one
        that never has: a fresh home is written with a policy alongside its
        config (``cli_setup.ensure_fresh_home``), so the legacy path is only
        reached by installations that predate the policy engine — and each
        process that takes it says so once, rather than silently.
        """
        from runner.services.enforcement import Enforcement

        enforced, path = perimeter_mode(self.config)
        if not enforced:
            global _legacy_warned
            configured_off = (
                isinstance(self.config, dict)
                and (self.config.get("perimeter") or {}).get("enabled") is False
            )
            if not _legacy_warned and not configured_off:
                logger.warning(LEGACY_PERIMETER_WARNING)
                _legacy_warned = True
            return None

        try:
            return Enforcement.for_run(policy_file=path if path.exists() else None, subject=subject)
        except Exception as exc:  # noqa: BLE001 - fail closed, loudly
            # A perimeter that cannot be built must stop the run, not fall back
            # to the unenforced path: the operator asked for enforcement, and
            # quietly not enforcing is the one outcome they cannot detect.
            raise ConfigurationError(f"the perimeter could not be assembled: {exc}") from exc

    def _reason_enforced(
        self,
        enforcement,
        prompt: str,
        context: Dict[str, Any],
        tools,
        max_iterations: int,
        attachments: Sequence[Attachment] = (),
        prefetch: Sequence[ToolCall] = (),
        prefer_quality: bool = False,
        cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """Run with placement, default-deny clearance and a ledger."""
        # One routing backend, not one per call site: it carries the placement
        # of the last turn, and a fresh instance would have forgotten it.
        routing = enforcement.backend(prefer_quality=prefer_quality)

        loop = AgentLoop(
            routing,
            enforcement.executor(RegistryToolExecutor(tools)),
            enforcement.gate(),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        result = loop.run(
            prompt, context, max_iterations, attachments, prefetch, cancel=cancel
        ).to_dict()

        if result.get("cancelled"):
            # A refusal is a first-class ledger outcome; so is a stop. Recording
            # it here — where the ledger lives — keeps `annona why`/`audit` able
            # to reconstruct that the run ended because a person stopped it,
            # after the turns it did complete, rather than leaving a silent gap.
            enforcement.ledger.record(
                "run",
                outcome="interrupted",
                klass=enforcement.klass,
                detail={
                    "reason": "cancelled by operator",
                    "iterations": result.get("iterations", 0),
                },
            )

        placement = routing.last_placement
        result["placement"] = {
            "class": enforcement.klass.label,
            "outcome": placement.outcome if placement else "none",
            "substrate": placement.substrate if placement else "",
            "reason": placement.reason if placement else "",
        }
        result["ledger"] = {"path": str(enforcement.ledger.path), "head": enforcement.ledger.head}
        # What actually left this machine, verbatim. The window shows it; it is
        # never written to disk. See `RoutingBackend.egress`.
        result["egress"] = [dict(item) for item in routing.egress]
        result["sealed"] = enforcement.working_set.sealed
        return result

    def _agentic_loop_generic(self, prompt: str, context: Dict[str, Any], tools) -> Any:
        """Fallback for providers with no tool-use adapter yet."""
        system_prompt = (
            f"You are an advanced AI agent.\n"
            f"Available tools: {', '.join(tools.list_tools()) if tools else 'none'}\n"
            f"Context: {context}\n\n"
            "Think step by step and describe what you would do to accomplish the task."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        response = self.chat_completion(messages)
        return {"response": response, "tool_calls": []}
