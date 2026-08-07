"""HTTP surface for the kernel: policy, substrates, ledger, and asking it things.

Until this module existed, everything the product claims to do was reachable only
from a terminal. The desktop window showed notes and a sync status — a person
could install Annona, use it all day, and never once see a placement decision or
a refusal. A guarantee nobody can watch is indistinguishable from a slogan, so
the read side of the kernel is now an API and the window can show it.

Four reads and one write:

``GET  /api/kernel/policy``       the document, as the runtime understands it
``GET  /api/kernel/substrates``   what is registered, and whether it answers
``GET  /api/kernel/ledger``       recent decisions, refusals included
``GET  /api/kernel/ledger/verify``the chain, checked offline
``GET  /api/kernel/formats``      what this installation can read, and what is missing
``POST /api/kernel/attachments``  take a file in, and say what it is
``GET  /api/kernel/attachments``  what is in the inbox
``DELETE /api/kernel/attachments/{id}``  remove one

The attachment routes are the only ones that write anything to the operator's
disk, and they write it where the operator can see it — see
:mod:`runner.services.attachments` for why an upload becomes a file rather than
a payload.

``POST /api/kernel/ask``          run one request through the perimeter

``ask`` returns the answer *and* the decisions the answer required, because the
two are one fact: an answer that arrived without saying where it was computed is
exactly what this product exists to replace.

``GET  /api/kernel/profiles``     the starting policies on offer, and this machine's models
``POST /api/kernel/policy``       write the *first* policy, from a chosen profile
``GET  /api/kernel/policy/source``the file as text, with the digest to edit against
``PUT  /api/kernel/policy``       replace it

Those four are the perimeter's write side, and it took a deliberate decision to
have one at all. Editing the policy from a page every process on the machine can
reach is a larger thing than reading it, and this module refused to for as long
as the alternative — open `policy.yaml` in an editor — was the only way. That
alternative is not one most people take. A perimeter nobody adjusts is a
perimeter that stops describing what somebody actually wants, and the failure
mode of *that* is worse: the tool gets switched off, or the policy gets widened
once in a hurry and never narrowed again.

So it is editable, and three things hold it up:

- **Nothing invalid reaches disk.** The replacement is parsed before the write.
  A daemon whose policy does not load stops enforcing, and this route must not
  be able to cause that.
- **Nothing is lost.** The previous text is copied aside first, every time.
- **Nothing is quiet.** Every replacement is appended to the same hash-chained
  ledger as the decisions, with the digests of before and after. Widening the
  perimeter, running something, and narrowing it back leaves three entries.

``POST`` is still the first policy only, and 409s when one exists — creating and
replacing are different acts and the second one is the one that needs the record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel, Field

from runner.audit.ledger import read_entries, verify_file
from runner.kernel.errors import ConfigurationError, PolicyError
from runner.kernel.types import ToolCall
from runner.pairing import is_this_machine
from runner.policy.loader import load_policy
from runner.policy.profiles import (
    FRONTIER_PROVIDERS,
    PROFILES,
    FrontierChoice,
    get_profile,
)
from runner.services import attachments as inbox
from runner.services.enforcement import policy_path
from runner.tools.extractors import capabilities, supported_extensions

__all__ = ["AskRequest", "CreatePolicyRequest", "kernel_router"]


# ── I/O models ────────────────────────────────────────────────────────────────


class ReplacePolicyRequest(BaseModel):
    """A replacement policy, as a document or as the text of one."""

    document: dict[str, Any] | None = None
    """The policy as structured data — what the editor's tables produce."""

    yaml_text: str | None = Field(default=None, alias="yaml")
    """The policy as text. Wins over ``document`` when both are sent.

    Editing the text is the only way to change something the tables do not
    show, and the only way to keep comments written inside the body.
    """

    expected_digest: str | None = None
    """SHA-256 of the file this edit was made against.

    Refused with 409 when it no longer matches: the file is editable from a
    terminal too, and silently overwriting somebody's `vim` session with a form
    submitted ten minutes ago is how a perimeter widens without anybody
    deciding to widen it.
    """

    model_config = {"populate_by_name": True}


class CreatePolicyRequest(BaseModel):
    """The answers the onboarding collected, on a machine with no policy yet."""

    profile: str = "local-only"
    model: str | None = None
    """Local model to register. ``None`` means whichever is installed."""

    readable_paths: list[str] | None = None
    """Folders the reading tools may open. ``None`` keeps the profile's defaults."""

    provider: str | None = None
    provider_model: str | None = None
    provider_endpoint: str | None = None
    api_key_env: str | None = None
    """The *name* of the environment variable holding the provider's key.

    Deliberately not the key. There is no field on this model that could carry
    one, so a window cannot post a secret into a file the operator later hands
    to an auditor.
    """


class AskRequest(BaseModel):
    """One request to run through the perimeter."""

    prompt: str = Field(min_length=1)
    max_iterations: int = Field(default=8, ge=1, le=30)
    escalate: bool = False
    """Ask for the best substrate this policy already permits for the material.

    Reordering, never widening: it cannot reach a substrate the rule does not
    allow, and a class that may not leave the machine still may not. What it
    changes is which of the permitted candidates is chosen when more than one
    could serve — which is the honest meaning of "use the good model".
    """

    attachments: list[str] = Field(default_factory=list)
    """Absolute paths of files to put in front of this run.

    Paths, not contents. The run reads them through ``document_reader``, under
    the same allow-list and into the same ledger as any other read — which is
    what keeps an attachment from being a second, unpoliced way into the model.
    """


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ledger_path() -> Path:
    return policy_path().parent / "ledger.jsonl"


def _entry_json(entry: Any) -> dict[str, Any]:
    """One ledger entry, flattened for a UI that has to render it in a row.

    ``detail`` is passed through rather than summarised: it carries the rejected
    candidates and the reason, which is the part an operator actually reads. It
    contains digests, never payloads — that is a property of what the ledger
    records, not something this function has to enforce.
    """
    return {
        "seq": entry.seq,
        "ts": entry.ts,
        "run_id": entry.run_id,
        "step_id": entry.step_id,
        "kind": entry.kind,
        "outcome": entry.outcome,
        "class": entry.klass,
        "substrate": entry.substrate,
        "rule_id": entry.rule_id,
        "payload_digest": entry.payload_digest,
        "detail": dict(entry.detail),
        "hash": entry.hash,
    }


READ_CHARS_PER_ATTACHMENT = 24_000
"""How much of each attached file is put in front of the model up front.

Enough for an invoice, a contract, a transcript of a short recording; short of
filling a 32k window with one spreadsheet. The model still has the tool and the
path, so anything past this is one call away.
"""


def _with_attachments(req: AskRequest, policy: Any) -> tuple[str, list[Any], list[ToolCall]]:
    """What the run gets from a list of attached paths: three separate things.

    *The prompt* names every attached file. Naming is what makes the path part
    of the payload, which is what makes the class of the file decide where the
    first turn runs.

    *The reads* are executed before the first inference — through the gate, into
    the ledger — rather than left as an instruction the model may or may not
    follow. Told to "call document_reader on this path", a 14B local model
    complies most of the time; the times it does not, it answers about a file it
    never opened, which is the worst failure this feature could have. Nothing is
    weakened by doing it here: same tool, same allow-list, same recorded
    decision, and the class of what was read is now known *before* placement
    rather than after.

    *The media* is attached only when the policy has a substrate that can
    actually see it. Attaching unconditionally would be more faithful to what
    the operator did and would hold the run — "not chosen: cannot read images" —
    on the overwhelmingly common policy that registers one local text model. The
    file is still read; it is read as text.
    """
    if not req.attachments:
        return req.prompt, [], []

    described = []
    for path in req.attachments:
        target = Path(path).expanduser()
        if not target.is_file():
            # Not fatal: the other attachments are still worth running with, and
            # the model is told what was missing rather than left to infer it.
            described.append(
                {
                    "name": target.name,
                    "path": str(target),
                    "family": "missing",
                    "format": "",
                    "bytes": 0,
                    "warnings": ["this file does not exist on this machine"],
                    "media": [],
                }
            )
            continue
        described.append(inbox.describe(target, policy=policy))

    prompt = f"{inbox.preamble(described)}\n\n{req.prompt}"
    media = inbox.attachments_for(described) if inbox.vision_families(policy) else []
    reads = [
        ToolCall(
            id=f"attach_{index}",
            name="document_reader",
            arguments={"path": item["path"], "max_chars": READ_CHARS_PER_ATTACHMENT},
        )
        for index, item in enumerate(described)
        if item.get("family") != "missing"
    ]
    return prompt, media, reads


def _split_header(text: str) -> tuple[str, bool]:
    """The leading comment block, and whether comments survive below it.

    Every policy this project writes is a long explanatory header followed by
    ``yaml.safe_dump`` output, so re-serialising the body after a structured
    edit loses nothing — *if* the header is put back. A policy somebody
    hand-edited may have comments inside the body, and those a round-trip
    through a dict cannot preserve. The caller is told rather than finding out
    by diffing a file it thought it owned.
    """
    lines = text.splitlines(keepends=True)
    end = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            end = index
            break
    else:
        end = len(lines)

    header = "".join(lines[:end])
    body_has_comments = any(line.lstrip().startswith("#") for line in lines[end:])
    return header, body_has_comments


def _record_policy_change(before: str, after: str, backup: Path, policy: Any) -> str:
    """Append the change to the ledger, and return the step id.

    A policy that can be edited from a window needs the edits in the same
    hash-chained record as the decisions, or "the policy is the document you
    hand an auditor" stops meaning anything: somebody could widen the perimeter,
    run something, and narrow it back with nothing to show it happened.

    Digests, not contents — the ledger's rule everywhere else, and the previous
    text is on disk in ``backup`` for anyone who needs to read it.
    """
    from runner.audit.ledger import Ledger, digest

    ledger = Ledger(_ledger_path())
    return ledger.record(
        "policy",
        outcome="replaced",
        # Not a classification of the edit: the field is required, and the most
        # useful policy fact to freeze here is what unrecognised material
        # becomes *under the policy being installed*.
        klass=policy.default_class,
        detail={
            "before_digest": digest(before),
            "after_digest": digest(after),
            "backup": str(backup),
            "substrates": [s.id for s in policy.substrates],
            "rules": len(policy.rules),
        },
    )


def _only_this_machine(request: Request) -> None:
    """Refuse a paired remote origin. See :func:`runner.pairing.is_this_machine`.

    The pairing token lets a web app run steps here; the middleware therefore
    lets it reach every ``/api/`` route. Writing the policy is not one of the
    things that grant was asked for — an origin able to widen the perimeter and
    then run under it holds everything the perimeter exists to withhold.
    """
    if not is_this_machine(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "the policy can only be changed from this machine. Pairing lets an "
                "app run steps here; it does not let it change what is permitted."
            ),
        )


def _load_policy_or_404():
    """The policy, or an HTTP error that says how to create one."""
    target = policy_path()
    if not target.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"no policy at {target}. Run `annona policy init` to write one — "
                "until then nothing is enforced."
            ),
        )
    try:
        return load_policy(target), target
    except PolicyError as exc:
        # 422, not 500: the file is there and this is what is wrong with it. A
        # policy that fails to load must never be reported as "no policy".
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Router ────────────────────────────────────────────────────────────────────


def kernel_router(executor: Any | None = None) -> APIRouter:
    """Build the kernel routes.

    Args:
        executor: The daemon's :class:`~runner.executor.TaskExecutor`. Supplies
            the tools, the permissions and the AI client that ``ask`` runs
            through. When absent — a daemon started without one, or a test —
            the reads still work and ``ask`` answers 503 rather than pretending.
    """
    router = APIRouter(prefix="/api/kernel", tags=["kernel"])

    @router.get("/policy")
    def get_policy() -> dict[str, Any]:
        """The policy as the runtime understands it, not as it is written."""
        policy, target = _load_policy_or_404()
        return {
            "source": str(target),
            "version": policy.version,
            "default_class": policy.default_class.label,
            # What earns a class, not just its name: the paths and patterns are
            # the answer to "why was my document restricted?", and that question
            # is asked far more often than "which classes exist?".
            "classes": [
                {
                    "label": klass.label,
                    "paths": list(spec.paths),
                    "patterns": [p.pattern for p in spec.patterns],
                    "default": spec.default,
                }
                for klass, spec in policy.classes.items()
            ],
            "substrates": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "jurisdiction": s.jurisdiction,
                    "max_class": s.max_class.label,
                    "endpoint": s.endpoint or "",
                    "model": s.model or "",
                    "tools": s.tools,
                    "vision": s.vision,
                    "distance": s.distance,
                }
                for s in policy.substrates
            ],
            "rules": [
                {
                    "id": r.id,
                    "class": r.klass.label,
                    "allow": list(r.allow),
                    "on_unavailable": r.on_unavailable,
                    "prefer": r.prefer,
                }
                for r in policy.rules
            ],
            "tools": {
                "allow": {tool: list(paths) for tool, paths in sorted(policy.tools.allow.items())},
                "deny_paths": list(policy.tools.deny_paths),
            },
            "skills": list(policy.skills.allow),
            "redaction": {
                "enabled": policy.redaction.enabled,
                "provider": policy.redaction.provider,
                "endpoint": policy.redaction.endpoint,
            },
        }

    @router.get("/substrates")
    def get_substrates(probe: bool = Query(True, description="Check liveness over HTTP")):
        """What is registered, where it is, and whether it answers right now."""
        from runner.placement.registry import SubstrateRegistry, http_prober

        policy, _ = _load_policy_or_404()
        registry = SubstrateRegistry.from_substrates(
            policy.substrates, prober=http_prober() if probe else None
        )
        out = []
        for sid, health in registry.snapshot().items():
            sub = registry.substrates[sid]
            out.append(
                {
                    "id": sid,
                    "kind": sub.kind,
                    "jurisdiction": sub.jurisdiction,
                    "max_class": sub.max_class.label,
                    "endpoint": sub.endpoint or "",
                    "model": sub.model or "",
                    "up": health.up,
                    "reason": health.reason,
                    "latency_ms": health.latency_ms,
                }
            )
        return {"probed": probe, "substrates": out}

    @router.get("/ledger")
    def get_ledger(
        limit: int = Query(60, ge=1, le=500),
        held: bool = Query(False, description="Refusals only"),
        run_id: str = Query("", description="One run"),
    ):
        """Recent decisions, newest last, refusals included.

        Refusals are not an error condition to be filtered out of a happy path:
        a held step is the product working. They come back in the same list, and
        `held=true` narrows to them rather than revealing them.
        """
        target = _ledger_path()
        if not target.exists():
            return {"path": str(target), "total": 0, "entries": []}

        entries = list(read_entries(target))
        selected = entries
        if run_id:
            selected = [e for e in selected if e.run_id == run_id]
        if held:
            selected = [e for e in selected if e.outcome == "held"]

        return {
            "path": str(target),
            "total": len(entries),
            "shown": len(selected[-limit:]),
            "entries": [_entry_json(e) for e in selected[-limit:]],
        }

    @router.get("/ledger/verify")
    def verify_ledger():
        """Check the chain. Reads the file; contacts nobody."""
        target = _ledger_path()
        if not target.exists():
            return {"path": str(target), "ok": True, "entries": 0, "problem": "", "empty": True}
        result = verify_file(target)
        return {
            "path": str(target),
            "ok": result.ok,
            "entries": result.entries,
            "problem": result.problem or "",
            "empty": False,
        }

    @router.get("/status")
    def kernel_status():
        """One call for a header: is a policy enforcing, and is anything up."""
        target = policy_path()
        if not target.exists():
            return {"enforcing": False, "policy": str(target), "reason": "no policy file"}
        try:
            policy = load_policy(target)
        except PolicyError as exc:
            return {"enforcing": False, "policy": str(target), "reason": str(exc)}

        # Counted rather than summarised: a monitoring probe needs numbers it
        # can alert on. `held` is the interesting one — a rising refusal rate is
        # either a policy that has drifted from the work or a substrate that has
        # gone away, and both are things somebody should be paged about.
        entries = list(read_entries(_ledger_path())) if _ledger_path().exists() else []
        held = sum(1 for e in entries if e.outcome == "held")
        return {
            "enforcing": True,
            "policy": str(target),
            "reason": "",
            "substrates": len(policy.substrates),
            "rules": len(policy.rules),
            "default_class": policy.default_class.label,
            "held": held,
            "last_decision_at": entries[-1].ts if entries else "",
            "decisions": len(entries),
        }

    # ── Attachments ───────────────────────────────────────────────────────────

    def _config() -> dict[str, Any]:
        return getattr(executor, "config", {}) or {}

    def _policy_or_none():
        """The policy if there is a usable one, else ``None``.

        Unlike the read routes this does not 404: an attachment can be stored
        without a perimeter, it simply cannot be told what class it carries, and
        answering "no policy" to an upload would be a strange way to say so.
        """
        try:
            return load_policy(policy_path()) if policy_path().exists() else None
        except PolicyError as exc:
            logger.warning(f"attachments: policy will not load ({exc})")
            return None

    @router.get("/formats")
    def formats():
        """What this installation can read right now, and how to widen it.

        Reported rather than promised. Half the readers depend on something
        optional, and a window that offers to open a DICOM study on a machine
        with no pydicom would be lying in the one place the product cannot
        afford to.
        """
        policy = _policy_or_none()
        return {
            "inbox": str(inbox.inbox_dir(_config())),
            # The same path with the home directory folded back to `~`: the
            # window puts this in front of the operator, and an absolute path
            # 90 characters long says less than the one they would type.
            "inbox_short": inbox.short_path(inbox.inbox_dir(_config())),
            "max_upload_mb": inbox.max_bytes(_config()) // (1024 * 1024),
            "extensions": list(supported_extensions()),
            "vision": inbox.vision_families(policy),
            **capabilities(),
        }

    @router.post("/attachments")
    def add_attachment(file: UploadFile = File(...)):
        """Take one file in, store it, and say what it turned out to be."""
        try:
            stored = inbox.store(file.filename or "attachment", file.file, config=_config())
        except ValueError as exc:
            # 413, not 400: the file is fine, the ceiling is the problem, and
            # the operator can raise it in the config.
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not store the file: {exc}") from exc

        described = inbox.describe(stored.path, policy=_policy_or_none())
        return {"id": stored.id, "sha256": stored.sha256, **described}

    @router.get("/attachments")
    def list_attachments(limit: int = Query(30, ge=1, le=200)):
        """What is in the inbox, newest first."""
        policy = _policy_or_none()
        items = []
        for stored in inbox.list_stored(limit=limit, config=_config()):
            items.append({"id": stored.id, **inbox.describe(stored.path, policy=policy)})
        return {"inbox": str(inbox.inbox_dir(_config())), "attachments": items}

    @router.get("/attachments/{identifier}/thumbnail")
    def attachment_thumbnail(identifier: str):
        """A small JPEG of the file, for the window that is showing it.

        Not an egress path and not a hole in the perimeter: the bytes go from a
        file the operator chose, over loopback, to the window they are already
        looking at. The perimeter governs what reaches a *model*; it was never
        meant to stop a person from seeing their own document.
        """
        target = inbox.resolve(identifier, config=_config())
        if target is None or not target.is_file():
            raise HTTPException(status_code=404, detail=f"no attachment {identifier!r}")

        image = inbox.thumbnail(target)
        if image is None:
            raise HTTPException(status_code=404, detail="this file has no visual preview")

        return FileResponse(
            image,
            media_type="image/jpeg",
            # Immutable: the id is the file's digest, so a thumbnail for a given
            # id can never be stale.
            headers={"Cache-Control": "private, max-age=86400, immutable"},
        )

    @router.delete("/attachments/{identifier}")
    def delete_attachment(identifier: str):
        """Remove one attachment from the inbox."""
        if not inbox.remove(identifier, config=_config()):
            raise HTTPException(status_code=404, detail=f"no attachment {identifier!r}")
        return {"deleted": identifier}

    @router.post("/ask")
    def ask(req: AskRequest):
        """Run one request through the perimeter and return what it decided.

        Deliberately synchronous: `def`, not `async def`, so FastAPI runs it in a
        worker thread. The agent loop is blocking and calls a local model that
        can take tens of seconds; running it on the event loop would freeze every
        other request, including the health check the window uses to decide
        whether the daemon is alive.
        """
        if executor is None:
            raise HTTPException(
                status_code=503,
                detail="this daemon has no executor; start it with `annona run`",
            )

        ledger = _ledger_path()
        # Where the ledger stood before the run, so the response can carry the
        # decisions *this* request caused rather than the whole history.
        before = sum(1 for _ in read_entries(ledger)) if ledger.exists() else 0

        prompt, media, reads = _with_attachments(req, _policy_or_none())

        try:
            result = executor.ai_client.reason_and_execute(
                prompt=prompt,
                context={"source": "desktop", "surface": "kernel-api"},
                tools=executor.tools,
                permissions=executor.permissions,
                max_iterations=req.max_iterations,
                attachments=media,
                prefetch=reads,
                prefer_quality=req.escalate,
            )
        except ConfigurationError as exc:
            # The perimeter could not be assembled. 409, not 500: nothing is
            # broken, the configuration refuses to run — and the run correctly
            # did not happen.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
            logger.exception("kernel ask failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        new_entries = []
        if ledger.exists():
            new_entries = [_entry_json(e) for e in list(read_entries(ledger))[before:]]

        return {
            "response": result.get("response", ""),
            "iterations": result.get("iterations", 0),
            "tool_calls": result.get("tool_calls", []),
            # Present only on the enforced path. Absent means no policy was in
            # force, and the UI has to say so rather than draw a reassuring chip.
            "placement": result.get("placement"),
            "enforced": "placement" in result,
            "decisions": new_entries,
            # Named versus shown, because they are different claims: every
            # attachment was put in front of the run, and only some of them
            # could be looked at by whatever served the turn.
            "attachments": {"named": len(req.attachments), "shown": len(media)},
            # Every payload that left this machine, verbatim, so the window can
            # show a person what was sent rather than a reassuring summary of
            # it. Truncated for transport only — the ceiling is generous enough
            # that a redacted question arrives whole.
            "egress": [
                {**item, "text": str(item.get("text", ""))[:20_000]}
                for item in result.get("egress", [])
            ],
            "sealed": result.get("sealed", ""),
        }

    # ── Onboarding: the first policy, and only the first ──────────────────────

    @router.get("/profiles")
    def get_profiles() -> dict[str, Any]:
        """The starting policies on offer, plus what this machine can actually run.

        Everything the chooser needs in one call: the profiles with their
        consequences, the hosted providers, the models this runtime has, and
        whether a policy already exists — because a window that offered to set
        one up on a machine that already has one would be offering something
        this API refuses.
        """
        from runner.cli_setup import choose_model, probe_runtime

        probe = probe_runtime()
        suggested, why = choose_model(probe.models)
        target = policy_path()

        return {
            "configured": target.exists(),
            "policy_path": str(target),
            "runtime": {
                "endpoint": probe.endpoint,
                "reachable": probe.reachable,
                "models": list(probe.models),
                "detail": probe.detail,
            },
            "suggested_model": suggested,
            "suggested_reason": why,
            "profiles": [
                {
                    "id": p.id,
                    "title": p.title,
                    "summary": p.summary,
                    "consequence": p.consequence,
                    "needs_frontier": p.needs_frontier,
                    "recommended": p.recommended,
                }
                for p in PROFILES
            ],
            "providers": [
                {
                    "id": fp.id,
                    "title": fp.title,
                    "model": fp.model,
                    "endpoint": fp.endpoint,
                    "api_key_env": fp.api_key_env,
                    "jurisdiction": fp.jurisdiction,
                }
                for fp in FRONTIER_PROVIDERS
            ],
        }

    @router.post("/policy", status_code=201)
    def create_policy(request: CreatePolicyRequest, http: Request) -> dict[str, Any]:
        """Write the first policy from a chosen profile. Refuses if one exists."""
        _only_this_machine(http)
        target = policy_path()
        if target.exists():
            # 409, not 403: the request is well-formed and would be legal on a
            # machine without a policy. Editing one is a decision this API has
            # not been given, and the CLI is where it is made.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a policy already exists at {target}. This route only ever writes "
                    "the first one; change an existing policy by editing that file, or "
                    "with `annona setup --force`, which keeps a copy of what it replaces."
                ),
            )

        try:
            profile = get_profile(request.profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        frontier = None
        if profile.needs_frontier:
            if request.provider is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"profile {profile.id!r} needs a provider, and none was given",
                )
            match = next((p for p in FRONTIER_PROVIDERS if p.id == request.provider), None)
            if match is None:
                raise HTTPException(
                    status_code=422, detail=f"unknown provider {request.provider!r}"
                )
            frontier = FrontierChoice(
                provider=match,
                model=request.provider_model or "",
                endpoint=request.provider_endpoint or "",
                api_key_env=request.api_key_env or "",
            )

        from runner.cli_setup import choose_model, probe_runtime, write_policy

        model = request.model or choose_model(probe_runtime().models)[0]
        readable = list(request.readable_paths) if request.readable_paths is not None else None

        written = write_policy(
            target,
            profile_id=profile.id,
            local_model=model,
            frontier=frontier,
            readable_paths=readable,
        )

        # Parse what was just written before reporting success. A profile that
        # produced an invalid document would otherwise leave the daemon with a
        # file it cannot load and a window that said it worked.
        try:
            load_policy(written)
        except PolicyError as exc:
            written.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail=f"the generated policy did not validate: {exc}"
            ) from exc

        logger.info(f"policy created from profile {profile.id!r} at {written}")
        return {
            "path": str(written),
            "profile": profile.id,
            "model": model,
            "consequence": profile.consequence,
        }

    # ── Editing an existing policy ────────────────────────────────────────────

    @router.get("/policy/source")
    def get_policy_source() -> dict[str, Any]:
        """The policy file as text, with the digest an edit must be made against."""
        from runner.audit.ledger import digest

        target = policy_path()
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"no policy at {target}")

        import yaml as yaml_lib

        text = target.read_text(encoding="utf-8")
        _, body_has_comments = _split_header(text)

        # The parsed document as well as the text, so an editor can offer fields
        # without shipping a YAML parser to the browser — and so the two views
        # of the same file cannot disagree about what it says.
        try:
            document = yaml_lib.safe_load(text)
        except yaml_lib.YAMLError:
            document = None

        return {
            "path": str(target),
            "text": text,
            "document": document,
            "digest": digest(text),
            # True means a structured save would drop something. The window uses
            # it to steer the person to the text editor instead of quietly
            # deleting a comment they wrote.
            "body_has_comments": body_has_comments,
        }

    @router.put("/policy")
    def replace_policy(request: ReplacePolicyRequest, http: Request) -> dict[str, Any]:
        """Replace the policy. Validates first, keeps a copy, records the change.

        Three things happen before anything is written, and the order is the
        point: the replacement is parsed, the file it replaces is checked to be
        the one the edit was made against, and the previous text is copied
        aside. A policy that fails to parse never reaches disk, because a
        daemon whose policy does not load is a daemon that stops enforcing —
        which is the one failure this route must not be able to cause.
        """
        import yaml as yaml_lib

        from runner.audit.ledger import digest
        from runner.policy.loader import parse_policy

        _only_this_machine(http)
        target = policy_path()
        if not target.exists():
            raise HTTPException(
                status_code=404,
                detail=f"no policy at {target}. Create one first — POST to this route.",
            )

        current = target.read_text(encoding="utf-8")
        if request.expected_digest and request.expected_digest != digest(current):
            raise HTTPException(
                status_code=409,
                detail=(
                    "the policy on disk changed since this edit began. Reload it and "
                    "reapply — this file is editable from a terminal too."
                ),
            )

        # Build the replacement text.
        if request.yaml_text is not None:
            new_text = request.yaml_text
            try:
                document = yaml_lib.safe_load(new_text)
            except yaml_lib.YAMLError as exc:
                raise HTTPException(status_code=422, detail=f"not valid YAML: {exc}") from exc
        elif request.document is not None:
            document = request.document
            header, _ = _split_header(current)
            new_text = header + yaml_lib.safe_dump(document, sort_keys=False, allow_unicode=True)
        else:
            raise HTTPException(status_code=422, detail="send either `document` or `yaml`")

        if not isinstance(document, dict):
            raise HTTPException(status_code=422, detail="a policy must be a mapping")

        # Parse before writing. This is the guard, not a courtesy.
        try:
            policy = parse_policy(document, source=str(target))
        except (PolicyError, ConfigurationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        backup = target.with_name(f"{target.name}.bak-{int(target.stat().st_mtime)}")
        backup.write_text(current, encoding="utf-8")
        target.write_text(new_text, encoding="utf-8")

        step_id = _record_policy_change(current, new_text, backup, policy)
        logger.warning(f"policy replaced at {target} (previous copy: {backup}, step {step_id})")

        return {
            "path": str(target),
            "digest": digest(new_text),
            "backup": str(backup),
            "step_id": step_id,
            "substrates": len(policy.substrates),
            "rules": len(policy.rules),
        }

    return router
