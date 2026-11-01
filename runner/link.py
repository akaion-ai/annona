"""The link: this machine as an executor for Agents Studio, dialling out only.

ADR 0006 is the argument; this module is the runner half of it. Three things
are worth knowing before reading the code.

**Nothing listens.** Every connection is opened from here, to one endpoint the
operator typed, over HTTPS. The machine that holds the material exposes no port
for this feature, which is what makes it acceptable on a DGX behind a corporate
firewall.

**The credential is the machine's, not a person's.** It is traded once for a
single-use enrollment code an org admin created, stored in ``link.json`` with
mode ``0600``, and revoked from Studio. It can heartbeat, claim a job addressed
to this runner, and report on a job it holds. Nothing else.

**What goes back is a policy decision.** Studio is a destination outside the
machine, so a job's answer is egress. It is released only when the run's working
set, its seal and the answer's own text all sit at or below ``link.release`` in
the policy — or at or below the ``link.endpoints`` entry for the endpoint this
machine enrolled to, when there is one (ADR 0007). Otherwise the job is reported
``withheld``: Studio learns the job finished and where it was placed, and the
answer stays here — in the inbox, ``$ANNONA_HOME/link/inbox``, readable with
``annona link show``. A policy with no ``link:`` section releases nothing but
that metadata.

**Studio names a skill, never writes one.** The heartbeat lists the skills this
policy enables; a job may name one, and it is loaded before the first turn
through the same ``skill`` tool a model would call — same permission, same pin,
same ledger entry. A name the policy does not enable fails the job.

**Studio asks for installs, the policy has already answered.** The heartbeat
also lists what the policy pre-approves in ``skill_catalogs`` and this machine
does not have yet. A job of kind ``install_skill`` fetches one of those — and
only those — from the catalog the policy names, verified against its SHA-256
(ADR 0008). No model runs; the ledger records who asked.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from runner.audit.ledger import Ledger, read_entries
from runner.kernel.errors import ConfigurationError
from runner.kernel.types import SensitivityClass
from runner.policy.classifier import PolicyClassifier
from runner.policy.loader import load_policy
from runner.policy.models import Policy, normalise_endpoint
from runner.services.catalog_http import http_fetch
from runner.services.enforcement import policy_path
from runner.skills.catalog import install_from_catalog, installable
from runner.skills.loader import discover_skills, skills_dirs
from runner.skills.registry import SkillRegistry

__all__ = [
    "LinkClient",
    "LinkConfig",
    "LinkConflictError",
    "LinkError",
    "LinkRevokedError",
    "LinkWorker",
    "check_endpoint",
    "enroll",
    "inbox_dir",
    "link_path",
    "read_inbox",
    "release_ceiling",
    "release_decision",
]

API_PREFIX = "/api/v1/runner/link"
POLL_SECONDS = 3.0
HEARTBEAT_SECONDS = 15.0
MAX_INSTRUCTION_CHARS = 20_000


class LinkError(RuntimeError):
    """The link could not do what was asked; the message says why."""


class LinkRevokedError(LinkError):
    """The control plane no longer accepts this runner's credential."""


class LinkConflictError(LinkError):
    """The control plane refused a state change (409): retrying cannot help."""


# ── Configuration ────────────────────────────────────────────────────────────


def link_path() -> Path:
    """``$ANNONA_HOME/link.json`` — next to the policy it answers to."""
    return policy_path().parent / "link.json"


def inbox_dir() -> Path:
    """``$ANNONA_HOME/link/inbox`` — where withheld answers are kept."""
    return policy_path().parent / "link" / "inbox"


def read_inbox() -> list[dict[str, Any]]:
    """The withheld answers kept here, newest first.

    Each record is the file as :func:`_keep` wrote it, plus ``received``: the
    file's mtime, as a Unix timestamp. A file that is not such a record is skipped
    rather than failing the whole inbox.
    """
    items = []
    for path in inbox_dir().glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            received = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("job_id"):
            items.append({**record, "received": received})
    return sorted(items, key=lambda it: it["received"], reverse=True)


def check_endpoint(url: str) -> str:
    """Refuse anything but HTTPS, except loopback for development.

    A runner secret sent over plain HTTP is a secret handed to every network
    between here and Studio, and the material behind it with it.
    """
    try:
        normalise_endpoint(url)
    except ValueError as exc:
        raise LinkError(str(exc)) from None
    return url.strip().rstrip("/")


@dataclass(frozen=True)
class LinkConfig:
    endpoint: str
    runner_id: str
    secret: str
    name: str
    organization: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> LinkConfig | None:
        path = path or link_path()
        if not path.exists():
            return None
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            # Readable by someone other than the owner means the secret may
            # already be somebody else's. Refuse rather than quietly chmod: the
            # operator needs to know it happened.
            raise LinkError(
                f"{path} is mode {oct(mode)}; it holds this runner's secret and must be 0600. "
                "Fix it with `chmod 600`, or revoke the runner in Studio and enroll again."
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            **{k: data[k] for k in ("endpoint", "runner_id", "secret", "name")},
            organization=data.get("organization", ""),
        )

    def save(self, path: Path | None = None) -> Path:
        path = path or link_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Created 0600 from the first byte, not chmod-ed after writing: there is
        # no window in which the secret sits in a world-readable file.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, path)
        return path


def machine_id() -> str:
    """Stable per machine, and not the hostname itself."""
    raw = f"{socket.gethostname()}:{uuid.getnode()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Wire ─────────────────────────────────────────────────────────────────────


def _raise_for(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise LinkRevokedError(
            "the control plane refused this runner's credential — revoked in Studio, "
            "or enrolled elsewhere. Enroll again with a new code."
        )
    if response.status_code >= 400:
        cls = LinkConflictError if response.status_code == 409 else LinkError
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise cls(
            f"{response.request.method} {response.request.url.path}: "
            f"{response.status_code} {detail}"
        )


def enroll(
    endpoint: str,
    code: str,
    name: str,
    *,
    version: str,
    client: httpx.Client | None = None,
) -> LinkConfig:
    """Trade a single-use enrollment code for this runner's own credential."""
    endpoint = check_endpoint(endpoint)
    http = client or httpx.Client(timeout=30)
    response = http.post(
        f"{endpoint}{API_PREFIX}/enroll",
        json={"code": code.strip(), "name": name, "machine_id": machine_id(), "version": version},
    )
    if response.status_code == 401:
        # Not "revoked": this machine has no credential yet. Unknown, used and
        # expired codes are one answer on purpose, so this cannot say which.
        raise LinkError("the enrollment code is invalid, already used, or expired (15 min)")
    _raise_for(response)
    body = response.json()
    org = body.get("organization") or ""
    return LinkConfig(
        endpoint=endpoint,
        runner_id=str(body["runner_id"]),
        secret=str(body["secret"]),
        name=name,
        organization=str(org.get("name", "")) if isinstance(org, Mapping) else str(org),
    )


class LinkClient:
    """The four calls a runner credential is good for."""

    def __init__(self, config: LinkConfig, *, client: httpx.Client | None = None) -> None:
        check_endpoint(config.endpoint)
        self._config = config
        # 60 s: a result is small, but a control plane under load should get
        # the time to answer rather than a retry racing the first write.
        self._http = client or httpx.Client(timeout=60)
        self._headers = {"Authorization": f"Runner {config.runner_id}.{config.secret}"}

    @property
    def endpoint(self) -> str:
        return self._config.endpoint

    def _post(self, path: str, body: Mapping[str, Any] | None = None) -> Any:
        response = self._http.post(
            f"{self._config.endpoint}{API_PREFIX}{path}",
            json=dict(body or {}),
            headers=self._headers,
        )
        _raise_for(response)
        return response.json()

    def heartbeat(self, report: Mapping[str, Any]) -> list[str]:
        return [str(j) for j in self._post("/heartbeat", report).get("cancel", [])]

    def claim(self) -> dict[str, Any] | None:
        return self._post("/claim").get("job")

    def result(self, job_id: str, body: Mapping[str, Any]) -> None:
        self._post(f"/jobs/{job_id}/result", body)


# ── The decision ─────────────────────────────────────────────────────────────


def release_ceiling(policy: Policy, endpoint: str) -> tuple[SensitivityClass | None, str]:
    """The ceiling for answers going to ``endpoint``, and the name of the rule that set it.

    A ``link.endpoints`` entry for exactly this URL wins; anything else gets
    ``link.release``. The endpoint is the one this machine enrolled to, read
    here — so a policy written for the Studio in the building still releases
    only ``link.release`` to any other Studio it is enrolled to later.
    """
    url = normalise_endpoint(endpoint)
    if url in policy.link.endpoints:
        return policy.link.endpoints[url], f"link.release for {url}"
    return policy.link.release, "link.release"


def release_decision(
    policy: Policy,
    *,
    endpoint: str,
    run_class: SensitivityClass,
    sealed: str,
    response: str,
) -> tuple[bool, str]:
    """Whether a job's answer may go back to ``endpoint``, and why.

    All three must hold: the run's working set, its seal, and the answer's own
    text. The last one is not redundant — a model can quote a fiscal code it was
    told in the instruction without ever reading a file.
    """
    ceiling, rule = release_ceiling(policy, endpoint)
    if ceiling is None:
        return False, f"the policy has no {rule}; only metadata leaves this machine"
    if sealed:
        return False, f"the run touched sealed material ({sealed}); sealed material never leaves"
    if run_class > ceiling:
        return False, (
            f"the run read {run_class.label} material; {rule} permits up to {ceiling.label}"
        )
    answer_class = PolicyClassifier(policy).classify_text(response)
    if answer_class > ceiling:
        return False, (
            f"the answer itself classifies as {answer_class.label}; "
            f"{rule} permits up to {ceiling.label}"
        )
    return True, f"within {rule} ({ceiling.label})"


# ── The worker ───────────────────────────────────────────────────────────────

RunFn = Callable[[str, Callable[[], bool], "str | None"], Mapping[str, Any]]
"""``(instruction, cancelled, skill) -> reason_and_execute result``."""


def usable_skills(policy: Policy) -> tuple[Any, ...]:
    """Skills this policy enables and this machine can run — what Studio may name."""
    if not policy.enabled_skills:
        return ()
    return SkillRegistry(
        discover_skills(),
        allowed=policy.enabled_skills,
        vision=any(s.vision for s in policy.substrates),
        allowed_tools=tuple(policy.tools.allow),
        context_window=max((s.context_window for s in policy.substrates), default=0),
    ).available()


def _keep(inbox: Path, job: Mapping[str, Any], **fields: Any) -> Path:
    """Write a withheld answer to the inbox, readable by this user only."""
    inbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = inbox / f"{job['id']}.json"
    record = {
        "job_id": str(job["id"]),
        "title": str(job.get("title") or ""),
        "instruction": str(job.get("instruction") or ""),
        "requested_by": dict(job.get("requested_by") or {}),
        **fields,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def _policy_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def _decision_summary(entries: list[Any], *, with_reasons: bool) -> list[dict[str, Any]]:
    """What Studio may see of the run's ledger entries.

    Kind, outcome, class and substrate always — they are the shape of the run,
    and without them a person cannot tell a held job from a finished one. The
    free-text ``detail`` carries paths and reasons that name material, so it
    travels only with a released answer, and even then only the reason.
    """
    out = []
    for e in entries[-500:]:
        item = {
            "seq": e.seq,
            "kind": e.kind,
            "outcome": e.outcome,
            "class": e.klass,
            "substrate": e.substrate,
            "rule_id": e.rule_id,
        }
        if with_reasons and isinstance(e.detail, Mapping) and e.detail.get("reason"):
            item["reason"] = str(e.detail["reason"])[:300]
        out.append(item)
    return out


def _host() -> dict[str, str]:
    from runner.services.host import machine, model_memory_gb  # noqa: PLC0415

    try:
        return {**machine(), "model_memory_gb": f"{model_memory_gb():.0f}"}
    except Exception:  # noqa: BLE001 — the heartbeat matters more than its detail
        return {}


class LinkWorker:
    """Claim a job, run it through the perimeter, report what the policy permits."""

    def __init__(
        self,
        client: LinkClient,
        run: RunFn,
        *,
        policy_file: Path | None = None,
        version: str = "",
        poll_seconds: float = POLL_SECONDS,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        catalog_client: httpx.Client | None = None,
    ) -> None:
        self._client = client
        self._catalog_client = catalog_client
        self._run = run
        self._policy_file = policy_file or policy_path()
        self._ledger_file = self._policy_file.parent / "ledger.jsonl"
        self._version = version
        self._poll = poll_seconds
        self._beat = heartbeat_seconds
        self._current: str | None = None
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # One place that writes job entries, so every one carries who asked.
    def _record(
        self,
        outcome: str,
        klass: SensitivityClass,
        job: Mapping[str, Any],
        *,
        kind: str = "link",
        **detail: Any,
    ) -> None:
        requested_by = job.get("requested_by") or {}
        Ledger(self._ledger_file).record(
            kind,
            outcome=outcome,
            klass=klass,
            step_id=f"job:{job.get('id', '')}",
            detail={
                "job_id": str(job.get("id", "")),
                "requested_by": {
                    k: requested_by.get(k, "") for k in ("email", "role", "organization_id")
                },
                **detail,
            },
        )

    def report(self) -> dict[str, Any]:
        """The heartbeat body: what this runner is, never where its data is."""
        substrates: list[dict[str, Any]] = []
        skills: list[dict[str, Any]] = []
        offered: list[dict[str, Any]] = []
        tools: list[str] = []
        try:
            policy = load_policy(self._policy_file)
            tools = sorted(policy.tools.allow)[:50]
            skills = [
                {"name": k.name, "description": k.description[:300], "pins": k.pins}
                for k in usable_skills(policy)
            ][:200]
            substrates = [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "jurisdiction": s.jurisdiction,
                    "max_class": s.max_class.label,
                    "model": s.model,
                }
                for s in policy.substrates
            ]
            offered = installable(
                policy, set(discover_skills()), fetch=http_fetch(self._catalog_client)
            )
        except Exception:  # noqa: BLE001 — a broken policy is reported as none
            pass
        return {
            "version": self._version,
            "policy_digest": _policy_digest(self._policy_file),
            "substrates": substrates[:50],
            "skills": skills,
            "installable": offered,
            "tools": tools,
            "busy": self._current is not None,
            "platform": platform.system().lower(),
            # What Studio is sending work to: chip, where a model runs, memory.
            # Hardware only — never a path, a hostname or a user name.
            "host": _host(),
        }

    def beat_once(self) -> None:
        for job_id in self._client.heartbeat(self.report()):
            with self._lock:
                self._cancelled.add(job_id)

    def run_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one claimed job and return the result body, without sending it."""
        job_id = str(job["id"])
        base = {"lease_id": job["lease_id"]}

        try:
            policy = load_policy(self._policy_file)
        except Exception as exc:  # noqa: BLE001
            # No perimeter, no remote work. Running an instruction from the
            # network on the legacy unenforced path is the one thing this
            # runner must never do.
            self._record("refused", SensitivityClass.PUBLIC, job, reason=f"no usable policy: {exc}")
            return {
                **base,
                "status": "failed",
                "error": "this runner has no usable policy and does not accept remote work without one",
            }

        kind = str(job.get("kind") or "run")[:40]
        if kind == "install_skill":
            return self._install(job, policy, base)
        if kind != "run":
            self._record("refused", SensitivityClass.PUBLIC, job, reason=f"unknown kind {kind!r}")
            return {**base, "status": "failed", "error": f"this runner has no job kind {kind!r}"}

        instruction = str(job.get("instruction", ""))[:MAX_INSTRUCTION_CHARS]
        skill = str(job.get("skill") or "") or None
        classifier = PolicyClassifier(policy)
        self._record(
            "received",
            classifier.classify_text(instruction),
            job,
            title=str(job.get("title", ""))[:200],
            **({"skill": skill} if skill else {}),
        )
        if skill and skill not in {k.name for k in usable_skills(policy)}:
            self._record(
                "refused", SensitivityClass.PUBLIC, job, reason=f"skill {skill!r} not enabled"
            )
            return {
                **base,
                "status": "failed",
                "error": f"skill {skill!r} is not enabled on this machine",
            }

        before = sum(1 for _ in read_entries(self._ledger_file))
        self._current = job_id
        try:
            result = self._run(instruction, lambda: job_id in self._cancelled, skill)
        except Exception as exc:  # noqa: BLE001
            logger.exception("link job failed")
            self._record("failed", SensitivityClass.PUBLIC, job, reason=type(exc).__name__)
            # The exception text can quote material; its type cannot.
            return {
                **base,
                "status": "failed",
                "error": f"the run failed on the runner ({type(exc).__name__})",
            }
        finally:
            self._current = None

        entries = list(read_entries(self._ledger_file))[before:]
        placement = result.get("placement")
        if not placement:
            self._record(
                "withheld", SensitivityClass.PUBLIC, job, reason="the run was not enforced"
            )
            return {
                **base,
                "status": "failed",
                "error": "the run did not go through the perimeter",
                "decisions": _decision_summary(entries, with_reasons=False),
            }

        run_class = SensitivityClass.parse(placement.get("class", "restricted"))
        response = str(result.get("response", ""))
        cancelled = job_id in self._cancelled
        released, why = release_decision(
            policy,
            endpoint=self._client.endpoint,
            run_class=run_class,
            sealed=str(result.get("sealed", "")),
            response=response,
        )

        safe_placement = {k: placement.get(k, "") for k in ("class", "outcome", "substrate")}
        # Reported only if the ledger shows it loaded: a requested skill the gate
        # held did not shape this answer, and saying otherwise would be a lie.
        if skill and not any(
            e.kind == "skill"
            and e.outcome == "cleared"
            and isinstance(e.detail, Mapping)
            and e.detail.get("skill") == skill
            for e in entries
        ):
            skill = None
        body: dict[str, Any] = {
            **base,
            "placement": safe_placement,
            "ledger_head": Ledger(self._ledger_file).head,
            "decisions": _decision_summary(entries, with_reasons=released),
            "release": why,
            **({"skill": skill} if skill else {}),
        }
        if cancelled:
            self._record("cancelled", run_class, job)
            return {**body, "status": "cancelled"}
        if placement.get("outcome") in ("held", "queued"):
            # Nothing ran, so there is nothing to release or keep. Reported as
            # completed, Studio showed a green tick over an empty answer (#38,
            # over the link). Studio has no "held" status; "failed" with the
            # policy's reason is the truthful one of the four it has. The
            # reason names a rule and a substrate, never material.
            outcome = str(placement["outcome"])
            reason = str(placement.get("reason", ""))[:300]
            self._record(outcome, run_class, job, reason=reason)
            return {
                **body,
                "status": "failed",
                "error": f"{outcome} by this machine's policy: {reason}",
            }
        if released:
            self._record("released", run_class, job, reason=why)
            return {**body, "status": "completed", "response": response}
        _keep(
            self._policy_file.parent / "link" / "inbox",
            job,
            skill=skill,
            response=response,
            release=why,
            placement=safe_placement,
            ledger_head=body["ledger_head"],
        )
        self._record("withheld", run_class, job, reason=why)
        return {**body, "status": "withheld"}

    def _install(
        self, job: Mapping[str, Any], policy: Policy, base: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Install one pre-approved skill. No model runs, no material is read.

        The name must be in some catalog's ``enable``: Studio chooses when a
        pre-approved skill arrives, never which skills are approved. An
        installed skill is never replaced from here — a newer version is the
        operator's ``skills-install --force`` at the machine.
        """
        name = str(job.get("skill") or "")[:64]
        catalog = policy.catalog_enabling(name)

        def refuse(error: str, **detail: Any) -> dict[str, Any]:
            self._record(
                "refused",
                SensitivityClass.PUBLIC,
                job,
                kind="skill_install",
                skill=name,
                reason=error,
                **detail,
            )
            return {**base, "status": "failed", "error": error}

        if catalog is None:
            return refuse(f"skill {name!r} is not pre-approved by this machine's policy")
        try:
            if name in discover_skills():
                return refuse(
                    f"skill {name!r} is already installed on this machine", catalog=catalog.name
                )
            installed, entry = install_from_catalog(
                catalog, name, skills_dirs()[-1], fetch=http_fetch(self._catalog_client)
            )
        except ConfigurationError as exc:
            # Catalog and archive errors name URLs and digests, never material.
            return refuse(str(exc), catalog=catalog.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("skill install failed")
            return refuse(
                f"the install failed on the runner ({type(exc).__name__})", catalog=catalog.name
            )

        pinning = "pinned local" if installed.pinned else "trusted"
        self._record(
            "installed",
            SensitivityClass.PUBLIC,
            job,
            kind="skill_install",
            skill=name,
            catalog=catalog.name,
            version=entry.version,
            sha256=entry.sha256,
            reason=f"pre-approved by skill_catalogs.enable; {pinning}",
        )
        # No release decision: the response is built from the catalog entry and
        # nothing on this machine was read, so there is no material to withhold.
        return {
            **base,
            "status": "completed",
            "response": (
                f"Installed {name} {entry.version} from {catalog.name} "
                f"(sha256 {entry.sha256[:12]}…); {pinning}; enabled by skill_catalogs.enable"
            ),
            "skill": name,
        }

    def serve(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set or the credential is revoked.

        Revocation raises ``LinkRevokedError`` whichever thread sees it first:
        if the heartbeat does, the poll loop ends and raises it here.
        """
        revoked: list[LinkRevokedError] = []

        def heartbeats() -> None:
            while not stop.is_set():
                try:
                    self.beat_once()
                except LinkRevokedError as exc:
                    revoked.append(exc)
                    stop.set()
                except Exception as exc:  # noqa: BLE001 — the network comes and goes
                    logger.warning(f"link heartbeat failed: {exc}")
                stop.wait(self._beat)

        threading.Thread(target=heartbeats, name="link-heartbeat", daemon=True).start()

        backoff = self._poll
        while not stop.is_set():
            try:
                job = self._client.claim()
                backoff = self._poll
            except LinkRevokedError:
                stop.set()
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"link claim failed: {exc}; retrying in {backoff:.0f}s")
                stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            if job is None:
                stop.wait(self._poll)
                continue

            logger.info(
                f"link: job {job['id']} from {(job.get('requested_by') or {}).get('email', '?')}"
            )
            body = self.run_job(job)
            # The result is retried, the run is not: a job that ran once must
            # not run twice because a network blip ate the acknowledgement.
            for attempt in range(5):
                try:
                    self._client.result(str(job["id"]), body)
                    break
                except LinkRevokedError:
                    stop.set()
                    raise
                except LinkConflictError as exc:
                    # The control plane has an outcome for this job that is
                    # not this one (lease expired, cancelled, revoked). The
                    # run stays in the local ledger either way.
                    logger.error(f"link result refused: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"link result not delivered ({exc}); attempt {attempt + 1}/5")
                    stop.wait(min(2**attempt, 30))
            logger.info(f"link: job {job['id']} → {body['status']}")
        if revoked:
            raise revoked[0]
