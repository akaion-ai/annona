"""A calibrated judge behind a Jev-compatible endpoint, as a content model (layer L1).

Any server that answers ``POST /v1/systemone`` with a ``choice`` question can
judge text: rizzo-flow serving its base model, or a model specialised for
commercial sensitivity (Iovis) served by the same runtime. The endpoint returns
a probability per option; this adapter turns them into a class with one number
the operator chooses.

**The threshold is a leak budget, not a confidence.** ``tau`` is compared with
the *strict mass* — the probability of ``restricted`` plus whatever the judge
put on "cannot tell". Fitted on labelled restricted documents as the
floor(alpha(n+1))-th smallest strict mass, it makes the rate at which a
restricted text is judged lower at most ``alpha`` (split conformal, under
exchangeability). That bound composes with the perimeter: the class only ever
rises, and placement is sound for the class it is given, so the judge's miss
rate bounds how often a restricted payload is *placed* where it may not go.

Adapter, not dependency: it talks HTTP to a server the operator runs, and a
deployment without one behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from runner.kernel.errors import BackendUnavailableError
from runner.kernel.types import SensitivityClass

__all__ = ["DEFAULT_ENDPOINT", "SystemOneJudge", "conformal_threshold"]

DEFAULT_ENDPOINT = "http://127.0.0.1:8017"

QUESTION = {
    "type": "choice",
    "instructions": (
        "How sensitive is this business document for the company that holds it, "
        "if it were read by someone outside the company?"
    ),
    "criteria": {
        "public": "Public: meant to be read by anyone, such as a datasheet, a brochure, "
        "a press release or a public tender.",
        "internal": "Internal: for the company's own people, harmless if seen outside, "
        "such as a generic procedure or routine meeting notes.",
        "restricted": "Restricted: would harm the company or a counterpart if seen "
        "outside, such as prices, offers, a customer's request or specification, an "
        "NDA or a contract.",
    },
}
"""The class question, worded as Iovis is trained on it (``iovis-questions-v2``)."""


def conformal_threshold(strict_masses: Sequence[float], alpha: float) -> float:
    """The floor(alpha(n+1))-th smallest strict mass of truly restricted texts.

    With ``tau`` set this way, a new restricted text whose strict mass falls
    below it — a miss — occurs with probability at most ``alpha`` when it is
    exchangeable with the calibration texts. Too few texts for ``alpha`` means
    no threshold can promise it: raise, rather than return one that cannot.
    """
    ordered = sorted(strict_masses)
    k = int(alpha * (len(ordered) + 1))
    if k < 1:
        raise ValueError(
            f"{len(ordered)} restricted calibration texts cannot support alpha={alpha}; "
            f"need at least {int(1 / alpha)}"
        )
    return ordered[k - 1]


class SystemOneJudge:
    """Satisfies :class:`runner.kernel.ports.ContentModel`."""

    def __init__(
        self,
        *,
        tau: float,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = "",
        internal_at: float = 0.5,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not 0.0 < tau <= 1.0:
            raise ValueError("tau is a probability threshold in (0, 1]")
        self._tau = tau
        self._internal_at = internal_at
        self._url = endpoint.rstrip("/") + "/v1/systemone"
        self._model = model
        self._client = client or httpx.Client(timeout=timeout)

    def probabilities(self, text: str) -> Mapping[str, float]:
        body: dict[str, Any] = {"state": text, "questions": {"sensitivity": QUESTION}}
        if self._model:
            body["model"] = self._model
        try:
            response = self._client.post(self._url, json=body)
            response.raise_for_status()
            answer = response.json()["answers"]["sensitivity"]
            return {str(k): float(v) for k, v in answer["probabilities"].items()}
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise BackendUnavailableError(f"sensitivity judge at {self._url}: {exc}") from exc

    def classify(self, text: str) -> SensitivityClass:
        p = self.probabilities(text)
        known = p.get("public", 0.0) + p.get("internal", 0.0) + p.get("restricted", 0.0)
        # Mass on anything else ("cannot tell", an option the server added) is
        # strict: a judge that does not know has not cleared the text.
        strict = p.get("restricted", 0.0) + max(0.0, 1.0 - known)
        if strict >= self._tau:
            return SensitivityClass.RESTRICTED
        if p.get("internal", 0.0) + strict >= self._internal_at:
            return SensitivityClass.INTERNAL
        return SensitivityClass.PUBLIC
