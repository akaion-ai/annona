"""Anthropic inference backend (layer L1).

Translates a :class:`~runner.kernel.types.CompletionRequest` into an Anthropic
Messages API call and the response back into a
:class:`~runner.kernel.types.Completion`. It holds no conversation state and
makes no decision about whether to continue — that is the loop's job — nor about
whether it may be called at all, which from Phase 1 is the perimeter's.

The SDK client is **injected, not constructed**. Construction stays in the
composition root (``runner.ai_client``) because that is where credentials, the
environment and the existing test seams live. An adapter that reaches out and
builds its own client is an adapter you cannot substitute in a test.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from runner.capability.backends.wire import decode_completion, encode_tools, encode_transcript
from runner.kernel.errors import BackendUnavailableError
from runner.kernel.types import Capabilities, Completion, CompletionRequest

__all__ = ["AnthropicBackend"]


class AnthropicBackend:
    """Inference through the Anthropic Messages API with native tool use."""

    def __init__(self, client: Any, model: str) -> None:
        """
        Args:
            client: A constructed ``anthropic.Anthropic`` instance (or any object
                exposing ``messages.create``).
            model: Default model identifier, overridable per request.
        """
        self._client = client
        self._model = model

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            native_tools=True,
            grammar="none",
            parallel_tool_calls=True,
            context_window=200_000,
            # Remote: calling this backend is egress. Phase 1's perimeter reads
            # exactly this field to decide whether it may.
            is_local=False,
        )

    def complete(self, request: CompletionRequest) -> Completion:
        kwargs: dict[str, Any] = {
            "model": request.model or self._model,
            "system": request.system,
            "messages": encode_transcript(request.transcript),
            "max_tokens": request.max_tokens,
        }
        # `temperature` is deliberately absent. Current Anthropic models —
        # Opus 5, Opus 4.8/4.7, Sonnet 5 — reject sampling parameters with a
        # 400, so sending one made *every* request to a correctly configured
        # frontier substrate fail. The request carries a temperature because
        # other providers still take one; this adapter is the place that knows
        # its provider does not.

        tools = encode_tools(request.tools)
        if tools:
            kwargs["tools"] = tools

        # SDK exceptions are deliberately *not* caught here. Phase 0 preserves
        # the existing behaviour, where a provider error propagates to the
        # caller; swallowing it would turn a credentials or rate-limit failure
        # into a silently empty answer. Retry and fail-closed policy arrive with
        # the perimeter in Phase 1 — see docs/adr/0002.
        response = self._client.messages.create(**kwargs)

        if response is None:
            logger.error("Anthropic returned no response object")
            raise BackendUnavailableError("Anthropic returned no response")

        return decode_completion(
            getattr(response, "content", None) or [],
            getattr(response, "stop_reason", None),
        )
