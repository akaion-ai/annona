"""The step's text stays out of the log (#37).

The ledger records a digest of what crossed; `logs/annona.log`, beside it,
recorded the step itself. Enzo's run: a codice fiscale in the prompt, memory
off, and the CF on disk twice anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from runner.cli_setup import write_policy
from runner.config import ConfigManager
from runner.kernel.logtext import shown
from runner.services.enforcement import policy_path

pytestmark = pytest.mark.unit

CF = "RSSMRA80A01H501U"
TASK = f"Prepara la lettera per Mario Rossi, CF {CF}"


def _files_containing(root: Path, needle: str) -> list[str]:
    return [
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and needle.encode() in p.read_bytes()
    ]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path / ".annona"))
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / ".akaion"))
    monkeypatch.setenv("AKAION_BRAIN_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("ANNONA_LOG_STEP_TEXT", raising=False)
    config = ConfigManager()
    config.create_default_config()
    config.set("runner.capture_to_brain", False)
    # The only substrate allowed to see a restricted step is unreachable: held.
    write_policy(policy_path(), local_endpoint="http://127.0.0.1:9", local_model="llama3.2:3b")
    return tmp_path


def test_with_memory_off_the_step_is_nowhere_on_disk(home):
    CliRunner().invoke(app, ["run", "--once", "--task", TASK])

    log = home / ".annona" / "logs" / "annona.log"
    assert log.exists() and "Executing task" in log.read_text()
    assert _files_containing(home, CF) == []
    assert _files_containing(home, "Mario Rossi") == []


def test_the_text_comes_back_only_when_asked_for(home, monkeypatch):
    monkeypatch.setenv("ANNONA_LOG_STEP_TEXT", "1")

    CliRunner().invoke(app, ["run", "--once", "--task", TASK])

    assert CF in (home / ".annona" / "logs" / "annona.log").read_text()


def test_shown_gives_the_size_not_the_words(monkeypatch):
    monkeypatch.delenv("ANNONA_LOG_STEP_TEXT", raising=False)
    assert shown(TASK) == f"<{len(TASK)} chars>"
    monkeypatch.setenv("ANNONA_LOG_STEP_TEXT", "1")
    assert shown(TASK) == repr(TASK)
