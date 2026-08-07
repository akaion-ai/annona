# Opening it on macOS

The macOS builds are **ad-hoc signed and not notarised**. Notarisation needs a
paid Apple Developer account and a signing certificate; during the beta there is
neither, and pretending otherwise in the download page would be worse than
saying it here.

So the first launch takes one extra step, and **which step depends on your macOS
version** — the answer changed, and instructions written before it are worse than
none.

## macOS 15 (Sequoia) and later — including macOS 26

Apple removed the right-click → **Open** bypass in macOS 15. An unsigned,
un-notarised app that carries a quarantine flag is no longer something you can
approve at launch: macOS reports it as damaged and **moves it to the Trash**.

Verified on macOS 26.4.1, from a real download of the published `.dmg`: the app
was copied to `/Applications`, launched once, and was in `~/.Trash` seconds
later. The bundle was not corrupt — `codesign --verify --deep --strict` passes on
it — it was simply not notarised and not permitted.

Clear the quarantine flag on the **disk image, before you open it**:

```bash
xattr -dr com.apple.quarantine ~/Downloads/Annona_*.dmg
```

Then open it and drag **Annona** to Applications. The app inherits no quarantine
flag and launches normally, this time and every time.

If the app has already been trashed, drag it back out, run the same command
against `/Applications/Annona.app`, and launch it again.

## macOS 14 (Sonoma) and earlier

1. Open the `.dmg` and drag **Annona** to Applications.
2. **Right-click the app → Open** (not a double-click), then confirm.

macOS remembers the decision; every later launch is a normal one. If the
right-click menu offers no **Open**, use **System Settings → Privacy & Security**,
scroll to the bottom, and press **Open Anyway** next to the message about Annona.

## What you are actually being told

That Apple has not checked this app, which is true. Downloading it from
[the releases page](https://github.com/akaion-ai/annona/releases) and building it
yourself from the same tag are the two ways to know what it is; the source is the
whole repository.

Removing a quarantine flag is a real decision — it is the mark macOS puts on
files that came from the internet, and stripping it from something you have no
reason to trust is how people get compromised. It is documented here because this
project cannot yet spare you the choice, not because it is a habit worth having.
Two routes avoid it entirely: `pip install annona`, and building from source.

## If macOS says the app is "damaged"

That message is different, and it means something specific: the signature on the
bundle is **invalid**, not merely unrecognised. macOS then offers only "Move to
Trash", and it is right to.

Builds before **v0.1.0 (1 August 2026)** had exactly this defect. Tauri's default
produces a *linker-signed* binary whose bundle has no sealed resources —

```console
$ codesign -dv --verbose=2 Annona.app
CodeDirectory ... flags=0x20002(adhoc,linker-signed)
Info.plist=not bound
Sealed Resources=none

$ spctl -a -vvv Annona.app
Annona.app: code has no resources but signature indicates they must be present
```

— which is fine until the file carries a quarantine flag, and fatal the moment it
does. A locally built copy works; the same build, downloaded, is "damaged". The
fix is `signingIdentity: "-"` in `tauri.conf.json`, which seals the bundle
properly:

```console
$ codesign -dv --verbose=2 Annona.app
Sealed Resources version=2 rules=13 files=2
Info.plist entries=17

$ spctl -a -vvv Annona.app
Annona.app: rejected          # "unidentified developer" — the recoverable one
```

If you have such a build, download it again. If you would rather repair the copy
you have:

```console
$ xattr -dr com.apple.quarantine /Applications/Annona.app
$ codesign --force --deep --sign - /Applications/Annona.app
```

Understand what the first line does before you run it: it removes the mark macOS
puts on files that came from the internet. Doing that to a file you have no
reason to trust is how people get compromised. It is here because the signature
was our mistake, not because stripping quarantine is a habit worth having.

## What would remove the step entirely

Notarisation: an Apple Developer account, a Developer ID certificate, and
`notarytool` in the release workflow. It is on the list, and on macOS 15+ it is
no longer a nicety — the version that shipped before this page was updated was
being trashed on launch rather than merely warned about, which is the difference
between an extra click and an install that silently does not happen.
