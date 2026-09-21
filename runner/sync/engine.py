"""
Sync Engine

Push and pull between the local vault and the cloud.

Design:
- PUSH: note con sync_status=pending_sync → POST /api/v1/cloud/thoughts
- PULL: GET /api/v1/cloud/messages and /clusters -> refresh local cluster_info
- Not everything is synced: local_only notes stay local until
  mark_pending() or an explicit push is called.

Local-first: every method checks auth BEFORE building an httpx client.
With no credentials sync is a silent no-op, never a `Bearer None` request.
"""

from typing import TypedDict

import httpx
from loguru import logger

from runner.auth import AuthManager
from runner.brain.manager import BrainManager
from runner.brain.models import SYNC_PENDING, Note

# Transient network errors: the note stays pending_sync and is retried
_NETWORK_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.NetworkError,
    OSError,  # include DNS failures (socket.gaierror è sottoclasse di OSError)
)


class SyncError(Exception):
    """A sync-side failure: missing auth, invalid payload, and so on."""


class _PushCounts(TypedDict):
    synced: int
    errors: int


class PushResult(_PushCounts, total=False):
    """Body of POST /api/sync/push. `error` is present only on a skipped push."""

    error: str


class SyncEngine:
    """Two-way sync with the cloud."""

    def __init__(
        self,
        brain: BrainManager,
        cot_url: str,
        auth: AuthManager,
        timeout: int = 30,
    ):
        self.brain = brain
        self.cot_url = cot_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        """Costruisce un httpx.Client autenticato. Solleva SyncError se manca il token."""
        token = self.auth.get_firebase_token()
        if not token:
            raise SyncError("not_authenticated")
        return httpx.Client(
            base_url=self.cot_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    # ── Push ──────────────────────────────────────────────────────────────────

    def push_pending(self) -> "PushResult":
        """
        Send every note with sync_status=pending_sync to the cloud.
        Returns {"synced": N, "errors": M, "error"?: "not_authenticated"}.
        """
        pending = self.brain.list(sync_status=SYNC_PENDING)
        if not pending:
            logger.info("Sync push: nothing pending")
            return {"synced": 0, "errors": 0}

        logger.info(f"Sync push: {len(pending)} note da inviare")
        synced = errors = 0

        try:
            with self._client() as client:
                for note in pending:
                    result = self._push_note(client, note)
                    if result:
                        synced += 1
                    else:
                        errors += 1
        except SyncError as e:
            logger.warning(f"Sync push skipped: {e}")
            return {"synced": 0, "errors": 0, "error": str(e)}

        logger.info(f"Sync push complete: {synced} ok, {errors} failed")
        return {"synced": synced, "errors": errors}

    def push_note(self, note_id: str) -> bool:
        """Push a single note, regardless of its current state."""
        note = self.brain.get(note_id)
        if not note:
            return False
        try:
            with self._client() as client:
                return self._push_note(client, note)
        except SyncError as e:
            logger.warning(f"Sync push skipped [{note_id}]: {e}")
            return False

    def _push_note(self, client: httpx.Client, note: Note) -> bool:
        try:
            payload = {
                "content": f"# {note.title}\n\n{note.content}",
                "metadata": {
                    "title": note.title,
                    "source": "local_runner",
                    "local_id": note.id,
                    "tags": note.tags,
                },
                "hint_tags": note.tags,
                "enable_embeddings": True,
            }

            resp = client.post("/api/v1/cloud/thoughts", json=payload)

            if resp.status_code in (200, 201):
                data = resp.json()
                # The cloud returns a message_id and optionally a cluster
                cot_message_id = data.get("message_id") or data.get("id")
                cot_cluster_id = data.get("cluster_id")
                cot_cluster_name = data.get("cluster_name")

                self.brain.mark_synced(
                    note.id,
                    cot_message_id=str(cot_message_id),
                    cot_cluster_id=str(cot_cluster_id) if cot_cluster_id else None,
                    cot_cluster_name=cot_cluster_name,
                )
                logger.debug(f"Note synced: {note.id} → COT {cot_message_id}")
                return True
            else:
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self.brain.mark_sync_error(note.id, error)
                logger.warning(f"Note sync failed [{note.id}]: {error}")
                return False

        except _NETWORK_ERRORS as e:
            # Transient network error -> leave it pending_sync and retry later
            logger.warning(
                f"Note sync offline [{note.id}]: {type(e).__name__} — rete non disponibile"
            )
            return False
        except Exception as e:
            # Permanent error (auth, payload, ...) -> mark sync_error
            self.brain.mark_sync_error(note.id, str(e))
            logger.error(f"Note sync error [{note.id}]: {e}")
            return False
