"""Setup and diagnosis: getting a fresh machine from nothing to an answer.

Why this is a module and not three more lines inside ``init``:

``annona init`` wrote ``~/.akaion/config.yaml``, printed "✅ Configuration
initialized!", and stopped. Everything the kernel actually does derives from a
*different* file — ``~/.annona/policy.yaml`` — which only ``annona policy init``
writes. So a first install reported success and then answered every real
question with ``❌ no policy at ~/.annona/policy.yaml``. Two files, two
directories, two commands, and the one called ``init`` created neither of the
things a new user needs in order to get an answer out of the machine.

``setup`` writes both, and then checks the thing neither file can state.

The default policy names a local model, and it named ``qwen2.5:14b`` regardless
of what the machine had. Liveness is probed with ``GET /api/tags``, which
answers as long as the *server* is up — it says nothing about whether the model
that policy names has ever been pulled. A machine with Ollama running and that
model absent therefore reads as perfectly healthy, places the step on it, and
fails at the first inference, with an error arriving from the far side of a
decision that had already been recorded as ``placed``. Choosing the model from
what is installed removes the common case; ``doctor`` names the rest, before
somebody discovers them one prompt at a time.

``doctor`` contacts nothing except the substrates the policy already names, and
makes no changes. Exit code 1 means something is broken, so it can be the last
line of a provisioning script.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from runner.audit.ledger import verify_file
from runner.config import ConfigManager
from runner.kernel.errors import PolicyError
from runner.policy.loader import load_policy, write_policy_document
from runner.policy.profiles import (
    FRONTIER_PROVIDERS,
    PROFILES,
    FrontierChoice,
    build_policy_document,
    get_profile,
)
from runner.services.enforcement import policy_path

console = Console()

# Models we have actually run the conformance matrix against, best first. This
# is a preference among what is *already installed*, never a reason to download
# anything: setup that pulls 9GB because it liked a name is not setup.
#
# The ordering is by tool-use reliability rather than size. The project's own
# claim is that small models fail tool calls on malformed arguments rather than
# wrong intent, and qwen2.5 is the family that claim was measured on.
PREFERRED_MODELS = (
    "qwen2.5:14b",
    "qwen2.5:7b",
    "qwen2.5:3b",
    "llama3.1:8b",
    "mistral:7b",
)

DEFAULT_ENDPOINT = "http://localhost:11434"

# Built here rather than inline in the option: a comprehension inside an f-string
# in a default argument reads as an undefined name to mypy.
_PROFILE_HELP = "Policy profile: " + ", ".join(p.id for p in PROFILES)

# What the policy falls back to when nothing is installed. It has to name
# something — the document has no "decide later" — and this is the one the
# appliance verification uses, so the fallback and the docs agree.
FALLBACK_MODEL = "qwen2.5:14b"


@dataclass
class RuntimeProbe:
    """What a local model runtime answered, or why it did not."""

    endpoint: str
    reachable: bool
    models: tuple[str, ...] = ()
    detail: str = ""

    @property
    def has_models(self) -> bool:
        return bool(self.models)


def probe_runtime(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 3.0) -> RuntimeProbe:
    """Ask a local runtime which models it has.

    Ollama's ``/api/tags`` and the OpenAI-compatible ``/models`` are both free
    and neither loads anything. Unlike the liveness prober in
    ``runner.placement.registry``, this reads the *list* — the point here is not
    whether a server answered but whether the thing policy will name is on it.
    """
    import httpx

    base = endpoint.rstrip("/")
    for path, extract in (
        ("/api/tags", lambda d: [m.get("name", "") for m in d.get("models", [])]),
        ("/models", lambda d: [m.get("id", "") for m in d.get("data", [])]),
    ):
        try:
            response = httpx.get(f"{base}{path}", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - any transport failure is "not reachable"
            return RuntimeProbe(endpoint, False, detail=f"{type(exc).__name__}: {exc}")
        if response.status_code >= 400:
            continue
        try:
            names = tuple(sorted(n for n in extract(response.json()) if n))
        except Exception:  # noqa: BLE001 - a 200 that is not the shape we expect
            continue
        return RuntimeProbe(endpoint, True, names)

    return RuntimeProbe(
        endpoint, False, detail=f"no model list at {base}/api/tags or {base}/models"
    )


def choose_model(installed: tuple[str, ...], requested: str | None = None) -> tuple[str, str]:
    """Pick the model the policy should name, and say why.

    Returns ``(model, reason)``. An explicit ``--model`` always wins, including
    when it is not installed: overriding the detection is a deliberate act, and
    ``doctor`` will keep saying the model is missing until it is not.
    """
    if requested:
        if requested in installed:
            return requested, "requested, and installed"
        return requested, "requested — not installed on this runtime"

    for candidate in PREFERRED_MODELS:
        if candidate in installed:
            return candidate, "installed, and the best-tested of what is here"

    if installed:
        return installed[0], "the only thing installed"

    return FALLBACK_MODEL, "nothing installed — this is the documented default"


def _ledger_path() -> Path:
    return policy_path().parent / "ledger.jsonl"


def write_policy(
    target: Path,
    *,
    profile_id: str = "local-only",
    local_endpoint: str = DEFAULT_ENDPOINT,
    local_model: str = FALLBACK_MODEL,
    frontier: FrontierChoice | None = None,
    readable_paths: list[str] | None = None,
) -> Path:
    """Build the chosen profile and write it. Never overwrites."""
    document = build_policy_document(
        profile_id,
        local_endpoint=local_endpoint,
        local_model=local_model,
        frontier=frontier,
        readable_paths=readable_paths,
    )
    return write_policy_document(target, document)


def _what_this_policy_permits(target: Path, profile_id: str | None) -> str:
    """One sentence about where material may go, read from the policy on disk.

    This line used to be the constant "Nothing can leave this machine", which
    was true of the only policy the program could write and stopped being true
    the moment profiles could register a hosted provider. It is also printed on
    reruns, over a policy somebody may have edited by hand months ago — so the
    honest source is the file, not what this invocation was asked to do.

    A substrate is off-machine unless its jurisdiction says otherwise. Guessing
    the other way would let a mislabelled substrate buy a reassurance.
    """
    if profile_id and not target.exists():
        return get_profile(profile_id).consequence

    try:
        policy = load_policy(target)
    except (PolicyError, OSError):
        return "Check what this policy allows with `annona policy show`."

    offsite = [s for s in policy.substrates if s.jurisdiction.lower() not in ("on-prem", "local")]
    if not offsite:
        return (
            "Nothing leaves this machine: every substrate in this policy is on-prem, "
            "and the default for everything else is deny."
        )

    named = ", ".join(f"{s.id} (up to {s.max_class.label}, {s.jurisdiction})" for s in offsite)
    return f"Material may leave this machine to: {named}. Everything else is held."


# ── setup ─────────────────────────────────────────────────────────────────────


def ensure_config() -> None:
    """Write the daemon configuration if there is none."""
    config_manager = ConfigManager()
    if config_manager.config_exists():
        console.print(f"  config    [dim]{config_manager.config_path}[/dim] · already there")
    else:
        config_manager.create_default_config()
        console.print(f"  config    [green]written[/green] [dim]{config_manager.config_path}[/dim]")


def setup(
    model: str = typer.Option(None, "--model", "-m", help="Local model to register in the policy"),
    endpoint: str = typer.Option(
        DEFAULT_ENDPOINT, "--endpoint", "-e", help="Local model runtime endpoint"
    ),
    profile: str = typer.Option(None, "--profile", "-p", help=_PROFILE_HELP),
    force: bool = typer.Option(
        False, "--force", help="Rewrite the policy even if one already exists"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Take the defaults without asking (for scripts)"
    ),
    interactive: bool = typer.Option(
        None,
        "--interactive/--no-interactive",
        "-i/-n",
        help="Ask the questions, or do not. Default: ask on a first run at a terminal.",
    ),
):
    """🔧 Set this machine up: configuration, policy, and a check that it can run.

    Asks three questions the first time — which local model, what may leave, and
    which folders may be read — because those are the three the policy file
    answers, and reading a schema in an editor is not a way to be asked. With
    ``--yes``, ``--profile`` or no terminal, it asks nothing.

    Safe to run twice. Nothing that already exists is overwritten without
    ``--force``, and even then the previous policy is copied aside first — it is
    the document every decision derives from, and losing an edited one to a
    rerun of setup would be the worst possible way to learn that.
    """
    console.print("\n🛡️  [bold]Annona setup[/bold]\n")
    ensure_config()

    # Ask unless somebody said otherwise, or there is nobody to ask. A
    # provisioning script reaching a prompt is a hang, not a question — and a
    # rerun on a machine that already has a policy has nothing to ask about,
    # since the answers would be written to a file that is not overwritten.
    if interactive is None:
        interactive = (
            not yes and profile is None and sys.stdin.isatty() and not policy_path().exists()
        )

    if interactive:
        _interactive_setup(model=model, endpoint=endpoint, force=force)
    else:
        ensure_policy(model=model, endpoint=endpoint, force=force, profile_id=profile)


def _ask_model(probe: RuntimeProbe, requested: str | None) -> str:
    """Which local model the policy should name."""
    suggested, why = choose_model(probe.models, requested)

    if not probe.reachable:
        console.print(f"  [yellow]No model runtime answering at {probe.endpoint}.[/yellow]")
        console.print(
            f"  The policy will name [cyan]{suggested}[/cyan]; pull it once Ollama is up."
        )
        return suggested

    if len(probe.models) <= 1:
        console.print(f"  Local model: [cyan]{suggested}[/cyan] [dim]({why})[/dim]")
        return suggested

    console.print("\n[bold]1. Which local model?[/bold]\n")
    for index, name in enumerate(probe.models, start=1):
        marker = "  ← suggested" if name == suggested else ""
        console.print(f"  [cyan]{index}[/cyan]  {name}[dim]{marker}[/dim]")

    default_index = probe.models.index(suggested) + 1 if suggested in probe.models else 1
    picked = typer.prompt("\n  Number", default=str(default_index))
    try:
        return probe.models[int(picked) - 1]
    except (ValueError, IndexError):
        console.print(f"  [dim]Not a number in range — using {suggested}.[/dim]")
        return suggested


def _ask_profile() -> str:
    """What may leave this machine. The only question that is really about policy."""
    console.print("\n[bold]2. What may leave this machine?[/bold]\n")
    for index, profile in enumerate(PROFILES, start=1):
        tag = "  [green](recommended)[/green]" if profile.recommended else ""
        console.print(f"  [cyan]{index}[/cyan]  [bold]{profile.title}[/bold]{tag}")
        console.print(f"      [dim]{profile.consequence}[/dim]\n")

    default_index = next(i for i, p in enumerate(PROFILES, start=1) if p.recommended)
    picked = typer.prompt("  Number", default=str(default_index))
    try:
        return PROFILES[int(picked) - 1].id
    except (ValueError, IndexError):
        console.print("  [dim]Not a number in range — staying local-only.[/dim]")
        return "local-only"


def _ask_frontier() -> FrontierChoice | None:
    """Which hosted provider, and under which environment variable its key lives."""
    console.print("\n[bold]   Which hosted provider?[/bold]\n")
    for index, provider in enumerate(FRONTIER_PROVIDERS, start=1):
        console.print(
            f"  [cyan]{index}[/cyan]  {provider.title} [dim]· {provider.jurisdiction}[/dim]"
        )

    picked = typer.prompt("\n  Number", default="1")
    try:
        provider = FRONTIER_PROVIDERS[int(picked) - 1]
    except (ValueError, IndexError):
        provider = FRONTIER_PROVIDERS[0]

    choice = FrontierChoice(provider=provider)
    choice.model = typer.prompt("  Model", default=provider.model or "")
    if provider.kind == "openai-compatible":
        choice.endpoint = typer.prompt(
            "  Endpoint (base URL, including /v1)", default=provider.endpoint
        )
    choice.api_key_env = typer.prompt(
        "  Environment variable holding the key", default=provider.api_key_env
    )

    # The name goes in the policy; the value must not. Saying so here is cheaper
    # than discovering it in a git history.
    if not os.getenv(choice.api_key_env):
        console.print(
            f"  [yellow]{choice.api_key_env} is not set in this shell.[/yellow] "
            "The policy stores the name, never the key — export it before running."
        )
    return choice


def _ask_readable_paths() -> list[str] | None:
    """Which folders the reading tools may open. None keeps the defaults."""
    console.print("\n[bold]3. Which folders may be read?[/bold]\n")
    console.print(
        "  [dim]Default: ~/Documents and ~/Downloads. Credentials, keys and ~/.ssh\n"
        "  are refused to every tool regardless of what you answer.[/dim]\n"
    )

    answer = typer.prompt("  Folders (comma-separated)", default="~/Documents, ~/Downloads")
    paths = [p.strip() for p in answer.split(",") if p.strip()]
    if not paths:
        return None
    # A person types a folder; the policy matches a glob. Without this, typing
    # exactly what was suggested would grant access to two directories and
    # nothing inside them.
    return [p if p.endswith("**") else f"{p.rstrip('/')}/**" for p in paths]


def _interactive_setup(
    model: str | None,
    endpoint: str,
    force: bool,
) -> None:
    """The three questions, asked once, on a machine with no policy yet.

    Three, and not the schema: which model, what may leave, what may be read.
    Everything else in ``policy.yaml`` has a defensible default, and a wizard
    that walks somebody through every key is a YAML editor that takes longer.
    """
    probe = probe_runtime(endpoint)
    chosen_model = _ask_model(probe, model)
    profile_id = _ask_profile()
    profile = get_profile(profile_id)

    frontier = _ask_frontier() if profile.needs_frontier else None
    readable = None if profile_id == "read-nothing" else _ask_readable_paths()

    # What is about to be written, in the words of the consequence rather than
    # the keys — then a chance to say no.
    console.print("\n[bold]This is what will be written:[/bold]\n")
    console.print(f"  local model    [cyan]{chosen_model}[/cyan]")
    console.print(f"  profile        [cyan]{profile.title}[/cyan]")
    if frontier:
        console.print(
            f"  frontier       [cyan]{frontier.model or frontier.provider.model}[/cyan] "
            f"[dim]· capped at public · key from ${frontier.api_key_env}[/dim]"
        )
    console.print(f"  readable       [cyan]{', '.join(readable) if readable else 'nothing'}[/cyan]")
    console.print(f"\n  [dim]{profile.consequence}[/dim]\n")

    if not typer.confirm("  Write it?", default=True):
        console.print("\n[yellow]Nothing written.[/yellow] Run [cyan]annona setup[/cyan] again.")
        raise typer.Exit(0)

    console.print("")
    ensure_policy(
        model=chosen_model,
        endpoint=endpoint,
        force=force,
        profile_id=profile_id,
        frontier=frontier,
        readable_paths=readable,
    )


def ensure_policy(
    model: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    force: bool = False,
    profile_id: str | None = None,
    frontier: FrontierChoice | None = None,
    readable_paths: list[str] | None = None,
) -> None:
    """Write the policy if there is none, then say what still blocks a run.

    Split out of ``setup`` so ``init`` and the interactive flow can end in the
    same place. Those differ in how they *ask*; there is no version of this
    repository in which they should differ about whether the machine is left
    able to answer a question.
    """
    # What is actually on this machine, before writing a policy that claims
    # something about it.
    probe = probe_runtime(endpoint)
    chosen, why = choose_model(probe.models, model)

    if probe.reachable and probe.has_models:
        console.print(f"  runtime   [green]up[/green] at {endpoint} · {len(probe.models)} model(s)")
    elif probe.reachable:
        console.print(f"  runtime   [yellow]up at {endpoint}, no models pulled[/yellow]")
    else:
        console.print(f"  runtime   [yellow]not reachable at {endpoint}[/yellow]")

    console.print(f"  model     [cyan]{chosen}[/cyan] [dim]({why})[/dim]")

    # Policy — the document everything derives from.
    target = policy_path()
    if target.exists() and force:
        backup = target.with_suffix(f".yaml.bak-{os.getpid()}")
        shutil.copy2(target, backup)
        target.unlink()
        console.print(f"  policy    [yellow]--force: previous copy kept at {backup}[/yellow]")

    if target.exists():
        console.print(f"  policy    [dim]{target}[/dim] · already there, left untouched")
    else:
        written = write_policy(
            target,
            profile_id=profile_id or "local-only",
            local_endpoint=endpoint,
            local_model=chosen,
            frontier=frontier,
            readable_paths=readable_paths,
        )
        chosen_profile = get_profile(profile_id or "local-only")
        console.print(f"  policy    [green]written[/green] [dim]{written}[/dim]")
        console.print(f"  profile   [cyan]{chosen_profile.title}[/cyan]")

    # Say what is still in the way, with the command that clears it. A setup
    # that ends on "✅" while the machine cannot answer a question is the defect
    # this whole module exists to remove.
    console.print("")
    blockers: list[tuple[str, str]] = []

    if not probe.reachable:
        blockers.append(
            (
                f"No model runtime is answering at {endpoint}.",
                "Install Ollama from https://ollama.com, then: ollama serve",
            )
        )
    elif chosen not in probe.models:
        blockers.append(
            (
                f"The policy names {chosen}, which is not pulled on this runtime.",
                f"ollama pull {chosen}",
            )
        )

    if blockers:
        console.print("[yellow]Not ready yet:[/yellow]\n")
        for problem, fix in blockers:
            console.print(f"  • {problem}")
            console.print(f"    [cyan]{fix}[/cyan]\n")
        console.print("Then re-check with [cyan]annona doctor[/cyan].")
        raise typer.Exit(0)

    console.print(f"[green]Ready.[/green] {_what_this_policy_permits(target, profile_id)}\n")
    console.print("  [cyan]annona run[/cyan]           start the daemon and the local interface")
    console.print("  [cyan]annona policy show[/cyan]   what is allowed, as the runtime reads it")
    console.print("  [cyan]annona doctor[/cyan]        check it again, any time")


# ── doctor ────────────────────────────────────────────────────────────────────


@dataclass
class Check:
    """One diagnosis: what was looked at, what was found, what to do about it."""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    fatal: bool = True  # a warning that does not fail the exit code sets this False


@dataclass
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, fix: str = "", fatal: bool = True) -> None:
        self.checks.append(Check(name, ok, detail, fix, fatal))

    @property
    def broken(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.fatal]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.fatal]


def diagnose(port: int = 7070) -> Diagnosis:
    """Every check ``doctor`` runs, as data, so the tests can assert on it."""
    d = Diagnosis()

    # Python. The wheel declares >=3.10 and the failure of running older is an
    # import error deep in a dependency, which reads as a broken install.
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    d.add(
        "python",
        sys.version_info >= (3, 10),
        version,
        "Annona needs Python 3.10 or newer",
    )

    # Configuration.
    config_manager = ConfigManager()
    d.add(
        "config",
        config_manager.config_exists(),
        str(config_manager.config_path),
        "annona setup",
    )

    # Policy — parsed, not merely present. A policy that exists and does not
    # load is a worse state than none, because the daemon starts anyway and
    # stops enforcing.
    target = policy_path()
    policy = None
    if not target.exists():
        d.add("policy", False, f"no file at {target}", "annona setup")
    else:
        try:
            policy = load_policy(target)
            d.add(
                "policy",
                True,
                f"{target} · {len(policy.substrates)} substrate(s), {len(policy.rules)} rule(s)",
            )
        except PolicyError as exc:
            d.add("policy", False, f"{target}: {exc}", "annona policy validate")

    # Substrates: reachable, and carrying the model the policy names. The
    # second half is the one the liveness probe cannot answer.
    if policy is not None:
        for substrate in policy.substrates:
            if not substrate.endpoint:
                d.add(f"substrate {substrate.id}", True, "no endpoint to probe")
                continue

            probe = probe_runtime(substrate.endpoint)
            if not probe.reachable:
                d.add(
                    f"substrate {substrate.id}",
                    False,
                    f"{substrate.endpoint} — {probe.detail}",
                    "start the runtime, or remove the substrate from the policy",
                )
                continue

            declared = getattr(substrate, "model", "") or ""
            if declared and probe.models and declared not in probe.models:
                d.add(
                    f"substrate {substrate.id}",
                    False,
                    f"up, but {declared} is not pulled "
                    f"(has: {', '.join(probe.models[:4]) or 'nothing'})",
                    f"ollama pull {declared}",
                )
            else:
                d.add(
                    f"substrate {substrate.id}",
                    True,
                    f"up at {substrate.endpoint}"
                    + (f" · {declared}" if declared else "")
                    + (f" · {len(probe.models)} model(s)" if probe.models else ""),
                )

    # Ledger. An empty one is fine and common; a broken chain is not.
    ledger = _ledger_path()
    result = verify_file(ledger)
    d.add(
        "ledger",
        result.ok,
        str(result),
        "the chain is broken — keep the file and see docs/design/hld.md",
    )

    # Daemon. Not running is normal, so this is a warning: `doctor` is most
    # often run *before* `annona run`.
    import httpx

    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        up = response.status_code == 200
    except Exception:  # noqa: BLE001 - not running is the ordinary case
        up = False
    d.add(
        "daemon",
        up,
        f"127.0.0.1:{port}" + (" · responding" if up else " · not running"),
        "annona run",
        fatal=False,
    )

    return d


def doctor(
    port: int = typer.Option(7070, "--port", help="Port the daemon would be listening on"),
):
    """🩺 Check this installation and say what is wrong. Changes nothing; exit 1 if broken."""
    d = diagnose(port=port)

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=3)
    table.add_column("check", style="cyan")
    table.add_column("detail", overflow="fold")

    for check in d.checks:
        mark = (
            "[green]✓[/green]"
            if check.ok
            else ("[red]✗[/red]" if check.fatal else "[yellow]![/yellow]")
        )
        table.add_row(mark, check.name, check.detail)

    console.print("")
    console.print(table)

    problems = d.broken + d.warnings
    if problems:
        console.print("")
        for check in problems:
            if check.fix:
                console.print(f"  {check.name}: [cyan]{check.fix}[/cyan]")

    if d.broken:
        console.print("\n❌ [red]This installation cannot run a step.[/red]")
        raise typer.Exit(1)

    console.print("\n✅ [green]Ready.[/green]")


def register(app: typer.Typer) -> None:
    """Attach the setup commands to the main CLI."""
    app.command("setup")(setup)
    app.command("doctor")(doctor)
