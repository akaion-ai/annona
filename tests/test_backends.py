"""L1 adapters: wire encoding, the three shipped backends, and the tool adapters.

The wire tests matter more than they look. Both the Anthropic and the Akaion
adapters share one encoder, so these assertions are what stop the two paths
drifting apart — which is the failure this refactor exists to prevent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from runner.capability.backends import AkaionBackend, AnthropicBackend, EchoBackend
from runner.capability.backends.echo import script_from_config
from runner.capability.backends.wire import (
    decode_completion,
    encode_block,
    encode_tools,
    encode_transcript,
    normalise_stop_reason,
)
from runner.capability.tooling import PermissionGate, RegistryToolExecutor
from runner.kernel.blocks import function_call_block, function_result_block, text_block
from runner.kernel.errors import BackendUnavailableError, ConfigurationError
from runner.kernel.types import (
    Completion,
    CompletionRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)

pytestmark = pytest.mark.unit


SPEC = ToolSpec(
    name="filesystem",
    description="Read files",
    schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)


def request(transcript=(), tools=(), **kwargs) -> CompletionRequest:
    return CompletionRequest(
        system=kwargs.pop("system", "SYSTEM"),
        transcript=transcript,
        tools=tools,
        **kwargs,
    )


# ── Wire format ───────────────────────────────────────────────────────────────


class TestWireEncoding:
    def test_tools_use_the_input_schema_key(self):
        assert encode_tools([SPEC]) == [
            {
                "name": "filesystem",
                "description": "Read files",
                "input_schema": dict(SPEC.schema),
            }
        ]

    def test_text_block(self):
        assert encode_block(text_block("hi")) == {"type": "text", "text": "hi"}

    def test_tool_use_block(self):
        call = ToolCall(id="tu_1", name="filesystem", arguments={"path": "/x"})

        assert encode_block(function_call_block(call, SPEC)) == {
            "type": "tool_use",
            "id": "tu_1",
            "name": "filesystem",
            "input": {"path": "/x"},
        }

    def test_tool_result_block_carries_the_error_flag(self):
        result = ToolResult(call_id="tu_1", name="filesystem", content="boom", is_error=True)

        assert encode_block(function_result_block(result, SPEC)) == {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": "boom",
            "is_error": True,
        }

    def test_transcript_becomes_a_messages_array(self):
        transcript = (
            Turn(role="user", blocks=(text_block("read /x"),)),
            Turn(role="assistant", blocks=(text_block("on it"),)),
        )

        assert encode_transcript(transcript) == [
            {"role": "user", "content": [{"type": "text", "text": "read /x"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
        ]


class TestStopReason:
    @pytest.mark.parametrize(
        ("raw", "has_calls", "expected"),
        [
            ("end_turn", False, "end_turn"),
            ("end_turn", True, "end_turn"),  # explicit end wins over pending calls
            ("tool_use", True, "tool_use"),
            ("tool_use", False, "end_turn"),  # no calls means nothing to do
            ("max_tokens", True, "tool_use"),  # still work to do
            ("max_tokens", False, "max_tokens"),
            (None, False, "end_turn"),
            ("something_new", True, "tool_use"),
            ("something_new", False, "end_turn"),
        ],
    )
    def test_matches_the_historical_continue_rule(self, raw, has_calls, expected):
        """Legacy continued iff `stop_reason != "end_turn" and tool_use_blocks`."""
        assert normalise_stop_reason(raw, has_calls) == expected


class TestDecoding:
    def test_decodes_dictionaries(self):
        completion = decode_completion(
            [
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "id": "t1", "name": "fs", "input": {"path": "/x"}},
            ],
            "tool_use",
        )

        assert completion.text_parts == ("thinking",)
        assert completion.tool_calls[0] == ToolCall(id="t1", name="fs", arguments={"path": "/x"})
        assert completion.stop_reason == "tool_use"

    def test_decodes_sdk_style_objects(self):
        block = MagicMock()
        block.type = "text"
        block.text = "from an object"

        assert decode_completion([block], "end_turn").text_parts == ("from an object",)

    def test_a_tool_use_block_without_input_is_not_fatal(self):
        completion = decode_completion([{"type": "tool_use", "id": "t", "name": "fs"}], "tool_use")

        assert completion.tool_calls[0].arguments == {}

    def test_unknown_block_types_are_ignored(self):
        completion = decode_completion([{"type": "thinking", "text": "hmm"}], "end_turn")

        assert completion.text_parts == ()


# ── Anthropic ─────────────────────────────────────────────────────────────────


class TestAnthropicBackend:
    def test_declares_itself_remote(self):
        backend = AnthropicBackend(client=MagicMock(), model="m")

        assert backend.capabilities.is_local is False
        assert backend.name == "anthropic"

    def test_sends_the_expected_request(self):
        client = MagicMock()
        client.messages.create.return_value = MagicMock(content=[], stop_reason="end_turn")
        backend = AnthropicBackend(client=client, model="claude-x")

        backend.complete(
            request(
                transcript=(Turn(role="user", blocks=(text_block("hi"),)),),
                tools=(SPEC,),
                temperature=0.25,
                max_tokens=999,
            )
        )

        kwargs = client.messages.create.call_args[1]
        assert kwargs["model"] == "claude-x"
        assert kwargs["system"] == "SYSTEM"
        assert kwargs["max_tokens"] == 999
        assert [t["name"] for t in kwargs["tools"]] == ["filesystem"]
        # Sampling parameters are not sent: current Anthropic models reject
        # them with a 400, so a request carrying one never reached the model.
        assert "temperature" not in kwargs
        assert "top_p" not in kwargs

    def test_tools_are_omitted_when_there_are_none(self):
        """Some models reject an empty tools array; the runner has never sent one."""
        client = MagicMock()
        client.messages.create.return_value = MagicMock(content=[], stop_reason="end_turn")

        AnthropicBackend(client=client, model="m").complete(request())

        assert "tools" not in client.messages.create.call_args[1]

    def test_the_request_model_overrides_the_default(self):
        client = MagicMock()
        client.messages.create.return_value = MagicMock(content=[], stop_reason="end_turn")

        AnthropicBackend(client=client, model="default").complete(request(model="override"))

        assert client.messages.create.call_args[1]["model"] == "override"

    def test_a_missing_response_is_reported_as_unavailable(self):
        client = MagicMock()
        client.messages.create.return_value = None

        with pytest.raises(BackendUnavailableError):
            AnthropicBackend(client=client, model="m").complete(request())

    def test_sdk_errors_propagate(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("rate limited")

        with pytest.raises(RuntimeError, match="rate limited"):
            AnthropicBackend(client=client, model="m").complete(request())


# ── Akaion ────────────────────────────────────────────────────────────────────


class TestAkaionBackend:
    def test_declares_itself_remote(self):
        """The whole transcript crosses the perimeter on this path."""
        assert AkaionBackend(client=MagicMock()).capabilities.is_local is False

    def test_sends_the_expected_request(self):
        client = MagicMock()
        client.runner_id = "runner-1"
        client.runner_agent_turn.return_value = {"content": [], "stop_reason": "end_turn"}

        AkaionBackend(client=client).complete(request(tools=(SPEC,), temperature=0.3))

        kwargs = client.runner_agent_turn.call_args[1]
        assert kwargs["runner_id"] == "runner-1"
        assert kwargs["system_prompt"] == "SYSTEM"
        assert kwargs["temperature"] == 0.3
        assert [t["name"] for t in kwargs["tools"]] == ["filesystem"]

    def test_no_model_is_sent(self):
        """The control plane selects the model; the runner has never overridden it."""
        client = MagicMock()
        client.runner_agent_turn.return_value = {"content": [], "stop_reason": "end_turn"}

        AkaionBackend(client=client).complete(request(model="something"))

        assert "model" not in client.runner_agent_turn.call_args[1]

    def test_the_runner_id_is_resolved_per_call(self):
        """Registration can complete after this backend is constructed."""
        client = MagicMock()
        client.runner_id = None
        client.runner_agent_turn.return_value = {"content": [], "stop_reason": "end_turn"}
        backend = AkaionBackend(client=client)

        client.runner_id = "assigned-later"
        backend.complete(request())

        assert client.runner_agent_turn.call_args[1]["runner_id"] == "assigned-later"

    def test_an_unidentified_runner_falls_back_to_unknown(self):
        client = MagicMock()
        client.runner_id = None
        client.runner_agent_turn.return_value = {"content": [], "stop_reason": "end_turn"}

        AkaionBackend(client=client).complete(request())

        assert client.runner_agent_turn.call_args[1]["runner_id"] == "unknown"

    def test_an_empty_reply_is_reported_as_unavailable(self):
        client = MagicMock()
        client.runner_agent_turn.return_value = None

        with pytest.raises(BackendUnavailableError):
            AkaionBackend(client=client).complete(request())


# ── Echo ──────────────────────────────────────────────────────────────────────


class TestEchoBackend:
    def test_declares_itself_local(self):
        """The only Phase 0 backend for which nothing leaves the process."""
        assert EchoBackend().capabilities.is_local is True

    def test_plays_the_script_in_order(self):
        backend = EchoBackend([Completion(text_parts=("one",)), Completion(text_parts=("two",))])

        assert backend.complete(request()).text == "one"
        assert backend.complete(request()).text == "two"
        assert backend.turns_played == 2

    def test_an_exhausted_script_says_so_rather_than_improvising(self):
        backend = EchoBackend([Completion(text_parts=("only",))])
        backend.complete(request())

        assert "exhausted" in backend.complete(request()).text

    def test_with_no_script_it_echoes_the_prompt(self):
        transcript = (Turn(role="user", blocks=(text_block("say this back"),)),)

        assert "say this back" in EchoBackend().complete(request(transcript)).text

    def test_reset_rewinds(self):
        backend = EchoBackend([Completion(text_parts=("one",))])
        backend.complete(request())
        backend.reset()

        assert backend.complete(request()).text == "one"


class TestScriptFromConfig:
    def test_an_empty_script_is_valid(self):
        assert script_from_config(None) == ()

    def test_a_turn_with_tools_continues_the_loop(self):
        script = script_from_config(
            [{"text": "looking", "tools": [{"name": "explorer", "arguments": {"path": "/x"}}]}]
        )

        assert script[0].stop_reason == "tool_use"
        assert script[0].tool_calls[0].name == "explorer"
        assert script[0].tool_calls[0].arguments == {"path": "/x"}

    def test_a_turn_without_tools_ends_the_loop(self):
        assert script_from_config([{"text": "done"}])[0].stop_reason == "end_turn"

    def test_call_ids_are_unique_within_a_script(self):
        script = script_from_config(
            [{"tools": [{"name": "a"}, {"name": "b"}]}, {"tools": [{"name": "c"}]}]
        )

        ids = [c.id for turn in script for c in turn.tool_calls]
        assert len(set(ids)) == len(ids) == 3

    @pytest.mark.parametrize(
        "bad",
        [
            ["not-a-mapping"],
            [{"text": 42}],
            [{"tools": "not-a-list"}],
            [{"tools": [{"missing": "name"}]}],
            [{"tools": [{"name": "x", "arguments": "not-a-mapping"}]}],
        ],
    )
    def test_malformed_configuration_is_refused(self, bad):
        """Configuration that cannot be honoured is refused, never half-applied."""
        with pytest.raises(ConfigurationError):
            script_from_config(bad)


# ── Tool adapters ─────────────────────────────────────────────────────────────


class TestRegistryToolExecutor:
    def test_specs_come_from_the_registry(self):
        registry = MagicMock()
        registry.get_all_schemas.return_value = [
            {"name": "fs", "description": "d", "parameters": {"properties": {}}}
        ]

        assert [s.name for s in RegistryToolExecutor(registry).specs()] == ["fs"]

    def test_a_successful_call_returns_the_tool_output_untouched(self):
        registry = MagicMock()
        registry.get_tool.return_value.execute.return_value = {"rows": [1, 2]}

        result = RegistryToolExecutor(registry).invoke(ToolCall(id="1", name="fs"))

        assert result.content == {"rows": [1, 2]}
        assert result.is_error is False

    def test_a_raising_tool_becomes_an_error_result(self):
        """Third-party code may raise anything; the run must continue."""
        registry = MagicMock()
        registry.get_tool.return_value.execute.side_effect = OSError("disk on fire")

        result = RegistryToolExecutor(registry).invoke(ToolCall(id="1", name="fs"))

        assert result.is_error is True
        assert result.content == {"error": "disk on fire"}

    def test_an_unknown_tool_becomes_an_error_result(self):
        registry = MagicMock()
        registry.get_tool.side_effect = ValueError("Tool not found: ghost")

        result = RegistryToolExecutor(registry).invoke(ToolCall(id="1", name="ghost"))

        assert result.is_error is True
        assert result.content == {"error": "Tool not found: ghost"}

    def test_without_a_registry_nothing_is_advertised_or_runnable(self):
        executor = RegistryToolExecutor(None)

        assert executor.specs() == ()
        assert executor.invoke(ToolCall(id="1", name="fs")).is_error is True


class TestPermissionGate:
    def test_consults_the_permission_manager(self):
        permissions = MagicMock()
        permissions.check_tool_permission.return_value = False

        assert (
            PermissionGate(permissions).permits(ToolCall(id="1", name="fs", arguments={"p": 1}))
            is False
        )
        permissions.check_tool_permission.assert_called_once_with("fs", {"p": 1})

    def test_without_a_manager_nothing_is_enforced(self):
        gate = PermissionGate(None)

        assert gate.enforcing is False
        assert gate.permits(ToolCall(id="1", name="anything")) is True
