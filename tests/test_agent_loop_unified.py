"""The unified agentic loop, driven through the offline scripted backend.

`test_agentic_loop.py` covers the same loop from the outside, through `AIClient`
with mocked vendor SDKs — that file is the regression net proving Phase 0 changed
no behaviour. This file tests the loop directly through its ports, which is
possible only because the loop no longer knows what a provider is.

Nothing here mocks an SDK. That is the point.
"""

from __future__ import annotations

import pytest

from runner.agent.loop import DEFAULT_MAX_ITERATIONS, AgentLoop, run_agent
from runner.agent.prompt import build_system_prompt
from runner.capability.backends.echo import EchoBackend
from runner.kernel.errors import BackendUnavailableError
from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
)

pytestmark = pytest.mark.unit


# ── Doubles built on the ports, not on any provider ───────────────────────────


class RecordingExecutor:
    """A tool executor that records calls and returns canned results."""

    def __init__(self, results: dict[str, object] | None = None, specs: tuple[ToolSpec, ...] = ()):
        self._results = results or {}
        self._specs = specs
        self.calls: list[ToolCall] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    def invoke(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if call.name not in self._results:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content={"error": f"Tool not found: {call.name}"},
                is_error=True,
            )
        return ToolResult(call_id=call.id, name=call.name, content=self._results[call.name])


class AllowAll:
    def permits(self, call: ToolCall) -> bool:
        return True


class DenyAll:
    def permits(self, call: ToolCall) -> bool:
        return False


class UnavailableBackend:
    """A backend that is never reachable."""

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def name(self) -> str:
        return "unavailable"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def complete(self, request: CompletionRequest) -> Completion:
        self.attempts += 1
        raise BackendUnavailableError("no route to host")


class ExplodingBackend:
    """A backend whose provider raises something that is not our error type."""

    @property
    def name(self) -> str:
        return "exploding"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def complete(self, request: CompletionRequest) -> Completion:
        raise RuntimeError("401 Unauthorized")


class CapturingBackend:
    """Records every request it is given, then ends the turn."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    @property
    def name(self) -> str:
        return "capturing"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(text_parts=("ok",), stop_reason="end_turn")


def loop(backend, executor=None, gate=None) -> AgentLoop:
    return AgentLoop(backend, executor or RecordingExecutor(), gate or AllowAll())


# ── Termination ───────────────────────────────────────────────────────────────


class TestTermination:
    def test_a_turn_without_tools_ends_the_run(self):
        backend = EchoBackend([Completion(text_parts=("42",), stop_reason="end_turn")])

        result = loop(backend).run("what is the answer?")

        assert result.response == "42"
        assert result.iterations == 1
        assert result.tool_calls == ()

    def test_the_iteration_ceiling_is_respected(self):
        """A backend that never finishes must not run forever."""
        forever = Completion(
            text_parts=("still working",),
            tool_calls=(ToolCall(id="t", name="echo_tool"),),
            stop_reason="tool_use",
        )
        backend = EchoBackend([forever] * 20)
        executor = RecordingExecutor({"echo_tool": "output"})

        result = loop(backend, executor).run("loop", max_iterations=3)

        assert result.iterations == 3
        assert backend.turns_played == 3
        assert len(result.tool_calls) == 3

    def test_a_zero_iteration_budget_returns_immediately(self):
        """Previously this raised UnboundLocalError; it now returns an empty run."""
        backend = EchoBackend([Completion(text_parts=("unused",))])

        result = loop(backend).run("task", max_iterations=0)

        assert result.iterations == 0
        assert result.response == ""
        assert backend.turns_played == 0

    def test_the_default_budget_is_applied(self):
        forever = Completion(
            tool_calls=(ToolCall(id="t", name="echo_tool"),), stop_reason="tool_use"
        )
        backend = EchoBackend([forever] * 50)

        result = loop(backend, RecordingExecutor({"echo_tool": "x"})).run("loop")

        assert result.iterations == DEFAULT_MAX_ITERATIONS


# ── Cancellation ──────────────────────────────────────────────────────────────


class TestCancellation:
    def test_cancel_set_before_the_first_turn_stops_immediately(self):
        """A run already asked to stop never reaches the model."""
        backend = EchoBackend([Completion(text_parts=("unused",))])

        result = loop(backend).run("task", cancel=lambda: True)

        assert result.cancelled is True
        assert result.iterations == 0
        assert result.response == ""
        assert backend.turns_played == 0

    def test_cancel_stops_the_run_between_turns_not_at_the_ceiling(self):
        """A backend that would loop forever is stopped by the cancel predicate,
        at the turn boundary — before the iteration ceiling, and set as cancelled."""
        forever = Completion(
            text_parts=("still working",),
            tool_calls=(ToolCall(id="t", name="echo_tool"),),
            stop_reason="tool_use",
        )
        backend = EchoBackend([forever] * 20)
        executor = RecordingExecutor({"echo_tool": "output"})

        turns = {"n": 0}

        def cancel() -> bool:
            # Let two turns run, then ask to stop on the third boundary.
            turns["n"] += 1
            return turns["n"] > 2

        result = loop(backend, executor).run("loop", max_iterations=10, cancel=cancel)

        assert result.cancelled is True
        assert result.iterations == 2
        assert backend.turns_played == 2

    def test_no_cancel_predicate_leaves_the_run_uncancelled(self):
        """The default path is unchanged: no predicate, nothing cancelled."""
        backend = EchoBackend([Completion(text_parts=("42",), stop_reason="end_turn")])

        result = loop(backend).run("what is the answer?")

        assert result.cancelled is False
        assert result.response == "42"


# ── Tool execution ────────────────────────────────────────────────────────────


class TestToolExecution:
    def test_a_tool_round_trips_and_the_run_continues(self):
        backend = EchoBackend(
            [
                Completion(
                    text_parts=("looking",),
                    tool_calls=(ToolCall(id="c1", name="reader", arguments={"path": "/x"}),),
                    stop_reason="tool_use",
                ),
                Completion(text_parts=("the file says hello",), stop_reason="end_turn"),
            ]
        )
        executor = RecordingExecutor({"reader": "hello"})

        result = loop(backend, executor).run("read /x")

        assert result.iterations == 2
        assert result.response == "the file says hello"
        assert [c.name for c in executor.calls] == ["reader"]
        assert result.tool_calls[0].result == "hello"
        assert result.tool_calls[0].error is False

    def test_parallel_calls_in_one_turn_all_execute_in_order(self):
        backend = EchoBackend(
            [
                Completion(
                    tool_calls=(
                        ToolCall(id="a", name="reader", arguments={"path": "/a"}),
                        ToolCall(id="b", name="reader", arguments={"path": "/b"}),
                    ),
                    stop_reason="tool_use",
                ),
                Completion(text_parts=("both read",)),
            ]
        )
        executor = RecordingExecutor({"reader": "content"})

        result = loop(backend, executor).run("read both")

        assert [c.id for c in executor.calls] == ["a", "b"]
        assert len(result.tool_calls) == 2

    def test_an_unknown_tool_is_reported_to_the_model_not_raised(self):
        backend = EchoBackend(
            [
                Completion(tool_calls=(ToolCall(id="g", name="ghost"),), stop_reason="tool_use"),
                Completion(text_parts=("no such tool",)),
            ]
        )

        result = loop(backend).run("use ghost")

        assert result.tool_calls[0].error is True
        assert "not found" in str(result.tool_calls[0].result).lower()
        assert result.response == "no such tool"

    def test_results_are_fed_back_into_the_transcript(self):
        """The next turn must be able to see what the tool returned."""
        capturing = CapturingBackend()
        scripted = EchoBackend(
            [
                Completion(
                    tool_calls=(ToolCall(id="c1", name="reader"),),
                    stop_reason="tool_use",
                )
            ]
        )

        class Chained:
            """First turn from the script, later turns from the recorder."""

            @property
            def name(self) -> str:
                return "chained"

            @property
            def capabilities(self) -> Capabilities:
                return Capabilities()

            def complete(self, request: CompletionRequest) -> Completion:
                if scripted.turns_played == 0:
                    return scripted.complete(request)
                return capturing.complete(request)

        loop(Chained(), RecordingExecutor({"reader": "file contents"})).run("read")

        transcript = capturing.requests[0].transcript
        assert [t.role for t in transcript] == ["user", "assistant", "user"]
        assert transcript[2].blocks[0].result == "file contents"
        assert transcript[2].blocks[0].is_error is False


# ── Policy ────────────────────────────────────────────────────────────────────


class TestPolicy:
    def test_a_denied_call_is_never_executed(self):
        backend = EchoBackend(
            [
                Completion(
                    tool_calls=(
                        ToolCall(id="d", name="reader", arguments={"path": "/etc/shadow"}),
                    ),
                    stop_reason="tool_use",
                ),
                Completion(text_parts=("denied",)),
            ]
        )
        executor = RecordingExecutor({"reader": "secret"})

        result = loop(backend, executor, DenyAll()).run("read the shadow file")

        assert executor.calls == [], "a denied call must not reach the executor"
        assert result.tool_calls[0].error is True
        assert result.tool_calls[0].result == {"error": "Permission denied for tool: reader"}

    def test_the_denial_reaches_the_model(self):
        capturing = CapturingBackend()
        scripted = EchoBackend(
            [Completion(tool_calls=(ToolCall(id="d", name="reader"),), stop_reason="tool_use")]
        )

        class Chained:
            @property
            def name(self) -> str:
                return "chained"

            @property
            def capabilities(self) -> Capabilities:
                return Capabilities()

            def complete(self, request: CompletionRequest) -> Completion:
                if scripted.turns_played == 0:
                    return scripted.complete(request)
                return capturing.complete(request)

        loop(Chained(), RecordingExecutor({"reader": "x"}), DenyAll()).run("read")

        result_block = capturing.requests[0].transcript[2].blocks[0]
        assert result_block.is_error is True
        assert "Permission denied" in result_block.result


# ── Failure handling ──────────────────────────────────────────────────────────


class TestFailures:
    def test_an_unavailable_backend_ends_the_run_with_partial_results(self):
        backend = UnavailableBackend()

        result = loop(backend).run("task")

        assert backend.attempts == 1
        assert result.response == ""
        assert result.tool_calls == ()

    def test_work_already_done_survives_a_backend_that_drops_out(self):
        scripted = EchoBackend(
            [
                Completion(
                    text_parts=("found it",),
                    tool_calls=(ToolCall(id="c1", name="reader"),),
                    stop_reason="tool_use",
                )
            ]
        )

        class DropsOut:
            @property
            def name(self) -> str:
                return "drops-out"

            @property
            def capabilities(self) -> Capabilities:
                return Capabilities()

            def complete(self, request: CompletionRequest) -> Completion:
                if scripted.turns_played == 0:
                    return scripted.complete(request)
                raise BackendUnavailableError("connection lost")

        result = loop(DropsOut(), RecordingExecutor({"reader": "data"})).run("read")

        assert result.response == "found it"
        assert len(result.tool_calls) == 1, "the completed tool call must not be lost"

    def test_provider_errors_are_not_swallowed(self):
        """A 401 is a real failure and must reach the caller, not become an empty answer."""
        with pytest.raises(RuntimeError, match="401"):
            loop(ExplodingBackend()).run("task")


# ── Request construction ──────────────────────────────────────────────────────


class TestRequest:
    def test_context_is_injected_into_the_system_prompt(self):
        backend = CapturingBackend()

        loop(backend).run("task", {"working_path": "/home/alice", "user": "alice"})

        system = backend.requests[0].system
        assert "working_path" in system
        assert "/home/alice" in system

    def test_tool_specs_are_advertised_every_turn(self):
        specs = (ToolSpec(name="reader", description="reads", schema={}),)
        capturing = CapturingBackend()
        scripted = EchoBackend(
            [Completion(tool_calls=(ToolCall(id="c", name="reader"),), stop_reason="tool_use")]
        )

        class Chained:
            @property
            def name(self) -> str:
                return "chained"

            @property
            def capabilities(self) -> Capabilities:
                return Capabilities()

            def complete(self, request: CompletionRequest) -> Completion:
                if scripted.turns_played == 0:
                    return scripted.complete(request)
                return capturing.complete(request)

        AgentLoop(Chained(), RecordingExecutor({"reader": "x"}, specs=specs), AllowAll()).run(
            "task"
        )

        assert [s.name for s in capturing.requests[0].tools] == ["reader"]

    def test_sampling_settings_reach_the_backend(self):
        backend = CapturingBackend()

        AgentLoop(
            backend,
            RecordingExecutor(),
            AllowAll(),
            temperature=0.15,
            max_tokens=333,
            model="a-model",
        ).run("task")

        request = backend.requests[0]
        assert request.temperature == 0.15
        assert request.max_tokens == 333
        assert request.model == "a-model"


class TestSystemPrompt:
    def test_no_context_renders_as_none(self):
        assert "Context: none" in build_system_prompt(None)

    def test_unserialisable_context_does_not_break_a_run(self):
        from pathlib import Path

        prompt = build_system_prompt({"cwd": Path("/tmp")})

        assert "/tmp" in prompt


class TestConvenienceWrapper:
    def test_run_agent_matches_the_class(self):
        backend = EchoBackend([Completion(text_parts=("done",))])

        result = run_agent("task", backend=backend, executor=RecordingExecutor(), gate=AllowAll())

        assert result.response == "done"
