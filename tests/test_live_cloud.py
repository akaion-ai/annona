"""Live checks against the real Akaion gateway.

Skipped unless ``AKAION_LIVE=1``. CI stays hermetic — a test suite that fails
because someone else's deployment is rolling is a test suite people learn to
ignore — but the contract is checkable on demand:

```bash
AKAION_LIVE=1 env/bin/python -m pytest tests/test_live_cloud.py -v
```

These assert the *unauthenticated* half of the contract, which is the half that
can be verified without credentials and the half that catches the failure that
actually happened: the runner shipped a default base URL pointing at a host that
does not serve the API.

Set `AKAION_LIVE_TOKEN` to a Firebase ID token to exercise the authenticated
path as well.
"""

from __future__ import annotations

import os

import httpx
import pytest

from runner.cloud_client import MainBackendClient
from runner.service_urls import DEFAULT_API_BASE, resolve_service_url

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("AKAION_LIVE") != "1",
        reason="live cloud checks are opt-in: set AKAION_LIVE=1",
    ),
]

TIMEOUT = 15


class TestGatewayReachable:
    def test_the_shipped_default_serves_the_api(self):
        """The regression that prompted this file.

        `https://api.akaion.com` was the shipped default and failed the TLS
        handshake, so a fresh clone could not sign in. The correct gateway is
        `api.prod.akaion.com`. This test fails loudly if the default drifts back.
        """
        response = httpx.get(f"{DEFAULT_API_BASE}/health", timeout=TIMEOUT)

        assert response.status_code == 200

    def test_the_identity_service_is_up(self):
        response = httpx.get(f"{resolve_service_url('main')}/health", timeout=TIMEOUT)

        assert response.status_code == 200

    def test_the_vault_sync_service_is_up(self):
        response = httpx.get(f"{resolve_service_url('cot')}/health", timeout=TIMEOUT)

        assert response.status_code == 200


class TestContract:
    def test_an_unauthenticated_identity_call_is_refused(self):
        """A real API, not a catch-all: 401 with a structured body."""
        response = httpx.get(f"{resolve_service_url('main')}/api/v1/users/me", timeout=TIMEOUT)

        assert response.status_code == 401
        assert "json" in response.headers.get("content-type", "")
        assert response.json().get("detail")

    def test_a_bogus_token_is_refused(self):
        client = MainBackendClient(api_key="not-a-real-token")

        assert client.verify_auth() is False

    @pytest.mark.parametrize(
        ("service", "path"),
        [
            ("cot", "/api/v1/cloud/thoughts"),
            ("ai", "/api/v1/runner/agent/turn"),
        ],
    )
    def test_the_write_endpoints_exist_and_are_post_only(self, service: str, path: str):
        """405 on GET proves the route exists; 404 would mean it does not."""
        response = httpx.get(f"{resolve_service_url(service)}{path}", timeout=TIMEOUT)

        assert response.status_code == 405, (
            f"{service}{path} answered {response.status_code}; "
            "404 means the route is missing, 200 means we hit a catch-all"
        )


def _delete_thought(cot_url: str, message_id: str) -> None:
    """Remove a Thought this test wrote. Best-effort, never raises."""
    try:
        response = httpx.delete(
            f"{cot_url}/api/v1/cloud/messages/{message_id}",
            headers={"Authorization": f"Bearer {os.environ['AKAION_LIVE_TOKEN']}"},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            print(
                f"\n[live] could not delete test thought {message_id}: "
                f"HTTP {response.status_code} — remove it by hand"
            )
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail the test
        print(f"\n[live] could not delete test thought {message_id}: {exc} — remove it by hand")


class TestAuthenticated:
    """Requires a real Firebase ID token in AKAION_LIVE_TOKEN."""

    @pytest.fixture(autouse=True)
    def _token(self):
        token = os.getenv("AKAION_LIVE_TOKEN")
        if not token:
            pytest.skip("set AKAION_LIVE_TOKEN to exercise the authenticated path")
        return token

    def test_a_valid_token_verifies(self):
        client = MainBackendClient(api_key=os.environ["AKAION_LIVE_TOKEN"])

        assert client.verify_auth() is True

    def test_a_note_really_reaches_the_cloud(self, tmp_path, monkeypatch):
        """The one check the hermetic suite cannot make: a real round trip.

        `tests/test_e2e_operational.py` proves the chain against a socket we
        control, which catches every wiring mistake except the one that matters
        on launch day — the deployed COT rejecting a payload our fake accepts.

        **This writes a real Thought** into the account the token belongs to,
        and deletes it again in a `finally` block. A live test that leaves
        litter behind is one people stop running: after a dozen runs the
        operator's Second Brain is full of "Annona live check" and the cleanup
        becomes someone's afternoon. The cleanup is best-effort and never fails
        the test — a passing push with a failed delete is still a passing push,
        and it says so on stdout so the leftover can be removed by hand.
        """
        from runner.auth import AuthManager
        from runner.brain.manager import BrainManager
        from runner.sync.engine import SyncEngine

        # AuthManager reads AKAION_API_KEY before touching disk, so the live
        # token flows through the same code path the desktop app uses.
        monkeypatch.setenv("AKAION_API_KEY", os.environ["AKAION_LIVE_TOKEN"])

        cot_url = resolve_service_url("cot")
        brain_dir = tmp_path / "live-vault"
        brain_dir.mkdir()
        brain = BrainManager(brain_dir)
        message_id = None
        try:
            note = brain.create(
                title="Annona live check",
                content="Written by tests/test_live_cloud.py. Safe to delete.",
                tags=["annona-live-test"],
            )
            brain.mark_pending(note.id)

            engine = SyncEngine(
                brain=brain,
                cot_url=cot_url,
                auth=AuthManager(config_dir=tmp_path / ".annona"),
            )
            result = engine.push_pending()

            assert result == {
                "synced": 1,
                "errors": 0,
            }, f"the deployed COT refused the push: {brain.get(note.id).sync_error}"

            stored = brain.get(note.id)
            assert stored.sync_status == "synced"
            assert stored.cot_message_id, "the cloud accepted the note but returned no message id"
            message_id = stored.cot_message_id
        finally:
            if message_id:
                _delete_thought(cot_url, message_id)
            brain.close()
