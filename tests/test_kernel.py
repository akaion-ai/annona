"""L0 kernel: value types and the datapizza block translation.

These are the cheapest tests in the suite and the ones most worth having: every
adapter and the loop itself depend on this vocabulary, so a defect here surfaces
everywhere at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from datapizza.type import FunctionCallBlock, FunctionCallResultBlock, TextBlock

from runner.kernel import (
    AgentResult,
    Capabilities,
    Completion,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from runner.kernel.blocks import (
    ToolResultBlock,
    as_datapizza_tool,
    encode_result_content,
    function_call_block,
    function_result_block,
    text_block,
    tool_spec_from_schema,
)

pytestmark = pytest.mark.unit


REGISTRY_SCHEMA = {
    "name": "filesystem",
    "description": "Read and write files",
    "parameters": {
        "type": "object",
        "properties": {"operation": {"type": "string"}, "path": {"type": "string"}},
        "required": ["operation", "path"],
    },
}


class TestToolSpec:
    def test_built_from_registry_schema(self):
        spec = tool_spec_from_schema(REGISTRY_SCHEMA)

        assert spec.name == "filesystem"
        assert spec.description == "Read and write files"
        assert set(spec.properties) == {"operation", "path"}
        assert list(spec.required) == ["operation", "path"]

    def test_tolerates_a_schema_without_properties(self):
        spec = tool_spec_from_schema({"name": "ping", "description": "", "parameters": {}})

        assert spec.properties == {}
        assert list(spec.required) == []

    def test_tolerates_a_malformed_required_field(self):
        """A schema is external data; a bad `required` must not crash a run."""
        spec = ToolSpec(name="x", description="", schema={"required": "not-a-list"})

        assert list(spec.required) == []


class TestBlockTranslation:
    def test_text_block_roundtrips(self):
        block = text_block("hello")

        assert isinstance(block, TextBlock)
        assert block.content == "hello"

    def test_function_call_block_carries_the_call(self):
        spec = tool_spec_from_schema(REGISTRY_SCHEMA)
        call = ToolCall(id="tu_1", name="filesystem", arguments={"operation": "read"})

        block = function_call_block(call, spec)

        assert isinstance(block, FunctionCallBlock)
        assert block.id == "tu_1"
        assert block.name == "filesystem"
        assert block.arguments == {"operation": "read"}

    def test_function_call_block_without_a_spec(self):
        """A hallucinated tool name is part of what happened and must be recordable."""
        block = function_call_block(ToolCall(id="tu_x", name="ghost", arguments={}))

        assert block.name == "ghost"

    def test_result_block_preserves_the_error_flag(self):
        result = ToolResult(call_id="tu_1", name="fs", content={"error": "nope"}, is_error=True)

        block = function_result_block(result)

        assert isinstance(block, ToolResultBlock)
        # Still a datapizza block: anything expecting the framework type works.
        assert isinstance(block, FunctionCallResultBlock)
        assert block.is_error is True

    def test_datapizza_tool_is_schema_only(self):
        """No callable is attached — the runner executes tools, under policy."""
        tool = as_datapizza_tool(tool_spec_from_schema(REGISTRY_SCHEMA))

        assert tool.name == "filesystem"
        assert tool.func is None
        assert tool.schema["parameters"]["required"] == ["operation", "path"]


class TestResultEncoding:
    def test_strings_pass_through_unchanged(self):
        assert encode_result_content("plain") == "plain"

    def test_structures_become_json(self):
        assert encode_result_content({"ok": True}) == '{"ok": true}'

    def test_unserialisable_values_degrade_instead_of_raising(self):
        """A Path in a tool result must not end the run."""
        assert encode_result_content({"p": Path("/tmp")}) == '{"p": "/tmp"}'

    def test_wholly_unserialisable_values_fall_back_to_str(self):
        class Opaque:
            def __repr__(self) -> str:
                return "<opaque>"

        assert "opaque" in encode_result_content(Opaque())


class TestCompletion:
    def test_text_joins_every_part(self):
        """Commentary emitted alongside a tool call is part of the answer."""
        assert Completion(text_parts=("first", "second")).text == "first second"

    def test_wants_tools_requires_both_signals(self):
        call = ToolCall(id="1", name="fs")

        assert Completion(tool_calls=(call,), stop_reason="tool_use").wants_tools
        assert not Completion(tool_calls=(), stop_reason="tool_use").wants_tools
        assert not Completion(tool_calls=(call,), stop_reason="end_turn").wants_tools


class TestLegacyShape:
    def test_agent_result_matches_the_historical_dictionary(self):
        result = AgentResult(
            response="done",
            iterations=2,
            tool_calls=(ToolInvocation(tool="fs", input={"p": 1}, result="ok", error=False),),
        )

        # `cancelled` is an additive field: a run that was not stopped reports
        # False, so existing consumers reading the historical keys are unaffected.
        assert result.to_dict() == {
            "response": "done",
            "iterations": 2,
            "tool_calls": [{"tool": "fs", "input": {"p": 1}, "result": "ok", "error": False}],
            "cancelled": False,
        }


class TestCapabilities:
    def test_defaults_are_conservative(self):
        """An adapter that declares nothing is treated as remote and dumb."""
        caps = Capabilities()

        assert caps.is_local is False
        assert caps.native_tools is False
        assert caps.grammar == "none"
