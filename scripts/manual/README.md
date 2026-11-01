# Manual checks

Scripts in this directory are **operator tools, not tests**. They need real
credentials, a reachable backend, or both, and they are deliberately outside
`tests/` so `pytest` does not collect them.

They used to live at the repository root as `test_e2e.py` and
`test_runner_ai.py`, where pytest collected them by name and ran them as part of
the suite — a manual cloud probe masquerading as a unit test.

| Script | What it does | Needs |
|---|---|---|
| `e2e_cloud_check.py` | Registers a runner against the cloud backend and drives one real agentic task through `/runner/agent/turn` | a Firebase token, network |

```bash
env/bin/python scripts/manual/e2e_cloud_check.py --token "$FIREBASE_TOKEN"
```

For an end-to-end run that needs **no credentials and no network**, use the
offline demo instead — that one is part of CI:

```bash
make demo
```
