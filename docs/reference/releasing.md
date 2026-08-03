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

Bundles are currently **unsigned**:

- macOS Gatekeeper — right-click → **Open** on first launch.
- Windows SmartScreen — *More info* → *Run anyway*.
- Linux — `chmod +x` the AppImage; install the `.deb` with `sudo dpkg -i`.

Apple notarisation and a Windows EV certificate are planned. Both need extra
repository secrets (`APPLE_*`, `WINDOWS_CERTIFICATE_*`).

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

- `*.dmg`, `*.exe`, `*.AppImage`, `*.deb` — the user-facing bundles
- `*.dmg.sig`, `*.exe.sig`, `*.AppImage.sig` — signatures the manifest needs
- `latest.json` — consumed by the updater plugin

`.deb` has no manifest entry: apt installations update through apt.

## Web UI

The runner serves the same UI over HTTP at `http://127.0.0.1:7070`, in-process,
without Tauri.

- Open it after `./start.sh`.
- Sign-in is Firebase (Google or email/password), and entirely optional.
- The UI is built into `ui/dist/` on first `./start.sh` (needs Node 18+).
- Rebuild with `cd ui && npm install && npm run build`, or
  `./start.sh --rebuild-ui`.
