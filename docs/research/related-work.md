# Related work

*Compiled 2026-09-27. Every entry was opened on its primary page (arXiv abstract, ACL Anthology,
USENIX, ACM DL, NDSS or the vendor's page); summaries paraphrase abstracts. Entries marked
**UNVERIFIED** were found by search and not opened. Re-check any number against the PDF before
citing it.*

This page exists so that the paper does not claim what others have already published. Its
conclusion, in one line: **none of Annona's mechanisms is new on its own; the unit of control
is.** See [Positioning](#positioning).

## The claim being compared

- **C1** each agent step — every inference and every tool call — is *placed* on a substrate
  (local GPU, company EU cluster, frontier API) by a policy over the data's class
  (public / internal / restricted);
- **C2** the runtime enforces it, default-deny, and **holds** a restricted step rather than failing
  it over to a less trusted substrate;
- **C3** every decision is written to a hash-chained ledger that `annona verify` checks afterwards;
- **C4** two-tier reasoning: a local model writes a brief, the brief is reclassified before egress;
- **C5** pseudonymisation with local re-identification (rizzo-pii);
- **C6** canary strings to measure the leak rate;
- **C7** content classification can only raise a class.

---

## 1. Information-flow control and capabilities for LLM agents

| Work | What it does | Overlap | Difference |
|---|---|---|---|
| **CaMeL** — *Defeating Prompt Injections by Design*. Debenedetti, Shumailov, Fan et al. (Google DeepMind, ETH). arXiv 2503.18813, 2025 | A privileged LLM turns the trusted query into a program; values carry capabilities; policies checked at every tool call. AgentDojo: 77% tasks with provable security vs 84% undefended | Deterministic runtime enforcement over labelled data at every tool call — the closest precedent for C2 | Sinks are tool recipients; *where inference runs* is never a policy variable; single provider; no hold, no ledger; threat model is injection + exfiltration, not sovereignty towards the provider |
| **FIDES** — *Securing AI Agents with Information-Flow Control*. Costa, Köpf, Kolluri et al. (Microsoft). arXiv 2505.23643, 2025; github.com/microsoft/fides | Dynamic taint tracking of confidentiality and integrity labels in the planner; primitives to hide values from the planner. AgentDojo | Labels only join upward (C7); the planner sees a reduced view (C4) | Labels decide what the planner sees and which tools receive data, not which substrate runs the step; single provider; no ledger |
| **f-secure** — *System-Level Defense against Indirect Prompt Injection: an IFC perspective*. Wu, Cecchetti, Xiao. arXiv 2409.19091, 2024 | Planner / executor / security monitor with a formal IFC model | Per-step monitor | Integrity only |
| **IsolateGPT**. Wu, Roesner, Kohno et al. NDSS 2025, arXiv 2403.04960 | Isolates LLM apps behind defined interfaces | Runtime mediation | Isolation between apps, no placement |
| **AirGapAgent**. Bagdasarian, Yi, Ghalebikesabi et al. CCS 2024, arXiv 2405.05175 | Contextual-integrity data minimisation before a third party | Minimisation (C4) | Adversary is a third party, not the model host |
| **Dual LLM pattern**. Willison, blog, Apr 2023 (UNVERIFIED: not opened) | Privileged LLM never sees untrusted text; quarantined LLM returns handles | Structural ancestor of C4 | Split by trust (integrity), both at the same provider |
| **Design Patterns for Securing LLM Agents**. Beurer-Kellner, Buesser, Creţu et al. arXiv 2506.08837, 2025 | Catalogue: plan-then-execute, dual LLM, context minimisation… | Reviewers will place C4 in this catalogue | Integrity, not sovereignty |
| **RTBAS**. Zhong, Chen, Wang et al. (CMU). arXiv 2502.08966, 2025 | IFC for tool agents; LM-judge and saliency screeners propagate labels; asks the user when unsure | Confidentiality labels + per-call enforcement; "ask" ≈ HOLD | No inference placement; heuristic propagation |
| **Progent**. Shi, He, Wang et al. arXiv 2504.11703, 2025 | DSL of least-privilege tool policies; SMT-checked updates; AgentDojo, ASB | Deterministic default-deny policy (C2) | Allow/deny, not *where* |
| **MELON**. Zhu et al. ICML 2025, arXiv 2502.05174 | Injection detection by masked re-execution | — | Integrity detector; a baseline |
| **Conseca** — *Contextual Agent Security*. Tsai, Bagdasarian. HotOS 2025, arXiv 2501.17070 | Just-in-time contextual policies, deterministic enforcement | Per-task policy | No placement or ledger |
| **GAAP** — *An AI Agent Execution Environment to Safeguard User Data*. Stanley, Verma, Tsai et al. arXiv 2604.19657, 2026 | IFC execution environment with persistent stores; disclosures blocked unless permitted | **Very close in spirit**: an execution environment, multi-step, with accounting | Controls disclosure to recipients, not the processing substrate. **Read in full before any novelty claim** |
| **Permissive IFC for LLMs**. Siddiqui, Gaonkar, Köpf (Microsoft). arXiv 2410.03055 | Propagates only the labels of inputs that influenced an output | **The formal counterpart of reclassifying the brief (C4)** | Argues for labels *below* the join; reviewers will ask why our reclassification is sound |
| **Twin Agent**. Hu, Jacob, Huang et al. arXiv 2607.19595, 2026 | Explore agent passes compact hints to a privileged Safe agent | Hint ≈ brief (C4) | Integrity direction |
| **Agentao**. Jin, Jiao, Tong. arXiv 2608.13574, 2026 | Permission engine mediates every tool call (allow/deny/route), keeps traces | Shape of C2/C3 | Results not yet reported. **Read in full** |
| **Organization-Scoped LLM Agent Runtime**. Fatouros, Makridis, Kousiouris et al. arXiv 2605.30604, 2026 | Runtime owns model access, tool mediation and an append-only audit; gateway to local/private endpoints | **Overlaps C1–C3 in shape** | Abstract claims neither class-based placement nor hash chaining. **Read in full** |

## 2. Privacy-aware routing and local–cloud collaboration

| Work | What it does | Overlap | Difference |
|---|---|---|---|
| **PAPILLON**. Li (Siyan), Raghuram, Khattab et al. NAACL 2025, arXiv 2410.17127 | *Privacy-conscious delegation*: a local model writes the prompt the API model sees and composes the answer; PUPA benchmark; 85.5% quality at 7.5% leakage | **C4 almost exactly — two-tier reasoning is not new** | Single-turn, no tools, no policy class, leakage measured not prevented, no hold, no ledger |
| PCD follow-ups: *Beyond Direct Identifiers* (Li, Yu, Hirschberg, arXiv 2608.09140); *Need to Know* (Huang, Cao, Yang, arXiv 2606.04067) | Better rewriters (k-anonymity estimate; RL under contextual integrity) | Quality of C4 | Not runtimes |
| **PRISM**. Zhan, Shen, Lin. AAAI 2026, arXiv 2511.22788 | Edge profiles entity sensitivity; a soft gate chooses local / cloud / collaborative | **Placement by sensitivity (C1), per prompt** | Learned soft gate, not default-deny; per prompt, not per step or tool call; two tiers; no hold, no ledger |
| **Privacy Guard**. Langiu. arXiv 2603.28972, 2026 | On-prem small model routes high-risk prompts to trusted tiers | C1 in spirit | Single prompt, no enforcement, no ledger |
| **PlanTwin**. Yu, Wang, Lang et al. arXiv 2603.18377, 2026 | Cloud plans over a de-identified local "digital twin" | Agent-level C4 | Fixed split, not policy placement |
| **Minions**. Narayan, Biderman, Eyuboglu et al. ICML 2025, arXiv 2502.15964 | Local model with the data collaborates with a frontier model; 5.7× cheaper at 97.9% quality | Shape of C4 | Motivated by cost; no enforcement |
| Secure Minions (Ollama blog) | Remote model in an H100 confidential VM | The *attested remote* alternative to "don't send" | Vendor blog |
| **CoGenesis**. Zhang, Wang, Hua et al. ACL 2024, arXiv 2403.03129 | Local model holds private context, cloud model generates without it | C4 | No agents |
| **RouteLLM** (Ong et al., arXiv 2406.18665); **Hybrid LLM** (Ding, Mallick, Wang et al., ICLR 2024, arXiv 2404.14618) | Route by cost/quality | Per-request placement | **Baseline**: routing without policy sends restricted data to the frontier |
| **PPRoute**. Wu, Zhang, Ji et al. arXiv 2604.15728, 2026 | MPC to hide the query from the router | — | Orthogonal |
| **RouteLabs router** (github.com/routelabsai/router, MIT) | Local-first router: prefers local for sensitive content, `allow_fallbacks: false`, scrub before cloud, decision logs | **Proof that "open-source privacy router" is not a first** | Early stage; per-agent-step routing is "future work" in its README; no hash chain |

## 3. Sanitisation, pseudonymisation, restoration

- **Hide and Seek (HaS)** — Chen, Li, Liu, Yu. arXiv 2309.03057, 2023. Anonymise locally, send,
  de-anonymise the answer locally. **This is C5.**
- **PrivacyRestore** — Zeng, Wang, Yang et al. ACL 2025, arXiv 2406.01394. Restoration server-side
  via steering vectors (needs a cooperating provider).
- **SanText** (Yue, Du, Wang et al., Findings ACL 2021), **TextObfuscator** (Zhou, Lu, Ma et al.,
  Findings ACL 2023), **DP-Prompt** (Utpala, Hooker, Chen, Findings EMNLP 2023, arXiv 2310.16111),
  **InferDPT** (Tong, Chen, Zhang et al., arXiv 2310.12214): DP or representation-level
  alternatives.
- **Redaction is not enough** — Staab, Vero, Balunović, Vechev, *Beyond Memorization*, ICLR 2024
  (arXiv 2310.07298): LLMs infer attributes from anonymised text. Staab et al., *LLMs are Advanced
  Anonymizers*, arXiv 2402.13846 (venue to check). Pang, Lu, Wang et al., *Reconstruction of DP Text
  Sanitization via LLMs*, RAID 2025 (arXiv 2410.12443). **Consequence for Annona:** redaction is a
  complement to placement, never a guarantee — which is what `reference/anonymisation.md` already
  says and what the HOLD design assumes.

## 4. Tamper-evident logs, verifiable and confidential inference

- **Foundations** (C3 is not new as a mechanism): Schneier & Kelsey, *Secure audit logs*, ACM
  TISSEC 1999; Crosby & Wallach, *Tamper-Evident Logging*, USENIX Security 2009; Certificate
  Transparency, RFC 6962 / 9162 (UNVERIFIED: not opened).
- **Hash-chained audit for agents, 2026 — crowded:** *Agent Flight Recorder* (Bindschaedler, Botha,
  Siebenbrunner et al., arXiv 2609.01931: hash chain, Merkle batching, on-chain anchoring, ~48 µs
  per event); *Auditable Agents* (Nian, Yuan, Zhang et al., arXiv 2604.05485). UNVERIFIED:
  *Verifiability-First Agents* (2512.17259), *A Black Box for Agentic Processes* (2609.04017),
  *AUDITA* (2608.22160).
- **What may be new is the content of the record**: the *placement decision* — class, rule,
  candidates, substrate, hold — so that a third party can check compliance with a sovereignty
  policy, not only what happened.
- **The ledger records a claim, not a proof, of where a step ran.** Verifiable inference: zkLLM
  (Sun, Li, Zhang, CCS 2024, arXiv 2404.16109); TOPLOC (Ong, Di Ferrante, Pazdera et al., arXiv
  2501.16007). Confidential computing: Apple Private Cloud Compute (2024); *Confidential Computing on
  NVIDIA Hopper GPUs* (Zhu, Yin, Deng et al., arXiv 2409.03992: <7% overhead); AgenTEE (Abdollahi,
  Maheri, Forough et al., arXiv 2604.18231). This is the competing answer — make the remote
  substrate trustworthy instead of refusing it — and `reference/frontier-substrates.md` should be
  cited against it.

## 5. Canary-based leak measurement

- **The Secret Sharer** — Carlini, Liu, Erlingsson, Kos, Song. arXiv 1802.08232 (USENIX Security
  2019 — venue to confirm). The canonical canary reference.
- **AgentLeak** — El Yagoubi, Badu-Marfo, Al Mallah. arXiv 2602.11510, 2026. 1,000 multi-agent
  scenarios; internal channels leak 68.8% vs 27.2% at the final output — **output-only audits miss
  41.7% of violations.** Measure at the egress boundary of every step: exactly where Annona's check
  sits.
- **AgentSecBench** — Alpay, Alpay. arXiv 2605.26269, 2026. Canary in blocked documents.
- *Caught in the Act(ivation)*, arXiv 2606.04141 (UNVERIFIED).

## 6. Benchmarks

| Benchmark | Fit |
|---|---|
| **AgentDojo** — Debenedetti et al., NeurIPS 2024 D&B, arXiv 2406.13352; 97 tasks, 629 injections | **Mandatory**: every IFC paper above reports on it. Label its tool outputs by class; report utility, attack success and *restricted egress to the frontier*. Alizadeh et al. (arXiv 2506.01055) added PII-exfiltration tasks |
| **InjecAgent** — Zhan, Liang, Ying, Kang, Findings ACL 2024, arXiv 2403.02691 | Data-stealing subset |
| **PUPA / PUPA-SD** (PAPILLON) | **Mandatory for any claim on C4** — head-to-head with PAPILLON |
| **AgentLeak** | Per-step egress leakage |
| ASB (ICLR 2025, arXiv 2410.02644) | Secondary |
| PrivacyLens (arXiv 2409.00138), AgentDAM (arXiv 2503.09780) | Leakage to recipients; authors UNVERIFIED |
| AgentHarm, WASP | **Do not fit** (misuse; web injection) — say why |

**No public benchmark tests placement correctness** — whether a restricted step ran on a permitted
substrate across a multi-step run with tools. Building and releasing it (AgentDojo suites with
class-labelled data and three substrates) is a contribution in its own right.

Adaptive attacks: Zhan, Fang, Panchal et al., *Adaptive Attacks Break Defenses Against Indirect
Prompt Injection*, Findings NAACL 2025 (arXiv 2503.00061) — any claim that rests on a classifier must
face them. Origin of indirect injection: Greshake, Abdelnabi, Mishra et al., arXiv 2302.12173.

---

## Positioning

### Not new — do not claim

1. IFC / capability enforcement over agent tool calls, default-deny: CaMeL, FIDES, f-secure, RTBAS,
   Progent, Conseca, GAAP.
2. Two-tier reasoning (C4): PAPILLON, CoGenesis, Minions, PlanTwin, PRISM, Twin Agent; reasoning about
   the label of an LLM output: Siddiqui & Köpf.
3. Pseudonymise, send, restore (C5): HaS, InferDPT, PrivacyRestore — and redaction is shown
   insufficient (Staab; Pang).
4. Hash-chained agent logs (C3): Schneier–Kelsey, Crosby–Wallach; Agent Flight Recorder, Auditable
   Agents.
5. Canary leak measurement (C6): Secret Sharer, AgentLeak, AgentSecBench.
6. Sensitivity-aware local/cloud routing per prompt (C1): PRISM, Privacy Guard, the RouteLabs router.
7. Raise-only labels (C7): the IFC lattice join.

So "the first open-source project that routes by sensitivity", "the first hash-chained ledger for
agents" and "the first local-brief / frontier pattern" are false or indefensible. **The sentence in
`design/hld.md` §1 ("the first open-source project in which the placement of every inference and
every tool call is a policy decision…") must be narrowed to the wording below.**

### Defensible

> Annona makes the **execution substrate** of every agent step — each inference and each tool call —
> the enforced object of an information-flow policy. Agent IFC systems (CaMeL, FIDES, RTBAS, Progent,
> GAAP) constrain *which data reaches which tool or recipient* under a single model provider;
> privacy-aware routers (PAPILLON, PRISM, Privacy Guard) choose *per prompt* between local and cloud
> with learned gates and no enforcement. Annona (i) places each step on one of several trust tiers by
> a default-deny policy over a monotone class; (ii) gives **fail-closed hold semantics** — a step whose
> class forbids every available substrate is suspended, never degraded; (iii) commits every
> **placement decision** — class, rule, substrate — to a hash-chained ledger, so compliance with a
> data-sovereignty policy can be checked by a third party after the run. To our knowledge no
> published system combines per-step, multi-tier, enforced placement with post-hoc verifiable
> placement evidence in an open-source agent runtime.

Before submitting: read GAAP (2604.19657), the organisation-scoped runtime (2605.30604) and Agentao
(2608.13574) in full.

### What a reviewer will expect

**Baselines:** frontier-only; local-only; a cost router (RouteLLM / Hybrid LLM); PAPILLON on PUPA;
redact-then-send without placement; an IFC defence (FIDES is open source) on AgentDojo — to show
complementarity, ideally composition; failover vs hold.

**Metrics:** utility; attack success; **restricted egress per step** at the frontier boundary;
leakage under an inference adversary (Staab); placement overhead; ledger verification cost; hold
rate.

**Weak points to answer in the paper:**

- **The classifier is the trusted base.** Raise-only makes it monotone, not correct: report its
  false-negative rate, including under adaptive attack. Today the class comes from path globs and
  regexes only ([paper plan](paper-plan.md), M10).
- **Soundness of reclassifying the brief** against Siddiqui & Köpf — or state that a brief derived
  from restricted data stays restricted unless explicitly declassified.
- **The ledger is a claim, not a proof, of where a step ran** — attestation (PCC, H100 CC, AgenTEE)
  or TOPLOC-style evidence as extension.
- **Injection interacts with placement**: an injected instruction could launder restricted data
  through a local brief, or out through a permitted network tool (see M8 in the
  [paper plan](paper-plan.md)).
