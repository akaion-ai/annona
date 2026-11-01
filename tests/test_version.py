"""One version, written once (#43).

The window used to say v0.1.0 at 0.1.3, and so did the banner, `annona version`
and the OpenAPI title: the number was typed in several places and bumped in one.
It is now written in `runner/__init__.py` only; pyproject reads it from there.
"""

import json
import re
from importlib.metadata import version
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cli import app
from runner import __version__
from tests.test_local_api_ui_mount import _build_app

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_reads_the_version_instead_of_typing_it():
    # Not tomllib: it is 3.11+, and the supported floor (and a CI job) is 3.10.
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "runner.__version__" }' in pyproject


def test_the_installed_metadata_agrees():
    assert version("annona") == __version__


def test_the_desktop_bundle_agrees():
    for path in ("ui/package.json", "ui/src-tauri/tauri.conf.json"):
        assert json.loads((ROOT / path).read_text())["version"] == __version__, path
    cargo = re.search(
        r'^version\s*=\s*"([^"]+)"', (ROOT / "ui/src-tauri/Cargo.toml").read_text(), re.M
    )
    assert cargo and cargo.group(1) == __version__, "ui/src-tauri/Cargo.toml"


def test_health_and_openapi_say_which_daemon_is_answering(tmp_path):
    client = TestClient(_build_app(tmp_path, None))
    assert client.get("/health").json()["version"] == __version__
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_the_cli_reports_it():
    assert __version__ in CliRunner().invoke(app, ["version"]).output


def test_nothing_in_the_package_types_a_version_of_its_own():
    typed = re.compile(r"""["']v?\d+\.\d+\.\d+["']""")
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in (ROOT / "runner").rglob("*.py")
        if path != ROOT / "runner" / "__init__.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if typed.search(line) and "version" in line.lower()
    ]
    assert offenders == []
