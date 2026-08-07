#!/usr/bin/env python3
"""
Annona CLI

Main entry point for managing the local runner.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

import typer
from dotenv import load_dotenv
from rich import print as rprint
from rich.console import Console
from rich.table import Table

load_dotenv()

# Imported after load_dotenv() on purpose: these modules resolve service URLs and
# configuration at import time, so the environment has to be populated first.
# Moving them above load_dotenv() changes which backend the CLI talks to.
from runner import branding  # noqa: E402
from runner.auth import AuthManager, firebase_browser_login  # noqa: E402
from runner.banner import print_simple_logo  # noqa: E402
from runner.brain.manager import BrainManager  # noqa: E402
from runner.brain.models import (  # noqa: E402
    SYNC_ERROR,
    SYNC_LOCAL_ONLY,
    SYNC_PENDING,
    SYNC_SYNCED,
    Note,
)
from runner.cloud_client import MainBackendClient  # noqa: E402
from runner.config import ConfigManager  # noqa: E402
from runner.main import RunnerDaemon  # noqa: E402
from runner.service_urls import resolve_service_url  # noqa: E402
from runner.sync.engine import SyncEngine  # noqa: E402

app = typer.Typer(
    name="annona",
    help=f"🛃 {branding.NAME} — {branding.TAGLINE}",
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()


@app.command()
def login(
    email: Optional[str] = typer.Option(
        None, "--email", "-e", help="Sign in with email and password instead of Google"
    ),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", help="Password (only with --email)"
    ),
):
    """
    🔐 Authenticate the runner against Akaion Cloud

    By default this opens a browser for Google sign-in, like `gcloud auth login`.
    Use --email to sign in with email and password instead.
    """
    try:
        auth_manager = AuthManager()
        backend_url = resolve_service_url("main")

        # ─── Branch: email and password ───
        if email:
            if not password:
                password = typer.prompt("Password", hide_input=True)
            console.print("🔐 Signing in with email/password...")
            try:
                resp = AuthManager.firebase_sign_in(email, password)
            except ValueError as e:
                console.print(f"❌ [red]{e}[/red]")
                raise typer.Exit(1)
            firebase_token = resp["idToken"]
            refresh_token = resp["refreshToken"]
            expires_in = int(resp.get("expiresIn", 3600))

        # ─── Branch: Google OAuth (browser) ───
        else:
            console.print("🌐 Opening the browser for Google sign-in...")
            console.print("   Finish signing in there, then come back.")
            try:
                result = firebase_browser_login(timeout=120)
            except TimeoutError:
                console.print("❌ [red]Timed out: sign-in was not completed within 2 minutes[/red]")
                raise typer.Exit(1)
            firebase_token = result["idToken"]
            refresh_token = result["refreshToken"]
            email = result.get("email", "")
            expires_in = 3600

        # ─── Sync with the Akaion backend ───
        console.print("🔍 Syncing with Akaion backend...")
        cloud_client = MainBackendClient(api_key=firebase_token, base_url=backend_url)
        try:
            sync_resp = cloud_client.client.post(
                "/api/v1/auth/firebase/verify",
                json={"firebase_token": firebase_token, "provider": "google.com"},
            )
            if sync_resp.status_code not in (200, 201):
                console.print(f"⚠️  [yellow]Backend sync warning ({sync_resp.status_code})[/yellow]")
        except Exception as e:
            console.print(f"⚠️  [yellow]Backend sync skipped: {e}[/yellow]")

        # ─── Store credentials ───
        auth_manager.save_credentials(firebase_token, refresh_token, expires_in, email=email)

        console.print("✅ [green]Successfully authenticated![/green]")
        if email:
            console.print(f"   Email:     [cyan]{email}[/cyan]")
        console.print(f"   Runner ID: [cyan]{auth_manager.get_runner_id()}[/cyan]")

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error during login: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def init(
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", "-i/-n", help="Interactive setup"
    ),
):
    """
    🔧 Create the runner configuration, then the policy (same as `annona setup`)
    """
    try:
        config_manager = ConfigManager()

        # No terminal means no answers. Falling through to `typer.prompt` in a
        # container or a CI job raises on the first question, which is how a
        # provisioning script discovers that setup is interactive.
        if interactive and not sys.stdin.isatty():
            console.print("⚙️  [dim]No terminal — writing the default configuration.[/dim]")
            interactive = False

        if interactive:
            console.print("🔧 [bold]Annona Setup[/bold]\n")

            # AI Provider
            ai_provider = typer.prompt(
                "AI Provider (akaion/openai/anthropic/google/local)", default="akaion"
            )

            # Permissions
            console.print("\n📂 [bold]Filesystem Permissions[/bold]")
            allowed_paths = typer.prompt(
                "Allowed paths (comma-separated)", default="~/Documents,~/Downloads"
            )

            shell_enabled = typer.confirm("Enable shell commands?", default=True)

            # Build the config
            config_data = {
                "ai": {"provider": ai_provider},
                "permissions": {
                    "filesystem": {"allowed_paths": [p.strip() for p in allowed_paths.split(",")]},
                    "shell": {"enabled": shell_enabled},
                },
            }

            config_manager.create_config(config_data)
        else:
            config_manager.create_default_config()

        console.print(f"  config    [green]written[/green] [dim]{config_manager.config_path}[/dim]")

        # And then the part that used to be missing. `init` reported success
        # after writing this one file, but the config is not what the kernel
        # reads: every placement, every refusal and every tool call derives from
        # ~/.annona/policy.yaml, which only `annona policy init` wrote. So a
        # first install ended on a green tick and answered the next question
        # with "no policy". Whatever else `init` asks, it now leaves the machine
        # in the state its own success message claims.
        from runner.cli_setup import ensure_policy

        ensure_policy()

    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error during initialization: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def run(
    daemon: bool = typer.Option(
        True,
        "--daemon/--once",
        "-d/-o",
        help="Run as a daemon (long-lived UI server) or execute a single task",
    ),
    dev: bool = typer.Option(False, "--dev", help="Development mode with verbose logging"),
    task: Optional[str] = typer.Option(
        None, "--task", "-t", help="Execute specific task (--once mode)"
    ),
    port: int = typer.Option(7070, "--port", help="Port for the local API server"),
    brain_dir: Optional[Path] = typer.Option(
        None, "--brain-dir", help="Vault directory (default: ~/akaion-brain)"
    ),
    no_cloud: bool = typer.Option(
        False, "--no-cloud", help="Force local-only mode for this run, overriding cloud.enabled"
    ),
):
    """
    🚀 Start the runner
    """
    try:
        # Authentication is not required: local-first by design.
        auth_manager = AuthManager()
        if not auth_manager.is_authenticated():
            console.print("ℹ️  [dim]Not signed in — starting in local-only mode.[/dim]")
            # `--port` moves the server; printing 7070 regardless sent anybody
            # who used the flag to a page that was not there.
            console.print(f"   Open http://127.0.0.1:{port} to use the local vault.")
            console.print("   To sync to the cloud, use the sidebar action in the UI,")
            console.print("   or run [cyan]annona login[/cyan].")

        # Load the config, writing the defaults if there is none.
        #
        # A first start used to fail here with "run `annona init` first", and
        # `annona init` is interactive — which meant the daemon could not come
        # up unattended: in a container there is no TTY, so the documented
        # `docker compose up -d` exited immediately and the appliance never
        # started. The defaults are the same ones `init` would write without
        # being asked anything, and they are safe: local-only, cloud off, three
        # read-only tools. Saying so on stdout keeps it from being a surprise.
        config_manager = ConfigManager()
        if not config_manager.config_exists():
            config_manager.create_default_config()
            console.print(
                f"⚙️  [dim]No configuration at {config_manager.config_path}; "
                "wrote the defaults (local-only, cloud off). "
                "Run [cyan]annona init[/cyan] to change them.[/dim]"
            )

        config = config_manager.load_config()

        # --no-cloud override, not persisted
        if no_cloud:
            config.setdefault("cloud", {})["enabled"] = False
            console.print("⚙️  [dim]--no-cloud: cloud sync disabled for this run[/dim]")

        # Build and start the runner
        runner = RunnerDaemon(config, dev_mode=dev, brain_dir=brain_dir, local_port=port)

        if daemon:
            console.print("🚀 [green]Starting the Annona daemon…[/green]")
            console.print(f"Runner ID: [cyan]{auth_manager.get_runner_id()}[/cyan]")
            console.print("\nPress Ctrl+C to stop\n")
            runner.start_daemon()
        else:
            if not task:
                console.print("❌ [red]--once requires --task <description>[/red]")
                raise typer.Exit(2)
            console.print(f"🎯 [green]Executing task:[/green] {task}")
            result = runner.execute_once(task)
            console.print(f"\n✅ Result: {result}")

    except KeyboardInterrupt:
        console.print("\n👋 [yellow]Runner stopped by user[/yellow]")
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detailed status"),
):
    """
    📊 Show runner status
    """
    try:
        auth_manager = AuthManager()
        config_manager = ConfigManager()

        # Table
        table = Table(title="🚀 Annona Status")
        table.add_column("Component", style="cyan")
        table.add_column("Status", style="green")
        table.add_column("Details", style="white")

        # Authentication
        auth_status = (
            "✅ Authenticated" if auth_manager.is_authenticated() else "❌ Not authenticated"
        )
        runner_id = auth_manager.get_runner_id() if auth_manager.is_authenticated() else "N/A"
        table.add_row("Authentication", auth_status, runner_id)

        # Configuration
        config_status = "✅ Configured" if config_manager.config_exists() else "❌ Not configured"
        config_path = str(config_manager.config_path) if config_manager.config_exists() else "N/A"
        table.add_row("Configuration", config_status, config_path)

        # Cloud connection
        if auth_manager.is_authenticated():
            cloud_client = MainBackendClient(api_key=auth_manager.get_api_key())
            cloud_status = "✅ Connected" if cloud_client.health_check() else "❌ Disconnected"
            table.add_row("Cloud Connection", cloud_status, cloud_client.base_url)

        console.print(table)

        if verbose and config_manager.config_exists():
            console.print("\n[bold]Configuration:[/bold]")
            config = config_manager.load_config()
            rprint(config)

    except Exception as e:
        console.print(f"❌ [red]Error checking status: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def logs(
    tail: int = typer.Option(50, "--tail", "-n", help="Number of lines to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
):
    """
    📜 Show runner logs
    """
    try:
        log_file = Path("logs/runner.log")

        if not log_file.exists():
            console.print("❌ [red]No log file found[/red]")
            raise typer.Exit(1)

        if follow:
            console.print(f"📜 Following {log_file}... (Ctrl+C to stop)\n")
            import subprocess

            subprocess.run(["tail", "-f", str(log_file)])
        else:
            with open(log_file) as f:
                lines = f.readlines()
                for line in lines[-tail:]:
                    console.print(line.rstrip())

    except KeyboardInterrupt:
        raise typer.Exit(0)
    except Exception as e:
        console.print(f"❌ [red]Error reading logs: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def config(
    show: bool = typer.Option(False, "--show", "-s", help="Show current configuration"),
    edit: bool = typer.Option(False, "--edit", "-e", help="Edit configuration file"),
    reset: bool = typer.Option(False, "--reset", "-r", help="Reset to default configuration"),
):
    """
    ⚙️  Manage the configuration
    """
    try:
        config_manager = ConfigManager()

        if reset:
            if typer.confirm("Are you sure you want to reset configuration?"):
                config_manager.reset_config()
                console.print("✅ [green]Configuration reset to defaults[/green]")
        elif edit:
            import subprocess

            editor = os.getenv("EDITOR", "nano")
            subprocess.run([editor, str(config_manager.config_path)])
        elif show:
            if config_manager.config_exists():
                config = config_manager.load_config()
                rprint(config)
            else:
                console.print("❌ [red]Configuration not found[/red]")
        else:
            console.print(f"Config file: [cyan]{config_manager.config_path}[/cyan]")

    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def logout():
    """
    🚪 Sign out and remove stored credentials
    """
    try:
        if typer.confirm("Are you sure you want to logout?"):
            auth_manager = AuthManager()
            auth_manager.clear_credentials()
            console.print("✅ [green]Successfully logged out[/green]")
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def version():
    """
    📌 Show the version
    """
    print_simple_logo()
    console.print()
    console.print("Version: [green]0.1.0[/green]")
    console.print("Python: [yellow]" + sys.version.split()[0] + "[/yellow]")
    console.print()


# ──────────────────────────────────────────────────────────────────────────────
# Brain note & sync sub-apps
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_BRAIN_DIR = Path.home() / "akaion-brain"


@app.command()
def dashboard():
    """
    🖥️  Open the interactive dashboard
    """
    auth_manager = AuthManager()
    if not auth_manager.is_authenticated():
        console.print("❌ [red]Not authenticated. Run 'annona login' first.[/red]")
        raise typer.Exit(1)

    token = auth_manager.get_firebase_token() or ""
    email = auth_manager.get_email() or ""
    runner_id = auth_manager.get_runner_id() or ""

    from runner.tui import run_dashboard

    run_dashboard(email=email, runner_id=runner_id, token=token)


note_app = typer.Typer(
    name="note",
    help="📝 Manage notes in the local vault",
    no_args_is_help=True,
)

sync_app = typer.Typer(
    name="sync",
    help="🔄 Push local vault notes to the cloud",
    no_args_is_help=True,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_VALID_SYNC_STATUSES = {SYNC_LOCAL_ONLY, SYNC_PENDING, SYNC_SYNCED, SYNC_ERROR}

_STATUS_STYLE = {
    SYNC_LOCAL_ONLY: "dim",
    SYNC_PENDING: "yellow",
    SYNC_SYNCED: "green",
    SYNC_ERROR: "red",
}


def _resolve_brain_dir() -> Path:
    """Risolve la brain dir dal config (se esiste) o usa il default."""
    config_manager = ConfigManager()
    if config_manager.config_exists():
        try:
            cfg = config_manager.load_config() or {}
            d = cfg.get("brain", {}).get("dir")
            if d:
                return Path(d).expanduser()
        except Exception:
            pass
    # Permette override via env (utile per i test)
    env_dir = os.getenv("AKAION_BRAIN_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return DEFAULT_BRAIN_DIR


def _open_brain() -> BrainManager:
    return BrainManager(_resolve_brain_dir())


def _find_editor() -> str:
    """$EDITOR, then nano, vi or vim — whichever is found in PATH first."""
    editor = os.getenv("EDITOR")
    if editor and shutil.which(editor.split()[0]):
        return editor
    for candidate in ("nano", "vi", "vim"):
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("No editor found. Set $EDITOR, or install nano, vi or vim.")


def _edit_in_editor(initial_content: str = "", suffix: str = ".md") -> str:
    """Apre $EDITOR su un tmpfile precaricato e ritorna il contenuto finale."""
    editor = _find_editor()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(initial_content)
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(f"{editor} {tmp_path}", shell=True, check=True)
        return tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _resolve_id(brain: BrainManager, prefix: str) -> str:
    """Expand an id prefix to a full id. Errors if ambiguous or unmatched."""
    # Match esatto: corto-circuita
    if brain.get(prefix):
        return prefix
    rows = brain._conn.execute(
        "SELECT id, title FROM notes WHERE id LIKE ? LIMIT 10",
        (prefix + "%",),
    ).fetchall()
    if not rows:
        console.print(f"❌ [red]No note whose id starts with '{prefix}'[/red]")
        raise typer.Exit(1)
    if len(rows) > 1:
        console.print(f"❌ [red]Ambiguous id '{prefix}' — {len(rows)} matches:[/red]")
        for r in rows:
            console.print(f"   [dim]{r['id'][:12]}[/dim]  {r['title']}")
        raise typer.Exit(1)
    return rows[0]["id"]


def _short(note_id: str) -> str:
    return note_id[:8]


def _fmt_dt(dt) -> str:
    if dt is None:
        return "-"
    return dt.strftime("%Y-%m-%d %H:%M")


def _notes_table(title: str, notes: List[Note]) -> Table:
    table = Table(title=title)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Tags", style="magenta")
    table.add_column("Sync", style="white")
    table.add_column("Updated", style="dim")
    for n in notes:
        style = _STATUS_STYLE.get(n.sync_status, "white")
        table.add_row(
            _short(n.id),
            n.title or "(untitled)",
            ", ".join(n.tags) if n.tags else "-",
            f"[{style}]{n.sync_status}[/{style}]",
            _fmt_dt(n.updated_at),
        )
    return table


def _require_auth() -> AuthManager:
    auth = AuthManager()
    if not auth.is_authenticated():
        console.print(
            "❌ [red]Not signed in.[/red] Run [cyan]annona login[/cyan] to push to the "
            "cloud, or use the sync action in the UI "
            "([cyan]http://127.0.0.1:7070[/cyan])."
        )
        raise typer.Exit(1)
    return auth


def _build_sync_engine(brain: BrainManager) -> SyncEngine:
    auth = _require_auth()
    return SyncEngine(brain=brain, cot_url=resolve_service_url("cot"), auth=auth)


# ──────────────────────────────────────────────────────────────────────────────
# `annona note ...`
# ──────────────────────────────────────────────────────────────────────────────


@note_app.command("add")
def note_add(
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Note title"),
    tag: List[str] = typer.Option([], "--tag", help="Tag; repeat for several"),
    from_file: Optional[Path] = typer.Option(
        None, "--from-file", "-f", help="Leggi il contenuto da file"
    ),
    stdin: bool = typer.Option(False, "--stdin", help="Leggi il contenuto da stdin"),
):
    """
    📝 Create a new local-only note.

    Without --stdin or --from-file this opens your editor ($EDITOR, falling back to nano/vi/vim).
    """
    try:
        # Contenuto
        if stdin and from_file:
            console.print("❌ [red]--stdin e --from-file sono mutuamente esclusivi[/red]")
            raise typer.Exit(1)

        if stdin:
            content = sys.stdin.read()
        elif from_file:
            if not from_file.exists():
                console.print(f"❌ [red]File not found: {from_file}[/red]")
                raise typer.Exit(1)
            content = from_file.read_text(encoding="utf-8")
        else:
            content = _edit_in_editor("")
            if not content.strip():
                console.print("⚠️  [yellow]Empty content — no note created.[/yellow]")
                raise typer.Exit(1)

        # Title: --title, else the first non-empty line, else "Untitled"
        if not title:
            for line in content.splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    title = line[:120]
                    break
        if not title:
            title = "Untitled"

        brain = _open_brain()
        note = brain.create(title=title, content=content, tags=list(tag))
        path = brain._note_path(note.id)
        console.print("✅ [green]Note created[/green]")
        console.print(f"   ID:     [cyan]{note.id}[/cyan]")
        console.print(f"   Title:  {note.title}")
        if note.tags:
            console.print(f"   Tags:   [magenta]{', '.join(note.tags)}[/magenta]")
        console.print(f"   Path:   [dim]{path}[/dim]")
        console.print(f"   Status: [dim]{note.sync_status}[/dim]")
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@note_app.command("list")
def note_list(
    tag: Optional[str] = typer.Option(None, "--tag", help="Filtra per tag"),
    status: Optional[str] = typer.Option(
        None, "--status", help=f"Filtra per stato ({'/'.join(sorted(_VALID_SYNC_STATUSES))})"
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum number of notes"),
):
    """📋 Lista le note locali."""
    try:
        if status and status not in _VALID_SYNC_STATUSES:
            console.print(
                f"❌ [red]Unknown status '{status}'. Valid values: {', '.join(sorted(_VALID_SYNC_STATUSES))}[/red]"
            )
            raise typer.Exit(1)

        brain = _open_brain()
        notes = brain.list(sync_status=status, tag=tag, limit=limit)

        if not notes:
            console.print("ℹ️  [dim]No notes found.[/dim]")
            brain.close()
            return

        table = _notes_table(f"📝 Brain notes ({len(notes)})", notes)
        console.print(table)
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@note_app.command("show")
def note_show(
    note_id: str = typer.Argument(..., help="Note id, or a unique prefix of one"),
):
    """🔍 Show a note's metadata and content."""
    try:
        brain = _open_brain()
        full_id = _resolve_id(brain, note_id)
        note = brain.get(full_id)
        if not note:
            console.print("❌ [red]Note not found[/red]")
            raise typer.Exit(1)

        style = _STATUS_STYLE.get(note.sync_status, "white")
        console.print(f"[bold cyan]{note.title}[/bold cyan]")
        console.print(f"   ID:         [cyan]{note.id}[/cyan]")
        console.print(
            f"   Tags:       [magenta]{', '.join(note.tags) if note.tags else '-'}[/magenta]"
        )
        console.print(f"   Status:     [{style}]{note.sync_status}[/{style}]")
        console.print(f"   Created:    [dim]{_fmt_dt(note.created_at)}[/dim]")
        console.print(f"   Updated:    [dim]{_fmt_dt(note.updated_at)}[/dim]")
        if note.synced_at:
            console.print(f"   Synced:     [dim]{_fmt_dt(note.synced_at)}[/dim]")
        if note.cot_message_id:
            console.print(f"   COT msg:    [dim]{note.cot_message_id}[/dim]")
        if note.cot_cluster_name:
            console.print(f"   Cluster:    [dim]{note.cot_cluster_name}[/dim]")
        if note.sync_error:
            console.print(f"   Error:      [red]{note.sync_error}[/red]")
        console.print(f"   Path:       [dim]{brain._note_path(note.id)}[/dim]")
        console.print()
        console.print("[bold]── Content ─────────────────────────────────────[/bold]")
        console.print(note.content or "[dim](empty)[/dim]")
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@note_app.command("edit")
def note_edit(
    note_id: str = typer.Argument(..., help="Note id, or a unique prefix of one"),
):
    """✏️  Open a note in your editor. A synced note returns to pending_sync."""
    try:
        brain = _open_brain()
        full_id = _resolve_id(brain, note_id)
        note = brain.get(full_id)
        if not note:
            console.print("❌ [red]Note not found[/red]")
            raise typer.Exit(1)

        new_content = _edit_in_editor(note.content or "")
        if new_content == note.content:
            console.print("ℹ️  [dim]Nessuna modifica.[/dim]")
            brain.close()
            return

        # BrainManager.update already flips synced → pending_sync
        updated = brain.update(full_id, content=new_content)
        if updated is None:
            console.print("❌ [red]Update failed[/red]")
            raise typer.Exit(1)

        console.print("✅ [green]Note updated[/green]")
        console.print(f"   ID:     [cyan]{updated.id}[/cyan]")
        console.print(f"   Status: [dim]{updated.sync_status}[/dim]")
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@note_app.command("delete")
def note_delete(
    note_id: str = typer.Argument(..., help="Note id, or a unique prefix of one"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Salta la conferma"),
):
    """🗑️  Delete a note: the markdown file and its index entry."""
    try:
        brain = _open_brain()
        full_id = _resolve_id(brain, note_id)
        note = brain.get(full_id)
        if not note:
            console.print("❌ [red]Note not found[/red]")
            raise typer.Exit(1)

        if not yes:
            if not typer.confirm(f"Eliminare '{note.title}' ({_short(full_id)})?"):
                console.print("[dim]Cancelled.[/dim]")
                brain.close()
                return

        if brain.delete(full_id):
            console.print(f"✅ [green]Note deleted[/green] [dim]({_short(full_id)})[/dim]")
        else:
            console.print("❌ [red]Eliminazione fallita[/red]")
            raise typer.Exit(1)
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@note_app.command("search")
def note_search(
    query: str = typer.Argument(..., help="Query FTS5"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum number of results"),
):
    """🔎 Full-text search across the local vault."""
    try:
        brain = _open_brain()
        notes = brain.search(query, limit=limit)
        if not notes:
            console.print(f"ℹ️  [dim]Nessun risultato per '{query}'.[/dim]")
            brain.close()
            return
        table = _notes_table(f"🔎 Risultati per '{query}' ({len(notes)})", notes)
        console.print(table)
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# `annona sync ...`
# ──────────────────────────────────────────────────────────────────────────────


@sync_app.command("status")
def sync_status_cmd():
    """📊 Summary of the local vault's sync state."""
    try:
        brain = _open_brain()
        stats = brain.stats()

        table = Table(title="🔄 Sync status")
        table.add_column("Status", style="cyan")
        table.add_column("Note", style="white", justify="right")
        table.add_row(
            f"[{_STATUS_STYLE[SYNC_LOCAL_ONLY]}]{SYNC_LOCAL_ONLY}[/]", str(stats.local_only)
        )
        table.add_row(f"[{_STATUS_STYLE[SYNC_PENDING]}]{SYNC_PENDING}[/]", str(stats.pending))
        table.add_row(f"[{_STATUS_STYLE[SYNC_SYNCED]}]{SYNC_SYNCED}[/]", str(stats.synced))
        table.add_row(f"[{_STATUS_STYLE[SYNC_ERROR]}]{SYNC_ERROR}[/]", str(stats.errors))
        console.print(table)

        total = stats.local_only + stats.pending + stats.synced + stats.errors
        console.print(f"[dim]{total} notes total[/dim]")
        if stats.last_push:
            console.print(f"Ultimo push:  [dim]{_fmt_dt(stats.last_push)}[/dim]")
        else:
            console.print("Last push:    [dim]never[/dim]")
        if stats.last_pull:
            console.print(f"Ultimo pull:  [dim]{_fmt_dt(stats.last_pull)}[/dim]")
        brain.close()
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@sync_app.command("push")
def sync_push_cmd(
    id: List[str] = typer.Option([], "--id", help="Note id or prefix; repeat for several"),
    all_pending: bool = typer.Option(False, "--all-pending", help="Push every pending note"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would happen without doing it"
    ),
):
    """⬆️  Push notes to the cloud."""
    try:
        if not id and not all_pending:
            console.print("❌ [red]Pass --id <ID> (repeatable) or --all-pending[/red]")
            raise typer.Exit(1)

        brain = _open_brain()

        # Risolvi target
        targets: List[Note] = []
        if id:
            for prefix in id:
                full = _resolve_id(brain, prefix)
                note = brain.get(full)
                if note:
                    targets.append(note)
        if all_pending:
            pending_notes = brain.list(sync_status=SYNC_PENDING, limit=10_000)
            seen = {n.id for n in targets}
            for n in pending_notes:
                if n.id not in seen:
                    targets.append(n)

        if not targets:
            console.print("ℹ️  [dim]Nothing to push.[/dim]")
            brain.close()
            return

        if dry_run:
            table = _notes_table(
                f"[yellow]DRY RUN[/yellow] — {len(targets)} note da pushare", targets
            )
            console.print(table)
            console.print("[dim]Nessuna azione eseguita (--dry-run).[/dim]")
            brain.close()
            return

        # Authenticate first
        engine = _build_sync_engine(brain)

        # Explicitly requested local-only notes are marked pending on the fly
        for n in targets:
            if n.sync_status == SYNC_LOCAL_ONLY:
                brain.mark_pending(n.id)

        # One at a time, so the report is per note
        result_table = Table(title=f"⬆️  Push report ({len(targets)})")
        result_table.add_column("ID", style="cyan", no_wrap=True)
        result_table.add_column("Title", style="white")
        result_table.add_column("Outcome", style="white")
        ok = err = 0
        for n in targets:
            success = engine.push_note(n.id)
            if success:
                ok += 1
                result_table.add_row(_short(n.id), n.title, "[green]ok[/green]")
            else:
                err += 1
                latest = brain.get(n.id)
                msg = (latest.sync_error if latest and latest.sync_error else "error")[:60]
                result_table.add_row(_short(n.id), n.title, f"[red]err[/red] [dim]{msg}[/dim]")

        console.print(result_table)
        console.print(f"[green]{ok} ok[/green] / [red]{err} err[/red]")
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@sync_app.command("pull")
def sync_pull_cmd(
    since: Optional[str] = typer.Option(None, "--since", help="Date filter (not implemented yet)"),
):
    """⬇️  Pull clusters from the cloud, refreshing metadata on synced notes."""
    try:
        if since:
            console.print(
                f"⚠️  [yellow]--since='{since}' ignored: pull currently syncs clusters only.[/yellow]"
            )
        brain = _open_brain()
        engine = _build_sync_engine(brain)
        result = engine.pull_clusters()
        console.print("✅ [green]Pull complete[/green]")
        console.print(
            f"   Clusters: [cyan]{result.get('clusters', 0)}[/cyan]"
            f"   Updated:  [cyan]{result.get('updated', 0)}[/cyan]"
        )
        brain.close()
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# `annona cloud ...`  — toggle local vs cloud-sync mode
# ──────────────────────────────────────────────────────────────────────────────

cloud_app = typer.Typer(
    name="cloud",
    help="☁️  Turn cloud sync on or off (off by default — local-first)",
    no_args_is_help=True,
)


@cloud_app.command("enable")
def cloud_enable():
    """Enable cloud sync (sets cloud.enabled=true in the config)."""
    try:
        cm = ConfigManager()
        if not cm.config_exists():
            cm.create_default_config()
        cm.set("cloud.enabled", True)
        console.print("✅ [green]Cloud sync enabled.[/green]")
        auth = AuthManager()
        if not auth.is_authenticated():
            console.print(
                "ℹ️  [dim]Not signed in — run [cyan]annona login[/cyan] "
                "to finish setting up sync.[/dim]"
            )
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@cloud_app.command("disable")
def cloud_disable():
    """Disable cloud sync — pure local mode."""
    try:
        cm = ConfigManager()
        if not cm.config_exists():
            cm.create_default_config()
        cm.set("cloud.enabled", False)
        console.print("✅ [green]Cloud sync disabled — pure local mode.[/green]")
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


@cloud_app.command("status")
def cloud_status():
    """Mostra la modalità corrente (local vs cloud)."""
    try:
        cm = ConfigManager()
        cloud_enabled = False
        if cm.config_exists():
            cloud_enabled = bool(cm.get("cloud.enabled", False))
        auth = AuthManager()
        authed = auth.is_authenticated()
        mode = "cloud" if (cloud_enabled and authed) else "local"

        table = Table(title="☁️  Cloud mode")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        table.add_row("mode", f"[{'green' if mode=='cloud' else 'yellow'}]{mode}[/]")
        table.add_row("cloud.enabled", str(cloud_enabled))
        table.add_row("authenticated", str(authed))
        if authed:
            table.add_row("email", auth.get_email() or "-")
        console.print(table)
    except Exception as e:
        console.print(f"❌ [red]Error: {e}[/red]")
        raise typer.Exit(1)


# Registriamo i sub-app sul root
app.add_typer(note_app, name="note")
app.add_typer(sync_app, name="sync")
app.add_typer(cloud_app, name="cloud")

# The perimeter: policy, substrates, why, verify, audit. Registered from its own
# module so the five commands that carry the sovereignty claim stay readable.
from runner.cli_perimeter import register as _register_perimeter  # noqa: E402

_register_perimeter(app)

# Setup and diagnosis. `setup` is the one command a new install needs; `doctor`
# is what answers "why does it not work" without anybody having to guess which
# of two home directories is missing which of two files.
from runner.cli_setup import register as _register_setup  # noqa: E402

_register_setup(app)


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context):
    """Show banner when running without command"""
    if ctx.invoked_subcommand is None:
        print_simple_logo()
        console.print()
        console.print("Start here: [cyan]annona setup[/cyan], then [cyan]annona run[/cyan]")
        console.print("All commands: [cyan]annona --help[/cyan]")
        console.print()


if __name__ == "__main__":
    app()
