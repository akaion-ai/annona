"""Anthropic-style wire encoding, shared by the adapters that speak it.

Both the direct Anthropic backend and the Akaion backend use this format: the
Akaion control plane proxies to Claude with native tool use, so its
``/runner/agent/turn`` contract is Anthropic's block vocabulary. Encoding it in
one place means the two adapters cannot drift apart, and there is exactly one
function to re-read when auditing what gets sent.

The encoding is deliberately total and lossless with respect to the transcript:
a transcript encodes to a request, and a response decodes to a
:class:`~runner.kernel.types.Completion`, with no provider objects retained on
either side. Retaining provider objects and replaying them — as the pre-Phase-0
code did — makes a transcript unserialisable, and therefore untraceable and
unreplayable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from datapizza.type import Block, FunctionCallBlock, TextBlock

from runner.capability.backends.media import encode_media
from runner.kernel.blocks import ToolResultBlock
from runner.kernel.types import (
    Completion,
    StopReason,
    ToolCall,
    ToolSpec,
    Transcript,
)

__all__ = [
    "decode_completion",
    "encode_block",
    "encode_tools",
    "encode_transcript",
    "normalise_stop_reason",
]


def encode_tools(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Encode tool specs as Anthropic tool definitions."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": dict(spec.schema),
        }
        for spec in specs
    ]


def encode_block(block: Block) -> dict[str, Any]:
    """Encode one canonical block as an Anthropic content block.

    Unknown block types degrade to text rather than raising: a transcript that
    cannot be sent is worse than one that loses a little structure, and the
    alternative — an exception mid-run — tells the model nothing.
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.content}

    if getattr(block, "type", "") == "media":
        return _encode_media_block(block)

    if isinstance(block, FunctionCallBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.arguments),
        }

    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": block.result,
            "is_error": block.is_error,
        }

    # A plain datapizza FunctionCallResultBlock: same shape, error flag unknown.
    result = getattr(block, "result", None)
    if result is not None and getattr(block, "type", "") == "function_call_result":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "id", ""),
            "content": result,
            "is_error": False,
        }

    return {"type": "text", "text": str(getattr(block, "content", block))}


def _encode_media_block(block: Block) -> dict[str, Any]:
    """An attached file as an Anthropic content block.

    Images and PDFs have native block types and are sent as bytes. Audio and
    video do not, and are named in text instead: the model is told the file
    exists and where, so it can reach for the reader tool. Silently dropping it
    would leave a model answering about a recording it was never given.
    """
    media = getattr(block, "media", None)
    path = str(getattr(media, "source", "")) if media is not None else ""
    kind = getattr(media, "media_type", "") if media is not None else ""

    if path and kind in ("image", "pdf"):
        encoded = encode_media(path)
        if encoded is not None:
            data, mime = encoded
            return {
                "type": "image" if kind == "image" else "document",
                "source": {"type": "base64", "media_type": mime, "data": data},
            }

    return {
        "type": "text",
        "text": f"[attached {kind or 'file'}: {path} — read it with the document_reader tool]",
    }


def encode_transcript(transcript: Transcript) -> list[dict[str, Any]]:
    """Encode a transcript as an Anthropic ``messages`` array."""
    return [
        {"role": turn.role, "content": [encode_block(b) for b in turn.blocks]}
        for turn in transcript
    ]


def normalise_stop_reason(raw: str | None, has_tool_calls: bool) -> StopReason:
    """Map a provider stop reason onto the runner's vocabulary.

    The rule reproduces the behaviour the runner has always had: continue the
    loop when the model asked for tools *and* did not declare the turn finished.
    A response carrying tool calls with, say, ``max_tokens`` is still work to do,
    so it is reported as ``tool_use``; anything else ends the turn.
    """
    if has_tool_calls and raw != "end_turn":
        return "tool_use"
    if raw in ("end_turn", "tool_use", "max_tokens", "error"):
        # Narrow to StopReason; `tool_use` without calls is a finished turn.
        return "end_turn" if raw == "tool_use" else raw  # type: ignore[return-value]
    return "end_turn"


def decode_completion(
    content_blocks: Iterable[Any],
    raw_stop_reason: str | None,
) -> Completion:
    """Decode a provider response into a :class:`Completion`.

    Accepts either dictionaries (the Akaion control plane returns JSON) or
    objects with ``.type`` attributes (the Anthropic SDK returns models), so one
    decoder serves both adapters.
    """
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        block_type = _get(block, "type", "")

        if block_type == "text":
            text_parts.append(str(_get(block, "text", "") or ""))
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=str(_get(block, "id", "") or ""),
                    name=str(_get(block, "name", "") or ""),
                    arguments=_get(block, "input", None) or {},
                )
            )

    return Completion(
        text_parts=tuple(text_parts),
        tool_calls=tuple(tool_calls),
        stop_reason=normalise_stop_reason(raw_stop_reason, bool(tool_calls)),
    )


def _get(block: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or an attribute-style object."""
    if isinstance(block, Mapping):
        return block.get(key, default)
    return getattr(block, key, default)
