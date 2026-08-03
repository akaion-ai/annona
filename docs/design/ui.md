# Local interface — design

Design document for Annona's local UI. Status: **specification**. The current
UI (`ui/`, React 19 + Tauri, ~1,200 lines across five views) is a working
first pass; this describes what it should become and why, so the next change to
it is deliberate rather than incremental.

!!! info "Reading of the brief"
    The direction given was "a cross between Hermes and Obsidian, local". Taken
    as: **Hermes** for the interaction model — keyboard-first, command-palette-
    driven, dense, fast, no chrome that does not earn its pixels — and
    **Obsidian** for the substance — a vault of plain markdown you own, panes,
    links, local search, no server required. If the intent was different, the
    principles below are the part to argue with; the layout follows from them.

## What this interface is for

Not "a chat window with a file tree". Annona is a perimeter, and the interface
has one job no other agent UI has:

> **Make it visible what the agent did, what it was allowed to do, and what left
> the machine.**

Every design decision below follows from that. A UI for a sovereignty product
that hides its egress is a UI that contradicts its product.

## Principles

1. **The vault is the truth, the UI is a view.** Everything the interface shows
   exists as a file on disk. Close the app, open the folder in any editor, lose
   nothing. This is Obsidian's contract and it is the reason to copy it.
2. **Keyboard-first, mouse-optional.** A command palette (`⌘K`) reaches every
   action. Anything reachable only by clicking is a bug for the users this
   product targets — people who live in a terminal and a text editor.
3. **Local by default, visibly.** Remote state is never the quiet default. The
   interface shows which backend answered and whether it was local, on every run.
4. **Show the work, not a spinner.** An agentic run is a sequence of tool calls
   with arguments and results. That sequence *is* the interesting content; a
   progress bar throws it away.
5. **Denials are content.** A refused tool call is not an error to hide behind a
   toast. It is the product working, and it belongs in the transcript with the
   same weight as a successful call.
6. **No telemetry, and it must be checkable.** No analytics, no phone-home, no
   remote fonts or CDN assets. A reviewer should be able to confirm that from the
   network tab in thirty seconds.

## Layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ⌘K  run a task…                              ● local · echo    ⌥⇧P      │
├───────────────┬──────────────────────────────────┬───────────────────────┤
│               │                                  │                       │
│   VAULT       │   RUN                            │   INSPECTOR           │
│               │                                  │                       │
│  ▸ pratiche   │  ▸ summarise the Q1 report       │  tool  document_reader│
│  ▸ notes      │                                  │  args  path=/…/q1.txt │
│  ▸ runs       │  ① explorer  map /documents      │  ─────────────────────│
│               │     → 2 dirs, 3 files            │  result               │
│  ── search ── │                                  │  Fatturato 412.000    │
│  q1                                              │  Margine 31,4 %       │
│               │  ② document_reader  q1_report    │  ─────────────────────│
│  3 results    │     → 412.000 EUR, 31,4 %        │  class      C1        │
│               │                                  │  stayed local  yes    │
│               │  ⊘ filesystem  ~/.ssh/id_rsa     │  duration     41 ms   │
│               │     denied — outside allowed     │                       │
│               │                                  │                       │
│               │  ▸ Q1 2026: 142 pratiche…        │                       │
│               │                                  │                       │
├───────────────┴──────────────────────────────────┴───────────────────────┤
│  ~/akaion-brain · 128 notes · 0 pending          nothing left this machine│
└──────────────────────────────────────────────────────────────────────────┘
```

Three panes, because there are exactly three things: **what you have** (vault),
**what happened** (run), **why** (inspector). A fourth pane would be a fourth
concept, and there is not one.

### The status bar is the product

The bottom-right slot is not decoration. It is a live, permanent statement of
egress:

| State | Shown |
|---|---|
| Detached, no remote configured | `nothing left this machine` |
| Attached, nothing sent this run | `nothing left this machine · cloud available` |
| Attached, transcript sent | `⚠ 3 turns sent to akaion · 14 KB` |
| Phase 3 | `verified · 0 canaries · trace 8f3a…` |

Today the middle rows are the honest ones. When Phase 1 lands, the same slot
gains the class ceiling and the gate's decisions with no redesign, because the
slot was designed for the eventual truth rather than the current one.

## The command palette

`⌘K` is the primary surface. It runs tasks, opens notes, and changes perimeter
settings — the same verbs the CLI exposes, so muscle memory transfers in both
directions.

```
⌘K  ▸ read the invoices in ~/Studio and list overdue ones
    ─────────────────────────────────────────────────────
    run task            in ~/Studio            ⏎
    new note            "invoices"             ⌘N
    search vault        "invoices"             ⌘⇧F
    ─────────────────────────────────────────────────────
    backend             echo · local           ⌥B
    policy              3 grants, shell off    ⌥P
```

Free text runs a task; everything else is a command. Typing is never ambiguous
between the two, because commands are matched only after a prefix, never
heuristically — a UI that guesses whether you meant to run something is a UI that
occasionally runs something you did not mean.

## The run view

A run is a document, not a chat log. It renders the `AgentResult` the loop already
returns: an ordered list of `ToolInvocation` plus the final answer.

Each row shows tool, arguments, and a one-line result preview. Selecting a row
fills the inspector. Denied calls use their own marker (`⊘`) and are never
collapsed by default.

The run is written to the vault as `runs/YYYY-MM-DD-hhmm-<slug>.md` — the same
capture the daemon already performs via `runner.brain.capture`. Reading a past run
is therefore reading a markdown file, with no separate history store to keep in
sync.

## What the UI needs from the runner

The local API (`127.0.0.1:7070`) covers the vault and sync well. Three gaps stand
between it and this design:

| Need | Today | Required |
|---|---|---|
| Run a task from the UI | not exposed | `POST /api/agent/run` |
| Watch a run unfold | not exposed | `GET /api/agent/run/{id}/events` (SSE) |
| Show the perimeter state | not exposed | `GET /api/perimeter` — backend, `is_local`, policy summary |

Streaming matters more than it looks: principle 4 says show the work, and work
shown after it finishes is a log, not a view. The loop already produces its
sequence incrementally; the API needs to emit it.

`GET /api/perimeter` is the one to design carefully, because it is the endpoint
the status bar reads and therefore the one a user trusts. It should report what
the process *is configured to do*, derived from `Capabilities.is_local` and the
policy — never a hardcoded string.

## Technology

Keep React 19 + Vite + Tauri. It works, it ships signed desktop bundles on three
platforms, and the release pipeline exists. Nothing about this design needs a
rewrite.

Constraints that follow from the principles:

- **No remote assets.** Fonts, icons and styles ship in the bundle. A UI that
  fetches from a CDN has already broken the promise on its own status bar.
- **No component library with a network story.** Local, unstyled primitives, own
  the CSS.
- **The web build and the Tauri build are the same build.** `http://127.0.0.1:7070`
  in a browser is a supported way to use this, not a debug mode.

## Phasing

| Phase | Work |
|---|---|
| **A** | Command palette; run view rendering `AgentResult`; `POST /api/agent/run`. Makes the UI able to do the thing the product is for. |
| **B** | SSE streaming; the inspector; runs written to the vault. |
| **C** | Status bar wired to `GET /api/perimeter`; denials as first-class rows. Lands with the Phase 1 perimeter. |
| **D** | Trace viewer and `akaion-verify` output in the UI. Lands with Phase 3. |

Phase A is the only one that blocks anything: until the UI can start a run, the
desktop app is a note editor next to an agent it cannot reach.

## Deliberately excluded

- **A graph view.** Obsidian's is beautiful and mostly ornamental. If it earns its
  place later it will be because someone needed it, not because we copied it.
- **Plugins.** The stub `PluginsView` and its nav entry have been removed. A
  plugin API on a product whose claim is a verifiable perimeter is a decision to
  make on purpose, with a capability model, not by leaving a panel in place — and
  a panel listing four plugins that do not exist tells a first-time user the rest
  of the app might be a mock-up too.
- **A chat transcript UI.** Runs are documents. Making them look like a chat
  invites people to treat an agent with filesystem and shell access like a
  chatbot, which is exactly the mental model this product exists to correct.
