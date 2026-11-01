"""What a log line may say about material: its size, not its words (#37).

The log sits beside the ledger and keeps none of its care. The ledger records a
digest of what crossed; the log, until 0.1.3, recorded the step itself — a
codice fiscale typed in a prompt landed in ``logs/annona.log`` in clear, rotated
for a week, copied into bug reports, readable by whoever can read the home.
"""

from __future__ import annotations

import os

__all__ = ["shown"]


def shown(text: object) -> str:
    """``<N chars>``, or the text itself when ``ANNONA_LOG_STEP_TEXT=1``.

    The variable is for debugging on fictional data. It is read on every call,
    not once at import, so turning it on needs no restart and a test can set it.
    """
    value = str(text)
    if os.getenv("ANNONA_LOG_STEP_TEXT") == "1":
        return repr(value)
    return f"<{len(value)} chars>"
