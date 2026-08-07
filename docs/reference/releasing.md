# Releasing

Publisher documentation: cutting a release, signing, and the auto-updater. If you
are here to use the runner rather than ship it, you want
[Quickstart](../getting-started/quickstart.md) instead.

Cross-platform bundles are built by `.github/workflows/release.yml`, which runs
**only** on a `v*` tag push or a manual dispatch.

## Cutting a release

### 1. Bump the version

These must match, or the bundles come out with inconsistent names:

- `ui/package.json` → `version`
- `ui/src-tauri/Cargo.toml` → `[package] version`
- `ui/src-tauri/tauri.conf.json` → `version`
- `pyproject.toml` → `[project] version`

### 2. Tag and push

```bash
git add ui/package.json ui/src-tauri/Cargo.toml ui/src-tauri/tauri.conf.json pyproject.toml
git commit -m "release: v0.2.0"
git tag v0.2.0
git push origin main --tags
```

### 3. What CI does

The `build` job runs in parallel on four runners: `macos-14` (Apple silicon),
`macos-15-intel` (Intel), `windows-latest`, `ubuntu-22.04`. Each one sets up
Python 3.11, Node 20 and stable Rust, builds the UI with Vite, produces the
PyInstaller sidecar, runs `tauri build --target <triple>`, and uploads the
artefacts.

> `macos-13` was retired in December 2025 and its label no longer resolves to a
> machine. A job asking for it queues until the run is cancelled, which is how
> v0.1.0 first stalled.

The `release` job runs only on a tag push. It collects every artefact, flattens
them into `release-assets/`, builds the updater manifest, and **publishes** the
GitHub Release.

### 4. Check the result

Neither a draft nor a prerelease, and both are deliberate:

- A **draft** is invisible to everyone but the maintainer, so
  `releases/latest/download/...` does not resolve to it — every download button
  on the site 404s while the run shows green.
- GitHub's `latest` pointer **skips prereleases**, with the same result.

"Beta, unsigned" is said in words, in the release body and on the site, which is
where that belongs. So there is nothing to press: check the published release has
all the expected assets — two `.dmg`, one `.exe`, one `.AppImage`, one `.deb`,
plus the `.sig` files and `latest.json`; the Windows `.msi` is optional.

### Smoke-testing without releasing

GitHub → Actions → **release** → **Run workflow**, and pick a branch. The
`release` job is skipped (it is gated on `startsWith(github.ref, 'refs/tags/v')`),
and the bundles are downloadable from the run's artefacts for 14 days.

## The Python package

`pip install annona` is advertised in the README and on the site, so it has to
work. `.github/workflows/publish-pypi.yml` builds the sdist and wheel and
publishes them on every **published** GitHub Release, keeping the desktop
bundles and the Python package on the same version by construction.

Uploads use **PyPI Trusted Publishing** (OIDC), not an API token: no credential
is stored in repository secrets, and none can leak from a repo that never held
one. It needs two things to exist, once, and neither can be created from CI:

1. A pending publisher at <https://pypi.org/manage/account/publishing/> —
   project `annona`, owner `akaion-ai`, repository `annona`, workflow
   `publish-pypi.yml`, environment `pypi`.
2. A `pypi` environment under **Settings → Environments**.

What the wheel contains is `runner*` and nothing else, which has one consequence
worth knowing before someone reports it as a bug: **the web UI is not in the
Python package.** `_UI_DIST` resolves relative to the installed package, so a
pip installation has no `ui/dist` and `runner/local_api.py` skips the static
mount with a warning. The CLI and the daemon API are complete; the window is
what the desktop bundles are for.

### Time and cost

A first build with a cold cargo cache takes 25–35 minutes, dominated by macOS
Apple silicon; subsequent builds run 10–15 minutes. **macOS runners bill at ten
times the Linux rate**, so do not wire this workflow to every commit.

## Signing

### macOS

The release pipeline signs and notarises **when the secrets exist**, and falls
back to an ad-hoc signature when they do not. The difference is not cosmetic:

| | Gatekeeper's verdict | What a user has to do |
|---|---|---|
| Notarised | accepted | nothing |
| Ad-hoc (today) | *damaged* on macOS 15+ | `xattr -dr com.apple.quarantine` on the dmg, **before** opening it |

Since macOS 15 the right-click → *Open* bypass no longer exists; an un-notarised
app carrying a quarantine flag is moved to the Trash. So on the ad-hoc path the
download does not merely warn, it fails, and the release notes have to say so.

To switch the pipeline on, add these repository secrets. Absent any of them, the
build takes the ad-hoc path and says which one it took.

Signing, always:

| Secret | What it is |
|---|---|
| `APPLE_CERTIFICATE` | base64 of the **Developer ID Application** `.p12` |
| `APPLE_CERTIFICATE_PASSWORD` | the password the `.p12` was built with |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Name (TEAMID)` |
| `APPLE_TEAM_ID` | the 10-character team identifier |

Notarising, one set or the other. **Prefer the API key**: it is not tied to
anybody's personal Apple ID, it carries only the role it was granted, and it is
revoked in a click without changing anyone's password.

| Secret | What it is |
|---|---|
| `APPLE_API_KEY_P8` | the contents of the `.p8` App Store Connect key |
| `APPLE_API_KEY_ID` | the key's ID, from the same page |
| `APPLE_API_ISSUER` | the issuer UUID, shown once above the key list |

The `.p8` downloads **once** and Apple never shows it again. App Store Connect →
Users and Access → Integrations → App Store Connect API → **+**, role Developer.

The older way, if you must:

| Secret | What it is |
|---|---|
| `APPLE_ID` | the Apple ID that owns the membership, an email address |
| `APPLE_PASSWORD` | an **app-specific password** — `xxxx-xxxx-xxxx-xxxx`, from appleid.apple.com |

Apple rejects the account password here. A build configured with one fails from
notarytool minutes later with a message about credentials that reads as if the
account were wrong, which is how an afternoon goes into checking an account that
was fine.

Creating the certificate needs the Account Holder or Admin role, and it is not
the "Apple Development" certificate Xcode makes for you — that one cannot sign
for distribution and Apple will not notarise with it. Xcode → Settings →
Accounts → Manage Certificates → **+** → *Developer ID Application*, then export
it from Keychain Access as `.p12`.

    base64 -i DeveloperID.p12 | pbcopy      # what goes in APPLE_CERTIFICATE

### Windows

Still unsigned; SmartScreen warns and a user clicks *More info* → *Run anyway*.
An EV certificate is the fix and has not been bought.

### Linux

No signing. `chmod +x` the AppImage; install the `.deb` with `sudo dpkg -i`.

## Auto-update

On launch the app makes a silent GET to the GitHub manifest at
`releases/latest/download/latest.json`. If the published version is newer than
the installed one, a non-blocking banner appears:

```
┌─────────────────────────────────────────────┐
│ ^ Akaion Runner 0.2.0 available             │
│ [Update now] [Later]                        │
└─────────────────────────────────────────────┘
```

- **Update now** — the Tauri plugin downloads the bundle, verifies its signature
  against the public key embedded in the app, applies it and relaunches.
- **Later** — dismissed for this session; it returns on the next launch.
- No network, or no manifest response within 10 seconds → silence, no banner.
- In web mode (`npm run dev` in a browser) the banner never appears.

### One-time publisher setup

Auto-update needs a **Tauri signing keypair**, separate from Apple and Windows
code signing. Once, before the first tag with auto-update enabled:

1. Generate the keys:

    ```bash
    ./scripts/generate-updater-keys.sh
    ```

    It asks for a password — do not skip it — and writes
    `~/.tauri/akaion-runner.key`, printing the public key.

2. In `ui/src-tauri/tauri.conf.json`, set `plugins.updater.pubkey` to the printed
   value. Commit and push: releases now verify bundles against this key.

3. Repository → Settings → Secrets and variables → Actions:

    - `TAURI_SIGNING_PRIVATE_KEY` — the full contents of `~/.tauri/akaion-runner.key`
    - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` — the password you set

4. **Back the key file up off this machine.** If it is lost, every installed
   client rejects all future updates with a signature mismatch, and the only
   remedy is asking users to reinstall by hand.

5. Optionally keep a local `SIGNING.md` — it is gitignored — recording where the
   key lives, the password-manager entry, and the rotation date.

### What CI publishes

Into `release-assets/`:

- `*.dmg`, `*.exe`, `*.AppImage`, `*.deb` — what a **person** downloads
- `*.app.tar.gz` (macOS), `*-setup.exe` (Windows), `*.AppImage` (Linux) — what the
  **updater** downloads, each with its `.sig`
- `latest.json` — consumed by the updater plugin

Those are not the same list, and confusing them is how the updater stayed broken
through two releases. **macOS never produces a `.dmg.sig`**: the updater replaces
an app bundle in place and a disk image is not one, so the signed artefact is the
`.app.tar.gz`. A manifest that looks for `.dmg.sig` finds nothing, skips both
Apple platforms with a warning, and publishes a manifest with no macOS entry.

`.deb` has no manifest entry: apt installations update through apt.

### Building locally now fails without the key

`bundle.createUpdaterArtifacts: true` plus a `pubkey` in the config makes Tauri
**fail the build** when `TAURI_SIGNING_PRIVATE_KEY` is unset, rather than quietly
producing an unsigned artefact. That is the behaviour worth having in CI — a
release whose updater silently does not work is worse than one that stops — but
it means a local `npx tauri build` errors with:

    A public key has been found, but no private key.

For local work, generate a throwaway key and use that. It will not match the
published pubkey, and Tauri warns about exactly that, which is correct: bundles
built this way must not be shipped.

```bash
npx tauri signer generate -w /tmp/local.key -p ""
export TAURI_SIGNING_PRIVATE_KEY="$(cat /tmp/local.key)"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""
```

Note the variable holds the key's **contents**. `TAURI_SIGNING_PRIVATE_KEY_PATH`
is documented by the CLI but the bundler in 2.11 asks for the contents, and the
build fails with the message above if only the path is set.

## Web UI

The runner serves the same UI over HTTP at `http://127.0.0.1:7070`, in-process,
without Tauri.

- Open it after `./start.sh`.
- Sign-in is Firebase (Google or email/password), and entirely optional.
- The UI is built into `ui/dist/` on first `./start.sh` (needs Node 18+).
- Rebuild with `cd ui && npm install && npm run build`, or
  `./start.sh --rebuild-ui`.
