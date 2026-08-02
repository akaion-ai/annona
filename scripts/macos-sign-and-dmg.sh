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
# Usage: macos-sign-and-dmg.sh <target-triple> [version]

set -euo pipefail

TARGET="${1:?usage: macos-sign-and-dmg.sh <target-triple> [version]}"
VERSION="${2:-$(python3 -c "import json;print(json.load(open('ui/src-tauri/tauri.conf.json'))['version'])")}"
IDENTITY="${MACOS_SIGNING_IDENTITY:--}"

BUNDLE_DIR="ui/src-tauri/target/${TARGET}/release/bundle"
APP="${BUNDLE_DIR}/macos/Annona.app"

[ -d "$APP" ] || { echo "::error::no app bundle at $APP — build with --bundles app first" >&2; exit 1; }

echo "→ signing $APP with identity '${IDENTITY}' (no --deep, on purpose)"
codesign --force --sign "$IDENTITY" "$APP"
codesign --verify --strict "$APP"
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "Signature|Sealed Resources|Info.plist"

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

echo "→ done: $DMG ($(du -h "$DMG" | cut -f1))"
