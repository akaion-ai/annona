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

from .cloud_client import AIBackendClient


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

    def execute_command(self, command: str, tools) -> Any:
        """
        Run a command through the model

        Args:
            command: The command to run
            tools: ToolRegistry disponibili

        Returns:
            The execution result
        """
        # Se il provider è Akaion, usa l'endpoint specifico per runner
        if self.provider == "akaion":
            if self.client is None:
                raise ConfigurationError(
                    "the Akaion provider needs credentials: run `annona login`, or set "
                    "ai.provider to a local runtime. (Nothing was sent anywhere.)"
                )
            return self._execute_command_akaion(command, tools)

        # Altrimenti usa il metodo generico con chat completion
        return self._execute_command_generic(command, tools)

    def _execute_command_akaion(self, command: str, tools) -> Any:
        """Run a command through the Akaion backend's runner endpoint."""
        try:
            import os
            import subprocess

            # Prepara il contesto per il runner
            working_dir = os.getcwd()
            available_tools = tools.list_tools() if tools else []

            # Chiama l'endpoint specifico per runner
            result = self.client.runner_execute(  # type: ignore
                command=command,
                working_directory=working_dir,
                available_tools=available_tools,
                permissions=self.config.get("permissions", {}),
                environment=dict(os.environ),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if result and result.get("success"):
                response_text = result.get("response", "")
                actions = result.get("actions", [])

                # Execute shell commands from actions
                command_results = []
                for action in actions:
                    if action.get("type") == "shell_command":
                        cmd = action.get("payload", {}).get("command")
                        if cmd:
                            try:
                                logger.info(f"Executing shell command: {cmd}")
                                proc_result = subprocess.run(
                                    cmd,
                                    shell=True,
                                    capture_output=True,
                                    text=True,
                                    timeout=30,
                                    cwd=working_dir,
                                )

                                command_output = proc_result.stdout.strip()
                                if proc_result.returncode == 0:
                                    logger.success(
                                        f"Command executed successfully: {command_output}"
                                    )
                                    command_results.append(
                                        {"command": cmd, "output": command_output, "success": True}
                                    )
                                else:
                                    error_output = proc_result.stderr.strip()
                                    logger.error(f"Command failed: {error_output}")
                                    command_results.append(
                                        {"command": cmd, "output": error_output, "success": False}
                                    )
                            except subprocess.TimeoutExpired:
                                logger.error(f"Command timed out: {cmd}")
                                command_results.append(
                                    {
                                        "command": cmd,
                                        "output": "Command timed out after 30 seconds",
                                        "success": False,
                                    }
                                )
                            except Exception as e:
                                logger.error(f"Error executing command: {e}")
                                command_results.append(
                                    {"command": cmd, "output": str(e), "success": False}
                                )

                # Build final response with command results
                final_response = response_text
                if command_results:
                    final_response += "\n\n**Execution Results:**\n"
                    for cmd_result in command_results:
                        if cmd_result["success"]:
                            final_response += f"\n✅ `{cmd_result['command']}`\n"
                            final_response += f"Output: {cmd_result['output']}\n"
                        else:
                            final_response += f"\n❌ `{cmd_result['command']}`\n"
                            final_response += f"Error: {cmd_result['output']}\n"

                return {
                    "response": final_response,
                    "actions": actions,
                    "command_results": command_results,
                    "reasoning": result.get("reasoning"),
                }
            else:
                error_msg = (
                    result.get("error", "Unknown error") if result else "No response from AI"
                )
                logger.error(f"AI command execution failed: {error_msg}")
                return {
                    "response": f"Error: {error_msg}",
                    "actions": [],
                }

        except Exception as e:
            logger.error(f"Error executing command via Akaion: {e}")
            return {
                "response": f"Execution error: {str(e)}",
                "actions": [],
            }

    def _execute_command_generic(self, command: str, tools) -> Any:
        """Run a command through a provider with no dedicated path."""
        # Costruisci il prompt per l'AI
        system_prompt = """You are a helpful AI assistant that can execute commands using available tools.
        
Available tools:
{tools}

When given a command, analyze it and use the appropriate tools to execute it.
Return the result in a structured format.""".format(
            tools="\n".join([f"- {t}" for t in tools.list_tools()]) if tools else "None"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Execute this command: {command}"},
        ]

        response = self.chat_completion(messages)

        # TODO: Parse response e chiamata tools per provider non-Akaion
        return {"response": response, "actions": []}

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
            # check. `execute_command` had it; the agentic loop, which replaced
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
        self, prompt: str, context: Dict[str, Any], tools, permissions, max_iterations: int = 10
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
        enforcement = self._build_enforcement()
        if enforcement is not None:
            return self._reason_enforced(enforcement, prompt, context, tools, max_iterations)

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
        return loop.run(prompt, context, max_iterations).to_dict()

    # ── The enforced path ─────────────────────────────────────────────────────

    def _build_enforcement(self):
        """Assemble the perimeter for this run, or ``None`` to stay legacy.

        Absence of a policy file is not treated as "enforce with defaults":
        turning a working installation into a default-deny one because a file
        appeared elsewhere would be a hostile upgrade. It is turned on by
        writing a policy (``annona init``) or by asking for it in the config.
        """
        from runner.services.enforcement import Enforcement, policy_path

        perimeter = self.config.get("perimeter", {}) if isinstance(self.config, dict) else {}
        configured = perimeter.get("enabled")
        path = Path(perimeter.get("policy") or policy_path())

        if configured is False:
            return None
        if not configured and not path.exists():
            return None

        try:
            return Enforcement.for_run(policy_file=path if path.exists() else None)
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
    ) -> Any:
        """Run with placement, default-deny clearance and a ledger."""
        # One routing backend, not one per call site: it carries the placement
        # of the last turn, and a fresh instance would have forgotten it.
        routing = enforcement.backend()

        loop = AgentLoop(
            routing,
            enforcement.executor(RegistryToolExecutor(tools)),
            enforcement.gate(),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        result = loop.run(prompt, context, max_iterations).to_dict()

        placement = routing.last_placement
        result["placement"] = {
            "class": enforcement.klass.label,
            "outcome": placement.outcome if placement else "none",
            "substrate": placement.substrate if placement else "",
            "reason": placement.reason if placement else "",
        }
        result["ledger"] = {"path": str(enforcement.ledger.path), "head": enforcement.ledger.head}
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
