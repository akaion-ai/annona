"""
Tests for TaskExecutor — all task types including 'explore'.
AI client is mocked in all tests.
"""

from unittest.mock import MagicMock, patch

import pytest

from runner.executor import TaskExecutor

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def executor(minimal_config):
    """TaskExecutor with mocked AIClient."""
    with patch("runner.executor.AIClient") as MockAI:
        mock_ai = MagicMock()
        MockAI.return_value = mock_ai
        ex = TaskExecutor(minimal_config)
        ex._mock_ai = mock_ai  # expose for assertions
    return ex


# ── command task ──────────────────────────────────────────────────────────────


class TestCommandTask:
    def test_a_command_goes_through_the_agentic_loop(self, executor):
        """A command is a task with a prompt, and it takes the same route.

        It used to call `AIClient.execute_command`, which posts to the control
        plane's runner endpoint: on a machine with no credentials that client is
        None and `annona run --once --task …` died, and when it worked it
        bypassed placement. This test pinned the old routing in place, so it is
        the test that changes.
        """
        executor._mock_ai.reason_and_execute.return_value = {"response": "Done", "tool_calls": []}
        task = {"type": "command", "payload": {"command": "list files"}}
        result = executor.execute(task)

        assert result["type"] == "command_result"
        assert result["command"] == "list files"
        executor._mock_ai.reason_and_execute.assert_called_once()
        assert executor._mock_ai.reason_and_execute.call_args.kwargs["prompt"] == "list files"

    def test_command_missing_command_raises(self, executor):
        with pytest.raises(ValueError, match="No command provided"):
            executor.execute({"type": "command", "payload": {}})


# ── tool task ─────────────────────────────────────────────────────────────────


class TestToolTask:
    def test_direct_tool_call_filesystem_list(self, executor, tmp_path):
        (tmp_path / "f.txt").write_text("x")
        task = {
            "type": "tool",
            "payload": {
                "tool": "filesystem",
                "args": {"operation": "list", "path": str(tmp_path)},
            },
        }
        result = executor.execute(task)
        assert result["type"] == "tool_result"
        assert result["tool"] == "filesystem"
        names = [item["name"] for item in result["result"]]
        assert "f.txt" in names

    def test_direct_tool_call_shell_echo(self, executor):
        task = {
            "type": "tool",
            "payload": {
                "tool": "shell",
                "args": {"command": "echo akaion"},
            },
        }
        result = executor.execute(task)
        assert result["result"]["stdout"].strip() == "akaion"

    def test_direct_tool_call_explorer_map(self, executor, sample_dir):
        task = {
            "type": "tool",
            "payload": {
                "tool": "explorer",
                "args": {"operation": "map", "path": str(sample_dir)},
            },
        }
        result = executor.execute(task)
        assert result["result"]["success"] is True
        assert "reports" in result["result"]["tree"]

    def test_unknown_tool_raises(self, executor):
        with pytest.raises(ValueError, match="Tool not found"):
            executor.execute({"type": "tool", "payload": {"tool": "ghost"}})

    def test_missing_tool_key_raises(self, executor):
        with pytest.raises(ValueError, match="No tool specified"):
            executor.execute({"type": "tool", "payload": {}})


# ── ai_task ───────────────────────────────────────────────────────────────────


class TestAITask:
    def test_ai_task_calls_reason_and_execute(self, executor):
        executor._mock_ai.reason_and_execute.return_value = {
            "response": "Analysis complete.",
            "tool_calls": [],
        }
        task = {
            "type": "ai_task",
            "payload": {
                "prompt": "Summarize documents in ~/docs",
                "context": {"user": "alice"},
            },
        }
        result = executor.execute(task)
        assert result["type"] == "ai_result"
        executor._mock_ai.reason_and_execute.assert_called_once()
        call_kwargs = executor._mock_ai.reason_and_execute.call_args
        assert call_kwargs[1]["prompt"] == "Summarize documents in ~/docs"

    def test_ai_task_missing_prompt_raises(self, executor):
        with pytest.raises(ValueError, match="No prompt provided"):
            executor.execute({"type": "ai_task", "payload": {}})


# ── explore task ──────────────────────────────────────────────────────────────


class TestExploreTask:
    def test_explore_injects_path_into_prompt(self, executor, sample_dir):
        executor._mock_ai.reason_and_execute.return_value = {
            "response": "Found 3 report files.",
            "tool_calls": [],
        }
        task = {
            "type": "explore",
            "payload": {
                "prompt": "Summarize all documents",
                "path": str(sample_dir),
            },
        }
        result = executor.execute(task)
        assert result["type"] == "explore_result"

        call_kwargs = executor._mock_ai.reason_and_execute.call_args[1]
        assert str(sample_dir) in call_kwargs["prompt"]
        assert call_kwargs["context"]["working_path"] == str(sample_dir)

    def test_explore_works_without_path(self, executor):
        executor._mock_ai.reason_and_execute.return_value = {"response": "Done.", "tool_calls": []}
        task = {
            "type": "explore",
            "payload": {"prompt": "What is the current directory?"},
        }
        result = executor.execute(task)
        assert result["type"] == "explore_result"

    def test_explore_missing_prompt_raises(self, executor):
        with pytest.raises(ValueError, match="No prompt provided"):
            executor.execute({"type": "explore", "payload": {}})

    def test_explore_passes_context(self, executor, sample_dir):
        executor._mock_ai.reason_and_execute.return_value = {"response": "", "tool_calls": []}
        task = {
            "type": "explore",
            "payload": {
                "prompt": "Analyze",
                "path": str(sample_dir),
                "context": {"user_id": "u123", "request_id": "r456"},
            },
        }
        executor.execute(task)
        call_kwargs = executor._mock_ai.reason_and_execute.call_args[1]
        assert call_kwargs["context"]["user_id"] == "u123"
        assert call_kwargs["context"]["working_path"] == str(sample_dir)


# ── workflow task ─────────────────────────────────────────────────────────────


class TestWorkflowTask:
    def test_workflow_executes_steps_in_order(self, executor, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("step data")

        task = {
            "type": "workflow",
            "payload": {
                "steps": [
                    # Step 1: direct tool call
                    {
                        "type": "tool",
                        "payload": {
                            "tool": "filesystem",
                            "args": {"operation": "read", "path": str(f)},
                        },
                    },
                    # Step 2: shell command
                    {
                        "type": "tool",
                        "payload": {
                            "tool": "shell",
                            "args": {"command": "echo step2"},
                        },
                    },
                ]
            },
        }
        result = executor.execute(task)
        assert result["type"] == "workflow_result"
        assert result["steps"] == 2
        assert len(result["results"]) == 2

        step1_result = result["results"][0]
        assert step1_result["result"] == "step data"

        step2_result = result["results"][1]
        assert step2_result["result"]["stdout"].strip() == "step2"

    def test_workflow_empty_steps_raises(self, executor):
        with pytest.raises(ValueError, match="No workflow steps"):
            executor.execute({"type": "workflow", "payload": {"steps": []}})

    def test_unknown_task_type_raises(self, executor):
        with pytest.raises(ValueError, match="Unknown task type"):
            executor.execute({"type": "teleport", "payload": {}})
