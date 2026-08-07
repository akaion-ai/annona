"""Tests for pairing a web app with this machine (runner/pairing.py).

The daemon listens on loopback, so its neighbours are not other computers — they
are the other tabs in the user's browser. Everything asserted here is about that:
what an unpaired page gets, what a listed page without a token gets, and the one
header that decides whether Chrome will let a public site reach 127.0.0.1 at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runner.pairing import DEFAULT_REMOTE_ORIGINS, PairedOriginMiddleware, Pairing, pairing_path

CLOUD = "https://app.akaion.com"
EVIL = "https://not-your-app.example"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "annona-home"
    h.mkdir()
    monkeypatch.setenv("ANNONA_HOME", str(h))
    return h


def app_with(pairing: Pairing | None) -> TestClient:
    app = FastAPI()
    app.add_middleware(PairedOriginMiddleware, pairing=pairing)

    @app.get("/api/kernel/status")
    def status():
        return {"enforcing": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


# ── The file ──────────────────────────────────────────────────────────────────


def test_no_pairing_file_means_not_paired(home):
    assert Pairing.load(pairing_path()) is None


def test_starting_the_daemon_does_not_mint_a_credential(home):
    """`create=False` is the default for a reason: a token appearing by itself
    is a token nobody decided to hand out."""
    Pairing.load(pairing_path())
    assert not pairing_path().exists()


def test_created_pairing_is_private_to_the_user(home):
    p = Pairing.create(pairing_path())
    assert p.token
    assert p.origins == DEFAULT_REMOTE_ORIGINS
    assert oct(p.path.stat().st_mode)[-3:] == "600"


def test_a_corrupt_pairing_file_is_not_silently_treated_as_absent(home):
    pairing_path().write_text("{ this is not json")
    with pytest.raises(ValueError):
        Pairing.load(pairing_path())


def test_rotation_replaces_the_token(home):
    first = Pairing.create(pairing_path()).token
    second = Pairing.create(pairing_path()).token
    assert first != second


# ── The boundary ──────────────────────────────────────────────────────────────


def test_the_local_window_is_untouched(home):
    """Same-origin calls carry no token and must keep working."""
    client = app_with(None)
    r = client.get("/api/kernel/status", headers={"Origin": "http://127.0.0.1:7070"})
    assert r.status_code == 200


def test_no_origin_header_is_the_cli_not_a_browser(home):
    assert app_with(None).get("/api/kernel/status").status_code == 200


def test_an_unpaired_machine_refuses_the_cloud_app(home):
    r = app_with(None).get("/api/kernel/status", headers={"Origin": CLOUD})
    assert r.status_code == 401
    assert "annona pair" in r.json()["detail"]


def test_a_listed_origin_without_a_token_is_refused(home):
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).get("/api/kernel/status", headers={"Origin": CLOUD})
    assert r.status_code == 401


def test_a_wrong_token_is_refused(home):
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).get(
        "/api/kernel/status", headers={"Origin": CLOUD, "X-Annona-Token": "not-it"}
    )
    assert r.status_code == 401


def test_an_unlisted_origin_holding_a_valid_token_is_still_refused(home):
    """A token that leaked must not turn every page into an executor."""
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).get(
        "/api/kernel/status", headers={"Origin": EVIL, "X-Annona-Token": pairing.token}
    )
    assert r.status_code == 401


def test_a_paired_origin_with_the_token_gets_through(home):
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).get(
        "/api/kernel/status", headers={"Origin": CLOUD, "X-Annona-Token": pairing.token}
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == CLOUD


def test_health_is_reachable_without_a_token_so_an_app_can_detect_the_daemon(home):
    """Discovery must not require the credential: a cloud app has to be able to
    say "Annona is running here, pair with it" before anyone has pasted one."""
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).get("/health", headers={"Origin": CLOUD})
    assert r.status_code == 200


# ── The preflight Chrome insists on ───────────────────────────────────────────


def test_preflight_from_a_listed_origin_allows_the_private_network(home):
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).options(
        "/api/kernel/ask",
        headers={
            "Origin": CLOUD,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-private-network"] == "true"
    assert "X-Annona-Token" in r.headers["access-control-allow-headers"]


def test_preflight_from_an_unlisted_origin_is_refused_outright(home):
    """The private-network header is a statement that this daemon accepts calls
    from a public page. It must never be said to a page the user did not list."""
    pairing = Pairing.create(pairing_path())
    r = app_with(pairing).options(
        "/api/kernel/ask",
        headers={
            "Origin": EVIL,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.status_code == 403
    assert "access-control-allow-private-network" not in r.headers


def test_a_custom_origin_list_replaces_the_default(home):
    pairing = Pairing.create(pairing_path(), origins=("https://studio.example",))
    client = app_with(pairing)
    assert (
        client.get(
            "/api/kernel/status", headers={"Origin": CLOUD, "X-Annona-Token": pairing.token}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/kernel/status",
            headers={"Origin": "https://studio.example", "X-Annona-Token": pairing.token},
        ).status_code
        == 200
    )


# ── The daemon's own interface, on whatever port ──────────────────────────────
#
# `annona run --port 7075` serves the interface from 7075, and the interface
# then calls the daemon at its own origin. With the local list hardcoded to
# 7070, those calls were treated as a stranger's and refused with "origin
# http://127.0.0.1:7075 is not paired with this machine" — advice to pair the
# daemon with itself, which `annona pair` cannot do.


def _served_on(port: int, *, peer: str = "127.0.0.1", pairing: Pairing | None = None) -> TestClient:
    """A client whose requests arrive at `port` from `peer`, as a browser's would."""
    app = FastAPI()
    app.add_middleware(PairedOriginMiddleware, pairing=pairing)

    @app.get("/api/kernel/status")
    def status():
        return {"enforcing": True}

    return TestClient(app, base_url=f"http://127.0.0.1:{port}", client=(peer, 54321))


def test_the_interface_reaches_the_daemon_that_served_it_on_any_port(home):
    client = _served_on(7075)
    r = client.get("/api/kernel/status", headers={"Origin": "http://127.0.0.1:7075"})
    assert r.status_code == 200


def test_localhost_and_127_are_the_same_machine(home):
    """Browsers do not agree on which one to put in the Origin header."""
    client = _served_on(7075)
    r = client.get("/api/kernel/status", headers={"Origin": "http://localhost:7075"})
    assert r.status_code == 200


def test_a_different_port_on_the_same_machine_is_still_a_stranger(home):
    """Another local server is another application, and pairs like any other."""
    client = _served_on(7075)
    r = client.get("/api/kernel/status", headers={"Origin": "http://127.0.0.1:9999"})
    assert r.status_code == 401


def test_a_remote_caller_cannot_claim_to_be_the_daemon_itself(home):
    """Origin and Host are the caller's to write; the peer address is not.

    Without this, exposing the daemon beyond loopback would turn the pairing
    gate into a matter of sending the right two headers.
    """
    client = _served_on(7075, peer="203.0.113.9")
    r = client.get("/api/kernel/status", headers={"Origin": "http://127.0.0.1:7075"})
    assert r.status_code == 401
