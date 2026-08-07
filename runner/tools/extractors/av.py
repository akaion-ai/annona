"""Audio and video — what was said, and what the file is.

A recording is the attachment where the honest answer is most often "this
machine cannot read that yet", and the design follows from taking that
seriously. Three tiers, and every one of them says which it was:

**Always.** Container and stream facts, from ``ffprobe``: duration, codecs,
resolution, channels, and whatever tags the file carries. Free, instant, and
enough to answer half the questions people ask about a recording.

**When a local speech model is installed.** A transcript, produced on this
machine — ``faster-whisper`` if present, ``openai-whisper`` otherwise. Nothing
is uploaded to obtain it. A cloud transcription API would be an egress this
kernel exists to prevent, so there is deliberately no code here that could
perform one.

**When the placed substrate can see.** Keyframes cut out of the video, offered
as images. Whether they are ever looked at is placement's decision.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions, missing_dependency

__all__ = ["ffprobe", "read_audio", "read_video", "still", "transcribe"]

AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr")
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".3gp")

_NO_FFMPEG = (
    "ffmpeg/ffprobe is not installed, so only the file's size is known: "
    "brew install ffmpeg (or: apt install ffmpeg)"
)


def _binary(name: str) -> str | None:
    """``ffprobe``/``ffmpeg``, honouring an explicit override.

    The override exists for machines where these are deliberately kept out of
    ``PATH`` — a bundled build, or an operator who wants the exact binary they
    audited.
    """
    return os.getenv(f"ANNONA_{name.upper()}") or shutil.which(name)


def ffprobe(path: Path) -> dict[str, Any] | None:
    """Container and stream facts, or ``None`` when ffprobe is unavailable."""
    binary = _binary("ffprobe")
    if not binary:
        return None

    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            binary,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        logger.debug(f"ffprobe failed on {path}: {result.stderr[:200]!r}")
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def still(path: Path, target: Path, *, at_seconds: float = 1.0) -> Path | None:
    """One frame out of a video, for whoever needs to look at it.

    Separate from :func:`_keyframes` because the callers want different things:
    that one cuts several frames for a model, this one cuts a single frame for a
    person — the thumbnail in the window that turns "clip.mp4, 4:12" into
    something recognisable at a glance.
    """
    binary = _binary("ffmpeg")
    if not binary:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [
            binary,
            "-y",
            "-ss",
            f"{at_seconds:.2f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-q:v",
            "5",
            str(target),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode == 0 and target.exists():
        return target
    # A clip shorter than the seek point: try the very first frame instead.
    result = subprocess.run(  # noqa: S603
        [binary, "-y", "-i", str(path), "-frames:v", "1", "-q:v", "5", str(target)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return target if result.returncode == 0 and target.exists() else None


def _summary(probe: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The handful of facts worth putting in front of a model."""
    container = probe.get("format", {})
    duration = float(container.get("duration") or 0.0)
    metadata: dict[str, Any] = {
        "duration_seconds": round(duration, 2),
        "container": container.get("format_name", ""),
        "bit_rate": container.get("bit_rate", ""),
        "tags": {k: v for k, v in (container.get("tags") or {}).items() if isinstance(v, str)},
        "streams": [],
    }

    lines = [f"Durata: {_clock(duration)}", f"Contenitore: {metadata['container'] or '—'}"]

    for stream in probe.get("streams", []):
        kind = stream.get("codec_type", "")
        entry = {"type": kind, "codec": stream.get("codec_name", "")}
        if kind == "video":
            entry |= {
                "width": stream.get("width"),
                "height": stream.get("height"),
                "fps": stream.get("r_frame_rate", ""),
            }
            lines.append(
                f"Video: {stream.get('codec_name', '?')} "
                f"{stream.get('width', '?')}×{stream.get('height', '?')} @ "
                f"{stream.get('r_frame_rate', '?')}"
            )
        elif kind == "audio":
            entry |= {
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate", ""),
                "language": (stream.get("tags") or {}).get("language", ""),
            }
            lines.append(
                f"Audio: {stream.get('codec_name', '?')}, "
                f"{stream.get('channels', '?')} canali, {stream.get('sample_rate', '?')} Hz"
            )
        metadata["streams"].append(entry)

    if metadata["tags"]:
        lines.append("Tag: " + ", ".join(f"{k}={v}" for k, v in metadata["tags"].items()))

    return metadata, lines


def _clock(seconds: float) -> str:
    if seconds <= 0:
        return "sconosciuta"
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{remainder:02d}" if hours else f"{minutes:d}:{remainder:02d}"


# ── Audio ─────────────────────────────────────────────────────────────────────


def read_audio(path: Path, opts: ReadOptions) -> Extraction:
    """A recording: what it is, and what was said in it if that can be known here."""
    lines = [f"=== Audio: {path.name} ==="]
    warnings: list[str] = []
    metadata: dict[str, Any] = {"format": path.suffix.lstrip(".").lower() or "audio"}

    probe = ffprobe(path)
    if probe is None:
        warnings.append(_NO_FFMPEG)
    else:
        facts, described = _summary(probe)
        metadata |= facts
        lines += described

    text, note = transcribe(path, opts, duration=metadata.get("duration_seconds", 0.0))
    if text.strip():
        metadata["transcribed"] = True
        lines += ["", "--- Trascrizione (locale) ---", text.strip()]
    elif note:
        warnings.append(note)

    return Extraction(
        format=metadata["format"],
        text="\n".join(lines),
        metadata=metadata,
        media=(MediaRef(path=path, media_type="audio", label=path.name),),
        warnings=tuple(warnings),
    )


# ── Video ─────────────────────────────────────────────────────────────────────


def read_video(path: Path, opts: ReadOptions) -> Extraction:
    """A video: its streams, keyframes for a substrate that can see, and the speech."""
    lines = [f"=== Video: {path.name} ==="]
    warnings: list[str] = []
    metadata: dict[str, Any] = {"format": path.suffix.lstrip(".").lower() or "video"}
    media: list[MediaRef] = [MediaRef(path=path, media_type="video", label=path.name)]

    probe = ffprobe(path)
    if probe is None:
        return Extraction(
            format=metadata["format"],
            text="\n".join(lines),
            metadata=metadata,
            media=tuple(media),
            warnings=(_NO_FFMPEG,),
        )

    facts, described = _summary(probe)
    metadata |= facts
    lines += described

    duration = float(metadata.get("duration_seconds") or 0.0)
    frames = _keyframes(path, opts, duration)
    if frames:
        metadata["keyframes"] = [str(frame.path) for frame in frames]
        media += frames
        lines.append(
            f"Fotogrammi estratti: {len(frames)} "
            "(visibili solo a un substrato che può leggere immagini)"
        )

    has_audio = any(stream.get("type") == "audio" for stream in metadata.get("streams", []))
    if has_audio:
        track = _audio_track(path, opts)
        if track is None:
            warnings.append("the audio track could not be extracted for transcription")
        else:
            text, note = transcribe(track, opts, duration=duration)
            if text.strip():
                metadata["transcribed"] = True
                lines += ["", "--- Trascrizione dell'audio (locale) ---", text.strip()]
            elif note:
                warnings.append(note)
    else:
        lines.append("Nessuna traccia audio.")

    return Extraction(
        format=metadata["format"],
        text="\n".join(lines),
        metadata=metadata,
        media=tuple(media),
        warnings=tuple(warnings),
    )


def _keyframes(path: Path, opts: ReadOptions, duration: float) -> list[MediaRef]:
    """Evenly spaced stills, so "what is in this video" has an answer."""
    binary = _binary("ffmpeg")
    if not binary or opts.frames <= 0 or duration <= 0:
        return []

    derived = opts.derived_dir(path)
    frames: list[MediaRef] = []
    for index in range(opts.frames):
        # Spaced away from both ends: the first and last second of a clip are
        # usually black or a title card.
        at = duration * (index + 1) / (opts.frames + 1)
        target = derived / f"{path.stem}-frame{index + 1}.jpg"
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                binary,
                "-y",
                "-ss",
                f"{at:.2f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(target),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and target.exists():
            frames.append(
                MediaRef(
                    path=target,
                    media_type="image",
                    label=f"{path.name} @ {_clock(at)}",
                    derived=True,
                )
            )
    return frames


def _audio_track(path: Path, opts: ReadOptions) -> Path | None:
    """The speech, as 16 kHz mono WAV — what every local recogniser wants."""
    binary = _binary("ffmpeg")
    if not binary:
        return None

    target = opts.derived_dir(path) / f"{path.stem}-audio.wav"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [binary, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(target)],
        capture_output=True,
        timeout=600,
        check=False,
    )
    return target if result.returncode == 0 and target.exists() else None


# ── Speech ────────────────────────────────────────────────────────────────────


def transcribe(path: Path, opts: ReadOptions, *, duration: float = 0.0) -> tuple[str, str]:
    """Speech to text, on this machine or not at all.

    Returns ``(text, note)``; the note explains an empty result. The model size
    comes from ``ANNONA_WHISPER_MODEL`` and defaults to ``base``, which is the
    largest that stays interactive on a laptop CPU.
    """
    if opts.transcribe == "never":
        return "", ""
    if duration and duration > opts.max_media_seconds:
        return "", (
            f"the recording is {_clock(duration)} long, past the "
            f"{_clock(opts.max_media_seconds)} transcription ceiling for one read"
        )

    size = os.getenv("ANNONA_WHISPER_MODEL", "base")

    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415 — optional, local
    except ImportError:
        pass
    else:
        logger.info(f"transcribing {path.name} locally with faster-whisper:{size}")
        model = WhisperModel(size, device="auto", compute_type="int8")
        segments, _ = model.transcribe(str(path), vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments), ""

    try:
        import whisper  # noqa: PLC0415 — the heavier reference implementation
    except ImportError:
        return "", missing_dependency(
            "faster-whisper", "transcribing speech locally", extra="media"
        )

    logger.info(f"transcribing {path.name} locally with openai-whisper:{size}")
    return str(whisper.load_model(size).transcribe(str(path)).get("text", "")), ""
