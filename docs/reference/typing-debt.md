# Typing debt

`mypy` runs over the whole package and passes. It passes because four modules are
listed in a ledger in `pyproject.toml` under `ignore_errors`, not because they are
clean.

They are listed **individually rather than by wildcard** so the debt is countable
and shrinks by deletion: type a module, delete its line, and the gate tightens
permanently. A blanket exclusion would have hidden the same errors and never
shrunk.

New code has no exemption. `runner/kernel`, `runner/capability` and `runner/agent`
are checked with `disallow_untyped_defs`, `warn_return_any` and
`no_implicit_optional`.

## The ledger

| Module | Errors | Nature |
|---|---|---|
| `runner.tools.*` | 6 | One design defect, six manifestations — see below |
| `runner.cli` | 2 | `str \| None` passed where `str` is required |
| `runner.local_api` | 2 | **Real defect** — see below |
| `runner.tui` | — | Untyped module, notes only |

## One of these is a defect, not an annotation gap

It is left in place deliberately. Phase 0 is a refactor that changes no
behaviour and the fix changes behaviour — it belongs in its own change, with its
own tests.

### `runner/local_api.py:225,257` — a missing note raises instead of 404

```python
NoteOut.from_note(note)   # note is Note | None
```

`get_note` can return `None`. When it does, `from_note` receives `None` and the
endpoint raises rather than returning a 404. A request for a note that does not
exist produces a 500.

The fix is a `None` check and an `HTTPException(404)`, which changes the
response an existing client sees — hence not here.

### `runner/tools/*` — one defect, six reports

`Tool.execute` is declared as:

```python
def execute(self, **kwargs: Any) -> Any: ...
```

while every concrete tool narrows it:

```python
def execute(self, operation: str, path: str, ..., **kwargs: Any) -> dict[str, Any]: ...
```

mypy is right: the base accepts `execute()` with no arguments and the subclasses
do not, so this is a Liskov violation. It is benign today because every call site
passes the arguments a tool needs, and it becomes real the moment something
dispatches generically over `Tool`.

The fix is to give the base class an honest signature — most likely a typed
per-tool arguments model, which is also what Phase 2's schema-to-grammar
compilation wants. Doing it now would mean rewriting all five tools mid-refactor.

## Fixing one

```bash
# 1. Remove the module from the ledger in pyproject.toml
# 2. See what it costs
env/bin/mypy
# 3. Fix, then make sure nothing moved
make check
```
