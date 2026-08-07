"""Letting a web app in your browser use this machine as its executor.

Annona's daemon listens on loopback, which makes it reachable by the window it
ships with — and by every page open in your browser. That second sentence is the
whole problem this module exists for. Someone wants their cloud app
(``app.akaion.com``) to run steps on their own GPU; the moment the daemon accepts
cross-origin calls, so could any other site the browser happens to load.

So the rule is: **same-origin is trusted, everything else pairs.**

- The window Annona ships with, and the Vite dev server, call from a local
  origin and are unauthenticated. Nothing changes for them.
- Any other origin must be listed *and* present a token the user copied out of
  this machine. Listed-but-tokenless is refused, tokened-but-unlisted is refused.
- The token lives in ``$ANNONA_HOME/pairing.json``, mode 0600, generated on
  first use. It is not a password and is not recoverable: rotate by deleting it.

There is one browser-specific wrinkle. Chrome treats a request from a public
site to ``127.0.0.1`` as a Private Network Access request: it sends a preflight
carrying ``Access-Control-Request-Private-Network`` and refuses to proceed unless
the response says ``Access-Control-Allow-Private-Network: true``. That header is
answered here, for permitted origins only — it is a statement that this daemon
knowingly accepts calls from a public page, and it must never be sent to an
origin the user has not paired.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

__all__ = [
    "DEFAULT_REMOTE_ORIGINS",
    "LOCAL_ORIGINS",
    "PairedOriginMiddleware",
    "Pairing",
    "is_this_machine",
    "pairing_path",
]

LOCAL_ORIGINS = (
    "http://localhost:7070",
    "http://127.0.0.1:7070",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
)
"""Origins that are this application talking to itself.

Ports are listed because they are the ones this project ships on, but the list
is not the whole test — see :func:`_is_same_origin`. A hardcoded 7070 here meant
``annona run --port 7075`` served its own interface and then answered its own
fetches with "origin http://127.0.0.1:7075 is not paired with this machine",
telling the user to pair the daemon with itself.
"""

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


def is_this_machine(request: Request) -> bool:
    """Whether this request came from the daemon's own interface, not a paired app.

    Pairing lets a web app **run steps** here. It is not a grant to rewrite the
    policy those steps are governed by: an origin that could widen the perimeter
    and then run something under it would hold every permission the perimeter
    exists to withhold, and the user who pasted a token was not asked about that.

    So the routes that write the policy ask this instead of relying on the
    middleware, which by design lets a paired origin through.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        # No Origin header: the CLI, curl, a test client — already bounded by
        # the daemon listening on loopback.
        return True
    return origin in LOCAL_ORIGINS or _is_same_origin(origin, request)


def _is_same_origin(origin: str, request: Request) -> bool:
    """Whether this Origin is the daemon's own interface, on whatever port.

    A page the daemon served is not a cross-origin caller, and the daemon knows
    the address it was reached on: comparing against that costs nothing and does
    not have to be kept in step with a flag. Loopback-to-loopback is treated as
    the same origin because ``localhost`` and ``127.0.0.1`` are the same machine
    and browsers do not agree on which one to send.

    The socket has the last word. ``Origin`` and ``Host`` are both attacker-
    controlled on a daemon somebody has exposed beyond loopback, so agreeing
    with themselves proves nothing; the peer address does not lie. Without this,
    widening the port check would have turned a pairing gate into a header
    puzzle for anyone who could reach the port at all.
    """
    peer = (request.client.host if request.client else "") or ""
    if peer not in _LOOPBACK_HOSTS:
        return False

    try:
        from urllib.parse import urlsplit

        theirs = urlsplit(origin)
        ours = request.url
    except Exception:  # noqa: BLE001 - an unparseable Origin is not our own
        return False

    if theirs.scheme not in ("http", "https"):
        return False
    if theirs.port != (ours.port or (443 if ours.scheme == "https" else 80)):
        return False

    return (theirs.hostname or "") in _LOOPBACK_HOSTS and (ours.hostname or "") in _LOOPBACK_HOSTS


DEFAULT_REMOTE_ORIGINS = ("https://app.akaion.com",)
"""Pre-listed, never pre-authorised: still needs a token the user hands over."""

_TOKEN_HEADER = "x-annona-token"


def pairing_path(home: str | Path | None = None) -> Path:
    """Where the pairing file lives, beside the policy."""
    if home:
        return Path(home).expanduser() / "pairing.json"
    explicit = os.getenv("ANNONA_HOME")
    if explicit:
        return Path(explicit).expanduser() / "pairing.json"
    return Path.home() / ".annona" / "pairing.json"


@dataclass
class Pairing:
    """The token and the origins allowed to present it."""

    token: str
    origins: tuple[str, ...]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None, *, create: bool = False) -> Pairing | None:
        """Read the pairing file.

        Args:
            create: Generate one if absent. Off by default — starting a daemon
                must not silently mint a credential; the user asks for it with
                ``annona pair``.

        Returns:
            The pairing, or ``None`` when no file exists and ``create`` is off.
        """
        target = path or pairing_path()
        if target.exists():
            try:
                raw = json.loads(target.read_text())
                return cls(
                    token=str(raw["token"]),
                    origins=tuple(raw.get("origins") or DEFAULT_REMOTE_ORIGINS),
                    path=target,
                )
            except (OSError, ValueError, KeyError) as exc:
                # A corrupt pairing file must not be treated as "no pairing":
                # that would silently downgrade to refusing every remote call
                # while the user believes the machine is paired.
                logger.error(f"pairing file at {target} is unreadable: {exc}")
                raise

        if not create:
            return None
        return cls.create(target)

    @classmethod
    def create(cls, path: Path | None = None, origins: tuple[str, ...] | None = None) -> Pairing:
        """Mint a new token, replacing any existing one."""
        target = path or pairing_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        pairing = cls(
            token=secrets.token_urlsafe(32),
            origins=tuple(origins or DEFAULT_REMOTE_ORIGINS),
            path=target,
        )
        target.write_text(
            json.dumps({"token": pairing.token, "origins": list(pairing.origins)}, indent=2)
        )
        # Before anyone can read it: the window between creating a file and
        # restricting it is short, and on a shared machine it is enough.
        target.chmod(0o600)
        return pairing

    def permits(self, origin: str, token: str | None) -> bool:
        """Whether this origin, presenting this token, may call the kernel."""
        if origin not in self.origins:
            return False
        return bool(token) and secrets.compare_digest(token or "", self.token)


class PairedOriginMiddleware(BaseHTTPMiddleware):
    """Refuse cross-origin calls that have not paired, and answer their preflight.

    Sits in front of the CORS middleware rather than replacing it: CORS decides
    what a browser is *told* it may do, which is advice to well-behaved clients.
    This decides what the daemon actually serves, which is the part that matters
    when the client is not a browser at all.
    """

    def __init__(self, app, *, pairing: Pairing | None = None, protect: str = "/api/"):
        super().__init__(app)
        self._pairing = pairing
        self._protect = protect

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # No Origin header: curl, the CLI, a test client. Loopback binding is
        # the boundary for those, and it always was.
        if not origin or origin in LOCAL_ORIGINS or _is_same_origin(origin, request):
            return await call_next(request)

        pairing = self._pairing or Pairing.load()
        listed = bool(pairing and origin in pairing.origins)

        if request.method == "OPTIONS":
            # Answer the preflight ourselves so the Private Network Access
            # header can be conditional on the origin being listed.
            if not listed:
                return Response(status_code=403)
            headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Annona-Token",
                "Access-Control-Max-Age": "600",
            }
            if request.headers.get("access-control-request-private-network") == "true":
                headers["Access-Control-Allow-Private-Network"] = "true"
            return Response(status_code=204, headers=headers)

        if not request.url.path.startswith(self._protect):
            return await call_next(request)

        token = request.headers.get(_TOKEN_HEADER)
        if pairing is None or not pairing.permits(origin, token):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        f"origin {origin} is not paired with this machine. "
                        "Run `annona pair` here and paste the token into the app."
                    )
                },
                headers={"Access-Control-Allow-Origin": origin} if listed else None,
            )

        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = origin
        return response
