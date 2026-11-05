"""E6 — what the perimeter costs per step, and what the audit costs per entry.

HLD §12 asks the question that decides whether a perimeter survives contact with
an operations team: *if the prefect adds 200 ms per step it will be turned off.*
This measures the three decisions on the hot path — classifying the outbound
payload, clearing a tool call, placing a step — over payloads from a tweet to
the 200k-character render limit, and the ledger append that records them.

Run: ``python -m experiments.overhead``.
"""

from __future__ import annotations

import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

from experiments.harness import corpus, policy_document, run_task
from runner.audit.compliance import audit_compliance
from runner.audit.ledger import Ledger, verify_file
from runner.kernel.types import Requirement, SensitivityClass, ToolCall
from runner.placement.engine import PlacementDecisionEngine
from runner.placement.registry import SubstrateRegistry
from runner.policy.classifier import PolicyClassifier, WorkingSet
from runner.policy.gate import DefaultDenyGate
from runner.policy.loader import parse_policy

SIZES = (280, 4_000, 40_000, 200_000)
FILLER = "Verbale: aggiornare il gestionale, verificare le consegne del lotto 7. "


def _time(fn, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1e6)
    samples.sort()
    return {
        "p50_us": round(statistics.median(samples), 1),
        "p95_us": round(samples[int(0.95 * len(samples)) - 1], 1),
    }


def run(repeats: int = 400) -> dict:
    root = Path(tempfile.mkdtemp())
    policy = parse_policy(policy_document(root))
    classifier = PolicyClassifier(policy)
    engine = PlacementDecisionEngine(policy, SubstrateRegistry.from_substrates(policy.substrates))
    gate = DefaultDenyGate(policy, classifier, WorkingSet())
    out: dict = {"classify_text": {}, "gate_clear": {}}

    for size in SIZES:
        text = (FILLER * (size // len(FILLER) + 1))[:size] + f" {root}/clients/doc-1.txt"
        out["classify_text"][size] = _time(lambda t=text: classifier.classify_text(t), repeats)
        call = ToolCall("c", "document_reader", {"path": f"{root}/work/a.txt", "note": text})
        out["gate_clear"][size] = _time(lambda c=call: gate.clear(c), repeats)

    out["place"] = {
        k.label: _time(lambda k=k: engine.place(k, Requirement()), repeats * 5)
        for k in SensitivityClass
    }

    ledger = Ledger(root / "bench.jsonl", run_id="bench", fsync=False)
    out["ledger_append"] = _time(
        lambda: ledger.record(
            "inference",
            outcome="placed",
            klass=SensitivityClass.PUBLIC,
            substrate="frontier",
            payload="x" * 4000,
        ),
        repeats,
    )
    durable = Ledger(root / "durable.jsonl", run_id="bench", fsync=True)
    out["ledger_append_fsync"] = _time(
        lambda: durable.record(
            "inference",
            outcome="placed",
            klass=SensitivityClass.PUBLIC,
            substrate="frontier",
            payload="x" * 4000,
        ),
        max(50, repeats // 4),
    )

    # Audit cost: a ledger from real runs of the harness.
    docs = corpus(root, 300, seed=0, misfile=0.2)
    files = {d.path: d.text for d in docs}
    audited = root / "audited.jsonl"
    for i, doc in enumerate(docs):
        run_task(doc, files, root=root, ledger=audited, run_id=str(i))
    entries = sum(1 for _ in audited.open())
    t = time.perf_counter()
    verify_file(audited)
    verify_s = time.perf_counter() - t
    t = time.perf_counter()
    audit_compliance(audited, policy)
    comply_s = time.perf_counter() - t
    out["audit"] = {
        "entries": entries,
        "verify_us_per_entry": round(verify_s / entries * 1e6, 1),
        "comply_us_per_entry": round(comply_s / entries * 1e6, 1),
    }
    return {
        "experiment": "E6 perimeter overhead",
        "machine": f"{platform.machine()} {platform.processor() or ''} {platform.python_version()}",
        "repeats": repeats,
        "results": out,
    }


if __name__ == "__main__":
    result = run()
    target = Path(__file__).parent / "results" / "overhead.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["results"], indent=1), file=sys.stderr)
