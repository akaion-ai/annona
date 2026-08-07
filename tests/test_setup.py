"""Tests for `annona setup` and `annona doctor`.

The defect these commands exist to prevent is not a crash: it is a green tick
on a machine that cannot answer a question. So the assertions here are mostly
about *outcome and exit code* — after setup, do both files exist; when the
model the policy names is absent, does anything actually say so — rather than
about wording.

Nothing here contacts a real runtime: `probe_runtime` is patched in every test
that needs an answer from one, which also keeps the suite honest about the fact
that these commands are the only place the model list is read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import app  # noqa: E402
from runner import cli_setup  # noqa: E402
from runner.cli_setup import RuntimeProbe, choose_model, diagnose  # noqa: E402
from runner.policy.profiles import (  # noqa: E402
    FRONTIER_PROVIDERS,
    PROFILES,
    FrontierChoice,
    build_policy_document,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """A machine with neither of the two directories Annona uses."""
    monkeypatch.setenv("ANNONA_HOME", str(tmp_path / ".annona"))
    monkeypatch.setenv("AKAION_HOME", str(tmp_path / ".akaion"))
    return tmp_path


def _probe(*models: str, reachable: bool = True):
    """Stand in for a local runtime holding `models`."""

    def fake(endpoint: str = "", timeout: float = 3.0) -> RuntimeProbe:
        return RuntimeProbe(
            endpoint or "http://localhost:11434",
            reachable,
            tuple(models),
            "" if reachable else "ConnectError: refused",
        )

    return fake


# ── choosing the model ────────────────────────────────────────────────────────


def test_prefers_the_best_tested_model_that_is_installed():
    model, _ = choose_model(("mistral:7b", "qwen2.5:14b", "llama3.1:8b"))
    assert model == "qwen2.5:14b"


def test_falls_back_to_whatever_is_there():
    model, why = choose_model(("phi3:mini",))
    assert model == "phi3:mini"
    assert "only" in why


def test_an_explicit_model_wins_even_when_it_is_not_installed():
    """Overriding detection is deliberate; `doctor` keeps complaining until it is true."""
    model, why = choose_model(("qwen2.5:14b",), requested="llama3:70b")
    assert model == "llama3:70b"
    assert "not installed" in why


def test_with_nothing_installed_the_policy_still_names_something():
    model, _ = choose_model(())
    assert model == cli_setup.FALLBACK_MODEL


# ── setup ─────────────────────────────────────────────────────────────────────


def test_setup_writes_both_files(runner, fresh_home, monkeypatch):
    """The whole point: one command, and the machine is configured *and* governed."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert (fresh_home / ".akaion" / "config.yaml").exists()
    assert (fresh_home / ".annona" / "policy.yaml").exists()


def test_init_leaves_the_machine_in_the_same_state_as_setup(runner, fresh_home, monkeypatch):
    """`init` used to write the config and stop, so every kernel command then failed."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))

    result = runner.invoke(app, ["init", "-n"])

    assert result.exit_code == 0
    assert (fresh_home / ".annona" / "policy.yaml").exists()


def test_the_policy_names_a_model_that_is_actually_installed(runner, fresh_home, monkeypatch):
    """The hardcoded qwen2.5:14b default put a model in the policy that was not there."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("phi3:mini"))

    runner.invoke(app, ["setup"])

    assert "phi3:mini" in (fresh_home / ".annona" / "policy.yaml").read_text()


def test_setup_is_safe_to_run_twice(runner, fresh_home, monkeypatch):
    """An edited policy must survive a rerun; it is the document everything derives from."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup"])

    policy = fresh_home / ".annona" / "policy.yaml"
    policy.write_text(policy.read_text() + "\n# an operator wrote this\n")

    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 0
    assert "an operator wrote this" in policy.read_text()


def test_force_keeps_a_copy_of_the_policy_it_replaces(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup"])

    policy = fresh_home / ".annona" / "policy.yaml"
    policy.write_text(policy.read_text() + "\n# irreplaceable\n")

    runner.invoke(app, ["setup", "--force"])

    backups = list((fresh_home / ".annona").glob("policy.yaml.bak-*"))
    assert backups, "the replaced policy was not kept"
    assert "irreplaceable" in backups[0].read_text()


def test_setup_says_what_to_pull_when_the_model_is_missing(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:3b"))

    result = runner.invoke(app, ["setup", "--model", "llama3:70b"])

    assert "ollama pull llama3:70b" in result.output


def test_setup_says_what_to_do_when_no_runtime_answers(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe(reachable=False))

    result = runner.invoke(app, ["setup"])

    assert "ollama.com" in result.output


# ── doctor ────────────────────────────────────────────────────────────────────


def _check(diagnosis, prefix: str):
    return next(c for c in diagnosis.checks if c.name.startswith(prefix))


def test_doctor_fails_on_a_machine_that_has_never_been_set_up(runner, fresh_home):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1


def test_doctor_passes_after_setup(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup"])

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0


def test_doctor_catches_the_model_the_liveness_probe_cannot_see(runner, fresh_home, monkeypatch):
    """A runtime that is up with the named model absent reads as healthy to the prober.

    That is the failure this check exists for: the step gets placed, the ledger
    records `placed`, and the error arrives from the far side of the decision.
    """
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:3b"))
    runner.invoke(app, ["setup", "--model", "qwen2.5:14b"])

    diagnosis = diagnose()

    substrate = _check(diagnosis, "substrate")
    assert not substrate.ok
    assert "not pulled" in substrate.detail
    assert substrate.fix == "ollama pull qwen2.5:14b"


def test_a_stopped_daemon_is_a_warning_not_a_failure(runner, fresh_home, monkeypatch):
    """`doctor` is most often run before `annona run`, not after."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup"])

    # A port nothing is listening on, so the check is deterministic offline.
    diagnosis = diagnose(port=1)

    daemon = _check(diagnosis, "daemon")
    assert not daemon.ok
    assert not daemon.fatal
    assert diagnosis.broken == []


def test_doctor_reports_a_policy_that_exists_but_does_not_load(runner, fresh_home, monkeypatch):
    """Worse than no policy: the daemon starts anyway and stops enforcing."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup"])
    (fresh_home / ".annona" / "policy.yaml").write_text("version: 1\nsubstrates: not-a-list\n")

    diagnosis = diagnose()

    assert not _check(diagnosis, "policy").ok
    assert diagnosis.broken


# ── choosing a policy, rather than inheriting one ─────────────────────────────


def test_the_local_only_profile_registers_nothing_that_can_send(fresh_home):
    doc = build_policy_document("local-only")
    assert all(s["jurisdiction"] == "on-prem" for s in doc["substrates"])


def test_the_frontier_profile_caps_the_hosted_provider_at_public(fresh_home):
    """The ceiling is the product. A profile that offered a frontier model and
    left it uncapped would be the default with extra steps."""
    provider = next(p for p in FRONTIER_PROVIDERS if p.id == "anthropic")
    doc = build_policy_document("frontier-for-public", frontier=FrontierChoice(provider=provider))

    hosted = next(s for s in doc["substrates"] if s["id"] == "frontier")
    assert hosted["max_class"] == "public"

    # And it is unreachable from the rules that carry real material.
    for rule in doc["rules"]:
        if rule["match"]["class"] in ("restricted", "internal"):
            assert "frontier" not in rule["allow"]


def test_the_frontier_profile_never_writes_the_key_itself(fresh_home):
    provider = next(p for p in FRONTIER_PROVIDERS if p.id == "anthropic")
    doc = build_policy_document(
        "frontier-for-public",
        frontier=FrontierChoice(provider=provider, api_key_env="MY_KEY"),
    )
    hosted = next(s for s in doc["substrates"] if s["id"] == "frontier")
    assert hosted["api_key_env"] == "MY_KEY"
    assert "api_key" not in hosted


def test_read_nothing_leaves_every_reading_tool_with_no_paths(fresh_home):
    """`tools` is default-deny, so an empty allow-list is a refusal."""
    doc = build_policy_document("read-nothing")
    assert all(paths == [] for paths in doc["tools"]["allow"].values())


def test_every_profile_keeps_the_deny_list_and_the_internal_floor(fresh_home):
    """Profiles add and remove; they must not be able to drop the strict parts."""
    for profile in PROFILES:
        doc = build_policy_document(profile.id)
        assert "~/.ssh/**" in doc["tools"]["deny_paths"]
        assert doc["classes"]["internal"]["default"] is True


def test_an_unknown_profile_is_refused_rather_than_defaulted(fresh_home):
    with pytest.raises(ValueError):
        build_policy_document("whatever-sounds-safe")


def test_the_configurator_writes_what_it_showed(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b", "qwen2.5:3b"))

    # model 2 (3b), profile 3 (read-nothing), confirm.
    result = runner.invoke(app, ["setup", "--interactive"], input="2\n3\ny\n")

    assert result.exit_code == 0
    written = (fresh_home / ".annona" / "policy.yaml").read_text()
    assert "qwen2.5:3b" in written


def test_declining_at_the_end_writes_nothing(runner, fresh_home, monkeypatch):
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))

    # profile 1, default folders, then decline. One model installed, so the
    # model question is not asked.
    result = runner.invoke(app, ["setup", "--interactive"], input="1\n\nn\n")

    assert result.exit_code == 0
    assert not (fresh_home / ".annona" / "policy.yaml").exists()


def test_a_typed_folder_becomes_a_glob(runner, fresh_home, monkeypatch):
    """Typing `~/Work` must grant what is inside it, not the directory entry."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))

    runner.invoke(app, ["setup", "--interactive"], input="1\n~/Work\ny\n")

    written = (fresh_home / ".annona" / "policy.yaml").read_text()
    assert "~/Work/**" in written


def test_setup_does_not_ask_on_a_machine_that_has_a_policy(runner, fresh_home, monkeypatch):
    """A rerun has nothing to ask: the answers would go to a file it will not overwrite."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    runner.invoke(app, ["setup", "--yes"])

    # No input supplied: if it asked anything, this would fail rather than hang.
    result = runner.invoke(app, ["setup"], input="")
    assert result.exit_code == 0


def test_the_closing_line_does_not_claim_isolation_a_policy_does_not_give(
    runner, fresh_home, monkeypatch
):
    """It said "nothing can leave this machine" whatever the policy registered."""
    monkeypatch.setattr(cli_setup, "probe_runtime", _probe("qwen2.5:14b"))
    provider = next(p for p in FRONTIER_PROVIDERS if p.id == "anthropic")
    runner.invoke(app, ["setup", "--yes"])  # writes config
    (fresh_home / ".annona" / "policy.yaml").unlink()
    cli_setup.write_policy(
        fresh_home / ".annona" / "policy.yaml",
        profile_id="frontier-for-public",
        frontier=FrontierChoice(provider=provider),
    )

    result = runner.invoke(app, ["setup", "--yes"])

    assert "Nothing leaves this machine" not in result.output
    assert "frontier" in result.output
