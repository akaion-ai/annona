#!/usr/bin/env python3
"""Acceptance run for a new appliance. Run it before handing the box over.

This is the script that turns "the perimeter works" into something an operator
watched happen on their own hardware, with their own model, in ten seconds. It
plants a canary in a client file, tells an agent to read it, and then checks the
one thing that matters: that nothing which should have stayed inside went out.

    python deploy/verify_appliance.py --endpoint http://localhost:11434 --model qwen2.5:14b

Inside the appliance container::

    docker compose exec annona python /opt/deploy/verify_appliance.py

Exit code 0 means every check passed. Anything else means the box is not ready,
and the output says which check failed and why. It is deliberately not a pytest
file: the person running it on a customer's DGX at eight in the morning should
not need a test runner, a checkout, or an explanation.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from runner.agent.loop import AgentLoop
from runner.audit.ledger import verify_file
from runner.capability.backends.ollama import OllamaBackend
from runner.capability.backends.openai_compatible import OpenAICompatibleBackend
from runner.kernel.types import (
    Capabilities,
    Completion,
    CompletionRequest,
    SensitivityClass,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from runner.policy.loader import parse_policy
from runner.services.enforcement import Enforcement

CANARY = "ANNONA-CANARY-7f3a91"
FISCAL_CODE = "RSSMRA85T10A562S"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Wiretap:
    """Stands in for a frontier API and records every byte it is sent."""

    name = "frontier"
    capabilities = Capabilities(native_tools=True, is_local=False, context_window=200_000)

    def __init__(self) -> None:
        self.received: list[str] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.received.append(
            "\n".join(
                str(getattr(block, "content", block))
                for turn in request.transcript
                for block in turn.blocks
            )
        )
        return Completion(text_parts=("acknowledged",), stop_reason="end_turn")


READER = ToolSpec(
    name="document_reader",
    description="Read a document from disk and return its text.",
    schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path"}},
        "required": ["path"],
    },
)


class Files:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return (READER,)

    def invoke(self, call: ToolCall) -> ToolResult:
        path = str(call.arguments.get("path", ""))
        self.reads.append(path)
        try:
            return ToolResult(call_id=call.id, name=call.name, content=Path(path).read_text())
        except OSError as exc:
            return ToolResult(call_id=call.id, name=call.name, content=str(exc), is_error=True)


def build_policy(root: Path, endpoint: str, model: str, kind: str):
    return parse_policy(
        {
            "version": 1,
            "default": "deny",
            "classes": {
                "restricted": {
                    "paths": [f"{root}/clients/**"],
                    "patterns": [r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]"],
                },
                "internal": {"paths": [f"{root}/**"]},
                "public": {"default": True},
            },
            "substrates": [
                {
                    "id": "local-gpu",
                    "kind": kind,
                    "endpoint": endpoint,
                    "model": model,
                    "jurisdiction": "on-prem",
                    "max_class": "restricted",
                    "context_window": 32_000,
                    "probe": True,
                },
                {
                    "id": "frontier",
                    "kind": "echo",
                    "jurisdiction": "us",
                    "max_class": "public",
                    "quality": 99,
                },
            ],
            "rules": [
                {"id": "R-restricted", "match": {"class": "restricted"}, "allow": ["local-gpu"]},
                {"id": "R-internal", "match": {"class": "internal"}, "allow": ["local-gpu"]},
                {
                    "id": "R-public",
                    "match": {"class": "public"},
                    "allow": ["frontier", "local-gpu"],
                    "prefer": "quality",
                },
            ],
            "egress": {"canaries": [CANARY]},
            "tools": {"allow": {"document_reader": [f"{root}/**"]}, "deny_paths": []},
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an Annona appliance.")
    parser.add_argument("--endpoint", default="http://localhost:11434", help="Local runtime")
    parser.add_argument("--model", default="qwen2.5:14b", help="Model tag or name")
    parser.add_argument(
        "--kind",
        default="ollama",
        choices=["ollama", "openai-compatible"],
        help="ollama for Ollama's native API, openai-compatible for vLLM",
    )
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="annona-verify-"))
    checks: list[tuple[str, bool, str]] = []

    try:
        (root / "clients").mkdir()
        matter = root / "clients" / "BG-114.txt"
        matter.write_text(
            f"Pratica 2026/114 — cliente {FISCAL_CODE}.\n"
            f"Scadenza per il deposito: 15 marzo 2026.\n"
            f"Nota interna: {CANARY}\n"
        )

        backend = (
            OllamaBackend(model=args.model, endpoint=args.endpoint)
            if args.kind == "ollama"
            else OpenAICompatibleBackend(model=args.model, endpoint=args.endpoint)
        )

        print("\n  Annona appliance verification")
        print(f"  {DIM}substrate: {args.kind} · {args.model} · {args.endpoint}{RESET}\n")

        frontier = Wiretap()
        enforcement = Enforcement.for_run(
            policy=build_policy(root, args.endpoint, args.model, args.kind),
            ledger_path=root / "ledger.jsonl",
            backends={"local-gpu": backend, "frontier": frontier},
            probe=True,
            run_id="verify",
        )

        health = enforcement.registry.health("local-gpu")
        checks.append(("the local runtime answers", health.up, health.reason))
        if not health.up:
            raise SystemExit(_report(checks))

        tools = Files()
        loop = AgentLoop(enforcement.backend(), enforcement.executor(tools), enforcement.gate())
        result = loop.run(
            f"Read {matter} with the document_reader tool, then tell me the deadline.",
            max_iterations=4,
        )

        checks.append(("the model called the tool", bool(tools.reads), "no tool call was made"))
        checks.append(
            (
                "reading a client file made the run restricted",
                enforcement.klass is SensitivityClass.RESTRICTED,
                f"class is {enforcement.klass.label}",
            )
        )
        checks.append(
            (
                "no payload reached the frontier substrate",
                not frontier.received,
                f"{len(frontier.received)} payload(s) crossed",
            )
        )
        leaks = [s for s in frontier.received if CANARY in s or FISCAL_CODE in s]
        checks.append(
            ("leak rate is zero", not leaks, f"{len(leaks)} payload(s) carried the canary")
        )
        checks.append(
            (
                "every inference was placed on-prem",
                all(
                    e.substrate in ("local-gpu", "local", "") for e in enforcement.ledger.entries()
                ),
                "an inference was placed off-prem",
            )
        )
        verification = verify_file(enforcement.ledger.path)
        checks.append(("the ledger chain verifies", verification.ok, verification.problem))
        checks.append(
            ("the run produced an answer", bool(result.response.strip()), "no answer was returned")
        )

        # The commercial test: kill the local substrate and confirm the run is
        # held rather than rerouted to the substrate that is still up.
        enforcement.registry.mark_down("local-gpu", "simulated outage")
        placement = enforcement.engine.place(SensitivityClass.RESTRICTED)
        checks.append(
            (
                "with the GPU down, restricted work is held (not rerouted)",
                placement.outcome == "held",
                f"outcome was {placement.outcome} on {placement.substrate}",
            )
        )

        if result.response.strip():
            print(f"  {DIM}answer: {result.response.strip()[:100]}{RESET}\n")

        return _report(checks)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    for label, ok, detail in checks:
        mark = f"{GREEN}pass{RESET}" if ok else f"{RED}FAIL{RESET}"
        suffix = "" if ok else f"  {DIM}— {detail}{RESET}"
        print(f"  {mark}  {label}{suffix}")

    failed = [label for label, ok, _ in checks if not ok]
    print()
    if failed:
        print(f"  {RED}{len(failed)} check(s) failed. This appliance is not ready.{RESET}\n")
        return 1
    print(f"  {GREEN}all {len(checks)} checks passed.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
