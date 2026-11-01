"""
Authentication Manager

Firebase authentication for the runner.

Due metodi supportati:
  1. Email + Password  → Firebase REST sign-in
  2. Google OAuth      -> browser popup + a local server, like `gcloud auth login`

In entrambi i casi il risultato è un Firebase ID token usato come Bearer.
Tokens are refreshed automatically via the refresh token when they expire (~1h).
"""

import functools
import json
import os
import socket
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import httpx
from cryptography.fernet import Fernet

# Firebase Web SDK API key for the default Akaion project (akaion-app-213b6).
# This is NOT a secret — Firebase Web API keys are designed to be exposed
# client-side. They identify the Firebase project so the SDK can route the
# request; actual user-level auth happens via OAuth/ID token flows.
# See: https://firebase.google.com/docs/projects/api-keys
# Override only if you want to point the runner at a different Firebase
# project (e.g. self-hosted Akaion deployment).
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "AIzaSyA5RDLqm4zCJnpmYz_Ms15wgXLDEmaTiy0")
FIREBASE_SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"

FIREBASE_CONFIG = {
    "apiKey": FIREBASE_API_KEY,
    "authDomain": "akaion-app-213b6.firebaseapp.com",
    "projectId": "akaion-app-213b6",
}


def _get_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def firebase_browser_login(timeout: int = 120) -> dict:
    """
    Apre il browser per il login Google via Firebase.
    Starts a small local HTTP server that captures the token after the popup.
    Returns a dict with idToken, refreshToken and email.
    (Come `gcloud auth login`)
    """
    import json as _json

    port = _get_free_port()
    result: dict = {}
    config_js = _json.dumps(FIREBASE_CONFIG)

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Annona</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background: #080c10;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; color: #e2e8f0;
    }}
    .card {{
      width: 340px; padding: 40px 36px; border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.07);
      background: rgba(15,20,28,0.95);
      display: flex; flex-direction: column; align-items: center; gap: 24px;
    }}
    .logo {{
      font-size: 26px; font-weight: 700; letter-spacing: -0.5px;
      background: linear-gradient(90deg, #00d9ff, #3b82f6);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }}
    .sub {{ font-size: 13px; color: #475569; text-align: center; line-height: 1.6; }}
    .btn {{
      width: 100%; padding: 11px 0; border-radius: 8px; border: none;
      background: #fff; color: #0f172a;
      font-size: 14px; font-weight: 600; cursor: pointer;
      display: flex; align-items: center; justify-content: center; gap: 10px;
      transition: opacity .15s;
    }}
    .btn:hover {{ opacity: .88; }}
    .btn:disabled {{ opacity: .35; cursor: not-allowed; }}
    .status {{ font-size: 12px; color: #475569; min-height: 16px; text-align: center; }}
    .status.ok  {{ color: #34d399; }}
    .status.err {{ color: #f87171; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="logo">Annona</span>
    <p class="sub">Connect your local runner<br>to Akaion Cloud.</p>
    <button class="btn" id="btn">
      <svg width="17" height="17" viewBox="0 0 18 18" fill="none">
        <path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844a4.14 4.14 0 0 1-1.796 2.716v2.259h2.908C16.658 14.251 17.64 11.943 17.64 9.2Z"/>
        <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.184l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18Z"/>
        <path fill="#FBBC05" d="M3.964 10.706A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.706V4.962H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.038l3.007-2.332Z"/>
        <path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.962L3.964 7.294C4.672 5.163 6.656 3.58 9 3.58Z"/>
      </svg>
      Continua con Google
    </button>
    <p class="status" id="status"></p>
  </div>

  <script type="module">
    import {{ initializeApp }}  from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js';
    import {{ getAuth, signInWithPopup, GoogleAuthProvider }}
      from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js';

    const app      = initializeApp({config_js});
    const auth     = getAuth(app);
    const provider = new GoogleAuthProvider();
    provider.addScope('email');
    provider.addScope('profile');
    provider.setCustomParameters({{ prompt: 'select_account' }});

    const btn    = document.getElementById('btn');
    const status = document.getElementById('status');

    btn.addEventListener('click', async () => {{
      btn.disabled = true;
      status.textContent = 'Apertura popup...';
      try {{
        const cred    = await signInWithPopup(auth, provider);
        const idToken = await cred.user.getIdToken();
        status.textContent = 'Connessione al runner...';
        await fetch('/callback', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ idToken, refreshToken: cred.user.refreshToken, email: cred.user.email }}),
        }});
        status.className = 'status ok';
        status.textContent = '✓ Signed in — you can close this tab.';
      }} catch (e) {{
        status.className = 'status err';
        status.textContent = e.message;
        btn.disabled = false;
      }}
    }});
  </script>
</body>
</html>"""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silenzia il log HTTP
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            result.update(_json.loads(body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
            # Shut the server down once the response is out
            threading.Thread(target=server.shutdown, daemon=True).start()

    server = HTTPServer(("localhost", port), _Handler)
    server.timeout = timeout

    url = f"http://localhost:{port}"
    webbrowser.open(url)

    # Serve fino a che non arriva il token o scade il timeout
    server.serve_forever()

    if not result.get("idToken"):
        raise TimeoutError("Sign-in timed out: no response within the time limit")

    return result


class AuthManager:
    """Firebase authentication for the runner."""

    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            override = os.getenv("AKAION_HOME")
            config_dir = Path(override).expanduser() if override else Path.home() / ".akaion"
        # Nothing is created here: `status` and `doctor` build an AuthManager to
        # ask whether one is signed in, and asking must not write (#42). The
        # directory and the key appear the first time something is encrypted.
        self.config_dir = config_dir
        self.auth_file = self.config_dir / "auth.json"
        self.key_file = self.config_dir / ".key"

    @functools.cached_property
    def cipher(self) -> Fernet:
        if not self.key_file.exists():
            self.config_dir.mkdir(parents=True, exist_ok=True)
            # Created 0600, not chmod-ed after: no moment where it is readable.
            try:
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # another process won the race; use its key
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
        return Fernet(self.key_file.read_bytes())

    def _encrypt(self, data: str) -> str:
        return self.cipher.encrypt(data.encode()).decode()

    def _decrypt(self, data: str) -> str:
        return self.cipher.decrypt(data.encode()).decode()

    # ────────────────────────────────────────────
    # Firebase REST helpers
    # ────────────────────────────────────────────

    @staticmethod
    def firebase_sign_in(email: str, password: str) -> dict:
        """
        Autentica con Firebase REST API.
        Returns a dict with idToken, refreshToken and expiresIn.
        """
        resp = httpx.post(
            FIREBASE_SIGN_IN_URL,
            params={"key": FIREBASE_API_KEY},
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=15,
        )
        if resp.status_code != 200:
            msg = resp.json().get("error", {}).get("message", "Authentication failed")
            # Messaggi Firebase più leggibili
            friendly = {
                "EMAIL_NOT_FOUND": "Email not found",
                "INVALID_PASSWORD": "Password errata",
                "USER_DISABLED": "Account disabled",
                "INVALID_LOGIN_CREDENTIALS": "Credenziali non valide",
            }
            raise ValueError(friendly.get(msg, msg))
        return resp.json()

    @staticmethod
    def _firebase_refresh(refresh_token: str) -> dict:
        """Renew the Firebase ID token using the refresh token."""
        resp = httpx.post(
            FIREBASE_REFRESH_URL,
            params={"key": FIREBASE_API_KEY},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError("Token refresh failed")
        return resp.json()  # id_token, refresh_token, expires_in

    # ────────────────────────────────────────────
    # Credential storage
    # ────────────────────────────────────────────

    def save_credentials(
        self,
        firebase_token: str,
        refresh_token: str,
        expires_in: int = 3600,
        email: Optional[str] = None,
    ):
        """Write the Firebase credentials to disk, encrypted."""
        runner_id = self.get_runner_id() or str(uuid.uuid4())
        auth_data = {
            "firebase_token": self._encrypt(firebase_token),
            "refresh_token": self._encrypt(refresh_token),
            "expires_at": int(time.time()) + int(expires_in) - 60,  # 1 min buffer
            "runner_id": runner_id,
            "email": email or "",
        }
        with open(self.auth_file, "w") as f:
            json.dump(auth_data, f, indent=2)
        self.auth_file.chmod(0o600)

    # ────────────────────────────────────────────
    # Token retrieval (con auto-refresh)
    # ────────────────────────────────────────────

    def get_firebase_token(self) -> Optional[str]:
        """
        Returns a valid Firebase ID token.
        Refreshes it automatically if it has expired.
        """
        # Token da env (dev/override, nessun auto-refresh)
        env_token = os.getenv("AKAION_API_KEY")
        if env_token:
            return env_token

        if not self.auth_file.exists():
            return None

        try:
            with open(self.auth_file) as f:
                auth_data = json.load(f)

            expires_at = auth_data.get("expires_at", 0)

            # Still valid
            if time.time() < expires_at:
                return self._decrypt(auth_data["firebase_token"])

            # Expired -> refresh
            refresh_token = self._decrypt(auth_data["refresh_token"])
            refreshed = self._firebase_refresh(refresh_token)
            new_token = refreshed["id_token"]
            new_refresh = refreshed["refresh_token"]
            new_expires = int(refreshed.get("expires_in", 3600))

            self.save_credentials(
                new_token,
                new_refresh,
                new_expires,
                email=auth_data.get("email"),
            )
            return new_token

        except Exception:
            return None

    def get_api_key(self) -> Optional[str]:
        """Alias di get_firebase_token() per compatibilità."""
        return self.get_firebase_token()

    def get_runner_id(self) -> Optional[str]:
        env_id = os.getenv("AKAION_RUNNER_ID")
        if env_id:
            return env_id
        if not self.auth_file.exists():
            return None
        try:
            with open(self.auth_file) as f:
                return json.load(f).get("runner_id")
        except Exception:
            return None

    def get_email(self) -> Optional[str]:
        if not self.auth_file.exists():
            return None
        try:
            with open(self.auth_file) as f:
                return json.load(f).get("email", "")
        except Exception:
            return None

    def is_authenticated(self) -> bool:
        return self.get_firebase_token() is not None

    def clear_credentials(self):
        if self.auth_file.exists():
            self.auth_file.unlink()
