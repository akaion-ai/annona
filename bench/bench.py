#!/usr/bin/env python3
"""Lab Note 08 + the F1 claim, measured on whatever box this runs on.

Two numbers the talk needs and the repo does not yet have:

1. decode throughput per model, straight from Ollama's own eval counters
   (no timing code of ours to get wrong), so "the ceiling is bandwidth, not
   capacity" stops being an assertion;
2. the tool-use failure taxonomy: of the calls that fail, how many are the
   wrong intent and how many are merely malformed arguments. That is the
   hypothesis docs/research/index.md says it wants falsified.

Usage:  python bench.py qwen2.5:3b qwen2.5:14b granite4:tiny-h
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import httpx

from runner.capability.backends.ollama import OllamaBackend
from runner.kernel.blocks import text_block
from runner.kernel.types import CompletionRequest, ToolSpec, Turn

ENDPOINT = "http://localhost:11434"

# --- 1. throughput -----------------------------------------------------------

THROUGHPUT_PROMPT = (
    "Spiega in un paragrafo perche' un modello quantizzato a 4 bit occupa meno "
    "memoria di uno a 16 bit, e cosa si perde."
)


def throughput(model: str, repeats: int = 3) -> dict[str, float]:
    """Decode and prefill tok/s, from the server's own counters."""
    decode, prefill = [], []
    with httpx.Client(timeout=600.0) as http:
        for _ in range(repeats):
            r = http.post(
                f"{ENDPOINT}/api/chat",
                json={
                    "model": model,
                    "stream": False,
                    "messages": [{"role": "user", "content": THROUGHPUT_PROMPT}],
                    "options": {"temperature": 0.0, "num_predict": 200},
                },
            )
            r.raise_for_status()
            d = r.json()
            if d.get("eval_duration"):
                decode.append(d["eval_count"] / (d["eval_duration"] / 1e9))
            if d.get("prompt_eval_duration"):
                prefill.append(d["prompt_eval_count"] / (d["prompt_eval_duration"] / 1e9))
    return {
        "decode_tok_s": round(statistics.median(decode), 1) if decode else 0.0,
        "prefill_tok_s": round(statistics.median(prefill), 1) if prefill else 0.0,
    }


# --- 2. tool use -------------------------------------------------------------

TOOLS = (
    ToolSpec(
        name="document_reader",
        description="Read a document from disk and return its text.",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path"}},
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="calendar_create",
        description="Create a calendar event.",
        schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {"type": "string", "description": "ISO 8601 start"},
                "duration_minutes": {"type": "integer"},
            },
            "required": ["title", "start_iso", "duration_minutes"],
        },
    ),
    ToolSpec(
        name="invoice_lookup",
        description="Look up invoices for a VAT number in a given year.",
        schema={
            "type": "object",
            "properties": {
                "vat_number": {"type": "string"},
                "year": {"type": "integer"},
                "unpaid_only": {"type": "boolean"},
            },
            "required": ["vat_number", "year"],
        },
    ),
    ToolSpec(
        name="email_send",
        description="Send an email.",
        schema={
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    ),
    ToolSpec(
        name="search_vault",
        description="Search the local knowledge vault.",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "since_iso": {"type": "string"},
            },
            "required": ["query", "top_k"],
        },
    ),
)

# A real agent menu is not five tools. These are never the right answer; they are
# there because the choice gets harder as the menu grows, and that is the
# condition the claim has to survive.
DISTRACTORS = tuple(
    ToolSpec(
        name=name,
        description=description,
        schema={
            "type": "object",
            "properties": {"argument": {"type": "string"}},
            "required": ["argument"],
        },
    )
    for name, description in (
        ("web_fetch", "Fetch a URL and return its text."),
        ("shell_run", "Run a shell command on this machine."),
        ("note_create", "Create a new note in the vault."),
        ("contact_lookup", "Look up a contact by name."),
        ("task_create", "Create a task in the local task list."),
        ("file_move", "Move a file from one path to another."),
        ("pec_send", "Send a certified email (PEC)."),
        ("translate", "Translate a piece of text."),
        ("summarise", "Summarise a piece of text."),
        ("image_describe", "Describe the contents of an image."),
    )
)

BY_NAME = {t.name: t for t in TOOLS}

# Each task: the prompt, and the tool a correct run must call.
TASKS = [
    ("Leggi il file /pratiche/BG-114.txt e dimmi la scadenza.", "document_reader"),
    (
        "Fissami una riunione intitolata 'Revisione offerta' il 4 marzo 2026 alle 14:30, "
        "di 45 minuti.",
        "calendar_create",
    ),
    (
        "Controlla se la partita IVA IT03828610284 ha fatture non pagate nel 2025.",
        "invoice_lookup",
    ),
    (
        "Manda una mail a mario.rossi@example.com con oggetto 'Preventivo' e nel corpo "
        "scrivi che lo mandiamo entro venerdi'.",
        "email_send",
    ),
    ("Cerca nel vault le ultime 5 note che parlano di collaudo.", "search_vault"),
]

# The same five tools, but nothing is spelled out: every required argument has
# to be derived from the sentence. This is where the taxonomy earns its keep —
# an easy prompt measures nothing, because every model passes it.
HARD_TASKS = [
    ("Apri la pratica BG-114, sta nella cartella /pratiche.", "document_reader"),
    (
        "Il contratto con ACME scade fra tre settimane. Mettimi mezz'ora in agenda "
        "due giorni prima, di mattina presto.",
        "calendar_create",
    ),
    (
        "Quanto ci devono ancora quelli della IT03828610284? Guarda solo l'anno scorso.",
        "invoice_lookup",
    ),
    (
        "Rispondi a Mario che confermiamo tutto, il suo indirizzo e' "
        "mario.rossi@example.com.",
        "email_send",
    ),
    (
        "Fammi vedere le tre note piu' recenti sul collaudo, ma solo da gennaio in poi.",
        "search_vault",
    ),
]

TYPES = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "number": (int, float),
}


def classify(call, expected: str) -> str:
    """ok | no_call | wrong_tool | bad_args — the taxonomy the claim rests on."""
    if call is None:
        return "no_call"
    if call.name != expected:
        return "wrong_tool"
    schema = BY_NAME[expected].schema
    args = call.arguments or {}
    for field in schema.get("required", []):
        if field not in args or args[field] in (None, ""):
            return "bad_args"
    for field, value in args.items():
        spec = schema.get("properties", {}).get(field)
        if spec is None:
            return "bad_args"  # invented a parameter
        want = TYPES.get(spec.get("type"))
        if want and not isinstance(value, want):
            return "bad_args"
        if spec.get("type") == "integer" and isinstance(value, bool):
            return "bad_args"
    return "ok"


def tool_use(
    model: str,
    tasks=TASKS,
    tools=TOOLS,
    temperature: float = 0.0,
    repeats: int = 4,
) -> dict:
    backend = OllamaBackend(model=model, endpoint=ENDPOINT, timeout=600.0)
    counts = {"ok": 0, "no_call": 0, "wrong_tool": 0, "bad_args": 0}
    failures: list[str] = []
    for prompt, expected in tasks:
        for _ in range(repeats):
            request = CompletionRequest(
                system=(
                    "Sei un assistente che opera su una postazione locale. "
                    "Oggi e' lunedi' 19 gennaio 2026. "
                    "Usa sempre lo strumento appropriato invece di rispondere a voce."
                ),
                transcript=(Turn(role="user", blocks=(text_block(prompt),)),),
                tools=tools,
                temperature=temperature,
            )
            try:
                completion = backend.complete(request)
            except Exception as exc:  # a dead substrate is not a taxonomy entry
                failures.append(f"{expected}: backend error {exc}")
                counts["no_call"] += 1
                continue
            call = completion.tool_calls[0] if completion.tool_calls else None
            verdict = classify(call, expected)
            counts[verdict] += 1
            if verdict != "ok":
                failures.append(
                    f"{expected} -> {verdict}: "
                    f"{call.name if call else '(none)'} "
                    f"{json.dumps(dict(call.arguments), ensure_ascii=False) if call else ''}"
                )
    total = sum(counts.values())
    wrong = counts["no_call"] + counts["wrong_tool"]
    counts["total"] = total
    counts["success_rate"] = round(100 * counts["ok"] / total, 1) if total else 0.0
    errors = total - counts["ok"]
    counts["share_of_errors_that_are_args"] = (
        round(100 * counts["bad_args"] / errors, 1) if errors else None
    )
    counts["share_of_errors_that_are_intent"] = (
        round(100 * wrong / errors, 1) if errors else None
    )
    return {"counts": counts, "failures": failures}


def main() -> int:
    models = sys.argv[1:] or ["qwen2.5:3b", "qwen2.5:14b"]
    results = {}
    for model in models:
        print(f"\n=== {model}", flush=True)
        started = time.time()
        tp = throughput(model)
        print(f"  decode {tp['decode_tok_s']} tok/s · prefill {tp['prefill_tok_s']} tok/s", flush=True)

        tiers = {
            "easy": tool_use(model, TASKS, TOOLS, temperature=0.0),
            # 15 tools, every argument derived, and the temperature the product
            # actually ships with.
            "hard": tool_use(model, HARD_TASKS, TOOLS + DISTRACTORS, temperature=0.7),
        }
        for tier, tu in tiers.items():
            c = tu["counts"]
            print(
                f"  [{tier}] {c['ok']}/{c['total']} ok ({c['success_rate']}%) · "
                f"no_call {c['no_call']} · wrong_tool {c['wrong_tool']} · bad_args {c['bad_args']}",
                flush=True,
            )
            if c["share_of_errors_that_are_args"] is not None:
                print(
                    f"         of the errors: {c['share_of_errors_that_are_args']}% malformed "
                    f"arguments, {c['share_of_errors_that_are_intent']}% wrong intent",
                    flush=True,
                )
            for line in tu["failures"][:6]:
                print(f"         - {line}", flush=True)

        results[model] = {
            "throughput": tp,
            "tiers": {t: tu["counts"] for t, tu in tiers.items()},
            "failures": {t: tu["failures"] for t, tu in tiers.items()},
            "wall_seconds": round(time.time() - started, 1),
        }

    out = "bench_results.json"
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
