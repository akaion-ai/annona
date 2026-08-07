"""rizzo-pii as a redactor (layer L1).

`rizzo-pii <https://github.com/Rizzo-AI-Academy/rizzo-pii>`_ is a 0.3B Italian
PII model by Simone Rizzo (Rizzo AI Academy), MIT licensed, that runs on a CPU
with no API key and recognises 22 categories — including *codice fiscale*,
*partita IVA* and cadastral identifiers, which general-purpose PII models do not
cover. It returns the anonymised text together with the reverse mapping, and
keeps that mapping on the machine.

That is precisely the instrument this project was missing, and the division of
labour is clean:

- **rizzo-pii decides what is an identifier.** It is better at that than any
  regex this repository would ever contain.
- **Annona decides whether the redacted text may cross, and records it.** A
  redactor cannot grant permission; it can only make a payload less sensitive,
  and the perimeter reclassifies the result before believing it.

Adapter, not dependency: this module talks HTTP to a server the operator runs.
Nothing else in the codebase imports it, the policy refers to it by name, and a
deployment without it behaves exactly as before — redaction is simply not an
available action.

Run the server with::

    python src/app/app.py     # http://127.0.0.1:5005
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from runner.kernel.errors import BackendUnavailableError
from runner.policy.redaction import Redaction

__all__ = ["DEFAULT_ENDPOINT", "EXCLUDED_BY_DEFAULT", "RizzoPiiRedactor"]

DEFAULT_ENDPOINT = "http://127.0.0.1:5005"

EXCLUDED_BY_DEFAULT: tuple[str, ...] = ()
"""Tags the perimeter asks the server *not* to replace.

Empty on purpose. Excluding a tag is a decision to send that kind of identifier
in the clear, and it belongs in a policy file rather than in a default nobody
read. The parameter exists because the server supports it per request.
"""

DEFAULT_LABEL_CLASSES: dict[str, str] = {
    # Direct identifiers under Italian law, and the ones no other open model
    # covers. Restricted: these do not leave the perimeter in the clear.
    "CF": "restricted",
    "PIVA": "restricted",
    "IBAN": "restricted",
    "CREDITCARDNUMBER": "restricted",
    "ID_DOC": "restricted",
    "DOCID": "restricted",
    "CATASTO": "restricted",
    "TARGA": "restricted",
    # Personal but not, on their own, an identity document.
    "FULLNAME": "internal",
    "EMAIL": "internal",
    "TELEPHONENUM": "internal",
    "STREET": "internal",
    "BUILDINGNUM": "internal",
    "ZIPCODE": "internal",
    "CITY": "internal",
    "PROVINCE": "internal",
    "AGE": "internal",
    "GENDER": "internal",
    "DATE": "internal",
    "TIME": "internal",
    "ORG": "internal",
    "AMOUNT": "internal",
    # Not one of the model's 22 tags: the server's own regex net finds it, and
    # it is the tag most likely to carry a whole matter in one string — a data
    # room link names the deal, the parties and the adviser in a hostname.
    "URL": "internal",
}
"""A starting map from rizzo-pii's 22 categories onto this project's classes.

Shipped as a default an operator can read and disagree with, not as a fact.
Whether a city name is internal or public is a question about a business, and
the policy file is where a business answers it.
"""


class RizzoPiiRedactor:
    """Redaction through a local rizzo-pii server. Satisfies ``policy.Redactor``."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 15.0,
        client: Any = None,
        model_name: str = "rizzo-pii",
        exclude_tags: Sequence[str] = EXCLUDED_BY_DEFAULT,
        keep_mapping: bool = True,
    ) -> None:
        """
        Args:
            exclude_tags: Categories the server should leave in place. Sent per
                request, so one deployment's choice is not a global setting.
            keep_mapping: Whether to ask for the reverse dictionary. ``False``
                is the server's *definitive* anonymisation: nothing can restore
                the original, including this machine. That is the right setting
                when the answer does not need the real names back and the
                weakest link is the mapping sitting in memory — and the wrong
                one when the operator expects to read a reply about their own
                client by name.
        """
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._model_name = model_name
        self._exclude_tags = tuple(exclude_tags)
        self._keep_mapping = keep_mapping

    @property
    def name(self) -> str:
        return f"{self._model_name}@{self._endpoint}"

    def analyse(self, text: str) -> Redaction:
        """Send text to the local model and return the redacted form.

        Raises:
            BackendUnavailableError: the server is not running, refused the
                request, or answered something this adapter cannot read. Never
                returns the original text on failure — a redactor that silently
                does nothing is the worst possible outcome, because the caller
                would send the material believing it was cleaned.
        """
        if not text.strip():
            return Redaction(text=text)

        payload: dict[str, Any] = {"text": text, "include_mapping": self._keep_mapping}
        if self._exclude_tags:
            payload["exclude_tags"] = list(self._exclude_tags)

        try:
            response = self._http().post(f"{self._endpoint}/analyze", json=payload)
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"rizzo-pii at {self._endpoint} is unreachable: {exc}. "
                "Start it with `python src/app/app.py`, or set redaction.on_error "
                "to 'ignore' if you accept running without it."
            ) from exc

        if response.status_code >= 400:
            raise BackendUnavailableError(
                f"rizzo-pii returned {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise BackendUnavailableError(f"rizzo-pii returned unreadable JSON: {exc}") from exc

        if "error" in body:
            raise BackendUnavailableError(f"rizzo-pii refused the text: {body['error']}")

        redacted = body.get("anonymized_text")
        if not isinstance(redacted, str):
            raise BackendUnavailableError(
                "rizzo-pii returned no anonymized_text; this adapter expects the "
                "/analyze contract of the reference server"
            )

        mapping = body.get("mapping") or {}
        labels = body.get("by_label") or {}

        if self._keep_mapping and not mapping and body.get("mapping_enabled") is False:
            # The server was started with --no-mapping. It is doing *more* than
            # asked, not less, but the caller believes an answer can be
            # re-identified afterwards and it cannot. Say so rather than let a
            # reply come back full of placeholders nobody can resolve.
            raise BackendUnavailableError(
                "rizzo-pii is running with the mapping disabled, so nothing can be "
                "re-identified locally. Restart it without --no-mapping, or construct "
                "the redactor with keep_mapping=False and accept a definitive anonymisation."
            )

        return Redaction(
            text=redacted,
            mapping={str(k): str(v) for k, v in mapping.items()}
            if isinstance(mapping, dict)
            else {},
            labels={str(k): int(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
        )

    def health(self) -> bool:
        """Whether the server is *ready*, not merely listening.

        ``/health`` is the endpoint the reference server documents for this, and
        it distinguishes the two states that matter: the process is up but the
        0.3B model is still loading (503, ``model_loaded: false``), versus ready
        to answer. Probing a page that renders without the model would report a
        redactor that is about to fail the first real request as healthy.
        """
        try:
            response = self._http().get(f"{self._endpoint}/health")
        except httpx.HTTPError:
            return False
        if response.status_code >= 400:
            return False
        try:
            return bool(response.json().get("model_loaded", True))
        except ValueError:
            return True

    def _http(self) -> Any:
        return self._client or httpx.Client(timeout=self._timeout)
