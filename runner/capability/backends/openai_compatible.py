"""OpenAI-compatible inference backend (layer L1).

One adapter for every serving runtime that speaks ``/v1/chat/completions``:
**vLLM**, llama.cpp's server, LM Studio, SGLang, TensorRT-LLM's OpenAI frontend,
and Ollama's compatibility endpoint. On a DGX this is the one that matters —
vLLM is what serves the appliance — and it is deliberately the same code path a
laptop uses against llama.cpp, because a sovereignty claim that only holds on
one runtime is a demo.

Two differences from :mod:`runner.capability.backends.ollama`, which speaks
Ollama's native dialect:

- tool call arguments arrive as a **JSON string** inside
  ``function.arguments``, not as an object. A model that emits malformed JSON
  there is the single most common failure of small models, so it is decoded
  defensively and reported as an empty argument set rather than crashing a run.
- ``finish_reason`` is authoritative: ``tool_calls`` means continue. Servers
  disagree about whether they also set it when tools are returned, so the
  presence of calls wins over the field.

The backend never sends an API key it was not given. Self-hosted servers usually
have none, and inventing a placeholder would make a misconfigured gateway look
authenticated.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from loguru import logger

from runner.capability.backends.media import encode_media
from runner.kernel.blocks import ToolResultBlock
from runner.kernel.errors import BackendUnavailableError
from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    ToolCall,
    ToolSpec,
    Transcript,
)

__all__ = ["DEFAULT_TIMEOUT", "OpenAICompatibleBackend"]

DEFAULT_TIMEOUT = 180.0
"""Seconds to wait for one completion.

Generous because the first request to a cold vLLM server pays for weight
loading, and because a 32B model on a bandwidth-bound GB10 is not fast. Still
finite: a wedged server must not hang a run forever.
"""


class OpenAICompatibleBackend:
    """Inference through any OpenAI-compatible ``/v1`` endpoint."""

    def __init__(
        self,
        model: str,
        endpoint: str,
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        client: Any = None,
        context_window: int = 32_768,
        is_local: bool = True,
        name: str = "",
    ) -> None:
        """
        Args:
            model: Model name as the server knows it, e.g. ``Qwen/Qwen2.5-14B-Instruct``.
            endpoint: Base URL including the version prefix, e.g.
                ``http://vllm:8000/v1``.
            api_key: Sent as a bearer token when non-empty. Self-hosted servers
                usually need none.
            timeout: Seconds to wait for one completion.
            client: An injected HTTP client, for tests.
            context_window: Declared context size, used by placement to reject a
                substrate that cannot hold the request.
            is_local: Whether reaching this endpoint counts as staying inside the
                perimeter. Declared by the operator through the policy, because
                only they know whether ``https://gpu.internal`` is their rack or
                someone else's.
        """
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client
        self._context_window = context_window
        self._is_local = is_local
        self._name = name or f"openai-compatible:{model}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            native_tools=True,
            # vLLM and llama.cpp both support guided decoding, but through
            # server-specific fields this adapter does not send yet. Declared as
            # json_schema rather than guided: the perimeter must not be told a
            # guarantee exists before the code requests it.
            grammar="json_schema",
            parallel_tool_calls=True,
            context_window=self._context_window,
            is_local=self._is_local,
        )

    def complete(self, request: CompletionRequest) -> Completion:
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "messages": _encode_transcript(request.system, request.transcript),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }

        tools = _encode_tools(request.tools)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        try:
            response = self._http().post(
                f"{self._endpoint}/chat/completions",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"OpenAI-compatible endpoint {self._endpoint} is unreachable: {exc}"
            ) from exc

        if response.status_code == 404:
            raise BackendUnavailableError(
                f"{self._endpoint} has no model {payload['model']!r} "
                "(or does not serve /chat/completions)"
            )
        if response.status_code >= 400:
            raise BackendUnavailableError(
                f"{self._endpoint} returned {response.status_code}: {response.text[:200]}"
            )

        try:
            return _decode(response.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise BackendUnavailableError(
                f"{self._endpoint} returned a response this adapter cannot read: {exc}"
            ) from exc

    def _http(self) -> Any:
        return self._client or httpx.Client(timeout=self._timeout)


# ── Wire format ───────────────────────────────────────────────────────────────


def _encode_tools(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": dict(spec.schema),
            },
        }
        for spec in specs
    ]


def _encode_transcript(system: str, transcript: Transcript) -> list[dict[str, Any]]:
    """Encode a transcript as an OpenAI message array.

    Every assistant tool call must be answered by a ``role: "tool"`` message
    carrying the same ``tool_call_id``, or the server rejects the conversation.
    Ids are therefore taken from the blocks rather than regenerated.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})

    for turn in transcript:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []

        for block in turn.blocks:
            kind = getattr(block, "type", "")

            if kind == "media":
                _encode_media_part(block, parts, text_parts)
            elif kind == "text":
                text_parts.append(block.content)
            elif kind == "function":
                call_id = getattr(block, "id", "") or f"call_{len(tool_calls)}"
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(dict(block.arguments), ensure_ascii=False),
                        },
                    }
                )
            elif kind == "function_call_result":
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": getattr(block, "id", "") or "call_0",
                        "content": _describe_result(block),
                    }
                )

        if text_parts or tool_calls or parts:
            message: dict[str, Any] = {
                "role": turn.role,
                "content": " ".join(text_parts),
            }
            if parts:
                # The multimodal shape: content becomes a list of parts rather
                # than a string. Only used when something visual is attached,
                # because servers that accept only strings are still common.
                message["content"] = [{"type": "text", "text": " ".join(text_parts)}, *parts]
            if tool_calls:
                message["role"] = "assistant"
                message["tool_calls"] = tool_calls
            messages.append(message)

        messages.extend(results)

    return messages


def _encode_media_part(block: Any, parts: list[dict[str, Any]], text_parts: list[str]) -> None:
    """Attach an image as a data URI, or name the file the server cannot take."""
    media = getattr(block, "media", None)
    path = str(getattr(media, "source", "")) if media is not None else ""
    kind = getattr(media, "media_type", "") if media is not None else ""

    encoded = encode_media(path) if path and kind == "image" else None
    if encoded is None:
        text_parts.append(
            f"[attached {kind or 'file'}: {path} — read it with the document_reader tool]"
        )
        return

    data, mime = encoded
    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})


def _describe_result(block: Any) -> str:
    """Render a tool result, making failure legible in the text itself."""
    body = getattr(block, "result", "")
    if isinstance(block, ToolResultBlock) and block.is_error:
        return f"ERROR: {body}"
    return str(body)


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """Decode ``function.arguments``, which is a JSON *string* in this dialect.

    Malformed JSON is the characteristic failure of a small model asked for a
    tool call, and it must not end the run: an empty argument set produces a
    tool error the model can read and retry from, which is the loop working as
    designed. This is exactly the gap grammar-constrained decoding closes.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"openai-compatible: malformed tool arguments, sending none: {raw[:120]}")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode(payload: dict[str, Any]) -> Completion:
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("response contains no choices")

    message = choices[0].get("message") or {}
    text = message.get("content") or ""

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=_decode_arguments(function.get("arguments")),
            )
        )

    if calls:
        logger.debug(f"openai-compatible: {len(calls)} tool call(s): {[c.name for c in calls]}")

    return Completion(
        text_parts=(text,) if text else (),
        tool_calls=tuple(calls),
        stop_reason="tool_use" if calls else "end_turn",
    )
