# Contributing

Thanks for looking. This project is a perimeter around other people's private
data, so the bar for changes is a little unusual — most of what follows is about
that, not about style.

## Setup

```bash
git clone https://github.com/akaion-ai/annona.git
cd akaion-app-runner
make setup          # venv + runtime + dev dependencies
make demo           # a real agentic run, offline, in ten seconds
make check          # lint · types · contracts · tests
```

`make setup` needs Python 3.10+. Nothing else, and no credentials.

## Before you open a pull request

```bash
make check
```

That runs exactly what CI runs:

| Step | Command | Fails when |
|---|---|---|
| Lint | `ruff check` | style or a real defect |
| Format | `ruff format --check` | formatting drifted |
| Types | `mypy` | new code is untyped or wrong |
| **Contracts** | `lint-imports` | **a layer boundary was crossed** |
| Tests | `pytest` | behaviour changed |

Optionally `pre-commit install` to run most of it on every commit.

## The architectural contracts

`.importlinter` encodes claims this project makes in public:

```
L0 kernel does not depend on outer layers
L3 agent depends only inward on L0
L3 agent and L1 capability do not know about each other
L0 kernel imports no provider SDK
L3 agent loop imports no provider SDK
```

If your change breaks one, the fix is almost never to edit `.importlinter`. It is
usually that a dependency is pointing outward — the thing you need probably
belongs behind a port in `runner/kernel/ports.py`.

If a contract genuinely should change, that is an
[ADR](https://github.com/akaion-ai/annona/blob/main/docs/adr/index.md), not a config edit.

## Changes on the trust boundary

Permissions, sync, egress, the audit trail, anything touching what leaves the
machine — say in the PR description **what a reviewer should check to convince
themselves the boundary still holds.** Not "I tested it": what to look at.

Adding an inference backend? `Capabilities.is_local` must be truthful. It is the
field the perimeter reads to decide whether a call is egress, and getting it wrong
is not a bug, it is a broken promise.

## Typing and lint debt

New code under `runner/kernel`, `runner/capability` and `runner/agent` is checked
strictly and has no exemptions.

Older modules are listed individually in ledgers — the mypy overrides in
`pyproject.toml` and the ruff per-file ignores. They are listed one by one rather
than by wildcard so the debt is countable and shrinks by deletion. Touching one of
those modules? Consider removing its line and paying down the difference. See
[typing debt](https://github.com/akaion-ai/annona/blob/main/docs/reference/typing-debt.md), which also records the two entries
that are real defects rather than missing annotations.

## Tests

- **Do not mock a vendor SDK.** Use `EchoBackend` and drive the loop through its
  ports — see `tests/test_agent_loop_unified.py`. Mocking an SDK tests the mock's
  shape as much as your behaviour.
- **Name the behaviour, not the function.** `test_a_denied_call_is_never_executed`
  beats `test_permits`.
- **A test for a bug reproduces the bug first.** If it passes before your fix, it
  is not testing your fix.

Markers: `unit`, `integration`, `e2e`.

```bash
env/bin/python -m pytest -m unit
env/bin/python -m pytest tests/test_backends.py -k stop_reason
```

## Documentation

The docs are the product here as much as the code — this project's central claim
is that you can check it yourself, and you cannot check what is not written down.

```bash
make docs-serve      # http://127.0.0.1:8000
make docs            # strict build, as CI runs it
```

Two rules that matter more than the rest:

- **Do not document what is not built.** Use the honest-status tables. A gap
  written down is an asset; a gap implied to be closed is a liability.
- **When you change behaviour, change the page that describes it** in the same
  pull request. `docs/design/architecture.md` was rewritten in Phase 0 because it
  had drifted into describing code that no longer existed — that is the failure
  mode to avoid.

## Commits

Conventional-ish, imperative, one concern each:

```
feat(agent): add progress guard to the loop
fix(backends): resolve runner_id per call, not at construction
docs(adr): record why we did not use datapizza's Agent
```

Keep formatting sweeps out of behavioural commits.

## Licence

Apache-2.0. By contributing you agree your contribution is licensed under it.
