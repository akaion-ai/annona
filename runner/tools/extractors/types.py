"""What an extraction is, and what every reader is allowed to assume.

One value type for every format. A DICOM study, a signed invoice and a voice
memo arrive here as the same thing — text the model can read, metadata an
operator can check, and, when the material is genuinely visual, a reference to
the bytes themselves for a substrate that can look at them.

Two decisions are worth stating, because they are the ones that keep the
perimeter honest when the file is a video rather than a paragraph:

**Nothing is decoded eagerly.** :class:`MediaRef` carries a path, never bytes.
The transcript stays small, the ledger keeps digesting paths rather than
megabytes of base64, and the decision about whether an image may be *seen* by a
substrate is taken by placement — after classification, not before.

**A missing optional dependency is a warning, not an exception.** A machine
without ``pydicom`` should still be able to say "this is a CT study of 220
slices, and here is what I could not read", because the alternative is a tool
error the model paraphrases as "the file is corrupt".
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = [
    "Extraction",
    "MediaRef",
    "ReadOptions",
    "missing_dependency",
    "writable_beside",
]

MEDIA_TYPES = ("image", "video", "audio", "pdf")
"""What a vision-capable substrate may be offered. Mirrors datapizza's ``Media``."""


@dataclass(frozen=True)
class MediaRef:
    """Bytes on disk a model could look at, if policy lets it.

    ``derived`` marks something this process wrote — a keyframe cut out of a
    video, a preview rendered from a DICOM slice. It is recorded because a
    derived artefact carries the class of its source while living at a path
    nobody chose, and an operator auditing a run needs to be able to tell the
    two apart.
    """

    path: Path
    media_type: str
    label: str = ""
    derived: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "media_type": self.media_type,
            "label": self.label,
            "derived": self.derived,
        }


@dataclass(frozen=True)
class Extraction:
    """The result of reading one file, whatever the file was."""

    format: str
    text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    media: tuple[MediaRef, ...] = ()
    warnings: tuple[str, ...] = ()

    def warn(self, message: str) -> Extraction:
        """The same extraction, plus one thing the reader should know."""
        if message in self.warnings:
            return self
        return replace(self, warnings=(*self.warnings, message))

    def truncated(self, max_chars: int) -> Extraction:
        """Cut the text to a budget, saying so where the model can see it."""
        if not max_chars or len(self.text) <= max_chars:
            return self
        marker = f"\n\n[... truncated at {max_chars} chars ...]"
        return replace(self, text=self.text[:max_chars] + marker)


@dataclass
class ReadOptions:
    """Everything a reader may vary, and the ceilings it may not exceed.

    The ceilings exist because this code runs on a laptop against files a
    person chose without thinking about their size: a 4 GB video, a zip with
    12,000 members, a spreadsheet with a million rows. A reader that tries to
    be complete on those is a reader that hangs the daemon.
    """

    max_chars: int = 100_000
    sheet_name: str | None = None
    ocr: str = "auto"
    """``auto`` (only when a document has no text layer), ``always``, ``never``."""
    transcribe: str = "auto"
    """``auto`` (when a local speech model is installed), ``always``, ``never``."""
    frames: int = 3
    """Keyframes to cut from a video for a substrate that can see them."""
    max_members: int = 40
    """Members of an archive to look inside."""
    max_media_seconds: float = 1800.0
    """Longest recording this will attempt to transcribe."""
    depth: int = 0
    max_depth: int = 3
    """How far to follow containers: a p7m holding a zip holding a PDF."""
    workdir: Path | None = None
    """Where derived artefacts go. Defaults to ``.annona-derived`` beside the source,
    which keeps them inside the same tree — and therefore the same class — as the
    file they came from, rather than in a temp directory the policy never saw."""

    recurse: Callable[[Path, ReadOptions], Extraction] | None = None
    """Set by the registry before dispatch, so a container can read its contents
    without importing the registry and creating a cycle."""

    def descend(self) -> ReadOptions:
        """The options a nested read runs under."""
        return replace(self, depth=self.depth + 1, workdir=None)

    def derived_dir(self, source: Path) -> Path:
        """Where to put something this process produced from ``source``."""
        if self.workdir is not None:
            self.workdir.mkdir(parents=True, exist_ok=True)
            return self.workdir
        return writable_beside(source, ".annona-derived")

    def read(self, path: Path) -> Extraction:
        """Read a nested file, or explain why this one stops here."""
        if self.recurse is None:
            return Extraction(format="unknown", warnings=("no reader was wired in",))
        if self.depth >= self.max_depth:
            return Extraction(
                format="unknown",
                warnings=(f"stopped at nesting depth {self.max_depth}",),
            )
        return self.recurse(path, self.descend())


def writable_beside(source: Path, name: str) -> Path:
    """``source.parent/name`` when that is writable, else a private fallback.

    Derived artefacts — the document inside a signed envelope, a video's
    keyframes, a cache entry — belong beside the file they came from: same
    directory, same class under the policy, same access control the filesystem
    already applies.

    On an appliance the material is mounted read-only, which is correct and is
    the whole point of mounting it that way. Writing beside the source then
    fails, and the failure is silent and expensive: the p7m never unwraps, the
    cache never hits, and a deployment looks like it is working. So when the
    source tree refuses a write, artefacts go under ``$ANNONA_HOME/derived/``,
    keyed by a digest of the source directory so two files with the same name in
    different folders do not collide.

    The fallback is a private directory of the daemon's own, not a shared one —
    the reasoning in ``docs/design/shared-context.md`` applies here too.
    """
    target = source.parent / name
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".w{os.getpid()}"
        probe.touch()
        probe.unlink()
    except OSError:
        home = Path(os.getenv("ANNONA_HOME", "~/.annona")).expanduser()
        digest = hashlib.sha256(str(source.parent).encode()).hexdigest()[:16]
        target = home / "derived" / name.lstrip(".") / digest
        target.mkdir(parents=True, exist_ok=True)
    return target


def missing_dependency(package: str, what: str, *, extra: str = "formats") -> str:
    """The sentence a reader emits when the optional piece is not installed.

    One phrasing everywhere, and it always names the exact install command:
    "unsupported format" sends someone to an issue tracker, "pip install
    pydicom" sends them back to work.
    """
    return f"{what} needs {package}: pip install {package}   (or: pip install 'annona[{extra}]')"
