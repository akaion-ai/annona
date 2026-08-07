"""Ollama inference backend — local models with native tool calling (layer L1).

The first backend where `Capabilities.is_local` is `True` *and* a real model is
doing the work. Everything stays on the machine: no credentials, no egress, no
account.

Ollama's `/api/chat` speaks its own dialect rather than Anthropic's, so this
adapter carries its own encoder instead of sharing `wire.py`:

- the system prompt is a message with `role: "system"`, not a separate field;
- tools are OpenAI-shaped — `{"type": "function", "function": {...}}`;
- tool results come back as `role: "tool"` messages, one per call;
- there is no `stop_reason`: the presence of `tool_calls` is the signal.

**Tier 1 of three.** This uses the model's native tool calling, which works well
on models trained for it and produces confidently malformed arguments on models
that are not. Observed on an M1 Pro with `qwen2.5:3b`: the right tool, the right
path, and a missing required field. Tier 2 — compiling each tool's JSON Schema
into a grammar so a malformed call is structurally impossible — is what makes
small models dependable, and is the next piece of work.

Until then, a missing required argument surfaces as a tool error the model can
read and retry from, which is the loop working as designed rather than a crash.
"""

from __future__ import annotations

import json
import re
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

__all__ = ["DEFAULT_ENDPOINT", "OllamaBackend"]

DEFAULT_ENDPOINT = "http://localhost:11434"

# Local models are slower than an API and the first call pays for loading the
# weights. A minute is patient enough for a 14B on a laptop and short enough that
# a wedged server does not hang a run forever.
DEFAULT_TIMEOUT = 120.0


class OllamaBackend:
    """Inference through a local Ollama server."""

    def __init__(
        self,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        client: Any = None,
        context_window: int = 32_768,
    ) -> None:
        """
        Args:
            model: An Ollama model tag, e.g. ``qwen2.5:3b``.
            endpoint: Where the Ollama server listens.
            timeout: Seconds to wait for one completion.
            client: An injected HTTP client, for tests.
            context_window: Declared context size; informational in Phase 2.
        """
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._context_window = context_window

    @property
    def name(self) -> str:
        return f"ollama:{self._model}"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            native_tools=True,
            # Ollama can shape output with `format`, but cannot yet constrain it
            # with a compiled grammar. Declared honestly: over-claiming here would
            # make the perimeter trust a guarantee that does not exist.
            grammar="json_schema",
            parallel_tool_calls=True,
            context_window=self._context_window,
            # The whole point.
            is_local=True,
        )

    def complete(self, request: CompletionRequest) -> Completion:
        payload = {
            "model": request.model or self._model,
            "stream": False,
            "messages": _encode_transcript(request.system, request.transcript),
            "options": {"temperature": request.temperature},
        }

        tools = _encode_tools(request.tools)
        if tools:
            payload["tools"] = tools

        try:
            response = self._http().post(f"{self._endpoint}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            # A local server that is not running is the common case, not an
            # exceptional one: someone forgot `ollama serve`. Report it as
            # unavailable so the loop stops cleanly with a readable message.
            raise BackendUnavailableError(
                f"Ollama at {self._endpoint} is unreachable: {exc}"
            ) from exc

        if response.status_code == 404:
            raise BackendUnavailableError(
                f"Ollama has no model {payload['model']!r}. Pull it with: "
                f"ollama pull {payload['model']}"
            )
        if response.status_code >= 400:
            raise BackendUnavailableError(
                f"Ollama returned {response.status_code}: {response.text[:200]}"
            )

        return _decode(response.json())

    def _http(self) -> Any:
        return self._client or httpx.Client(timeout=self._timeout)


# ── Wire format ───────────────────────────────────────────────────────────────


def _encode_tools(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Encode tool specs in the OpenAI function shape Ollama expects."""
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
    """Encode a transcript as an Ollama message array.

    Tool results become individual ``role: "tool"`` messages rather than blocks
    inside a user turn, which is what Ollama's chat format expects.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})

    for turn in transcript:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        images: list[str] = []

        for block in turn.blocks:
            kind = getattr(block, "type", "")

            if kind == "media":
                encoded = _encode_media(block, text_parts)
                if encoded:
                    images.append(encoded)
            elif kind == "text":
                text_parts.append(block.content)
            elif kind == "function":
                tool_calls.append(
                    {
                        "function": {
                            "name": block.name,
                            "arguments": dict(block.arguments),
                        }
                    }
                )
            elif kind == "function_call_result":
                results.append(
                    {
                        "role": "tool",
                        "content": _describe_result(block),
                    }
                )

        if text_parts or tool_calls or images:
            message: dict[str, Any] = {"role": turn.role, "content": " ".join(text_parts)}
            if tool_calls:
                message["tool_calls"] = tool_calls
            if images:
                # Ollama's own shape: raw base64 on the message, no MIME type and
                # no content blocks. Only a multimodal model will do anything
                # with them, which is what `vision: true` in the policy asserts
                # about the substrate.
                message["images"] = images
            messages.append(message)

        messages.extend(results)

    return messages


def _encode_media(block: Any, text_parts: list[str]) -> str | None:
    """Attach an image, or say in words what could not be attached.

    Ollama takes images and nothing else. A PDF or a video reaching this point
    means placement chose a substrate for a turn carrying material it cannot
    take, and the honest recovery is to tell the model the file exists and where
    it is — it has a reader tool — rather than to drop it silently.
    """
    media = getattr(block, "media", None)
    path = str(getattr(media, "source", "")) if media is not None else ""
    if not path:
        return None

    if getattr(media, "media_type", "") != "image":
        text_parts.append(
            f"[attached {getattr(media, 'media_type', 'file')}: {path} — "
            "this runtime takes images only; read it with the document_reader tool]"
        )
        return None

    encoded = encode_media(path)
    if encoded is None:
        text_parts.append(f"[attached image {path} could not be sent; read it with a tool]")
        return None
    return encoded[0]


def _describe_result(block: Any) -> str:
    """Render a tool result for the model, flagging failures in words.

    Ollama has no `is_error` field, so a failure has to be legible in the text —
    otherwise a small model reads an error payload as data and carries on
    cheerfully.
    """
    body = getattr(block, "result", "")
    if isinstance(block, ToolResultBlock) and block.is_error:
        return f"ERROR: {body}"
    return str(body)


_TOOL_CALL_START = re.compile(r'\{\s*"name"\s*:')
"""Where a tool call written as prose begins.

Not a hypothetical. Observed on qwen2.5:14b through Ollama, on the same prompt
that had worked a minute earlier:

    forCell
    {"name": "document_reader", "arguments": {"path": "…/BG-114.txt"}}
    </tool_call>

The intent is right and the arguments are right; only the channel is wrong, and
the run failed anyway because nothing was reading the text. Recovering it is the
difference between a small model being usable and being a demo — and it is the
gap grammar-constrained decoding closes properly, which is why this is a
mitigation with a comment rather than a solution.
"""


def _json_object_at(text: str, start: int) -> tuple[str, int] | None:
    """The complete JSON object beginning at ``start``, brace-balanced.

    A regex cannot do this: `arguments` nests, so a non-greedy match stops at
    the first inner brace and produces something that will not parse. Strings
    are tracked so a brace inside a path or a message does not end the object.
    """
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1], index + 1

    return None


def _recover_text_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Pull tool calls out of assistant prose, and return the text without them."""
    calls: list[ToolCall] = []
    spans: list[tuple[int, int]] = []

    for match in _TOOL_CALL_START.finditer(text):
        found = _json_object_at(text, match.start())
        if found is None:
            continue
        blob, end = found

        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue

        name = parsed.get("name")
        if not isinstance(name, str) or not name:
            continue

        arguments = parsed.get("arguments")
        calls.append(
            ToolCall(
                id=f"recovered_{len(calls)}",
                name=name,
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
        spans.append((match.start(), end))

    if not calls:
        return text, []

    cleaned = text
    for begin, end in reversed(spans):
        cleaned = cleaned[:begin] + cleaned[end:]
    # The wrapper tags the model sometimes emits around the call are noise once
    # the call itself is gone.
    cleaned = cleaned.replace("<tool_call>", "").replace("</tool_call>", "")

    logger.warning(
        f"ollama: recovered {len(calls)} tool call(s) the model wrote as text — "
        "the intent was right and the channel was wrong"
    )
    return cleaned.strip(), calls


def _decode(payload: dict[str, Any]) -> Completion:
    """Decode an Ollama chat response."""
    message = payload.get("message") or {}
    text = message.get("content") or ""

    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments = function.get("arguments")
        calls.append(
            ToolCall(
                # Recent Ollama emits an id; older builds do not. Synthesising a
                # stable one keeps results matchable either way.
                id=str(raw.get("id") or f"ollama_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )

    if not calls and text:
        text, calls = _recover_text_tool_calls(text)

    if calls:
        logger.debug(f"ollama: {len(calls)} tool call(s): {[c.name for c in calls]}")

    return Completion(
        text_parts=(text,) if text else (),
        tool_calls=tuple(calls),
        # Ollama has no stop reason: tool calls mean continue, their absence ends
        # the turn. That is the same rule the other backends normalise to.
        stop_reason="tool_use" if calls else "end_turn",
    )
