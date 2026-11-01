"""The image, tested as a deployment rather than as a build.

`docker build` succeeding proves nothing an operator cares about. These tests
assert the properties the appliance is sold on: it runs unprivileged, it writes
its policy to a volume that survives the container, it refuses to start with a
policy it cannot parse, and its idea of where restricted work goes is the same
inside a container as outside one.

Enabled with::

    ANNONA_CONTAINER_TESTS=1 pytest -m container

Skipped otherwise, because a laptop without a Docker daemon should not be red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest

pytestmark = [pytest.mark.container, pytest.mark.slow]

IMAGE = os.getenv("ANNONA_IMAGE", "annona:dev")

pytestmark.append(
    pytest.mark.skipif(
        os.getenv("ANNONA_CONTAINER_TESTS") != "1" or shutil.which("docker") is None,
        reason="set ANNONA_CONTAINER_TESTS=1 with a working Docker daemon",
    )
)


def docker(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


@pytest.fixture
def volume():
    """A throwaway named volume, removed even if the test fails."""
    name = f"annona-test-{uuid.uuid4().hex[:8]}"
    docker("volume", "create", name)
    yield name
    docker("volume", "rm", "-f", name, check=False)


def run_in(volume: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return docker(
        "run",
        "--rm",
        "-v",
        f"{volume}:/home/annona/.annona",
        IMAGE,
        *args,
        check=check,
    )


# ── Posture ───────────────────────────────────────────────────────────────────


def test_the_daemon_does_not_run_as_root():
    """A kernel that reads every file on the machine has no business being root."""
    result = docker("run", "--rm", "--entrypoint", "id", IMAGE)
    assert "uid=10001(annona)" in result.stdout
    assert "uid=0" not in result.stdout


def test_the_image_carries_no_policy_of_its_own(volume):
    """A policy baked into an image is one nobody in the customer's org wrote."""
    result = run_in(volume, "policy", "validate", check=False)
    assert result.returncode == 1
    assert "no policy at" in result.stdout


def test_the_image_ships_no_credentials():
    """The .dockerignore claim, verified rather than trusted.

    Scoped to the application tree: the CA bundles that ship inside `certifi`
    and `grpc` are public roots, and asserting their absence would be a test
    that fails for being right.
    """
    listing = docker(
        "run",
        "--rm",
        "--entrypoint",
        "find",
        IMAGE,
        "/home/annona",
        "/opt/venv/bin",
        "(",
        "-name",
        ".env*",
        "-o",
        "-name",
        "auth.json",
        "-o",
        "-name",
        "id_rsa*",
        "-o",
        "-name",
        "*.key",
        ")",
    )
    assert listing.stdout.strip() == ""


# ── The policy lifecycle inside a container ───────────────────────────────────


def test_policy_init_writes_to_the_volume_and_survives_the_container(volume):
    created = run_in(volume, "policy", "init", "--model", "qwen2.5:3b")
    assert "Policy written to" in created.stdout

    # A different container, the same volume: this is what an image upgrade is.
    validated = run_in(volume, "policy", "validate")
    assert "is valid" in validated.stdout


def test_policy_init_never_overwrites_what_the_operator_wrote(volume):
    run_in(volume, "policy", "init")
    again = run_in(volume, "policy", "init", "--model", "something-else")
    # Rich wraps to the terminal width, so the sentence is matched on its words.
    assert "already exists" in " ".join(again.stdout.split())
    assert "left untouched" in " ".join(again.stdout.split())


def test_placement_inside_a_container_matches_placement_outside_one(volume):
    """One binary, three topologies: a container is a configuration, not a fork."""
    run_in(volume, "policy", "init")
    result = run_in(volume, "policy", "test", "restricted")

    assert "restricted → placed" in result.stdout
    assert "local-gpu" in result.stdout


def test_a_malformed_policy_stops_the_command_rather_than_being_ignored(volume):
    run_in(volume, "policy", "init")
    docker(
        "run",
        "--rm",
        "-v",
        f"{volume}:/home/annona/.annona",
        "--entrypoint",
        "sh",
        IMAGE,
        "-c",
        "echo 'classes: [broken' > /home/annona/.annona/policy.yaml",
    )

    result = run_in(volume, "policy", "validate", check=False)
    assert result.returncode == 1
    assert "not valid YAML" in result.stdout


# ── The record ────────────────────────────────────────────────────────────────


def test_the_ledger_verifies_from_inside_the_container(volume):
    run_in(volume, "policy", "init")

    # Two entries written by the CLI's own code path, through the volume.
    docker(
        "run",
        "--rm",
        "-v",
        f"{volume}:/home/annona/.annona",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        "from runner.audit.ledger import Ledger;"
        "from runner.kernel.types import SensitivityClass as C;"
        "l = Ledger('/home/annona/.annona/ledger.jsonl');"
        "l.record('inference', outcome='placed', klass=C.PUBLIC, substrate='local-gpu');"
        "l.record('inference', outcome='held', klass=C.RESTRICTED)",
    )

    verified = run_in(volume, "verify")
    assert "chain intact" in verified.stdout

    audited = run_in(volume, "audit")
    assert "2 decisions" in audited.stdout


def test_tampering_with_the_ledger_on_the_volume_is_detected(volume):
    run_in(volume, "policy", "init")
    docker(
        "run",
        "--rm",
        "-v",
        f"{volume}:/home/annona/.annona",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        "from runner.audit.ledger import Ledger;"
        "from runner.kernel.types import SensitivityClass as C;"
        "l = Ledger('/home/annona/.annona/ledger.jsonl');"
        "l.record('inference', outcome='held', klass=C.RESTRICTED)",
    )

    docker(
        "run",
        "--rm",
        "-v",
        f"{volume}:/home/annona/.annona",
        "--entrypoint",
        "python",
        IMAGE,
        "-c",
        "import json, pathlib;"
        "p = pathlib.Path('/home/annona/.annona/ledger.jsonl');"
        "rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()];"
        "rows[0]['outcome'] = 'placed';"
        "p.write_text(''.join(json.dumps(r, sort_keys=True, separators=(',', ':')) + chr(10) for r in rows))",
    )

    result = run_in(volume, "verify", check=False)
    assert result.returncode == 1
    assert "BROKEN" in result.stdout


# ── Substrate health, from inside the network namespace ───────────────────────


def test_an_unreachable_substrate_is_reported_as_down_not_assumed_up(volume):
    """The container cannot reach the host's Ollama unless told how.

    That is the honest answer for a fresh deployment, and it must be visible
    rather than discovered when a run is held.
    """
    run_in(volume, "policy", "init", "--endpoint", "http://127.0.0.1:11434")
    result = run_in(volume, "substrates")

    assert "local-gpu" in result.stdout
    assert "down" in result.stdout


def test_the_image_reports_its_own_version(volume):
    from runner import __version__

    result = docker("run", "--rm", IMAGE, "version")
    assert f"Version: {__version__}" in result.stdout  # it said 0.1.0 at 0.1.3 (#43)


def test_the_healthcheck_is_declared_on_the_image():
    """A compose file that restarts a dead daemon needs the image to say so."""
    inspected = json.loads(docker("inspect", IMAGE).stdout)[0]
    healthcheck = inspected["Config"].get("Healthcheck") or {}
    assert healthcheck.get("Test"), "the image declares no HEALTHCHECK"
    assert any("7070" in part for part in healthcheck["Test"])
