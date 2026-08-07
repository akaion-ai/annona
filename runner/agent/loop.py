"""The agentic loop (layer L3).

One loop, every backend. It owns the transcript, the iteration budget and the
decision to continue; it owns nothing about providers, and cannot — importing a
vendor SDK from this package fails CI (``.importlinter``).

Before Phase 0 this logic existed three times: once for the Akaion control plane,
once for Anthropic, and a third degraded copy for everything else that dropped
tool use entirely. Three copies meant three places where content could leave the
perimeter, which is why no egress control could be total. See
``docs/adr/0002-unify-the-agentic-loop.md``.

The loop depends only on ports:

``backend``
    :class:`~runner.kernel.ports.InferenceBackend` — one turn of inference.
``executor``
    :class:`~runner.kernel.ports.ToolExecutor` — advertises and runs tools.
``gate``
    :class:`~runner.kernel.ports.PolicyGate` — decides whether a call may run.

Phase 1 inserts the perimeter by passing a different ``backend`` (one that
classifies and gates before delegating) and a different ``gate`` (default-deny).
Neither the loop nor the adapters change. That substitutability is the reason the
indirection exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger

from runner.agent.prompt import build_system_prompt
from runner.kernel.blocks import (
    function_call_block,
    function_result_block,
    media_block,
    text_block,
)
from runner.kernel.errors import BackendUnavailableError
from runner.kernel.ports import InferenceBackend, PolicyGate, ToolExecutor
from runner.kernel.types import (
    AgentResult,
    Attachment,
    Completion,
    CompletionRequest,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    Turn,
)

__all__ = ["DEFAULT_MAX_ITERATIONS", "AgentLoop", "run_agent"]

DEFAULT_MAX_ITERATIONS = 10
"""Iteration ceiling when the caller does not specify one.

A budget, not a guess at task difficulty. Phase 4 adds a no-progress detector so
a stuck run stops early instead of spending the whole budget repeating itself.
"""


def _denial(call: ToolCall) -> ToolResult:
    """The result a denied call produces.

    The message text is part of the contract: it is what the model sees, and what
    the existing tests assert on.
    """
    return ToolResult(
        call_id=call.id,
        name=call.name,
        content={"error": f"Permission denied for tool: {call.name}"},
        is_error=True,
    )


class AgentLoop:
    """Drives a task to completion through repeated inference and tool use."""

    def __init__(
        self,
        backend: InferenceBackend,
        executor: ToolExecutor,
        gate: PolicyGate,
        *,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> None:
        self._backend = backend
        self._executor = executor
        self._gate = gate
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._model = model

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        context: Mapping[str, Any] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        attachments: Sequence[Attachment] = (),
        prefetch: Sequence[ToolCall] = (),
    ) -> AgentResult:
        """Run a task to completion, or to the iteration ceiling.

        Args:
            prompt: The task, in natural language.
            context: Run context injected into the system prompt.
            max_iterations: Maximum inference turns. A run that reaches this
                ceiling returns what it has; it does not raise.
            attachments: Files put in front of the run by the operator, carried
                as references in the first turn. They are part of the payload
                from the first placement onward, which is the point: attaching a
                client's scan must decide where the *first* turn runs, not be
                discovered halfway through by a tool call.
            prefetch: Tool calls to run *before* the first inference, through the
                same gate and into the same ledger as any other call. This is how
                an attachment becomes content the model can rely on: asking a 7B
                model to remember to call a reader works most of the time, and a
                feature that works most of the time is one people stop using. It
                also means the class of what was read is known before the first
                placement rather than after it.

        Returns:
            The final text, how many turns it took, and every tool call made.

        Raises:
            Exception: Provider errors propagate. A backend that reports
                :class:`~runner.kernel.errors.BackendUnavailableError` is handled
                here — the run stops and returns partial results — but anything
                else is a real failure and is not swallowed.
        """
        system = build_system_prompt(context)
        specs = self._executor.specs()
        specs_by_name = {spec.name: spec for spec in specs}

        opening: list[Any] = [text_block(prompt)]
        opening += [media_block(a.path, a.media_type) for a in attachments]
        transcript: list[Turn] = [Turn(role="user", blocks=tuple(opening))]
        invocations: list[ToolInvocation] = []
        response = ""
        iterations = 0

        if prefetch:
            # Rendered as a tool round the model did not ask for, because that is
            # what it is: the calls, then their results, in the shape every
            # provider already understands. A refusal lands here too — a denied
            # read is content the model must see, not a gap it will fill in.
            results = self._execute(prefetch, invocations)
            transcript.extend(
                self._continuation(
                    Completion(tool_calls=tuple(prefetch), stop_reason="tool_use"),
                    results,
                    specs_by_name,
                )
            )

        while iterations < max_iterations:
            iterations += 1
            logger.info(f"Agent turn {iterations}/{max_iterations} via {self._backend.name}")

            try:
                completion = self._backend.complete(
                    CompletionRequest(
                        system=system,
                        transcript=tuple(transcript),
                        tools=tuple(specs),
                        temperature=self._temperature,
                        max_tokens=self._max_tokens,
                        model=self._model,
                    )
                )
            except BackendUnavailableError as exc:
                # Expected on a laptop that lost its connection. Return what we
                # have rather than losing the work done so far.
                logger.error(f"Backend {self._backend.name} unavailable: {exc}")
                break

            if completion.text_parts:
                response = completion.text

            if not completion.wants_tools:
                break

            results = self._execute(completion.tool_calls, invocations)
            transcript.extend(self._continuation(completion, results, specs_by_name))

        return AgentResult(
            response=response,
            iterations=iterations,
            tool_calls=tuple(invocations),
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _execute(
        self,
        calls: Sequence[ToolCall],
        invocations: list[ToolInvocation],
    ) -> list[ToolResult]:
        """Run every call in a turn, recording each outcome.

        Calls within a turn are executed in the order the model asked for them.
        Sequential rather than concurrent on purpose: tools touch the filesystem
        and the shell, and concurrent side effects would make a run
        non-reproducible — which Phase 3's replay depends on.
        """
        results: list[ToolResult] = []

        for call in calls:
            result = self._executor.invoke(call) if self._gate.permits(call) else _denial(call)

            invocations.append(
                ToolInvocation(
                    tool=call.name,
                    input=dict(call.arguments),
                    result=result.content,
                    error=result.is_error,
                )
            )
            results.append(result)

        return results

    @staticmethod
    def _continuation(
        completion: Completion,
        results: Sequence[ToolResult],
        specs_by_name: Mapping[str, ToolSpec],
    ) -> list[Turn]:
        """Build the two turns that carry a tool round back to the model.

        The assistant turn restates what the model said and asked for; the user
        turn carries the results. Both are rebuilt from canonical blocks rather
        than by replaying provider objects, so the transcript stays serialisable —
        the precondition for hashing it into a trace and for replaying a run.
        """
        assistant_blocks: list[Any] = [text_block(t) for t in completion.text_parts]
        assistant_blocks += [
            function_call_block(call, specs_by_name.get(call.name))
            for call in completion.tool_calls
        ]

        result_blocks = [
            function_result_block(result, specs_by_name.get(result.name)) for result in results
        ]

        return [
            Turn(role="assistant", blocks=tuple(assistant_blocks)),
            Turn(role="user", blocks=tuple(result_blocks)),
        ]


def run_agent(
    prompt: str,
    *,
    backend: InferenceBackend,
    executor: ToolExecutor,
    gate: PolicyGate,
    context: Mapping[str, Any] | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    model: str | None = None,
    attachments: Sequence[Attachment] = (),
    prefetch: Sequence[ToolCall] = (),
) -> AgentResult:
    """Convenience wrapper for a one-shot run.

    Equivalent to constructing an :class:`AgentLoop` and calling
    :meth:`AgentLoop.run`; keyword-only to keep call sites readable.
    """
    return AgentLoop(
        backend,
        executor,
        gate,
        temperature=temperature,
        max_tokens=max_tokens,
        model=model,
    ).run(prompt, context, max_iterations, attachments, prefetch)
