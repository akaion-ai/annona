# Anonymisation, and what it does not do

Annona can replace the identifiers in a payload before it crosses, using
[rizzo-pii](https://github.com/Rizzo-AI-Academy/rizzo-pii) — a 0.3B Italian PII
model by Simone Rizzo that runs on a CPU, recognises 22 categories including
*codice fiscale*, *partita IVA* and cadastral data, and returns the reverse
mapping so the answer can be re-identified **on this machine and nowhere else**.

That is a real instrument and this page is mostly about its limits, because the
limits are where a perimeter is actually lost.

## The failure this page exists for

A studio configures `on_unavailable: redact` so the good model can be used when
the local GPU is down. An M&A memorandum is *restricted* — it carries a codice
fiscale. The GPU dies. The redactor does its job perfectly, and this is what
crosses to a US API:

```
Progetto Falcon — memorandum riservato.
Il nostro cliente [ORG_1] ha incaricato lo studio di assistere l'acquisizione
del 70% di [ORG_2] per [AMOUNT_1]. Signing entro il [DATE_1].
```

Nothing personal remains. The entire secret is intact. The provider now knows
that this firm is advising on an acquisition of that size on that timetable —
and it knows *which firm*, because the firm holds the account. Two facts and a
newspaper name the target.

Worse, the class went **down**: the memorandum was restricted because of the tax
code, and the tax code is what was removed. Redaction had laundered it to
`public`.

The lesson is not that the detector was bad. The detector was flawless. It is
that **a class is a statement about identifiers, and sensitivity is not** — so a
mechanism that removes identifiers must never be able to grant permission by
itself.

## The three controls

### 1 · Redaction is opt-in, per class

```yaml
egress:
  redact:
    allowed_for: [internal]            # or [internal, restricted]
```

Empty by default: until a policy names the classes, redaction is not an
available action. It is deliberately a *different* list from `egress.brief`,
because the two let out very different amounts. A brief is a paragraph a local
model chose to write. A redaction is the document **entire**, minus the
identifiers — every fact, every number, every sentence about what the matter is.

`restricted` may appear in this list, unlike in `brief.allowed_for`, and the
asymmetry is on purpose: most restricted material is restricted *because of* its
identifiers, and the letter carrying a codice fiscale is precisely the case
redaction exists for. Refusing the class outright would delete the feature
rather than secure it. Choose your reading in one line and the file records it.

### 2 · Sealed matter

```yaml
egress:
  sealed:
    paths:    ["~/Pratiche/M&A/**", "~/clienti/*/due-diligence/**"]
    patterns: ['Progetto\s+\w+', 'memorandum riservato', 'term sheet']
```

Sealing is not a class. Classes say *where* material may run and are lowered
when identifiers are removed. A seal is a property of the **matter**, and it
survives every transformation: sealed material is never briefed, never redacted,
never sent — the step is held and the ledger says which rule sealed it.

It is monotone, like the class: a run that has once touched sealed matter stays
sealed, because the second turn ("and the timetable?") is about the same deal.

This is the control that does not depend on a detector being good. It does not
look for identifiers at all; it recognises the matter, because a person told it
what the matter is.

### 3 · You can read what crossed

Every payload that leaves the machine is kept in memory for the run and shown in
the window, verbatim, with every substitution marked. Not a count — the text.
A count cannot tell you that the codename of your deal survived; the text can,
and it is how an operator learns which patterns to seal.

It is never written to the ledger. The ledger holds digests and counts on
purpose: a tamper-evident record of what was sent would be a second copy of the
material, with a longer life than the run.

## What redaction still does not fix

Even a perfectly redacted payload tells the provider:

- **that you asked** — the account is yours, and the question is timestamped;
- **what kind of work it is** — the vocabulary of a due diligence is not the
  vocabulary of a lease renewal;
- **the shape of the facts** — 70%, 480 million, September signing.

For material where those three are the secret, the answer is not a better
detector. It is the seal, or the local model, or the brief — a short abstraction
someone reviewed — and nothing else.

## Running the redactor

```bash
git clone https://github.com/Rizzo-AI-Academy/rizzo-pii && cd rizzo-pii
pip install -r requirements.txt
python src/app/app.py            # http://127.0.0.1:5005
```

```yaml
redaction:
  provider: rizzo-pii
  endpoint: http://127.0.0.1:5005
  on_error: hold                 # a detector outage stops the step
  floor: public                  # lowest class redacted material may reach
  labels:                        # the detector's 22 categories, mapped to yours
    CF: restricted
    PIVA: restricted
    IBAN: restricted
    FULLNAME: internal
    ORG: internal
    URL: internal
```

Three behaviours worth knowing:

- **`on_error: hold` is the default.** A redactor that cannot be reached stops
  the step. The alternative — proceeding on regex classification alone — is a
  decision somebody makes on purpose.
- **The output is reclassified from scratch.** A redaction that left an
  identifier behind produces text that merely *looks* safe, so the perimeter
  treats it as freshly arrived material and holds it if anything remains.
- **Readiness, not liveness.** The adapter probes `/health` and reads
  `model_loaded`: a server whose 0.3B model is still loading is not a redactor
  yet.

## Escalating on purpose

The Ask window has a **✦ migliore** toggle, and it means one specific thing:
*use the best substrate this policy already permits for this material.* It
reorders the candidates a rule allows; it can never add one, and material that
may not leave the machine still does not leave. Asking for a better model is a
request about ranking, and a request about ranking cannot become a request about
jurisdiction.

Escalation also happens without being asked, in exactly one situation: the
substrates a rule allows are unavailable, and the rule says what to do about it
(`hold`, `queue`, `brief`, `redact`). There is deliberately no "this task looks
hard, send it abroad" heuristic — that would be a placement decision taken by a
guess rather than by the policy.
