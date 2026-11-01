# Configuration

Two layers: a YAML file at `~/.akaion/config.yaml` and environment variables.
`akaion init` writes the file; every key has a default, so a missing file is a
working configuration rather than an error.

## The whole file

```yaml
cloud:
  enabled: false                  # opt-in. false = the runner never contacts a host
  api_url: https://api.prod.akaion.com
  polling_interval: 5
  timeout: 30

ai:
  provider: akaion                # akaion | anthropic | echo | openai | google | local
  model: claude-opus-4-6
  temperature: 0.7
  max_tokens: 4000

  anthropic:
    api_key: null                 # or the ANTHROPIC_API_KEY environment variable
  local:
    endpoint: http://localhost:11434    # Ollama
  echo:
    script: []                    # see "The echo provider" below

tools:
  enabled: [filesystem, shell, document_reader, explorer]

permissions:
  filesystem:
    allowed_paths: [~/Documents, ~/Downloads]
    denied_paths: [~/.ssh]
    max_file_size_mb: 100
  shell:
    enabled: true
    allowed_commands: [ls, cat, grep, find, git]
  network:
    enabled: true

runner:
  mode: daemon
  max_concurrent_tasks: 3
  retry_attempts: 3
  capture_to_brain: true          # save every executed task as a local note (its text included)

logging:
  level: INFO
  file: logs/annona.log          # under $ANNONA_HOME; never the text of a step
```

## Permissions

!!! danger "An empty allow-list permits everything"
    `PermissionManager` is allow-by-default: a tool name it does not recognise is
    permitted, and a category whose allow-list is empty permits every call in that
    category. The *safe* configuration is the verbose one, which is backwards.

    Until that inverts (Phase 1, invariant I1 in
    [Sovereign runtime](../design/sovereign-runtime.md)), **write your allow-lists
    explicitly**. An empty `allowed_paths` does not mean "no filesystem access";
    it means "all of it".

    This only applies to an installation with no policy. A fresh install writes
    one alongside its config and is default-deny; an older installation without
    one says it is unenforced on every `annona run` and in `annona status`. See
    [The perimeter](perimeter.md).

`denied_paths` is checked first and always wins, so it is the reliable half of the
model today.

## Providers

| `provider` | Reaches a model | Calls tools | Notes |
|---|---|---|---|
| `akaion` | yes | **yes** | Control plane. Sends the whole transcript — see below |
| `anthropic` | yes | **yes** | Needs `ANTHROPIC_API_KEY` or `ai.anthropic.api_key` |
| `echo` | no | **yes** | Scripted, offline. No credentials, no network |
| `openai` | yes | no | Chat only until Phase 2 |
| `google` | yes | no | Chat only until Phase 2 |
| `local` | yes | no | Ollama, chat only until Phase 2 |

!!! warning "`akaion` and `anthropic` send everything the agent reads"
    The transcript accumulates tool results, so a task that reads a file sends
    that file's contents on the next turn. Local tool execution is not local data
    handling. If that matters for your data, the only Phase 0 provider for which
    nothing leaves the process is `echo` — and Phase 2 gives you a real local
    model with the same property.

### The echo provider

Deterministic, offline, and useful beyond testing: it drives an exact sequence of
tool calls without spending tokens or hoping a model cooperates, which makes it
the fastest way to check a policy.

```yaml
ai:
  provider: echo
  echo:
    script:
      - text: "Let me look at that directory."
        tools:
          - name: explorer
            arguments: { operation: map, path: "~/Studio" }
      - text: "Done — the tree is above."
```

A turn carrying `tools` continues the loop; a turn without them ends it — the same
rule every backend follows. A malformed script raises `ConfigurationError` at
startup rather than being partially applied.

## Environment variables

Read from `~/.akaion/.env` or the shell. `start.sh` loads `.env.prod` / `.env.dev`
via `RUNNER_ENV=prod|dev`.

| Variable | Purpose | Default |
|---|---|---|
| `AKAION_API_BASE` | Cloud backend host | `https://api.prod.akaion.com` |
| `AKAION_HOME` | Config, auth and vault parent | `~/.akaion` |
| `AKAION_BRAIN_DIR` | Vault directory | `~/akaion-brain` |
| `AKAION_LOG_LEVEL` | Log level | `INFO` |
| `AKAION_CAPTURE_TO_BRAIN` | Capture runs as notes (`0`/`1`) | `1` |
| `ANTHROPIC_API_KEY` | Anthropic credentials | — |
| `OPENAI_API_KEY` | OpenAI credentials | — |
| `GOOGLE_API_KEY` | Google credentials | — |

## Verifying a configuration

```bash
akaion status                 # config, vault stats, cloud connection
akaion cloud disable          # back to pure local
make demo                     # prove the loop and the policy work, offline
```

To confirm nothing reaches the network, the strongest check is the simplest:
disable cloud, set `ai.provider: echo`, and watch the interface. There is nothing
for it to talk to.
