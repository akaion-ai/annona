# Configuring a frontier model

A substrate is four facts: **where it is, what it may hold, what it costs, and
which environment variable holds its key.** All four live in `policy.yaml`; the
key itself never does.

```yaml
substrates:
  - id: local-gpu
    kind: ollama
    endpoint: http://localhost:11434
    model: qwen2.5:14b
    jurisdiction: on-prem
    max_class: restricted

  - id: claude
    kind: anthropic
    model: claude-opus-5
    api_key_env: ANTHROPIC_API_KEY     # the NAME, never the value
    jurisdiction: us
    max_class: public                  # the field the whole product turns on
    quality: 98
    cost_per_mtok: 15.0

rules:
  - match: {class: restricted}
    allow: [local-gpu]
    on_unavailable: hold
  - match: {class: internal}
    allow: [local-gpu]
  - match: {class: public}
    allow: [claude, local-gpu]
    prefer: quality
```

`max_class` is the whole story. A substrate may serve a step only when the
step's class is at or below its ceiling, so `max_class: public` means this
provider never sees anything the policy classified higher — no matter which
rule allows it, no matter what the model is worth. Raising that ceiling is one
line, and it is the line an auditor reads first.

## The two kinds you need

**`anthropic`** — the native Messages API, with tool use.

```yaml
- {id: claude, kind: anthropic, model: claude-opus-5, api_key_env: ANTHROPIC_API_KEY, …}
```

**`openai-compatible`** — anything speaking `/v1/chat/completions`, which is
most of the industry. `endpoint` is the base URL *including* `/v1`.

| Provider | `endpoint` | `model` (example) | `api_key_env` |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` | `OPENAI_API_KEY` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.0-flash` | `GEMINI_API_KEY` |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | `GROQ_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-5` | `OPENROUTER_API_KEY` |
| Mistral | `https://api.mistral.ai/v1` | `mistral-large-latest` | `MISTRAL_API_KEY` |
| Together | `https://api.together.xyz/v1` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | `TOGETHER_API_KEY` |
| vLLM (your rack) | `http://gpu-01.internal:8000/v1` | whatever you serve | — |
| LM Studio | `http://localhost:1234/v1` | whatever you loaded | — |

`kind: ollama` is separate because Ollama's `/api/chat` is its own dialect.

**Two frontier providers need two keys**, which is why `api_key_env` is per
substrate. Without it a policy could only ever hold one credential — the older
global names (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ANNONA_SUBSTRATE_KEY`)
still work as fallbacks for single-provider installs.

An on-prem vLLM behind your own firewall is *not* a frontier model, and the
policy should say so: `jurisdiction: on-prem`, `max_class: restricted`. The
same `kind` serves both — geography is a claim you make, not one the HTTP
client can verify.

## Check it before you trust it

```bash
annona substrates            # registered, jurisdiction, ceiling, and whether it answers
annona why --class internal  # which substrate would take this class, and why not the others
```

`annona substrates` probes over HTTP. A substrate that is configured but not
answering shows as down and is skipped by placement — with the reason in the
ledger, not in a log nobody reads.

## Testing against the real thing

The hermetic suite never touches the network. The live checks do, and they run
only when a credential is present:

```bash
ANTHROPIC_API_KEY=sk-…  env/bin/python -m pytest tests/test_live_frontier.py -v
OPENAI_API_KEY=sk-…     env/bin/python -m pytest tests/test_live_frontier.py -v

# any other provider
ANNONA_LIVE_ENDPOINT=https://openrouter.ai/api/v1 \
ANNONA_LIVE_MODEL=anthropic/claude-sonnet-5 \
ANNONA_LIVE_KEY=sk-or-… env/bin/python -m pytest tests/test_live_frontier.py -v
```

Four checks per provider: the substrate becomes a backend that answers, a tool
definition survives the round trip, the crossing is placed and recorded, and —
the one worth having — **restricted material does not reach a real provider
that is one HTTP call away and fully credentialed.** Every other guarantee in
this repository is verified against a fake substrate; that one is verified
against the network.

## Two things that were broken until they were tested

Written down because both are the kind of defect that only a live test finds,
and both shipped:

- **`temperature` made every Anthropic request fail.** The adapter sent a
  sampling parameter on every call; current Anthropic models (Opus 5, Opus
  4.8/4.7, Sonnet 5) reject sampling parameters with a 400. A correctly
  configured frontier substrate returned an error on the first request. The
  adapter no longer sends it.
- **The default model was a retired one.** A policy that named `kind:
  anthropic` and omitted `model` got a model ID that no longer resolves. The
  default is now `claude-opus-5`, and the live test fails the day that stops
  being true.
