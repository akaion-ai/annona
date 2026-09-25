# Architecture, as built

What the code does today. For where it is going and why, read
[Sovereign runtime](sovereign-runtime.md); this page is deliberately descriptive,
so the gap between the two stays visible.

!!! note "This page replaced a stale one"
    The previous version described a control-plane polling loop with four backend
    clients and three per-provider agentic loops. Neither exists: the loops were
    unified in [ADR 0002](../adr/0002-unify-the-agentic-loop.md), and the runner
    is local-first with opt-in cloud. Documentation that describes code which is
    not there is worse than none, because it is trusted.

## Layers

```
┌──────────────────────────────────────────────────────────┐
│  L4  composition   cli · local_api · ai_client · demo     │
├──────────────────────────────────────────────────────────┤
│  L3  agent         loop · prompt                          │
├──────────────────────────────────────────────────────────┤
│  L2  perimeter     policy · placement · audit · skills    │
├──────────────────────────────────────────────────────────┤
│  L1  capability    backends/ · tooling · tools · brain    │
├──────────────────────────────────────────────────────────┤
│  L0  kernel        types · ports · blocks · errors        │
└──────────────────────────────────────────────────────────┘
```

Enforced in CI by `lint-imports` against `.importlinter`:

| Contract | Asserts |
|---|---|
| `kernel-is-innermost` | L0 depends on no outer layer |
| `agent-above-kernel` | L3 depends only inward on L0 |
| `agent-and-capability-are-independent` | the loop and the adapters do not know about each other |
| `kernel-has-no-vendors` | L0 imports no provider SDK |
| `agent-has-no-vendors` | the loop imports no provider SDK |
| `policy-above-kernel` | L2 policy depends only inward on L0 |
| `placement-above-policy` | L2 placement depends on policy and the kernel, never outward |
| `audit-is-independent` | L2 audit depends on nothing but the kernel |
| `perimeter-knows-no-adapters` | the decision layer cannot reach an L1 adapter |
| `perimeter-has-no-vendors` | L2 imports no provider SDK |
| `skills-know-no-adapters` | a skill cannot reach a backend, which would bypass placement |

The last two are why "the loop is provider-agnostic" is a fact here rather than a
claim. Breaking one fails the build.

!!! warning "The tree does not match the layers yet"
    `runner/tools`, `runner/brain` and `runner/sync` are L1 by role but still sit
    at the package root. Phase 0 introduced the new layers without relocating
    existing packages, because a big-bang move during a refactor that must not
    change behaviour is how refactors go wrong. They move during the layout
    migration.

## L0 — kernel

`runner/kernel/` is pure: no I/O, no vendors, no dependencies on outer layers.

- **`types.py`** — immutable values: `ToolSpec`, `ToolCall`, `ToolResult`,
  `Completion`, `CompletionRequest`, `Capabilities`, `AgentResult`, `Turn`.
- **`ports.py`** — the three protocols the loop depends on: `InferenceBackend`,
  `ToolExecutor`, `PolicyGate`. Structural, so an adapter satisfies one by shape,
  with no base class and no registration.
- **`blocks.py`** — translation to and from datapizza blocks
  ([ADR 0001](../adr/0001-adopt-datapizza-ai.md)), plus `ToolResultBlock`, which
  adds the error flag datapizza's result block lacks.
- **`errors.py`** — the error taxonomy. The distinction that matters is
  recoverable (`BackendUnavailableError`) versus fatal (`ConfigurationError`).

A transcript is a `tuple[Turn, ...]` of datapizza blocks — a *value*, not a bag of
retained provider objects. That is what lets it be serialised, hashed and replayed
later.

## L1 — capability

### Inference backends

`runner/capability/backends/`. Each translates a request out and an answer back,
holds no state, and decides nothing.

| Backend | `is_local` | Tool use | Notes |
|---|---|---|---|
| `echo` | **yes** | yes | Scripted, offline — [ADR 0003](../adr/0003-offline-echo-backend.md) |
| `anthropic` | no | yes | Messages API, native tool use |
| `akaion` | no | yes | Control-plane proxy — **sends the whole transcript** |

`wire.py` holds the Anthropic-style encoding shared by the last two: the Akaion
control plane proxies to Claude, so its contract is Anthropic's block vocabulary.
One encoder means the two paths cannot drift — which they already had.

!!! danger "The Akaion backend is egress for everything the agent reads"
    The transcript accumulates tool results, so a plan that reads a file sends
    that file's contents on the next turn. Tools executing locally is not the same
    as data staying local. `Capabilities.is_local` is `False` for exactly this
    reason, and today nothing reads it — the gap the README documents and Phase 1
    closes.

### Tool adapters

`runner/capability/tooling.py` puts the existing registry and permission manager
behind the L0 ports:

- **`RegistryToolExecutor`** is *total*: every call yields a `ToolResult`,
  including for unknown tools and tools that raise. The model always gets
  something it can reason about, and the run continues.
- **`PermissionGate`** consults `PermissionManager`. Phase 0 behaviour is
  unchanged, which means **allow by default** — an unrecognised tool is permitted,
  and an empty allow-list permits its whole category.

## L3 — agent

`runner/agent/loop.py` is the only place that decides what happens next.

```
prompt ──▶ transcript ──▶ backend.complete()
                              │
                     ┌────────┴────────┐
              text only            tool calls
                  │                    │
                stop        gate.permits() ──no──▶ denial result
                                   │yes
                            executor.invoke()
                                   │
                    append assistant + result turns, repeat
```

Bounded by `max_iterations` (default 10). Tool calls within a turn run
sequentially, in the order the model asked — concurrency would make a run
non-reproducible, and Phase 3's replay depends on reproducibility.

`prompt.py` builds the system prompt. It is separate because it is the most
frequently edited and least testable-in-place part of an agent, and because
[Lab Note 04](../research/index.md) ablates it.

## L4 — composition

`runner/ai_client.py` is the composition root: it constructs provider clients —
credentials, environment, the `_init_*` seams — then wires ports to adapters and
delegates. `reason_and_execute` returns the same dictionary it always has.

Providers without an adapter (`openai`, `google`, `local`) route to
`_agentic_loop_generic`: they reach a model and cannot call tools. That is the
behaviour they have always had, and Phase 2 replaces it.

## Storage

```
~/akaion-brain/                  vault — markdown, greppable, Git-friendly
├── notes/*.md                   body + YAML frontmatter: the record of truth
└── .akaion/index.db             SQLite cache for search, rebuilt from the notes

~/.akaion/
├── config.yaml                  configuration
├── auth.json                    Fernet-encrypted Firebase token
└── .key                         the Fernet key
```

The vault format is a guarantee, not an implementation detail: you can walk away
from the runner and keep your data in a form any tool can read.

Each note's title, tags and sync state are in its frontmatter, and the index is
refreshed from the files every time the vault is opened — **where they disagree,
the file wins**. Delete `index.db` and the next open rebuilds it unchanged; edit
a note's `title:` or `tags:` in another editor and the runner picks it up. A
hand-typed sync state is checked, not trusted: an unknown value, or `synced`
with no `cloud_message_id`, reads as `local_only`.

## Network behaviour

The runner opens no listening port to the internet — the local API binds
`127.0.0.1:7070`. It contacts a remote host in exactly three situations: you log
in, you push pending notes, or a run uses a remote inference backend. Each is an
explicit action. There is no telemetry and no background upload.

## Testing

| Suite | Covers |
|---|---|
| `test_agentic_loop.py` | `AIClient` with mocked SDKs — the Phase 0 regression net |
| `test_agent_loop_unified.py` | the loop through its ports, no SDK mocks |
| `test_backends.py` | wire format, the three adapters, the tool adapters |
| `test_kernel.py` | value types and block translation |
| `test_demo_e2e.py` | the layers composed, offline, against real files |

```bash
make check      # lint · types · contracts · tests
make demo       # see it run
```
