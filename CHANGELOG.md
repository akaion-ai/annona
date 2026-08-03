# Changelog

Notable changes to this project. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[semantic versioning](https://semver.org/) from 1.0 onward.

## [Unreleased]

## [0.1.1] — 2026-08-03

### Updates can actually be delivered

0.1.0 shipped with the updater switched on, pointing at a `latest.json` that did
not exist, and verifying against `"PLACEHOLDER_PUBLIC_KEY_GENERATED_BY_TAURI_SIGNER"`.
No signing key existed in the repository, so every bundle was built unsigned and
no `.sig` was emitted; `build-manifest.sh` then did the right thing and published
without a manifest rather than pretending. The result was an app that makes a
silent GET to a 404 on every launch and would reject any signature it did receive.

The key now exists, its public half is in `tauri.conf.json`, and this release
carries the `.sig` files and the manifest.

**0.1.0 installations will not auto-update to this release.** They were built
against the placeholder key and will reject anything signed with the real one.
Reinstalling once from the releases page is the only way across; from 0.1.1
onward updates arrive on their own.

### Published, for the first time

0.1.0 was the first tag whose bundles reached a GitHub Release at all — the tag
had been sitting on a commit whose Apple Silicon dmg step failed, so the release
job never ran and every download button on the site led to an empty page. The
Python package went up the same day: `pip install annona` is now true, via PyPI
Trusted Publishing rather than a stored token.

### The desktop app is Annona, and there is a site

The Tauri shell, the PyInstaller sidecar, the bundle names and the release
matrix all still said *Akaion Runner*. They now say Annona, end to end: product
name, bundle identifier (`com.akaion.annona`), window title, sidecar binary,
Cargo crates, and the four release targets — macOS arm64 and x64, Windows x64,
Linux x64. A tagged release drafts a GitHub Release with `.dmg`, `.exe`,
`.AppImage` and `.deb` attached.

`docs/index.md` became a landing page rather than a table of contents: the
mascot, five numbers instead of five adjectives, per-platform download cards,
how it works in four steps, and the honest list of what it is not — with a
`pages` workflow that publishes it to GitHub Pages on every push to main.

### Documentation caught up with the code

The research programme was still describing four shipped mechanisms as *not
implemented*, which is the one kind of staleness this project cannot afford: the
credibility of every other claim rests on the gap table being true. Each section
now separates the **mechanism** (shipped) from the **measurement** it exists to
produce (mostly not run), because a control that works on a laptop and a leak
rate over fifty thousand requests are different claims and only the second is
research.

New pages: [Skills](docs/skills.md) — the format, when to pin, installing
Claude's, writing your own — and [Turning the perimeter on](docs/getting-started/perimeter.md),
five minutes from `policy init` to watching a run be refused, including the two
things everyone hits first (tools stopped working; everything is held).

The HLD gained §5.6 redaction and §5.7 skills, so the design of record describes
the design that exists.

### Skills, with a jurisdiction

Anthropic's Agent Skills format — a folder with `SKILL.md`, front matter plus
prose, disclosed progressively — plus one field the kernel enforces:

    pins: local

Loading a skill that declares it raises the working set *before* the instruction
is handed over, so the rest of the run cannot be placed outside the perimeter,
whatever the prompt looks like by then. A prompt library asks; a capability
system decides. Everything else about the format is deliberately identical, so a
skill written for one runtime is not wasted on the other.

Seven ship, generic in name and specific in effect: `image-report` (structured
visual reading with explicit limits — radiology, damage, site photos, identity
documents), `document-triage`, `case-timeline`, `bulk-extract`, `evidence-pack`,
all pinned local; `redact-and-ask` and `second-opinion`, which touch nothing and
are not.

Skills are default-deny like tools, validated at load time like policies, and
asking for a disabled one is answered exactly like asking for one that does not
exist — a model must not be able to enumerate what an operator chose not to
enable. Bodies are classified by the tracker like any other material entering a
transcript, so an instruction file carrying an identifier raises the class of
the run rather than sneaking in under the classifier.

`annona skills-install` takes a folder, a `SKILL.md`, or the name of a skill in
`~/.claude/skills` — so anything from `anthropics/skills` or anything already
written for Claude Code installs unchanged, `scripts/` and `references/`
included. An imported skill is **pinned local by default**: prose you did not
write is still a supply-chain dependency, and `--trust` is the flag somebody has
to type after reading it. Provenance lands in the front matter, the body is
copied byte for byte, and installed is still not enabled — the policy has to
name it.

`annona skills` lists what is installed, allowed and usable here, and
`--show` prints an instruction so an operator can read what their model is
being told. Substrates gained a `vision` flag; a skill requiring it is not
offered where nothing can read an image.

### Redaction: a fourth answer, and a mascot

`on_unavailable: redact`. When a step is too sensitive for every available
substrate, the policy can now ask for the identifiers to be replaced locally
instead of only holding the step: the redacted text is reclassified from
scratch, placed on the strength of what it now contains, and the answer is
re-identified on the machine from a mapping that never leaves it.

The first redactor is an adapter for
[rizzo-pii](https://github.com/Rizzo-AI-Academy/rizzo-pii) (Simone Rizzo, MIT) —
0.3B, CPU-only, 22 Italian categories including codice fiscale, partita IVA and
cadastral identifiers. No upstream code is vendored: `runner/capability/
redactors/rizzo_pii.py` is a client for its documented `/analyze` contract, and
any redactor satisfying the same protocol can take its place.

Three properties are tested rather than asserted: output that still carries an
identifier is **held**, a redactor outage **holds by default**, and the mapping
never reaches the ledger — which records the kinds and counts of what was
replaced and nothing else. Pseudonymous is not anonymous, and the documentation
says so.

Also: Annona has a face. `docs/assets/annona-mascot.png`.

### The perimeter, built: placement, classification, and a verifiable record

Phases F0 through F3 of the HLD. The kernel Annona is named after now exists:
every inference and every tool call is classified, placed, enforced and
recorded, and the acceptance run that proves it takes ten seconds on a fresh
machine.

**Three new layers under L2**, each depending only inward and checked by five
new import contracts:

- `runner/policy` — the policy model and its loader (every rejection names the
  offending key), a classifier that considers paths, symlink targets, content
  patterns and paths *named in a prompt*, the monotone working set, a
  **default-deny** tool gate, and the executor decorator that taints a run with
  what its tools actually returned.
- `runner/placement` — substrate health with an active probe and a circuit
  breaker, the placement engine, and the routing backend that enforces the
  decision. **Failover recomputes placement inside the same rule**: it may cost
  latency, money or model quality, never jurisdiction.
- `runner/audit` — an append-only, hash-chained ledger. `annona verify` checks
  it offline and distinguishes a rewritten entry from a deleted one, a reordered
  one and a corrupt line.

**Briefs.** When no permitted substrate is available and the policy allows it, a
local model writes a summary, which is then reclassified from scratch. A brief
that is no less sensitive than the material is held rather than sent — the
instruction to the model is the cheap layer, the reclassification is the
control. Briefs are never permitted for `restricted`, and the loader refuses a
policy that tries.

**New commands**: `annona policy init|show|validate|test`, `annona substrates`,
`annona why <step>`, `annona verify`, `annona audit --held`.

**Containers.** A multi-stage image built and tested for `linux/arm64` (a DGX
Spark is arm64; an amd64-only image silently does not run on a GB10) and
`linux/amd64`, running unprivileged, with no GPU access and no policy of its
own. `docker-compose.yml` brings up the kernel with Ollama, or with vLLM under
`--profile vllm`. `deploy/verify_appliance.py` is the nine-check acceptance run
an operator executes before handing a box over — including the commercial test:
with the GPU down, restricted work is **held, not rerouted**.

**Tests**: 490 offline, plus 18 opt-in against a real local model
(`ANNONA_LIVE_OLLAMA=1`) and a real Docker daemon (`ANNONA_CONTAINER_TESTS=1`).
The placement conformance matrix covers three classes against five liveness
states; the leak canary asserts a number rather than an argument.

**Fixed — `requirements.txt` had been unsatisfiable since datapizza-ai was
adopted.** pydantic was pinned to 2.7.1 while `datapizza-ai-core` requires
>= 2.10.5, so `pip install -r requirements.txt` failed on any clean machine.
Nobody had noticed because every developer environment was built from
`pyproject.toml`. The container build is what found it, which is the argument
for the container build being part of CI.

### Renamed: Annona — a sovereign execution kernel

The project is **Annona** — after the *cura annonae*, the office that kept Rome
fed by deciding where grain was sourced, which route it took, which granary held
it and who received it. It is described as a *sovereign execution kernel* rather
than a perimeter, because the scope grew: it does not only refuse what an agent
tries to do, it decides **where each step runs** — local GPU, private cluster,
frontier API — enforces the decision and records it. See
[ADR 0005](docs/adr/0005-name-the-project-annona.md), which supersedes 0004.

New vocabulary: **placement**, **prefect** (the policy authority), **horreum**
(the local store), **ration** (remote-capacity quota), **brief**, and **ledger**
(was *manifest*, which now names only what a step may carry). **declaration**,
**clearance**, **held** and **green lane** are unchanged.

`annona` is the primary command; `dogana` and `akaion` stay as aliases. The
PyPI distribution is now `annona`; the Python package stays `runner`.

Added [`docs/design/hld.md`](docs/design/hld.md) — the design of record:
component model, the step state machine, the placement algorithm and its policy
file, two-tier reasoning via briefs, the DGX Spark appliance (arm64, memory
bandwidth, what attestation does *not* buy on a GB10), the threat model, the
metrics the design is judged by, and the laptop→DGX acceptance run.

### Named: Dogana

The project got its first name — *Dogana*, Italian for customs — and the
vocabulary that came with it. Superseded by ADR 0005; the record stays in the
tree. See [ADR 0004](docs/adr/0004-name-the-project-dogana.md).

### Fixed — two divergent CLIs

`runner/cli.py` and the root `cli.py` were **two separate CLIs**, and they had
drifted. The one `pip` installed was the stale copy: it had no `note`, `sync` or
`cloud` commands — which the README documents — and six shared commands differed
in options and in how they resolved service URLs. A user got a different program
depending on whether they installed the wheel or the `.dmg`.

There is now one CLI in `runner/cli.py` (all 12 commands, `dashboard` included)
and a 21-line shim at the root for PyInstaller.

### Changed — English throughout

399 lines of Italian across 36 files: CLI help and output, docstrings, comments,
the desktop UI, and the quickstart. The project ships in English.

`docs/getting-started/quickstart.md` was three documents in one; the release
engineering and auto-updater halves moved to `docs/reference/releasing.md`.

### Added — end-to-end coverage of both topologies

`tests/test_e2e_topologies.py` (15 tests) covers what no test covered before:

- **Detached** — a full agentic run with no credentials and no network; sync
  without credentials as a silent no-op rather than a `Bearer None` request.
- **Attached** — a **live HTTP server** implementing the documented
  three-endpoint contract. Auth verification, a note pushed with the exact
  documented payload, an agentic turn planned remotely and executed locally, a
  policy denial reported upstream without leaking the file, and an unreachable
  control plane degrading instead of crashing.

The attached tests use a real server rather than a mocked client on purpose: the
README tells people they can point the runner at their own backend, and this is
what proves it.

### Fixed — the shipped cloud host was wrong

`service_urls.DEFAULT_API_BASE`, `.env.example` and `.env.prod` all pointed at
`https://api.akaion.com`, which does not serve the API — the TLS handshake fails.
The production gateway is **`https://api.prod.akaion.com`**, where all three
documented services answer: `service1` and `service3` return 200 on `/health`,
`/api/v1/users/me` returns a structured 401, and both write endpoints return 405
on GET, proving the routes exist rather than a catch-all answering.

A fresh clone could not complete `akaion login`. Every default now points at the
real gateway.

`tests/test_live_cloud.py` pins it: seven checks against the real gateway,
opt-in via `AKAION_LIVE=1` so CI stays hermetic. Set `AKAION_LIVE_TOKEN` to
exercise the authenticated path as well.

```bash
AKAION_LIVE=1 env/bin/python -m pytest tests/test_live_cloud.py -v
```

### Known issues found while testing

- **Vault metadata is not portable.** Markdown files hold the note body only;
  titles, tags and sync state live in the SQLite index, and files are named by
  uuid. "Walk away and keep your data" was true of the prose and false of the
  structure. The README now says so, and a test asserts it so it cannot change
  unnoticed.


### Phase 0 — one loop, behind ports

The agentic loop existed three times: once per provider, of which only two
supported tool use. That meant three places where content could leave the
perimeter, so no egress control could ever be total. This release makes it one.

No observable behaviour changed. The existing suite — 205 tests, 34 of them
covering both provider loops — passes unchanged.

#### Added

- `runner/kernel` (L0) — immutable value types, the three ports the loop depends
  on (`InferenceBackend`, `ToolExecutor`, `PolicyGate`), an error taxonomy, and
  translation to and from datapizza blocks.
- `runner/capability` (L1) — inference adapters for `anthropic`, `akaion` and the
  new offline `echo` backend, a shared Anthropic-style wire encoder, and adapters
  putting the existing tool registry and permission manager behind the ports.
- `runner/agent` (L3) — the single agentic loop, and the system prompt as its own
  testable unit.
- `runner/demo.py` — a real agentic run with no credentials and no network.
  `make demo` narrates it; `python -m runner.demo --check` verifies it and runs in
  CI on four platform and version combinations.
- `ai.provider: echo` — a scripted, deterministic, offline backend. Useful beyond
  testing: it drives an exact sequence of tool calls without spending tokens.
- 93 tests across the kernel, the adapters, the loop, and an offline end-to-end
  suite. Total: 298 passing, 6 skipped.
- Architectural contracts in `.importlinter`, enforced in CI. Five of them, two of
  which prove that neither the kernel nor the loop can import a provider SDK.
- `pyproject.toml` (PEP 621), `Makefile`, `.pre-commit-config.yaml`, a `ci`
  workflow, ruff, mypy, and a coverage configuration.
- Documentation: an mkdocs site, three ADRs, a rewritten architecture page, a
  local UI design spec, a configuration reference, a backend authoring guide, plus
  `CONTRIBUTING.md`, `SECURITY.md` and this file.

#### Changed

- `AIClient` is now the composition root. It constructs provider clients and wires
  ports to adapters; it no longer contains control flow. `reason_and_execute`
  returns the same dictionary as before.
- **Text aggregation is uniform.** The Akaion path kept only the last text block
  of a turn while the Anthropic path joined them all. Both now join. A model that
  commented before calling a tool previously had that commentary discarded on the
  Akaion path.
- **`max_iterations=0` returns instead of raising.** It previously raised
  `UnboundLocalError` from an unbound loop variable.
- **Unserialisable tool results degrade instead of raising.** Encoding used
  `json.dumps` with no fallback, so a `Path` in a tool result ended the run.
- Documentation restructured under `docs/` for mkdocs. `docs/ARCHITECTURE.md` was
  rewritten in English: it described a control-plane polling loop and three
  agentic loops, none of which existed.

#### Fixed

- **pytest configuration was silently ignored.** `pytest.ini` used the `setup.cfg`
  spelling `[tool:pytest]`, so pytest never applied it — `addopts`, coverage and
  markers had no effect, and `pytest-cov` was not even installed. Configuration
  now lives in `pyproject.toml`.
- **`asyncio==3.4.3` removed from `requirements.txt`.** `asyncio` is in the
  standard library; the PyPI package of that name is a 2015 backport that shadows
  it. Also removed duplicate pins for `rich`, `click` and `typer`.
- **Manual scripts no longer run as tests.** `test_e2e.py` and `test_runner_ai.py`
  sat at the repository root, where pytest collected them by name — a cloud probe
  requiring a Firebase token was part of the suite. They moved to
  `scripts/manual/`.
- 43 unused imports removed and 38 import blocks sorted across the codebase.

#### Known gaps

Unchanged by this release and documented with metrics in
[docs/research](docs/research/index.md):

- policy is allow-by-default;
- nothing classifies or gates egress;
- the audit trail is a log file, not a verifiable artefact;
- `openai`, `google` and `local` still reach a model without being able to call
  tools. Phase 2 closes the last one with real local inference.

[Unreleased]: https://github.com/Akaion-repos/akaion-app-runner/compare/v0.1.0...HEAD
