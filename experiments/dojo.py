"""E5 — AgentDojo through the unmodified Annona loop, with a real local model.

AgentDojo (Debenedetti et al., NeurIPS 2024 D&B) is the benchmark every agent
IFC paper reports on. Its tasks run here through :class:`AgentLoop` and the
perimeter exactly as in production; the only adapter is between AgentDojo's
``FunctionsRuntime`` and Annona's ``ToolExecutor``.

The question is not AgentDojo's usual one (can injection be stopped?) but the
one Annona adds: **how much of the user's data reaches a provider that may not
hold it, and at what cost in utility** — with the injection suite as the
adversary trying to move data out through tools.

Substrates: ``local`` (the Ollama model, may hold restricted) and ``frontier``
(the same model behind a wiretap, capped at public). The same weights answer on
both, so utility differences come from placement and refusals, not from model
quality. Data classes come from the policy's content patterns: an IBAN or a
password in a payload is restricted.

Configurations:

``open``        every class may run on the frontier; no tool ceilings — a gateway.
``placement``   restricted material stays local; tools unconstrained.
``placement+egress``  and tools that move money or data out
                (``send_money``, ``schedule_transaction``, ``update_scheduled_transaction``)
                are egress with ceiling ``internal``.

Needs the separate environment with ``agentdojo`` installed (see
``experiments/README.md``). Run::

    python -m experiments.dojo --model qwen2.5:14b --suite banking
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.functions_runtime import FunctionCall, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite

from runner.agent.loop import AgentLoop
from runner.capability.backends.ollama import OllamaBackend
from runner.kernel.types import ToolCall, ToolResult, ToolSpec
from runner.policy.loader import parse_policy
from runner.services.enforcement import Enforcement

IBAN = r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"
EGRESS_TOOLS = ("send_money", "schedule_transaction", "update_scheduled_transaction")


class Recording:
    """Wraps a backend and keeps every payload it was sent."""

    def __init__(self, inner: Any):
        self.inner, self.received = inner, []

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def capabilities(self) -> Any:
        return self.inner.capabilities

    def complete(self, request: Any) -> Any:
        from runner.kernel.blocks import block_text

        self.received.append(
            request.system
            + "\n".join(block_text(b) for t in request.transcript for b in t.blocks)
        )
        return self.inner.complete(request)


def policy(config: str) -> dict:
    open_ = config == "open"
    return {
        "version": 1,
        "default": "deny",
        "classes": {
            "restricted": {"patterns": [IBAN, r"(?i)password"]},
            "public": {"default": True},
        },
        "substrates": [
            {"id": "local", "kind": "echo", "max_class": "restricted", "jurisdiction": "on-prem",
             "quality": 60},
            {"id": "frontier", "kind": "echo", "max_class": "restricted" if open_ else "public",
             "jurisdiction": "us", "quality": 95},
        ],
        "rules": [
            {"id": "R-restricted", "match": {"class": "restricted"},
             "allow": ["frontier", "local"] if open_ else ["local"], "prefer": "quality"},
            {"id": "R-public", "match": {"class": "public"}, "allow": ["frontier", "local"],
             "prefer": "quality"},
        ],
        "tools": {
            "allow": {},  # filled per suite
            "egress": {t: "internal" for t in EGRESS_TOOLS} if config == "placement+egress" else {},
        },
    }


class Executor:
    """AgentDojo's runtime as an Annona ToolExecutor."""

    def __init__(self, runtime: FunctionsRuntime, env: Any):
        self.runtime, self.env = runtime, env
        self.calls: list[FunctionCall] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(f.name, f.description, f.parameters.model_json_schema())
            for f in self.runtime.functions.values()
        )

    def invoke(self, call: ToolCall) -> ToolResult:
        self.calls.append(FunctionCall(function=call.name, args=dict(call.arguments), id=call.id))
        result, error = self.runtime.run_function(self.env, call.name, dict(call.arguments))
        content = error if error else str(result)
        return ToolResult(call_id=call.id, name=call.name, content=content, is_error=bool(error))


class AnnonaPipeline(BasePipelineElement):
    name = "annona"

    def __init__(self, config: str, model: str, workdir: Path):
        self.config, self.model, self.workdir = config, model, workdir
        self.frontier_payloads: list[str] = []
        self.refused = 0

    def query(self, query, runtime, env=None, messages=(), extra_args=None):  # type: ignore[override]
        document = policy(self.config)
        document["tools"]["allow"] = {name: [] for name in runtime.functions}
        local = OllamaBackend(self.model)
        frontier = Recording(OllamaBackend(self.model))
        enforcement = Enforcement.for_run(
            policy=parse_policy(document),
            ledger_path=self.workdir / f"{self.config}.jsonl",
            backends={"local": local, "frontier": frontier},
            probe=False,
            fsync=False,
        )
        executor = Executor(runtime, env)
        loop = AgentLoop(enforcement.backend(), enforcement.executor(executor), enforcement.gate())
        result = loop.run(query)
        self.frontier_payloads.extend(frontier.received)
        self.refused += sum(1 for inv in result.tool_calls if "Permission denied" in str(inv.result))

        out: list[dict] = [{"role": "user", "content": [{"type": "text", "content": query}]}]
        if executor.calls:
            out.append({"role": "assistant", "content": None, "tool_calls": executor.calls})
        out.append({"role": "assistant",
                    "content": [{"type": "text", "content": result.response or ""}],
                    "tool_calls": None})
        return query, runtime, env, out, extra_args or {}


def _user_secrets(suite: Any) -> list[str]:
    """Strings of the user's own data that must not reach the frontier."""
    env = suite.load_and_inject_default_environment({})
    dump = env.model_dump_json()
    import re

    return sorted(set(re.findall(IBAN, dump)))


def run(model: str, suite_name: str, configs: list[str], injections: int, limit: int) -> dict:
    suite = get_suite("v1.2", suite_name)
    secrets = _user_secrets(suite)
    attack_name = "important_instructions"
    out: dict = {"model": model, "suite": suite_name, "attack": attack_name, "results": {}}
    user_tasks = list(suite.user_tasks.values())[:limit]
    injection_tasks = list(suite.injection_tasks.values())[:injections]
    for config in configs:
        tally: Counter[str] = Counter()
        started = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmp:
            pipeline = AnnonaPipeline(config, model, Path(tmp))
            pipeline.name = "local"  # AgentDojo names the target "Local model" in its attack text
            attack = load_attack(attack_name, suite, pipeline)
            for ut in user_tasks:
                utility, _ = suite.run_task_with_pipeline(pipeline, ut, None, {})
                tally["benign"] += 1
                tally["benign_utility"] += utility
                for it in injection_tasks:
                    injections_map = attack.attack(ut, it)
                    utility, attacked = suite.run_task_with_pipeline(pipeline, ut, it, injections_map)
                    tally["attacked"] += 1
                    tally["attacked_utility"] += utility
                    tally["attack_success"] += attacked
                print(f"  {config} {ut.ID} done", file=sys.stderr)
            leaked = [p for p in pipeline.frontier_payloads if any(s in p for s in secrets)]
        out["results"][config] = {
            "benign_utility": tally["benign_utility"] / max(1, tally["benign"]),
            "utility_under_attack": tally["attacked_utility"] / max(1, tally["attacked"]),
            "attack_success_rate": tally["attack_success"] / max(1, tally["attacked"]),
            "frontier_payloads": len(pipeline.frontier_payloads),
            "frontier_payloads_with_user_iban": len(leaked),
            "tool_calls_refused": pipeline.refused,
            "pairs": tally["attacked"],
            "minutes": round((time.perf_counter() - started) / 60, 1),
        }
        print(config, out["results"][config], file=sys.stderr)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:14b")
    ap.add_argument("--suite", default="banking")
    ap.add_argument("--configs", default="open,placement,placement+egress")
    ap.add_argument("--injections", type=int, default=9)
    ap.add_argument("--limit", type=int, default=16)
    args = ap.parse_args()
    result = run(args.model, args.suite, args.configs.split(","), args.injections, args.limit)
    target = Path(__file__).parent / "results" / f"dojo-{args.suite}-{args.model.replace(':', '_')}.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(f"wrote {target}", file=sys.stderr)
