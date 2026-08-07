#!/usr/bin/env bash
# Ad-hoc sign the macOS app bundle, then build the .dmg around the signed copy.
#
# Why this exists, in order of discovery:
#
# 1. Tauri's default leaves a *linker-signed* bundle: `Info.plist=not bound`,
#    `Sealed Resources=none`. That is fine locally and fatal once the file
#    carries a quarantine flag — macOS reports the app as **damaged** and offers
#    only "Move to Trash". Users downloading v0.1.0 hit exactly this.
#
# 2. `bundle.macOS.signingIdentity: "-"` fixes the seal and breaks the app a
#    different way: it signs *nested* binaries too, including the PyInstaller
#    sidecar. A onefile sidecar extracts its own Python.framework at runtime, and
#    that framework still carries the signature PyInstaller gave it, so the
#    loader refuses it:
#
#        Failed to load Python shared library ... mapping process and mapped
#        file (non-platform) have different Team IDs
#
#    The window opens and the daemon never starts.
#
# 3. Signing the bundle *without* `--deep` does both jobs: resources are sealed,
#    and the sidecar keeps the self-consistent signature PyInstaller produced.
#    Verified: `codesign --verify --strict` passes, Gatekeeper's verdict becomes
#    the recoverable "rejected" (unidentified developer) rather than an invalid
#    signature, and the daemon comes up.
#
# The .dmg is then built here rather than by Tauri, because Tauri creates the
# .app and the .dmg in one pass — there is no point in between at which the
# bundle can be signed.
#
# 4. The updater archive has to be rebuilt after signing, and this is not
#    optional. `tauri build` writes `Annona.app.tar.gz` from the bundle *as it
#    was when it bundled it* — which, on the ad-hoc path, is before this script
#    signs anything. Shipping that archive means every auto-update delivers the
#    unsigned, unsealed bundle: the "damaged, move to Trash" state that step 1
#    exists to prevent, reintroduced through the update channel where nobody
#    would look for it. So the archive is repacked from the signed app and
#    re-signed with the updater key.
#
#    With a real Developer ID (APPLE_SIGNING_IDENTITY set), Tauri signs during
#    the build and its own archive is already correct — the repack is harmless
#    there, and it keeps one code path instead of two.
#
# Usage: macos-sign-and-dmg.sh <target-triple> [version]

set -euo pipefail

TARGET="${1:?usage: macos-sign-and-dmg.sh <target-triple> [version]}"
VERSION="${2:-$(python3 -c "import json;print(json.load(open('ui/src-tauri/tauri.conf.json'))['version'])")}"
# A Developer ID identity when the release has one, ad-hoc otherwise. Ad-hoc is
# a valid signature that Apple has not vouched for: Gatekeeper says
# "unidentified developer", which since macOS 15 is not a dialog a user can
# click through. See docs/getting-started/macos-gatekeeper.md.
IDENTITY="${APPLE_SIGNING_IDENTITY:-${MACOS_SIGNING_IDENTITY:--}}"

BUNDLE_DIR="ui/src-tauri/target/${TARGET}/release/bundle"
APP="${BUNDLE_DIR}/macos/Annona.app"

[ -d "$APP" ] || { echo "::error::no app bundle at $APP — build with --bundles app first" >&2; exit 1; }

if [ "$IDENTITY" = "-" ]; then
  echo "→ signing $APP ad-hoc (no Developer ID in this build)"
  codesign --force --sign - "$APP"
else
  # --options runtime is what notarisation requires; Apple rejects a submission
  # without the hardened runtime, and the rejection arrives minutes later from
  # a service rather than from the build.
  echo "→ signing $APP with '${IDENTITY}' and the hardened runtime"
  codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
fi
codesign --verify --strict "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "Signature|Sealed Resources|Info.plist"

# ── Notarisation ──────────────────────────────────────────────────────────────
#
# Only with a real identity and credentials. Apple does not notarise an ad-hoc
# signature, so on that path this is skipped rather than attempted and failed.
# Two ways to authenticate, and the first is the one to use.
#
#   App Store Connect API key — an issuer, a key id, and a .p8. It is not tied
#   to anybody's personal Apple ID, it carries only the role it was given, and
#   it is revoked in one click without changing anyone's password.
#
#   Apple ID and an app-specific password — the older way. Note *app-specific*:
#   Apple rejects the account password here, so a build configured with one
#   fails minutes later, from a service, with a message about credentials that
#   reads as if the account were wrong.
#
# APPLE_API_KEY_P8 holds the key's contents, because a GitHub secret is a
# string and writing it to a file is this script's job rather than the
# workflow's — a .p8 left in the workspace is a credential in an artefact.
notarise() {
  local artefact="$1"
  if [ "$IDENTITY" = "-" ]; then
    return 0
  fi

  local keyfile=""
  if [ -n "${APPLE_API_KEY_P8:-}" ]; then
    keyfile="$(mktemp -t ascapi).p8"
    printf '%s' "$APPLE_API_KEY_P8" > "$keyfile"
    # Removed however this function exits, including on a failed submission.
    trap 'rm -f "$keyfile"' RETURN
  elif [ -z "${APPLE_ID:-}" ]; then
    echo "::warning::signed but not notarised — no App Store Connect key and no Apple ID"
    return 0
  fi

  echo "→ notarising $artefact (this waits for Apple, typically 1-5 minutes)"
  if [ -n "$keyfile" ]; then
    xcrun notarytool submit "$artefact" --wait \
      --key "$keyfile" \
      --key-id "${APPLE_API_KEY_ID}" \
      --issuer "${APPLE_API_ISSUER}"
  else
    xcrun notarytool submit "$artefact" --wait \
      --apple-id "${APPLE_ID}" \
      --password "${APPLE_PASSWORD}" \
      --team-id "${APPLE_TEAM_ID}"
  fi

  # Stapling puts the ticket inside the file, so the first launch works on a
  # machine that is offline or behind a firewall that eats Apple's OCSP.
  xcrun stapler staple "$artefact"
  xcrun stapler validate "$artefact"
}

notarise "$APP"

# ── The updater archive, repacked from what was actually signed ───────────────
TARBALL="${BUNDLE_DIR}/macos/Annona.app.tar.gz"
if [ -f "$TARBALL" ]; then
  echo "→ repacking $TARBALL from the signed bundle"
  rm -f "$TARBALL" "${TARBALL}.sig"
  # -C so the archive holds `Annona.app` at its root, which is the layout the
  # updater unpacks and replaces in place. Anything else installs a nested
  # directory and the update silently does nothing.
  tar -czf "$TARBALL" -C "${BUNDLE_DIR}/macos" "Annona.app"

  if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
    (cd ui && npx --no-install tauri signer sign "../${TARBALL}")
    [ -f "${TARBALL}.sig" ] || { echo "::error::signing produced no ${TARBALL}.sig" >&2; exit 1; }
    echo "→ signed: ${TARBALL}.sig"
  else
    # Not a warning to skip past: an unsigned archive means this platform has no
    # auto-update in the release, and `latest.json` will omit it.
    echo "::warning::TAURI_SIGNING_PRIVATE_KEY is unset — no updater signature for macOS"
  fi
fi

# Arch label matching Tauri's own naming, so the release assets and every
# download link on the site keep the names they already have.
case "$TARGET" in
  aarch64-apple-darwin) ARCH="aarch64" ;;
  x86_64-apple-darwin)  ARCH="x64" ;;
  *) echo "::error::unexpected target $TARGET" >&2; exit 1 ;;
esac

DMG="${BUNDLE_DIR}/dmg/Annona_${VERSION}_${ARCH}.dmg"
mkdir -p "${BUNDLE_DIR}/dmg"

# None of the intermediate build output is needed once the bundle exists, so it
# goes before the image is built. This is also why the app is *moved* into the
# staging folder rather than copied — on one volume that costs nothing, and it
# halves the peak.
#
# This is housekeeping, not the fix for anything: "No space left on device" from
# `hdiutil` below was read as a full disk and it never was one. The same failure
# reproduced with 124Gi free on the volume this line prints.
echo "→ freeing build intermediates"
rm -rf build/work dist-sidecar \
       "ui/src-tauri/target/${TARGET}/release/incremental" \
       "ui/src-tauri/target/${TARGET}/release/deps" \
       "ui/src-tauri/target/${TARGET}/release/build"
df -h . | tail -1

STAGE_ROOT="$(mktemp -d)"
STAGE="${STAGE_ROOT}/Annona"
mkdir -p "$STAGE"
mv "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
echo "→ building $DMG"

# `hdiutil create -srcfolder ... -format UDZO` — one call, no size, no -fs —
# fails on the Apple Silicon runner with "No space left on device". The message
# is about the image, not the machine: left to size an APFS image on its own,
# hdiutil allocates a container the copy then does not fit into. It reproduced
# with 124Gi free on the volume, always ~5s in, which is the copy hitting the
# end of the image and nothing else.
#
# So: size it explicitly from the staged content with room to spare, on HFS+,
# read/write first, and compress in a second pass. This is what create-dmg does
# and what Tauri's own bundle_dmg.sh did on this same runner when it produced a
# working 93MB dmg — the step below is the only part of that path this script
# had dropped.
SIZE_MB=$(( $(du -sm "$STAGE" | cut -f1) + 200 ))
RW_DMG="${STAGE_ROOT}/rw.dmg"
echo "→ staging ${SIZE_MB}MB read/write image for $(du -sh "$STAGE" | cut -f1) of content"
hdiutil create -volname "Annona" -srcfolder "$STAGE" \
               -fs HFS+ -fsargs "-c c=64,a=16,e=16" \
               -format UDRW -size "${SIZE_MB}m" -ov "$RW_DMG"
hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -ov -o "$DMG"
rm -f "$RW_DMG"

# Put the bundle back where it was: the `app` bundle is a release asset in its
# own right on some platforms, and a step that leaves the tree different from
# how it found it is a step nobody can run twice.
mv "$STAGE/Annona.app" "$APP"
rm -rf "$STAGE_ROOT"

# The dmg is what a person downloads, so it is the file whose ticket decides
# whether the first launch works. Notarising the app inside it is not enough on
# its own: an unstapled disk image makes Gatekeeper ask Apple over the network,
# and the answer on a machine that cannot reach Apple is no.
notarise "$DMG"

echo "→ done: $DMG ($(du -h "$DMG" | cut -f1))"
