"""
Local API Server

FastAPI su 127.0.0.1:7070 — espone API + serve la web UI buildata da `ui/dist`.
Gira in un thread separato accanto al daemon di polling.

Endpoints:
  GET  /health
  GET  /api/auth/status
  POST /api/auth/save
  POST /api/auth/logout
  GET  /api/brain/notes
  POST /api/brain/notes
  GET  /api/brain/notes/{id}
  PATCH /api/brain/notes/{id}
  DELETE /api/brain/notes/{id}
  POST /api/brain/notes/{id}/mark-sync
  GET  /api/brain/search?q=...
  GET  /api/sync/status
  POST /api/sync/push
  POST /api/sync/push/{id}
  GET  /api/link/inbox
  GET  /api/link/inbox/{job_id}
  GET  /            → ui/dist/index.html (se la UI è stata buildata)
"""

import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel

from runner import __version__
from runner.audit.metrics import METRICS
from runner.services.host import sample as sample_host

from .auth import AuthManager
from .brain.manager import BrainManager
from .brain.models import Note
from .kernel_api import kernel_router
from .link import read_inbox
from .pairing import LOCAL_ORIGINS, PairedOriginMiddleware, is_this_machine
from .sync.engine import SyncEngine

# UI dist path: <runner-root>/ui/dist
_UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


# ── Pydantic I/O models ───────────────────────────────────────────────────────


class NoteCreate(BaseModel):
    title: str
    content: str = ""
    tags: List[str] = []


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None


class NoteOut(BaseModel):
    id: str
    title: str
    content: str
    tags: List[str]
    sync_status: str
    cot_message_id: Optional[str]
    cot_cluster_id: Optional[str]
    cot_cluster_name: Optional[str]
    created_at: datetime
    updated_at: datetime
    synced_at: Optional[datetime]
    sync_error: Optional[str]

    @classmethod
    def from_note(cls, n: Note) -> "NoteOut":
        return cls(**n.__dict__)


class SyncStatusOut(BaseModel):
    pending: int
    synced: int
    local_only: int
    errors: int
    last_push: Optional[datetime]


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(
    brain: BrainManager,
    sync: SyncEngine,
    auth: Optional[AuthManager] = None,
    cloud_enabled: bool = False,
    executor: Optional[object] = None,
) -> FastAPI:
    app = FastAPI(title="Annona local API", version=__version__)

    # Local origins are this app talking to itself and stay unauthenticated.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Anything else — a cloud app asking to use this machine as its executor —
    # must be listed *and* present the token from `annona pair`. Added after the
    # CORS middleware so it runs before it: Starlette applies middleware in
    # reverse order of registration, and an unpaired origin must be refused
    # rather than negotiated with.
    app.add_middleware(PairedOriginMiddleware)

    # The kernel's own surface: policy, substrates, ledger, and asking it
    # something. Registered before the static mount at "/", which swallows
    # anything registered after it.
    app.include_router(kernel_router(executor))

    _auth = auth or AuthManager()

    # ── Health ────────────────────────────────────────────────────────────────

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "annona", "version": __version__}

    # ── Metrics ───────────────────────────────────────────────────────────────

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(authorization: str = Header(default="")):
        """Prometheus text format. Numbers only: no path, prompt or person is a label.

        Bound with the API (loopback by default). A scraper on another host goes
        through the same tunnel or proxy, or presents ANNONA_METRICS_TOKEN.
        """
        token = os.getenv("ANNONA_METRICS_TOKEN", "")
        if token and not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(status_code=401, detail="metrics token required")
        sample_host()
        return PlainTextResponse(METRICS.prometheus(), media_type="text/plain; version=0.0.4")

    # ── Auth ──────────────────────────────────────────────────────────────────

    @app.get("/api/auth/status")
    def auth_status():
        """Whether the runner is signed in. Always 200 — never blocks the UI."""
        authenticated = _auth.is_authenticated()
        return {
            "authenticated": authenticated,
            "email": _auth.get_email() if authenticated else None,
            "runner_id": _auth.get_runner_id() if authenticated else None,
            "mode": "cloud" if authenticated else "local",
        }

    # ── Runner mode ───────────────────────────────────────────────────────────

    @app.get("/api/runner/mode")
    def runner_mode():
        """
        Modalità attuale del runner (la UI la usa per decidere cosa mostrare
        in the sidebar: a "Local" versus "Synced" badge).
        """
        authenticated = _auth.is_authenticated()
        return {
            "mode": "cloud" if (cloud_enabled and authenticated) else "local",
            "cloud_enabled": cloud_enabled,
            "authenticated": authenticated,
            "vault_path": str(brain.brain_dir) if hasattr(brain, "brain_dir") else "~/akaion-brain",
        }

    class AuthSaveRequest(BaseModel):
        firebase_token: str
        refresh_token: str
        expires_in: int = 3600
        email: Optional[str] = None

    @app.post("/api/auth/save")
    def auth_save(body: AuthSaveRequest):
        """
        Store the Firebase credentials produced by the in-app sign-in.
        Called by the Tauri UI after signing in with the Firebase JS SDK.
        """
        try:
            _auth.save_credentials(
                firebase_token=body.firebase_token,
                refresh_token=body.refresh_token,
                expires_in=body.expires_in,
                email=body.email,
            )
            return {
                "authenticated": True,
                "email": body.email,
                "runner_id": _auth.get_runner_id(),
            }
        except Exception as e:
            raise HTTPException(500, f"Failed to save credentials: {e}")

    @app.post("/api/auth/logout")
    def auth_logout():
        """Remove the stored credentials."""
        _auth.clear_credentials()
        return {"authenticated": False}

    # ── Brain: Notes ──────────────────────────────────────────────────────────

    @app.get("/api/brain/notes", response_model=List[NoteOut])
    def list_notes(
        sync_status: Optional[str] = Query(None),
        tag: Optional[str] = Query(None),
        limit: int = Query(100, le=500),
        offset: int = Query(0, ge=0),
    ):
        notes = brain.list(sync_status=sync_status, tag=tag, limit=limit, offset=offset)
        return [NoteOut.from_note(n) for n in notes]

    @app.post("/api/brain/notes", response_model=NoteOut, status_code=201)
    def create_note(body: NoteCreate):
        note = brain.create(title=body.title, content=body.content, tags=body.tags)
        return NoteOut.from_note(note)

    @app.get("/api/brain/notes/{note_id}", response_model=NoteOut)
    def get_note(note_id: str):
        note = brain.get(note_id)
        if not note:
            raise HTTPException(404, "Note not found")
        return NoteOut.from_note(note)

    @app.patch("/api/brain/notes/{note_id}", response_model=NoteOut)
    def update_note(note_id: str, body: NoteUpdate):
        note = brain.update(
            note_id,
            title=body.title,
            content=body.content,
            tags=body.tags,
        )
        if not note:
            raise HTTPException(404, "Note not found")
        return NoteOut.from_note(note)

    @app.delete("/api/brain/notes/{note_id}", status_code=204)
    def delete_note(note_id: str):
        if not brain.delete(note_id):
            raise HTTPException(404, "Note not found")

    @app.post("/api/brain/notes/{note_id}/mark-sync", response_model=NoteOut)
    def mark_note_for_sync(note_id: str):
        """Mark the note pending_sync so the next push sends it."""
        if not brain.mark_pending(note_id):
            raise HTTPException(404, "Note not found or already pending")
        note = brain.get(note_id)
        return NoteOut.from_note(note)

    @app.get("/api/brain/search", response_model=List[NoteOut])
    def search_notes(q: str = Query(..., min_length=1), limit: int = Query(20, le=100)):
        notes = brain.search(q, limit=limit)
        return [NoteOut.from_note(n) for n in notes]

    # ── Sync ──────────────────────────────────────────────────────────────────

    @app.get("/api/sync/status", response_model=SyncStatusOut)
    def sync_status():
        stats = brain.stats()
        return SyncStatusOut(
            pending=stats.pending,
            synced=stats.synced,
            local_only=stats.local_only,
            errors=stats.errors,
            last_push=stats.last_push,
        )

    @app.post("/api/sync/push")
    def sync_push():
        """Push every pending note to the cloud."""
        return sync.push_pending()

    @app.post("/api/sync/push/{note_id}")
    def sync_push_one(note_id: str):
        """Push a single note, regardless of its current state."""
        ok = sync.push_note(note_id)
        if not ok:
            raise HTTPException(400, "Sync failed — controlla i log")
        note = brain.get(note_id)
        return NoteOut.from_note(note)

    # ── Link inbox ────────────────────────────────────────────────────────────
    # Answers this machine's policy kept from Agents Studio. Handing them to a
    # paired web app would release them by another door, so only the window on
    # this machine may read them — the same rule as writing the policy.

    def _only_this_machine(request: Request) -> None:
        if not is_this_machine(request):
            raise HTTPException(403, "withheld answers can only be read on this machine")

    @app.get("/api/link/inbox")
    def link_inbox(request: Request):
        """The withheld answers, newest first — who asked and why, not what."""
        _only_this_machine(request)
        return [
            {
                "job_id": it["job_id"],
                "title": it.get("title", ""),
                "requested_by": (it.get("requested_by") or {}).get("email", ""),
                "skill": it.get("skill"),
                "release": it.get("release", ""),
                "placement_class": (it.get("placement") or {}).get("class", ""),
                "received": it["received"],
            }
            for it in read_inbox()
        ]

    @app.get("/api/link/inbox/{job_id}")
    def link_inbox_item(job_id: str, request: Request):
        """One withheld answer in full. Looked up in the listing, never opened by name."""
        _only_this_machine(request)
        for it in read_inbox():
            if it["job_id"] == job_id:
                return it
        raise HTTPException(404, "No withheld answer with that id")

    # ── Static UI mount ────────────────────────────────────────────────────────
    # Must be LAST: it's mounted at "/" with html=True so it would otherwise
    # intercept /api/* routes registered above. FastAPI walks routes in order
    # and mounts are last-priority anyway, but we register it here to be explicit.
    _mount_ui(app)

    return app


class _UIFiles(StaticFiles):
    """The built UI, with ``index.html`` revalidated on every load.

    ``index.html`` names the hashed bundle. Served without a cache header, a
    browser keeps it by heuristic and the window stays on the previous release
    after an update. The hashed assets themselves can be cached freely.
    """

    def file_response(self, full_path: Any, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        if str(full_path).endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def _mount_ui(app: FastAPI) -> None:
    """Mount the built React UI at `/`. Skip-with-warning if dist isn't built yet."""
    index_html = _UI_DIST / "index.html"
    if not index_html.exists():
        logger.warning(
            f"UI not built at {_UI_DIST}. Run `npm run build` in ui/ "
            f"or use start.sh (auto-builds on first run)."
        )
        return
    app.mount("/", _UIFiles(directory=str(_UI_DIST), html=True), name="ui")
    logger.info(f"UI mounted at / from {_UI_DIST}")


# ── Runner del server in thread separato ─────────────────────────────────────


class LocalAPIServer:
    """Run FastAPI in a daemon thread alongside the polling loop."""

    def __init__(
        self,
        brain: BrainManager,
        sync: SyncEngine,
        auth: Optional[AuthManager] = None,
        port: int = 7070,
        cloud_enabled: bool = False,
        host: Optional[str] = None,
        executor: Optional[object] = None,
    ):
        self.brain = brain
        self.sync = sync
        self.auth = auth
        self.executor = executor
        self.port = port
        self.cloud_enabled = cloud_enabled
        # Loopback everywhere except in a container, where loopback would mean
        # "reachable by nothing". ANNONA_BIND is set by the image, never by a
        # default, so a laptop install cannot start listening on the LAN by
        # accident — the daemon opens no port to the world unless asked.
        self.host = host or os.getenv("ANNONA_BIND", "127.0.0.1")
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None

    def start(self):
        app = create_app(
            self.brain,
            self.sync,
            self.auth,
            cloud_enabled=self.cloud_enabled,
            executor=self.executor,
        )
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",  # silenzia i log HTTP nel terminale del runner
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="annona-local-api",
        )
        self._thread.start()
        # Piccola attesa per lasciar partire uvicorn
        import time

        time.sleep(0.5)
        logger.info(f"Local API ready on http://127.0.0.1:{self.port}")

    def stop(self):
        if self._server:
            self._server.should_exit = True
