# Experiments

Every number in the paper comes from a script here and a JSON file in
`results/`. Each script is deterministic from its seeds, runs the production loop
and perimeter unchanged, and needs no network unless stated.

| | Script | Question | Needs |
|---|---|---|---|
| E2 | `python -m experiments.leak` | How much truly restricted material reaches a sink that may not hold it? Canaries are **not** in any policy | — |
| E3 | `python -m experiments.compliance` | Does an auditor holding only the ledger and the policy detect a faulty perimeter? | — |
| E4 | `python -m experiments.tamper` | Which edits of a real ledger does verification catch, with and without a witness? | — |
| E5 | `python -m experiments.dojo --model qwen2.5:14b --suite banking` | AgentDojo through the Annona loop: utility, attack success, user data at the frontier | Ollama; a separate env with `agentdojo==0.1.35` |
| E6 | `python -m experiments.overhead` | Per-step cost of classification, clearance, placement, ledger; audit cost per entry | — |

`harness.py` holds what they share: the seeded corpus (a third per class; restricted
documents carry a unique canary; a share is *misfiled* where the policy's paths say
public), the adversarial scripted agent (read, exfiltrate through the browser,
answer), wiretapped substrates and the network sink.

E5 runs in its own environment, because AgentDojo pulls LangChain and a newer
`click` than the CLI pins:

```bash
uv venv .dojo && uv pip install -p .dojo/bin/python -e . agentdojo==0.1.35
ollama serve & ollama pull qwen2.5:14b
.dojo/bin/python -m experiments.dojo --model qwen2.5:14b --suite banking --injections 4
```

The test suite keeps a small version of E3 (`tests/test_compliance.py`), so the
detection result cannot regress silently.
