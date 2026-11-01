# Where Annona writes

Every file Annona creates, what is in it, and whether it can hold the text of
your steps. If something is on disk and not in this table, it is a bug.

`$ANNONA_HOME` defaults to `~/.annona`, `$AKAION_HOME` to `~/.akaion` (the
older name, kept so existing installations keep working), and the vault to
`~/akaion-brain` (`$AKAION_BRAIN_DIR`, or `--brain-dir`).

## What the perimeter wrote

| Path | Holds | Step text? |
| ---- | ----- | ---------- |
| `$ANNONA_HOME/policy.yaml` | the policy | no |
| `$ANNONA_HOME/ledger.jsonl` | every decision: class, substrate, rule, outcome, and a **digest** of what crossed | no — a digest, never the text |
| `$ANNONA_HOME/logs/annona.log` | what the daemon did, rotated daily, kept 7 days | no — steps, prompts, tool arguments and shell commands appear as their size (`<54 chars>`). `ANNONA_LOG_STEP_TEXT=1` puts them back: for debugging on fictional data |
| `$ANNONA_HOME/link.json`, `$ANNONA_HOME/link/inbox/` | the link to Agents Studio, and answers withheld from it | the inbox: **yes**, that is what it is for |
| `$ANNONA_HOME/pairing.json` | the token that pairs the window, mode 0600 | no |
| `$ANNONA_HOME/skills/` | installed skills | no |
| `$ANNONA_HOME/memory/index.sqlite` | the search index over the vault | **yes**, as the vault does |
| `$ANNONA_HOME/derived/` | extraction artefacts for sources on a read-only mount | **yes** — text extracted from your documents |

## What the daemon and the CLI wrote

| Path | Holds | Step text? |
| ---- | ----- | ---------- |
| `$AKAION_HOME/config.yaml` | daemon settings | no |
| `$AKAION_HOME/.key` | the key that encrypts `auth.json`, mode 0600, created the first time credentials are saved | no |
| `$AKAION_HOME/auth.json` | cloud credentials, encrypted | no |

## Beside your material

| Path | Holds | Step text? |
| ---- | ----- | ---------- |
| `.annona-cache/` next to a document | the text extracted from it, so it is read once | **yes** — the document's text, in the document's own folder, so the folder's class still applies |
| `~/Documents/Annona/Inbox/` (`attachments.inbox`) | files attached in the window | **yes**, they are the files |

## Memory

| Path | Holds | Step text? |
| ---- | ----- | ---------- |
| `~/akaion-brain/notes/*.md` | the vault, as plain markdown | **yes** when `runner.capture_to_brain` is on (the default): every executed step becomes a `local_only` note — the request, the outcome and the answer |
| `~/akaion-brain/.akaion/index.db` | the vault's index, rebuilt from the notes | titles and tags |

`capture_to_brain: false` turns that off. A `local_only` note leaves the machine
only when you push it by hand.

## Nothing is deleted for you

Uninstalling removes the program, not this. The ledger in particular is the
record of what was decided and refused. See
[Uninstall](../getting-started/install.md#uninstall).
