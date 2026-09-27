# Paper plan — measurements before words

*2026-09-27. Audit of what the code can measure today, against what a systems/security paper
needs. Companion to [related work](related-work.md). No number below may appear in a paper until
the run that produced it is committed with its inputs.*

**Verdict.** The mechanisms exist and are tested (975 tests, 35 of them live/container and skipped
without env vars); **no paper-grade number exists yet.** What we have are deterministic tests against
scripted stubs, one live acceptance script (`make verify`) and one small tool-use bench. Without the
measurements below the paper is a whitepaper.

## Findings that change the story

1. **The canary check is circular as a leak measurement.** Canaries are declared in
   `egress.canaries`, and the router forces any payload containing one to `restricted` before
   placing it (`runner/placement/router.py:264-266`; the same on briefs `:411` and redactions
   `:559`). A leak rate of 0 with declared canaries proves string matching, not classification. A
   paper number must seed canaries the policy does **not** list and measure whether the
   path/pattern classifier catches their carriers. In `deploy/verify_appliance.py` the frontier
   receives **zero** payloads, so that leak check has no denominator; its `Wiretap` also records
   transcript blocks but not `request.system` (the test double at `tests/test_enforcement.py:73`
   records both).
2. **Exfiltration through a permitted tool is unmeasured and probably open.** The gate classifies a
   call's arguments but permits any allow-listed tool at any class (`runner/policy/gate.py:79-104`);
   only model inference passes the router's egress check. An allow-listed network tool (browser,
   shell) carrying restricted text would pass. This is exactly what an AgentDojo evaluation probes —
   fix or state it before the paper does.
3. **The class has no model behind it.** It comes from path globs and content regexes
   (`runner/policy/classifier.py:97`, `PolicyClassifier`), and there is no seam to plug a content
   classifier: `Enforcement.for_run` hard-codes `PolicyClassifier(policy)`
   (`runner/services/enforcement.py:394`), as do `runner/link.py:319,514` and
   `runner/services/attachments.py:445`; the `Classifier` protocol (`runner/kernel/ports.py:116`)
   declares only `classify_path`/`classify_content`, while router, gate and tracker call
   `classify_text`/`classify_call`/`classify_result` on the concrete class. Cleanest seam: a
   `classifier=` parameter and a widened protocol; a model-backed classifier (Iovis) returns
   `max(policy, model)`.
4. **The tool-call bench falsifies a claim the code still makes.** `bench/results-m1pro.json`
   (M1 Pro, Ollama): qwen2.5:3b hard tier 11/20, errors 5 no-call + 4 wrong-tool + **0 bad-args** —
   all intent, no malformed arguments; qwen2.5:14b and granite4:tiny-h 20/20 on both tiers.
   `runner/cli_setup.py:57-63` still asserts that malformed arguments are the problem. Grammar
   decoding is declared (`grammar="json_schema"` in the Ollama and OpenAI-compatible backends) but
   no `format` / guided-decoding parameter is ever sent.
5. **Docs and code disagree.** `design/hld.md` §11 still lists the prefect, placement engine, ledger
   and classification as missing while its phasing table and the code say F0–F3 are done; T3 says 12
   tasks, the matrix has 15 cases; `annona audit --canaries`, `bench/toolcall` and `bench/serve` do
   not exist.

## Measurements

| # | Measurement | Status | Entry point | Smallest work for a paper number |
|---|---|---|---|---|
| M1 | **Leak rate** (restricted egress per step) | partial — demonstration, circular | `tests/test_enforcement.py:256`; `deploy/verify_appliance.py` (one canary, one run) | Seeded corpus generator (IBAN, codice fiscale, names, case numbers); workload runner ≥ 1,000 steps with a real local model and a wiretap frontier (`Wiretap`, `verify_appliance.py:51`, or `RoutingBackend.egress`, `router.py:148`); canaries **not** in policy; JSON results |
| M2 | **Placement conformance** | runnable, deterministic | `tests/test_placement.py:98-126` (15 cases: 3 classes × 5 liveness states); `tests/test_enforcement.py:204,224` | Ledger replay: re-run `PlacementDecisionEngine.place()` per ledger entry against policy + health snapshot, count mismatches; run it on the M1 ledger |
| M3 | **Hold vs failover** | runnable, deterministic + one live check | `tests/test_enforcement.py:308,342`; `verify_appliance.py:237-245` (uses `mark_down`, not a real kill) | Live test killing Ollama/vLLM mid-run; report hold rate and time-to-reroute |
| M4 | **Ledger tamper detection** | runnable | `annona verify` (`runner/cli_perimeter.py:397` → `runner/audit/ledger.py:162`); `tests/test_ledger.py:101-190` (whole-chain rebuild explicitly *not* detected) | Byte/field fuzz, detection rate with correct `at_seq`; state the rebuild limit and external anchoring |
| M5 | **Cost/privacy frontier** | not built | ingredients: `annona audit` counts; Prometheus metrics in `runner/audit/metrics.py` | Task set with a quality metric; three configs (all-local, all-frontier, policy); analysis of placement efficiency, hold rate, €/token, quality |
| M6 | **Policy latency per step** | not built | — | `timeit` over `RoutingBackend._effective_class`, `DefaultDenyGate.clear`, `PlacementDecisionEngine.place` on payloads up to the 200k-character render limit; p50/p95 (~50 lines) |
| M7 | **Local tool-call validity** | partial | `bench/bench.py` (untracked; needs Ollama) | Implement constrained decoding behind a flag; ≥ 200 calls per model; grammar on vs off; confidence intervals |
| M8 | **Injection / exfiltration** | not built | defences: `DefaultDenyGate` (`gate.py:57-104`) | AgentDojo tools wrapped as a `ToolExecutor`; runs under no policy / legacy `permissions/manager.py` / default-deny; utility, attack success, restricted egress. Finding 2 first |
| M9 | **Redaction leakage and quality** | mechanism only | `router._complete_via_redaction` (`router.py:494`); `runner/capability/redactors/rizzo_pii.py` | Labelled PII corpus (M1's generator); residual identifiers, hold rate from reclassification, answer quality redacted vs frontier vs local |
| M10 | **Classifier error** | policy globs + regex only | `runner/policy/classifier.py:97` | The seam of finding 3; FN/FP on the M1 corpus — the number the whole leak claim rests on |

Test suite: `make test`; `make check` = lint + mypy + import contracts + tests (as CI);
`make test-live` (`ANNONA_LIVE_OLLAMA=1`, model from `ANNONA_LIVE_MODEL`, default `qwen2.5:3b`);
`make test-container`.

## Experiments the paper needs

Mapped to the reviewer expectations in [related work](related-work.md#positioning).

| | Question | Built from | Baselines |
|---|---|---|---|
| **E1 Placement correctness benchmark** | Does every step run on a permitted substrate, across multi-step runs with tools? | M1 + M2 on AgentDojo suites with class-labelled data and three substrates — **released as a benchmark** (none exists) | frontier-only; cost router (RouteLLM) — shows routing without policy leaks |
| **E2 Restricted egress per step** | How much restricted material reaches the frontier boundary? | M1 (undeclared canaries), M10 | frontier-only; redact-then-send; PAPILLON on PUPA for the brief |
| **E3 Hold vs failover** | What does fail-closed cost in completion and latency, and what does failover leak? | M3 | failover-enabled Annona |
| **E4 Injection** | Do placement and IFC compose? | M8 | FIDES (open source) and/or CaMeL on AgentDojo; no-policy; legacy gate |
| **E5 Cost/privacy frontier** | Quality and € as the share of local steps varies | M5 | all-local; all-frontier |
| **E6 Overhead** | Policy latency per step; ledger verification cost | M6, M4 | — |
| **E7 Classifier** | FN/FP of the class, path/regex vs a content model, under adaptive attack | M10 | regex only; Iovis (when released) |

## Order of work

1. **Fix before measuring:** finding 2 (egress check on permitted network tools) and the classifier
   seam (finding 3). A paper that measures a known hole reviews badly.
2. **Cheap numbers first:** M6 (overhead), M4 (fuzz), M2 (ledger replay) — days, no GPU.
3. **The corpus:** one generator for seeded documents with undeclared canaries and labelled PII,
   reused by M1, M9, M10.
4. **AgentDojo integration** (M8, E1, E4) — the evaluation every reviewer will look for.
5. **GPU runs on the DGX:** M1 at ≥ 1,000 steps, M3 with real kills, M5, M7.
6. Correct `design/hld.md` §1 (novelty sentence, see related work) and §9–§11 (docs vs code).
