"""A held step is not finished work (#38).

`annona run --once` printed "completed successfully" and exited 0 when the
perimeter refused the step, so a script or a cron job believed it was done.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli import app
from runner.cli_setup import write_policy
from runner.config import ConfigManager
from runner.main import EXIT_CODES, step_outcome
from runner.services.enforcement import policy_path

pytestmark = pytest.mark.unit


def _result(outcome: str, reason: str = "") -> dict:
    return {
        "type": "command_result",
        "result": {"placement": {"outcome": outcome, "reason": reason}},
    }


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result("placed"), ("completed", "")),
        (_result("redacted"), ("completed", "")),
        (_result("held", "no permitted substrate"), ("held", "no permitted substrate")),
        (_result("queued", "local-gpu is down"), ("queued", "local-gpu is down")),
        ({"type": "command_result", "result": {"response": "legacy"}}, ("completed", "")),
        ("not a dict", ("completed", "")),
    ],
)
def test_three_outcomes(result, expected):
    assert step_outcome(result) == expected


def test_each_outcome_has_its_own_exit_code():
    assert EXIT_CODES["completed"] == 0
    assert len(set(EXIT_CODES.values()) | {1, 2}) == len(EXIT_CODES) + 2


def test_a_step_held_by_the_real_perimeter_exits_3(tmp_path, monkeypatch):
    """Enzo's run: a codice fiscale, and the only substrate allowed to see it is down."""
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path / ".annona"))
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / ".akaion"))
    ConfigManager().create_default_config()
    write_policy(policy_path(), local_endpoint="http://127.0.0.1:9", local_model="llama3.2:3b")

    result = CliRunner().invoke(
        app, ["run", "--once", "--task", "Lettera per Mario Rossi, CF RSSMRA80A01H501U"]
    )

    assert result.exit_code == 3, result.output
    assert "Held" in result.output
    assert "completed" not in result.output.lower()


def test_once_without_a_task_is_a_usage_error(tmp_path, monkeypatch):
    """typer.Exit(2) was caught by `except Exception` and came out as 1."""
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path / ".annona"))
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / ".akaion"))
    ConfigManager().create_default_config()
    write_policy(policy_path())

    assert CliRunner().invoke(app, ["run", "--once"]).exit_code == 2
