#!/usr/bin/env python3
"""Acceptance run for the link to Agents Studio. Run it before the first real job.

`verify_appliance.py` proves the perimeter on this machine. This proves the
other half: that work sent from Studio arrives here, runs under this machine's
policy and on its hardware, and that what comes back is what the policy allows —
against the real Studio, with a throwaway runner that is revoked at the end.

    python deploy/verify_link.py --org <organization uuid> --token "$STUDIO_TOKEN"
    make verify-link ORG=<organization uuid>

The token is a Studio session (Firebase ID token) of an admin of that
organization: enrolling a machine is an admin's act. It is read from
``--token`` or ``ANNONA_STUDIO_TOKEN`` and never printed or written.

What it checks, in order:

    enroll      a single-use code from Studio becomes this machine's credential
    cancel      a job cancelled before it is claimed never runs
    online      the heartbeat reaches Studio, with the substrates and the machine
    released    an ordinary question runs here and its answer goes back
    withheld    a question carrying a codice fiscale runs here; the answer stays
                in this machine's inbox and Studio never receives it
    revoked     revoking the runner in Studio stops it here
    ledger      every job is in the local ledger, and the chain verifies

Everything happens in a temporary home: the operator's policy, ledger and link
are not touched. Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FISCAL_CODE = "RSSMRA80A01H501U"  # fictional, valid in shape
TERMINAL = {"completed", "withheld", "failed", "cancelled"}


@dataclass
class Report:
    checks: list[tuple[str, bool, str, float]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, started: float) -> bool:
        self.checks.append((name, ok, detail, time.monotonic() - started))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {name:<9} {detail}  ({time.monotonic() - started:.1f}s)", flush=True)
        return ok

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _, _ in self.checks)


class Studio:
    """The calls a person makes in Studio, as that person."""

    def __init__(self, endpoint: str, org: str, token: str, client: httpx.Client | None = None):
        from runner.link import API_PREFIX

        self._base = f"{endpoint.rstrip('/')}{API_PREFIX}"
        self._http = client or httpx.Client(timeout=30)
        self._headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self._http.request(
            method, f"{self._base}{path}", json=body, headers=self._headers
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else None

    def enrollment(self, name: str) -> str:
        return str(self._call("POST", "/enrollments", {"name": name})["code"])

    def runner(self, runner_id: str) -> dict[str, Any] | None:
        return next((r for r in self._call("GET", "/runners") if r["id"] == runner_id), None)

    def job(self, runner_id: str, instruction: str, title: str) -> dict[str, Any]:
        return self._call(
            "POST", "/jobs", {"runner_id": runner_id, "instruction": instruction, "title": title}
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._call("GET", f"/jobs/{job_id}")

    def cancel(self, job_id: str) -> None:
        self._call("POST", f"/jobs/{job_id}/cancel")

    def revoke(self, runner_id: str) -> None:
        self._call("DELETE", f"/runners/{runner_id}")

    def wait(self, job_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        job = self.get_job(job_id)
        while job["status"] not in TERMINAL and time.monotonic() < deadline:
            time.sleep(1.0)
            job = self.get_job(job_id)
        return job


def _isolate(home: Path) -> None:
    """Everything this run writes goes under ``home``; set before runner reads it."""
    os.environ["ANNONA_HOME"] = str(home / ".annona")
    os.environ["AKAION_HOME"] = str(home / ".akaion")
    os.environ["AKAION_BRAIN_DIR"] = str(home / "vault")


def _prepare(model: str, ollama: str) -> None:
    """A fresh config and a local-only policy whose answers may go back up to internal."""
    from runner.cli_setup import write_policy
    from runner.config import ConfigManager
    from runner.services.enforcement import policy_path

    ConfigManager().create_default_config()
    target = policy_path()
    if model == "echo":  # offline: the real perimeter, a scripted model (ADR 0003)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "version: 1\ndefault: deny\n"
            "classes:\n"
            "  restricted: {patterns: ['[A-Z]{6}\\d{2}[A-Z]\\d{2}[A-Z]\\d{3}[A-Z]']}\n"
            "  internal: {default: true}\n"
            "  public: {}\n"
            "substrates:\n"
            "  - {id: local, kind: echo, jurisdiction: on-prem, max_class: restricted}\n"
            "rules:\n"
            "  - {match: {class: restricted}, allow: [local]}\n"
            "  - {match: {class: internal}, allow: [local]}\n"
            "  - {match: {class: public}, allow: [local]}\n"
            "link: {release: internal}\n"
        )
        return
    write_policy(target, local_endpoint=ollama, local_model=model)
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\nlink:\n  release: internal\n")


def verify(
    studio: Studio,
    endpoint: str,
    *,
    name: str,
    timeout: float,
    run: Callable[..., dict[str, Any]] | None = None,
) -> Report:
    """Every check, in order. Stops at the first failure that makes the rest moot."""
    from runner import __version__
    from runner.audit.ledger import read_entries, verify_file
    from runner.cli_link import local_run
    from runner.link import LinkClient, LinkRevokedError, LinkWorker, enroll, link_path
    from runner.services.enforcement import policy_path

    report = Report()
    runner_id = ""
    stop = threading.Event()
    revoked: list[BaseException] = []

    try:
        t = time.monotonic()
        config = enroll(endpoint, studio.enrollment(name), name, version=__version__)
        config.save(link_path())
        runner_id = config.runner_id
        if not report.add(
            "enroll", True, f"runner {runner_id[:8]} in {config.organization or 'the org'}", t
        ):
            return report

        # Before the worker starts, so the outcome cannot depend on a race.
        t = time.monotonic()
        doomed = studio.job(
            runner_id, "Questo job viene annullato prima di partire.", "verify-link: cancel"
        )
        studio.cancel(doomed["id"])
        status = studio.get_job(doomed["id"])["status"]
        report.add("cancel", status == "cancelled", f"job {doomed['id'][:8]} → {status}", t)

        worker = LinkWorker(
            LinkClient(config),
            run or local_run(),
            version=__version__,
            poll_seconds=1.0,
            heartbeat_seconds=5.0,
        )

        def serve() -> None:
            try:
                worker.serve(stop)
            except LinkRevokedError as exc:
                revoked.append(exc)

        thread = threading.Thread(target=serve, name="verify-link", daemon=True)
        thread.start()

        t = time.monotonic()
        seen = None
        while time.monotonic() - t < 60:
            seen = studio.runner(runner_id)
            if seen and seen.get("online") and seen.get("substrates"):
                break
            time.sleep(1.0)
        substrates = ", ".join(
            f"{s.get('id')}:{s.get('model') or s.get('kind')}"
            for s in (seen or {}).get("substrates", [])
        )
        if not report.add(
            "online",
            bool(seen and seen.get("online")),
            f"heartbeat · {substrates or 'no substrates'}",
            t,
        ):
            return report

        t = time.monotonic()
        job = studio.wait(
            studio.job(
                runner_id, "Rispondi soltanto con la parola PRONTO.", "verify-link: released"
            )["id"],
            timeout,
        )
        answer = (job.get("response") or "").strip()
        report.add(
            "released",
            job["status"] == "completed" and bool(answer),
            f"{job['status']} · {json.dumps(answer[:60], ensure_ascii=False)} · "
            f"ran on {((job.get('result') or {}).get('placement') or {}).get('substrate', '?')}",
            t,
        )

        t = time.monotonic()
        job = studio.wait(
            studio.job(
                runner_id,
                f"Scrivi una riga di saluto per Mario Rossi, codice fiscale {FISCAL_CODE}.",
                "verify-link: withheld",
            )["id"],
            timeout,
        )
        inbox = policy_path().parent / "link" / "inbox" / f"{job['id']}.json"
        leaked = FISCAL_CODE in json.dumps({k: v for k, v in job.items() if k != "instruction"})
        report.add(
            "withheld",
            job["status"] == "withheld"
            and not job.get("response")
            and not leaked
            and inbox.exists(),
            f"{job['status']} · answer in this machine's inbox: {'yes' if inbox.exists() else 'no'}"
            f" · CF in what Studio holds: {'yes' if leaked else 'no'}",
            t,
        )

        t = time.monotonic()
        studio.revoke(runner_id)
        thread.join(timeout=30)
        report.add(
            "revoked",
            not thread.is_alive() and bool(revoked),
            "serve stopped on the next call" if revoked else "still running",
            t,
        )
        runner_id = ""

        t = time.monotonic()
        ledger = policy_path().parent / "ledger.jsonl"
        outcomes = [e.outcome for e in read_entries(ledger) if e.kind == "link"]
        chain = verify_file(ledger)
        report.add(
            "ledger",
            chain.ok and {"received", "released", "withheld"} <= set(outcomes),
            f"{chain} · link: {', '.join(outcomes)}",
            t,
        )
        return report
    except Exception as exc:  # noqa: BLE001 — reported, not raised: the cleanup must run
        report.add("error", False, f"{type(exc).__name__}: {exc}", time.monotonic())
        return report
    finally:
        stop.set()
        if runner_id:
            try:
                studio.revoke(runner_id)
            except Exception:  # noqa: BLE001
                print(f"  ! revoke runner {runner_id} in Studio by hand", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    from runner.service_urls import resolve_service_url

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--org", required=True, help="organization id in Studio")
    parser.add_argument("--token", default=os.getenv("ANNONA_STUDIO_TOKEN", ""))
    parser.add_argument("--endpoint", default=None, help="Studio API (default: production)")
    parser.add_argument(
        "--model",
        default=os.getenv("ANNONA_LIVE_MODEL", "qwen2.5:7b"),
        help="local model, or 'echo' for the offline scripted backend",
    )
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--name", default=f"verify-link-{os.uname().nodename.split('.')[0]}")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per job")
    parser.add_argument("--keep", action="store_true", help="leave the home for inspection")
    args = parser.parse_args(argv)
    if not args.token:
        parser.error("a Studio session token is needed: --token or ANNONA_STUDIO_TOKEN")

    home = Path(tempfile.mkdtemp(prefix="annona-verify-link-"))
    _isolate(home)
    _prepare(args.model, args.ollama)
    endpoint = args.endpoint or resolve_service_url("ai")
    print(f"\nverify-link · {endpoint} · org {args.org} · model {args.model} · home {home}\n")

    try:
        report = verify(
            Studio(endpoint, args.org, args.token), endpoint, name=args.name, timeout=args.timeout
        )
    finally:
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)
    passed = sum(ok for _, ok, _, _ in report.checks)
    print(f"\n{'✅' if report.ok else '❌'} {passed}/{len(report.checks)} checks passed\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
