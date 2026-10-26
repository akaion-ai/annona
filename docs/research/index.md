# Research

Annona is the reference implementation for work at **Akaion AI Lab** on one
claim: *sovereignty should be measurable*.

"Your data stays inside" is an architectural argument. Architectural arguments
are unfalsifiable, which is why every vendor makes one. We are trying to replace
it with three things a customer can check: **a measured leak rate**, **a
cost/privacy frontier**, and **a command they run themselves**.

This document states what we are trying to prove, how we intend to measure it,
and what is not built yet. Negative results are published here too.

Before any paper: [related work](related-work.md) — what others have already published and the
claim that survives it — and the [paper plan](paper-plan.md) — which numbers the code can produce
today, which it cannot, and in what order to build them.

**Where the programme stands.** Four of the six sections below have shipped: the
policy kernel, privacy-constrained routing, the leak canary and the verifiable
trace. What shipped is the *mechanism*; most of the *measurements* they exist to
produce are still to be run, and each section now says which is which. The
distinction matters more here than anywhere else in the documentation — a
mechanism that works on a laptop and a leak rate measured over fifty thousand
requests are different claims, and only the second one is research.

| § | Mechanism | Measurement |
|---|---|---|
| Enforcement | shipped — default-deny, `runner/policy` | injection corpus not run |
| PCR | shipped — `runner/placement`, 15-case conformance matrix | cost/privacy frontier not measured |
| Leak canary | shipped — canaries in policy, wiretap in tests | 0 over the acceptance corpus; the 1 000-step run is not done |
| Trace-as-proof | shipped — hash-chained ledger, `annona verify` | external anchoring open |
| Local agentic loop | tier 1 shipped — real tool calls from qwen2.5 3B and 14B | grammar-constrained decoding not built; no per-model table |
| Redacted speculative execution | shipped in its simple form — redact, cross, re-identify | span acceptance and quality delta not measured |

---

## § Enforcement — from advisory filter to policy kernel

**Status:** shipped. `runner/policy`, default-deny, and the legacy path kept.

The advisory filter is still there for installations without a policy —
`runner/permissions/manager.py`, which permits any tool it does not recognise
and reads an empty allow-list as *allow everything*. Writing a policy replaces
it with `DefaultDenyGate`: a tool that is not named does not run, a named tool
asked to touch a path outside its allow-list does not run, deny beats allow, and
symlinks are resolved so a link out of an allowed directory is refused by its
target. Every decision, including every refusal, is a ledger entry.

**What is still research.** The *granularity* question is unanswered. Path
globs and a tool allow-list are expressive enough for a practice and too coarse
for an enterprise; we do not yet know where the line is that keeps a policy
auditable in an afternoon. And the metric below has not been run.

**Why it is research and not a patch.** Default-deny is trivial to write and hard
to ship: it breaks every existing configuration and turns silent over-permission
into visible friction. The open question is the *granularity* at which policy is
both expressive enough to be useful and simple enough that a practice's IT
consultant can audit it in an afternoon. We intend to publish the policy DSL and
the failure modes we hit, including the ones that forced us to loosen it.

**Metric.** Attack success rate on a prompt-injection corpus: documents in the
vault that instruct the agent to exfiltrate or destroy, measured as *fraction
that produce a policy-violating tool call*. Baselines: no policy, current
advisory policy, default-deny policy.

---

## § PCR — Privacy-Constrained Routing

**Status:** shipped as a mechanism; the frontier it exists to trace is unmeasured.

`runner/placement` decides, per step, from the class of the working set and the
health of each substrate: 15-case conformance matrix, failover that recomputes
inside the same rule, and a hold rather than a downgrade when nothing permitted
is available. What is *not* done is the science — nobody has measured what that
policy costs in money and latency against the two baselines, which is the whole
point of calling it a frontier.

Formally: an agent plan is a DAG of steps. Each step's inputs carry sensitivity
labels estimated by a local classifier. Choose an execution plan — which model,
executed where — that minimises cost and latency **subject to a hard
constraint**: no token of class ≥ S crosses the perimeter.

It is constrained scheduling where the constraint is itself estimated by a model.
That is what makes it a research problem rather than a configuration screen: **the
constraint can be violated by classifier error, and nobody measures that error.**

Interesting cases, in increasing difficulty:

1. **Uniformly generic step** — route to the best/cheapest remote model. Easy.
2. **Uniformly sensitive step** — must stay local. Costs quality; how much is an
   empirical question (see § Local agentic loop).
3. **Mixed step** — a document that is 5% privileged and 95% boilerplate. All-local
   pays quality for nothing; all-remote violates. This is where the value is, and
   where § Redacted speculative execution applies.

**Metrics.**

- Pareto frontier of **€ / latency vs residual leak rate**, against two baselines:
  all-local (safe, slower, lower quality) and all-remote (violating).
- Fraction of tokens retained locally at equal task quality.
- Quality delta on the vertical benchmark, per data class.

**Precondition, met.** PCR needs exactly one place where content can leave the
perimeter. There were three, one per provider loop; there is now one
(`RoutingBackend`), and five import contracts keep the decision layer from
reaching an adapter, so a second one cannot appear without failing the build.

---

## § Leak canary — measuring what actually escapes

**Status:** mechanism shipped, corpus not built.

Canaries are declared in the policy (`egress.canaries`) and treated as
restricted material by definition: finding one in an outbound payload is not a
signal to weigh, it is a stop. The acceptance run (`make verify`) plants one in
a client file, lets a real local model read it, and asserts zero at an
instrumented frontier substrate — nine checks, twenty-five seconds, and the
number is currently **0 over that corpus**.

That is a demonstration, not a measurement. The measurement needs the corpus
below, at a scale where a leak rate has a denominator worth quoting.

Seed the corpus with synthetic but realistic identifiers that exist nowhere else
— IBANs, tax codes, names, case numbers — with known ground truth. Run real
workloads. Count how many canaries appear in outbound traffic.

The output is a **measured leak rate**, not a promise. To our knowledge nobody
publishes measured leak rates for hybrid local/remote agent stacks; the claim is
always architectural.

Two things this gives us that an architecture diagram cannot:

- A regression test. A leak rate is a number that can go up in a pull request.
- A sales artifact. Zero canaries out of fifty thousand requests, on a dashboard,
  in a room full of lawyers.

**Design notes.** Canaries must be realistic enough to be classified the same way
real data is (a canary the classifier finds *easier* than real data inflates the
result), and unique enough to be detected with certainty in egress. Both
properties are in tension. We will publish the generator and the tension.

---

## § Trace-as-proof — an audit trail you can verify

**Status:** shipped, with one claim deliberately not made.

`runner/audit/ledger.py` is an append-only, hash-chained JSONL record;
`annona verify` checks it offline and distinguishes a rewritten entry from a
deleted one, a reordered one and a corrupt line. Payloads are never stored, only
their digests — a record of sensitive material is still sensitive material.
`annona why <step>` reconstructs a single decision months later, and
`annona audit --held` lists every refusal with its reason.

**What is not claimed, and is tested as not claimed:** a chain rebuilt from
scratch by someone with write access is internally consistent and undetectable
here. That needs an external anchor for the head hash, and it is the open piece.

This turns "don't trust us, verify" from a slogan into a shell command. It is
also the artifact that aligns with AI Act logging obligations — and a published
spec, unlike a feature, sets a standard rather than competing inside one.

**Secondary use.** A complete trace makes deterministic replay possible, which
makes agent regression testing possible. See Lab Note 12.

---

## § Local agentic loop — can the appliance actually act?

**Status:** tier 1 shipped and exercised against real models; tier 2 open.

A local model now completes real agentic runs: `OllamaBackend` and
`OpenAICompatibleBackend` (vLLM, llama.cpp, SGLang) both do native tool calling,
and the live suite drives qwen2.5 3B and 14B through a full read-then-answer
task on a laptop. Fully local mode can act.

The second problem — a transcript carrying every file the agent read to whichever
substrate serves the next turn — is what the working set and per-step placement
exist to solve, and it is solved: contamination is monotone, and the class of a
run only goes up.

**What is still open** is the reliability tier. Native tool calling produces
confidently malformed arguments on small models; the adapter turns that into a
tool error the model can retry from, which is the loop working as designed and
not a fix. Grammar-constrained decoding is not built.

**Open questions.**

- How small can a local model be and still complete each real task class? The
  answer is a routing table, and it is the input to both PCR and appliance
  sizing (Lab Note 08).
- Does constrained decoding (grammar-forced, schema-valid tool calls) close the
  reliability gap between an 8B local model and a frontier model on tool use? Our
  hypothesis is that most local-model tool-use failure is malformed arguments
  rather than wrong intent — if so, grammars convert a quality problem into a
  solved problem.
- Does plan-then-execute beat free-form ReAct on small models? Fewer degrees of
  freedom should mean less drift over a ten-step horizon.

---

## § Redacted speculative execution

**Status:** the simple form shipped; the speculative form is still exploratory.

What exists: `on_unavailable: redact`. A local redactor — the first is an
adapter for [rizzo-pii](https://github.com/Rizzo-AI-Academy/rizzo-pii), 0.3B,
CPU-only, 22 Italian categories — replaces the identifiers, the result is
**reclassified from scratch**, and only then may it cross; the answer is
re-identified locally from a mapping that never leaves. A redaction that still
carries an identifier is held, and a redactor outage holds the step by default.

What is still research is the interesting half: not replacing spans, but having
the remote model expand only the non-sensitive ones.

For the mixed-sensitivity step: the local model drafts and redacts, the remote
model verifies and expands **only the non-sensitive spans**, the local model
stitches. It is speculative decoding across a trust boundary — normally draft and
verifier share a machine, which is why nobody has studied it with an egress
constraint in the middle.

**Metrics.** Span acceptance rate; quality vs full-remote; tokens emitted vs
full-remote; added latency.

---

## Lab Notes

Small empirical results, published on a cadence. One question, one measurement,
one artifact, one to two weeks. Every note is an optimisation of something we
already run in production — that is what makes them defensible and hard to
reproduce from the outside.

| # | Question | Metric | Artifact |
|---|---|---|---|
| 01 | Do telegraphic ("caveman") prompts hold up for **tool calling**, or do they degrade argument fidelity and schema adherence? | tokens saved vs schema-violation rate, per model | `caveman-tools` + per-model table |
| 02 | How far can **MCP tool descriptions** be compressed before tool selection collapses? | selection accuracy vs schema tokens, at growing N tools | tool-schema minifier |
| 03 | Past how many RAG chunks does quality **get worse**? | task success vs k, per model and context length | auto-k policy |
| 04 | Ablate the system prompts of the vertical agents: which sections are dead weight? | success with/without each section | prompt linter |
| 05 | What is **prefix-cache-aware prompt ordering** (stable → volatile) actually worth? | € and ms saved on real traffic | guidelines + middleware |
| 06 | **Speculative tool prefetch**: start executing the likely call while the model is still emitting | p50/p95 latency on booking flows | executor patch |
| 07 | **Barge-in thresholds** on 8 kHz Italian telephone audio | false-interrupt rate vs response latency | curve + config |
| 08 | For each real task class, what is the **smallest local model that passes**? | success threshold per family and quantisation | routing table → feeds PCR and appliance sizing |
| 09 | **8B × 3 attempts + verifier** or frontier × 1? | cost and quality on structured extraction | if it holds, it is what makes appliance economics work |
| 10 | **Embedding quantisation** (int8 / binary) on a real corpus | recall@k lost vs memory and latency gained | larger corpora on the same appliance |
| 11 | When does **summarising** conversation history beat truncating it? | multi-turn task success vs context strategy | compaction policy |
| 12 | How often do identical agent runs **diverge**, and why — sampling or tool nondeterminism? | divergence rate by cause | replay harness |

---

## Programme order

Dependency order, not preference order:

1. ~~**Loop unification**~~ — done. One place where content can leave, and five
   contracts that stop a second one appearing.
2. ~~**Enforcement**, **PCR**, **trace-as-proof**~~ — mechanisms done.
3. **The corpus.** Fifty thousand requests with seeded canaries, on the vertical
   workloads, producing a leak rate with a denominator. Everything above is a
   demonstration until this exists.
4. **Lab Note 08** — the smallest local model that passes each task class. It is
   the routing table PCR decides against and the input to appliance sizing, and
   it needs the DGX to be honest about token/s.
5. **Grammars** — tier 2 tool calling. The hypothesis to falsify: most local-model
   tool-use failure is malformed arguments rather than wrong intent.
6. **Ledger anchoring**, then **speculative redaction**.
