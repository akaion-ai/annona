"""Turning a file — any file — into something a model can read.

This package is the answer to the only question a person asks of an assistant
that lives on their machine: "read this". What "this" is varies more than any
tool interface admits — a signed invoice, a scan with no text layer, a voice
memo, a CT slice, a zip somebody emailed — and each of those needs a different
piece of code and produces a different kind of truth.

The contract is one value type, :class:`~runner.tools.extractors.types.Extraction`,
and one entry point, :func:`extract`. Everything else is a reader for a family
of formats, registered by extension in
:mod:`~runner.tools.extractors.registry`.

Nothing in here knows about the perimeter, and that is deliberate: extraction
produces material, and deciding what may be done with material is somebody
else's job. What this package *does* owe the perimeter is honesty — every
degraded read (a missing dependency, a truncated archive, an unverified
signature) comes back as a warning rather than as silence.
"""

from runner.tools.extractors.registry import (
    FAMILIES,
    READERS,
    capabilities,
    extract,
    family_for,
    is_readable,
    supported_extensions,
)
from runner.tools.extractors.types import Extraction, MediaRef, ReadOptions

__all__ = [
    "FAMILIES",
    "READERS",
    "Extraction",
    "MediaRef",
    "ReadOptions",
    "capabilities",
    "extract",
    "family_for",
    "is_readable",
    "supported_extensions",
]
