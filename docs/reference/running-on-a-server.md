# Running it on a server

Everything on this page was checked by building the image for `linux/arm64` —
the DGX Spark's architecture — and running it, not by reading the Dockerfile.
Two defects turned up doing that, and both are fixed; they are recorded at the
bottom because they are the kind that only a real run finds.

## Does it stay up

Yes, in both shapes.

**Container.** `restart: unless-stopped` in the compose file, plus a
`HEALTHCHECK` in the image that polls `/health` every 30s. A container that
stops answering is restarted by Docker; a container that never becomes healthy
shows as `unhealthy` in `docker ps` rather than pretending.

```bash
make up                        # or: docker compose up -d
docker compose ps              # STATUS shows (healthy)
docker compose logs -f annona
```

**Bare metal.** `deploy/annona.service` is a systemd unit with `Restart=on-failure`
and the hardening a process that reads client files should have:
`ProtectSystem=strict`, `ProtectHome=read-only`, an empty
`CapabilityBoundingSet`, `MemoryDenyWriteExecute`, and `ReadWritePaths` limited
to its own state directory. DGX OS is Ubuntu, so this is the native path.

```bash
sudo cp deploy/annona.service /etc/systemd/system/
sudo systemctl enable --now annona
journalctl -u annona -f
```

## What to watch

Four signals, in the order they matter.

| Signal | Where | Alert when |
|---|---|---|
| Liveness | `GET /health` | not 200 for two consecutive probes |
| Enforcement | `GET /api/kernel/status` → `enforcing` | `false` — the perimeter is not on, and the reason field says why |
| Refusals | same → `held` | the rate rises: either the policy has drifted from the work, or a substrate has gone away |
| Ledger integrity | `GET /api/kernel/ledger/verify` → `ok` | `false`, ever — the hash chain is broken and that is an incident, not a warning |

```bash
curl -s localhost:7070/api/kernel/status | jq '{enforcing, substrates, decisions, held, last_decision_at}'
curl -s localhost:7070/api/kernel/ledger/verify | jq '{ok, entries, problem}'
annona substrates            # which substrates answer right now
```

`last_decision_at` is the one people forget: a kernel that is healthy and has
decided nothing for a day is usually a client that stopped talking to it, and
nothing else will tell you.

There is no Prometheus endpoint yet. `/api/kernel/status` is JSON with counters
in it, which is enough for a scrape script or a Checkmk/Zabbix check; a real
`/metrics` is a good first contribution.

## Logs

In a container, to stdout — `docker compose logs`, or whatever ships journald.
Under systemd, to the journal. In a source install, also to `logs/annona.log`.
Level via `AKAION_LOG_LEVEL`.

The logs are for operating the process. **The record of what it decided is the
ledger**, which is append-only, hash-chained, and verifiable offline — that is
the artefact to keep and to back up, not the log file.

## Back up two things

```
~/.annona/          # the policy and the ledger  (annona-home volume)
~/akaion-brain/     # the vault                  (annona-vault volume)
```

Everything else is reproducible from the image. The ledger is append-only, so
an incremental backup is cheap and `annona audit --verify` on the restored copy
proves the restore did not lose or alter anything.

## Upgrading

```bash
docker compose pull && docker compose up -d      # volumes survive
annona policy validate                           # before, on the new image
```

The policy schema is versioned and the loader refuses a document it does not
fully understand rather than starting half-configured — so a bad upgrade fails
at start, loudly, instead of quietly enforcing less.

## Reaching it from another machine

The port binds to loopback on purpose: `127.0.0.1:7070`. Anything that can
reach it has the operator's access, because **there is no per-user identity
yet** — see `docs/design/shared-context.md`. On a shared DGX that is the fact to
plan around: put it behind a tunnel or an authenticating reverse proxy, one
instance per person, until subject identity lands.

`annona pair` authorises a *remote origin* with a token. That is authorisation,
not identity: it says which app may use this machine, not which person may see
which matter.

## What was actually broken

Both found by building and running the image, and both fixed:

- **The appliance would not start unattended.** `annona run` exited with "run
  `annona init` first", and `annona init` prompts for answers — in a container
  there is no TTY, so the documented `docker compose up -d` started and
  immediately stopped. `run` now writes the defaults (local-only, cloud off) and
  says so; `init` detects a missing terminal and does the same instead of
  raising on the first prompt.
- **Derived artefacts could not be written.** The compose file mounts your
  material read-only — correctly — and the readers write beside the source: the
  document unwrapped from a `.p7m`, a video's keyframes, the extraction cache.
  On the appliance every one of those failed silently, so signed invoices never
  unwrapped and the cache never hit. They now fall back to
  `$ANNONA_HOME/derived/`, keyed by a digest of the source directory.

And one thing that was missing rather than broken: the image had no `ffmpeg`,
so the box people would most want to drop a recorded call on could not read
audio or video. It is in the image now. OCR and speech models are still opt-in
— they are hundreds of megabytes of language data and weights — and
`GET /api/kernel/formats` reports exactly which families this installation can
read, with the install line for the ones it cannot.
