"""CLI for the perimeter: policy, substrates, why, verify, audit (layer L4).

Kept in its own module rather than added to the 1 000-line ``runner/cli.py``,
because these five commands are the operator-facing surface of the whole
sovereignty claim and they should be readable on one screen.

The commands exist for one reason each:

``policy``      show, validate or create the document everything derives from
``substrates``  what is registered, where it is, and whether it is up
``why``         reconstruct a single decision from the ledger, months later
``verify``      check the chain, offline, without trusting this process
``audit``       count what actually happened, including what was refused
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from runner.audit.ledger import verify_file
from runner.cli_setup import choose_model, probe_runtime
from runner.kernel.errors import ConfigurationError, PolicyError
from runner.kernel.types import Requirement, SensitivityClass
from runner.placement.engine import PlacementDecisionEngine
from runner.placement.registry import SubstrateRegistry, http_prober
from runner.policy.loader import load_policy, write_default_policy
from runner.services.enforcement import policy_path

console = Console()

policy_app = typer.Typer(
    name="policy", help="📜 The policy: show, validate, create", no_args_is_help=True
)


def _load(path: Path | None = None):
    """Load the policy, or exit with the reason it could not be loaded."""
    target = Path(path) if path else policy_path()
    try:
        return load_policy(target), target
    except PolicyError as exc:
        console.print(f"❌ [red]{exc}[/red]")
        console.print("   Create one with: [cyan]annona policy init[/cyan]")
        raise typer.Exit(1) from exc


def _ledger_path() -> Path:
    return policy_path().parent / "ledger.jsonl"


# ── policy ────────────────────────────────────────────────────────────────────


@policy_app.command("init")
def policy_init(
    model: str = typer.Option(
        None, "--model", "-m", help="Local model to register (default: whichever is installed)"
    ),
    endpoint: str = typer.Option(
        "http://localhost:11434", "--endpoint", "-e", help="Local runtime endpoint"
    ),
    path: Path = typer.Option(None, "--path", "-p", help="Where to write the policy"),
):
    """Write a starting policy. Never overwrites an existing one."""
    target = Path(path) if path else policy_path()
    existed = target.exists()

    # The model used to default to a hardcoded qwen2.5:14b whatever the machine
    # had. Liveness is probed with GET /api/tags, which answers as long as the
    # *server* is up — so a runtime without that model pulled read as healthy,
    # the step was placed on it, and the failure arrived from the far side of a
    # decision already written to the ledger as `placed`. Naming what is
    # actually installed removes the common case; `annona doctor` names the rest.
    chosen, why = choose_model(probe_runtime(endpoint).models, model)
    written = write_default_policy(target, local_endpoint=endpoint, local_model=chosen)

    if existed:
        console.print(f"ℹ️  [yellow]A policy already exists at {written} — left untouched.[/yellow]")
        raise typer.Exit(0)

    console.print(f"✅ [green]Policy written to[/green] [cyan]{written}[/cyan]")
    console.print(f"   Local model: [cyan]{chosen}[/cyan] [dim]({why})[/dim]")
    console.print(
        "\nIt registers only your local runtime, so nothing can leave this machine "
        "until you add a substrate yourself."
    )


@policy_app.command("show")
def policy_show(path: Path = typer.Option(None, "--path", "-p")):
    """Print the policy as the runtime understands it, not as it is written."""
    policy, target = _load(path)

    console.print(f"\n📜 [bold]{target}[/bold]  ·  version {policy.version}  ·  default: deny\n")

    table = Table(title="Substrates", show_lines=False)
    table.add_column("id", style="cyan")
    table.add_column("kind")
    table.add_column("jurisdiction")
    table.add_column("max class", style="magenta")
    table.add_column("tools")
    table.add_column("endpoint", overflow="fold")
    for sub in policy.substrates:
        table.add_row(
            sub.id,
            sub.kind,
            sub.jurisdiction,
            sub.max_class.label,
            "yes" if sub.tools else "no",
            sub.endpoint or "—",
        )
    console.print(table)

    rules = Table(title="Rules (first match wins)")
    rules.add_column("id", style="cyan")
    rules.add_column("class", style="magenta")
    rules.add_column("allow")
    rules.add_column("on_unavailable")
    rules.add_column("prefer")
    for rule in policy.rules:
        rules.add_row(
            rule.id,
            rule.klass.label,
            ", ".join(rule.allow) or "—",
            rule.on_unavailable,
            rule.prefer,
        )
    console.print(rules)

    tools = Table(title="Tools (default-deny)")
    tools.add_column("tool", style="cyan")
    tools.add_column("may touch", overflow="fold")
    for tool, paths in sorted(policy.tools.allow.items()):
        tools.add_row(tool, ", ".join(paths) or "—")
    console.print(tools)

    if policy.tools.deny_paths:
        console.print(f"\n[red]Never, for any tool:[/red] {', '.join(policy.tools.deny_paths)}")


@policy_app.command("validate")
def policy_validate(path: Path = typer.Option(None, "--path", "-p")):
    """Check a policy without running anything. Exit code 1 if it is not valid."""
    policy, target = _load(path)
    console.print(f"✅ [green]{target} is valid[/green]")
    console.print(
        f"   {len(policy.classes)} classes · {len(policy.substrates)} substrates · "
        f"{len(policy.rules)} rules · default class: {policy.default_class.label}"
    )


@policy_app.command("test")
def policy_test(
    klass: str = typer.Argument(..., help="Class to place: public, internal, restricted"),
    tools: bool = typer.Option(True, "--tools/--no-tools", help="Does the step use tools?"),
    path: Path = typer.Option(None, "--path", "-p"),
    probe: bool = typer.Option(False, "--probe", help="Actually check substrate health"),
):
    """Ask the policy where a step of this class would run, right now.

    The command an operator runs before a deployment rather than after an
    incident: it answers "if this were restricted, where would it go?" without
    needing a restricted document to try it with.
    """
    policy, _ = _load(path)
    try:
        target_class = SensitivityClass.parse(klass)
    except ValueError as exc:
        console.print(f"❌ [red]{exc}[/red]")
        raise typer.Exit(1) from exc

    registry = SubstrateRegistry.from_substrates(
        policy.substrates, prober=http_prober() if probe else None
    )
    engine = PlacementDecisionEngine(policy, registry)
    placement = engine.place(target_class, Requirement(tools=tools))

    colour = "green" if placement.permitted else "red"
    console.print(f"\n[bold]{target_class.label}[/bold] → [{colour}]{placement.outcome}[/{colour}]")
    # Escaped: a rule id like `rules[0]` and an allow-list like `[local-gpu]`
    # are Rich markup, and Rich silently swallows what it cannot parse — which
    # would print an empty allow-list for a rule that has one.
    console.print(escape(engine.explain(target_class, Requirement(tools=tools))))
    if not probe:
        console.print("\n[dim]Health was not probed; add --probe to include liveness.[/dim]")


# ── top-level commands ────────────────────────────────────────────────────────


def substrates(
    path: Path = typer.Option(None, "--path", "-p", help="Policy file"),
    probe: bool = typer.Option(True, "--probe/--no-probe", help="Check liveness over HTTP"),
):
    """🌍 Registered substrates, their jurisdiction, and whether they are up."""
    policy, _ = _load(path)
    registry = SubstrateRegistry.from_substrates(
        policy.substrates, prober=http_prober() if probe else None
    )

    table = Table(title="Substrates")
    table.add_column("id", style="cyan")
    table.add_column("jurisdiction")
    table.add_column("max class", style="magenta")
    table.add_column("health")
    table.add_column("detail", overflow="fold")

    for sid, health in registry.snapshot().items():
        sub = registry.substrates[sid]
        state = "[green]up[/green]" if health.up else "[red]down[/red]"
        detail = health.reason or (f"{health.latency_ms:.0f} ms" if health.latency_ms else "")
        table.add_row(sid, sub.jurisdiction, sub.max_class.label, state, detail)

    console.print(table)


def skills(
    path: Path = typer.Option(None, "--path", "-p", help="Policy file"),
    show: str = typer.Option("", "--show", "-s", help="Print one skill's instructions"),
):
    """🧩 Skills: what is installed, what the policy allows, what can run here."""
    from runner.skills.loader import discover_skills, skills_dirs
    from runner.skills.registry import SkillRegistry

    policy, _ = _load(path)
    installed = discover_skills()

    if show:
        skill = installed.get(show)
        if skill is None:
            console.print(f"❌ [red]no skill named {show!r}[/red]")
            raise typer.Exit(1)
        console.print(f"\n[bold]{skill.name}[/bold] · v{skill.version} · {skill.source}\n")
        console.print(escape(skill.body))
        raise typer.Exit(0)

    registry = SkillRegistry(
        installed,
        allowed=policy.skills.allow,
        vision=any(s.vision for s in policy.substrates),
        allowed_tools=tuple(policy.tools.allow),
        context_window=max((s.context_window for s in policy.substrates), default=0),
    )
    usable = {s.name for s in registry.available()}
    blocked = dict(registry.unusable())

    table = Table(title="Skills")
    table.add_column("skill", style="cyan")
    table.add_column("state")
    table.add_column("pins")
    table.add_column("what it does", overflow="fold")

    for name in sorted(installed):
        skill = installed[name]
        if name in usable:
            state = "[green]available[/green]"
        elif name in blocked:
            state = "[yellow]blocked[/yellow]"
        else:
            state = "[dim]not allowed[/dim]"
        detail = blocked.get(name) or skill.description
        pins = "[magenta]local only[/magenta]" if skill.pins_local else "—"
        table.add_row(name, state, pins, escape(detail))

    console.print(table)
    console.print(f"\n[dim]searched: {' · '.join(str(d) for d in skills_dirs())}[/dim]")
    console.print(
        "[dim]a skill the policy does not name is never offered to a model; "
        "one that pins local confines the run to your perimeter.[/dim]"
    )


def skills_install(
    source: str = typer.Argument(
        ..., help="Folder, SKILL.md, or the name of a skill in ~/.claude/skills"
    ),
    name: str = typer.Option("", "--name", "-n", help="Install under a different name"),
    trust: bool = typer.Option(
        False, "--trust", help="Keep the skill's own pins value instead of pinning it local"
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing installation"),
    home: Path = typer.Option(None, "--home", help="Skills directory (default ~/.annona/skills)"),
):
    """📥 Install a skill somebody else wrote — including Claude's."""
    from runner.skills.install import install_skill
    from runner.skills.loader import skills_dirs

    target = Path(home) if home else skills_dirs()[-1]

    try:
        installed = install_skill(source, target, name=name or None, trust=trust, force=force)
    except ConfigurationError as exc:
        console.print(f"❌ [red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc

    skill = installed.skill
    console.print(
        f"\n✅ [green]installed[/green] [cyan]{skill.name}[/cyan] → {installed.destination}"
    )
    console.print(f"   [dim]{escape(skill.description)}[/dim]\n")

    if installed.pinned and not trust:
        console.print(
            "🔒 [magenta]pinned local[/magenta] — you did not write this instruction, so any run "
            "that loads it stays inside your perimeter.\n"
            "   [dim]Read it (annona skills --show "
            f"{skill.name}), then reinstall with --trust if you want it unpinned.[/dim]"
        )

    if installed.has_scripts:
        console.print(
            "\n⚠️  [yellow]this skill bundles scripts[/yellow] — they will not run unless your "
            "policy allows the `shell` tool, which by default it does not. The instructions "
            "still work; the automation in them does not."
        )

    console.print(
        f"\n[bold]Not enabled yet.[/bold] Add it to your policy to offer it to a model:\n"
        f"  [cyan]{escape(f'skills: [{skill.name}]')}[/cyan]"
    )


def why(
    step_id: str = typer.Argument(..., help="Step id, as printed by a run or by `annona audit`"),
    ledger: Path = typer.Option(None, "--ledger", "-l", help="Ledger file"),
):
    """🔎 Explain one decision, reconstructed from the ledger."""
    from runner.audit.ledger import read_entries

    target = Path(ledger) if ledger else _ledger_path()
    entry = None
    for candidate in read_entries(target):
        if candidate.step_id == step_id or candidate.step_id.endswith(step_id):
            entry = candidate

    if entry is None:
        console.print(f"❌ [red]no step {step_id!r} in {target}[/red]")
        raise typer.Exit(1)

    colour = "red" if entry.outcome == "held" else "green"
    console.print(
        f"\n[bold]{entry.step_id}[/bold]  {entry.kind}  [{colour}]{entry.outcome.upper()}[/{colour}]"
    )
    console.print(f"  class        {entry.klass}")
    if entry.detail.get("working_set"):
        console.print(f"  working set  {entry.detail['working_set']}")
    if entry.rule_id:
        console.print(f"  rule         {escape(entry.rule_id)}")
    if entry.substrate:
        console.print(f"  substrate    {entry.substrate}")
    if entry.detail.get("candidates"):
        console.print(f"  candidates   {escape(', '.join(entry.detail['candidates']))}")
    for rejected in entry.detail.get("rejected", []):
        console.print(f"  not chosen   {escape(str(rejected[0]))} — {escape(str(rejected[1]))}")
    console.print(f"  reason       {escape(str(entry.detail.get('reason', '')))}")
    console.print(f"  recorded     {entry.ts}")
    console.print(f"  ledger       #{entry.seq}  sha256:{entry.hash[:12]}…")


def verify(
    ledger: Path = typer.Option(None, "--ledger", "-l", help="Ledger file"),
):
    """🔐 Check the ledger chain. Contacts nobody; exit code 1 if broken."""
    target = Path(ledger) if ledger else _ledger_path()
    result = verify_file(target)

    if not result.ok:
        console.print(f"❌ [red]{result}[/red]")
        console.print(f"   [dim]{target}[/dim]")
        raise typer.Exit(1)

    console.print(f"✅ [green]{result}[/green]")
    console.print(f"   [dim]{target}[/dim]")


def audit(
    ledger: Path = typer.Option(None, "--ledger", "-l", help="Ledger file"),
    held: bool = typer.Option(False, "--held", help="List every refusal in full"),
):
    """📊 What actually happened: placements, holds, classes."""
    from runner.audit.ledger import read_entries

    target = Path(ledger) if ledger else _ledger_path()
    entries = list(read_entries(target))

    if not entries:
        console.print(f"[yellow]No decisions recorded yet at {target}[/yellow]")
        raise typer.Exit(0)

    placements: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    classes: dict[str, int] = {}
    for entry in entries:
        outcomes[entry.outcome] = outcomes.get(entry.outcome, 0) + 1
        classes[entry.klass] = classes.get(entry.klass, 0) + 1
        if entry.substrate:
            placements[entry.substrate] = placements.get(entry.substrate, 0) + 1

    result = verify_file(target)
    chain = "[green]intact[/green]" if result.ok else f"[red]{result.problem}[/red]"

    console.print(f"\n📊 [bold]{len(entries)} decisions[/bold]  ·  chain {chain}")
    console.print(f"   placements   {placements or '—'}")
    console.print(f"   outcomes     {outcomes}")
    console.print(f"   classes      {classes}")

    refusals = [e for e in entries if e.outcome == "held"]
    if refusals and not held:
        console.print(
            f"\n[yellow]{len(refusals)} refused[/yellow] — rerun with --held to list them"
        )
    elif refusals:
        console.print("")
        table = Table(title="Refused")
        table.add_column("step", style="cyan")
        table.add_column("kind")
        table.add_column("class", style="magenta")
        table.add_column("reason", overflow="fold")
        for entry in refusals:
            table.add_row(
                entry.step_id, entry.kind, entry.klass, escape(str(entry.detail.get("reason", "")))
            )
        console.print(table)


def pair(
    show: bool = typer.Option(
        False, "--show", help="Print the existing token instead of a new one"
    ),
    origin: list[str] = typer.Option(
        None, "--origin", "-o", help="Web origin allowed to use this machine (repeatable)"
    ),
    revoke: bool = typer.Option(False, "--revoke", help="Delete the pairing; refuse every web app"),
):
    """🔗 Let a web app use this machine as its executor.

    Prints a token to paste into the app. Until one exists, the daemon serves
    only the window it ships with — a page open in your browser cannot reach it,
    which is the correct default and the reason this is a command rather than a
    setting that is already on.
    """
    from runner.pairing import DEFAULT_REMOTE_ORIGINS, Pairing, pairing_path

    target = pairing_path()

    if revoke:
        if target.exists():
            target.unlink()
            console.print(f"🔗 [green]pairing revoked[/green] — {target} deleted")
            console.print("   Every web app now has to pair again before it can run anything here.")
        else:
            console.print("[yellow]nothing to revoke — this machine is not paired[/yellow]")
        raise typer.Exit(0)

    if show:
        existing = Pairing.load(target)
        if existing is None:
            console.print("[yellow]this machine is not paired[/yellow]")
            console.print("   Create a token with: [cyan]annona pair[/cyan]")
            raise typer.Exit(1)
        console.print(f"\n🔗 [bold]token[/bold]  [cyan]{existing.token}[/cyan]")
        console.print(f"   origins  {', '.join(existing.origins)}")
        console.print(f"   file     [dim]{existing.path}[/dim]")
        raise typer.Exit(0)

    origins = tuple(origin) if origin else DEFAULT_REMOTE_ORIGINS
    pairing = Pairing.create(target, origins=origins)

    console.print(
        "\n🔗 [bold]Paired.[/bold] Paste this token into the app's local-execution setting:\n"
    )
    console.print(f"   [cyan]{pairing.token}[/cyan]\n")
    console.print(f"   allowed origins  {', '.join(pairing.origins)}")
    console.print(f"   stored in        [dim]{pairing.path}[/dim] (mode 600)")
    console.print(
        "\n[dim]Anyone holding this token can run steps on this machine from a listed origin. "
        "Revoke with `annona pair --revoke`; there is no recovery, only a new token.[/dim]"
    )


def register(app: typer.Typer) -> None:
    """Attach the perimeter commands to the main CLI.

    A function rather than decorators at import time, so this module can be
    imported by tests without a Typer app existing.
    """
    app.add_typer(policy_app)
    app.command("substrates")(substrates)
    app.command("skills")(skills)
    app.command("skills-install")(skills_install)
    app.command("why")(why)
    app.command("verify")(verify)
    app.command("audit")(audit)
    app.command("pair")(pair)
