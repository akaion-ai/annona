# Shared context, checkpoints, and who is allowed to read them

Design note. Status: **proposal**. Nothing here is implemented yet; it is
written down so the first pull request is deliberate.

## The problem, stated properly

On a shared appliance — a DGX Spark in a firm, one box, twelve colleagues — the
scarce resource is not GPU time. It is **context**. Every run re-reads the same
40-page contract, re-derives the same summary, and pays for it in tokens and in
minutes. Twelve people analysing the same due-diligence folder do the same
expensive work twelve times, and the model starts each one knowing nothing.

The obvious fix is to cache: keep what was derived, and hand it to the next
person. It is the right instinct and it is worth a great deal. It is also, done
naively, the fastest way to turn a perimeter into a leak — so the design has to
start from what a cache actually *is* here.

**A shared cache is an egress channel between colleagues.** The perimeter's
whole job is deciding what may cross a boundary. Today it recognises one
boundary: this machine versus everywhere else. A shared checkpoint store adds a
second boundary — person A versus person B — and puts a copy of the *most
distilled, most portable* form of the material on the wrong side of it. If a
partner analyses a sealed M&A memorandum and the summary lands in a store the
whole firm reads, the kernel refused to send that document to a frontier API
and then published its conclusions to the desk next door. That is worse than
the leak it prevented, because it looks like a feature.

So: yes, build it. In three steps, and not in the other order.

## Step 1 — the local cache, which has no sharing problem at all

Most of the win needs no sharing and no identity.

**Extraction is deterministic and repeated.** The same PDF, read twice, produces
the same text. A content-addressed cache keyed on the file's SHA-256 plus the
reader's options makes the second read free — for the same person, on the same
machine, with no new trust boundary. On a box where twelve people open the same
data room, the *same* file has one digest regardless of who opens it, so this
alone removes most duplicated work.

Cache what the reader produced, keyed by digest, stored beside the file (an
`.annona-cache/` sibling, like `.annona-derived/` today). Invalidation is free:
a different digest is a different key. This is a day of work and it is the
highest-value item on this page.

**Run digests are cheap and already have a home.** The vault (`~/akaion-brain/`)
is markdown on disk, which is exactly the right shape for a checkpoint: a note
per analysed matter, carrying what was concluded and which files it came from.
`runner.brain` already writes notes and already syncs. A run that ends with
"write what you learned to the vault" is a checkpoint, in the operator's own
storage, reviewable in any editor.

Neither of these crosses a person-boundary. Ship them first.

## Step 2 — checkpoints as a reviewed artefact

The thing worth sharing is not the transcript. It is a **checkpoint**: a
distilled statement of what was established, written to be read by someone who
was not there.

Three properties make it shareable where a raw cache entry is not:

1. **It is derived, not extracted.** A summary of a memorandum is a smaller
   surface than the memorandum, in the same way a brief is — and the perimeter
   already has machinery for exactly this: produce it locally, then
   *reclassify the result from scratch* rather than trusting that summarising
   made it safe.
2. **It carries its provenance.** Which files it came from, at which digests,
   under which class. A checkpoint whose sources were restricted is restricted;
   one whose sources were sealed is sealed. Provenance is not metadata here, it
   is the access-control input.
3. **A person approved it.** This is the part that cannot be automated, and the
   reason to build checkpoints rather than a transparent cache. The residual-
   inference problem — a summary that names no one but still tells you a deal
   is happening — is not solvable by a classifier. It is solvable by the person
   who wrote it looking at it once and saying yes. The window already shows
   exactly what crossed to a frontier model; the same surface shows what is
   about to cross to a colleague.

A checkpoint is therefore: **local by default, publishable by an act.**

## Step 3 — sharing, which requires identity, which Annona does not have

Here is the honest state of the product today:

> **There is no RBAC. There is no identity at all.** The perimeter is a
> *machine* perimeter: one policy, one ledger, one operator. The ledger records
> what was decided and why, and does not record *who* asked — because on a
> laptop there is only one answer. `annona pair` authorises a remote origin with
> a token, which is authorisation without identity: it says "this app may use
> this machine", not "this person may see this matter".

On a laptop that is a coherent design. On a shared DGX it stops being one — the
moment twelve people share a box, "may this run read `/mnt/pratiche/BG-114`" has
twelve different answers, and the policy language cannot express any of them.

What v1 needs, in dependency order:

| # | Piece | Why it has to come first |
|---|---|---|
| 1 | **A subject on every request.** OS user, mTLS client cert, or an OIDC token from the firm's IdP. | Everything else is a function of "who". Without it, rules can only be written about material. |
| 2 | **The subject in the ledger.** One field. | An audit trail that cannot answer "who read the client file" is not an audit trail a professional firm can use. |
| 3 | **Subject-scoped rules.** `match: {class: restricted, subject: partners}` — the same first-match-wins evaluation, one more dimension. | This is RBAC, and it is a small change to a policy model that was designed for it. |
| 4 | **Tool allow-lists per subject.** `document_reader: ["~/matters/${subject}/**"]`. | Path allow-lists are already the enforcement point; they just need to be parameterised. |
| 5 | **Checkpoint ACLs derived from provenance.** A checkpoint is readable by whoever could have read its sources. | Derived rather than declared: an ACL somebody sets by hand is an ACL that drifts from the material. |

Note the shape: **RBAC is not a new subsystem, it is one more dimension on the
existing one.** Classes say *what*; subjects say *who*; the rule table already
does first-match-wins over a match expression. That is why this is a
contribution-sized piece of work rather than a rewrite — and why the ordering
matters: (1) and (2) are worth landing even if nobody ever writes a
subject-scoped rule, because a shared machine with an anonymous ledger is the
problem underneath all the others.

## What this means for the cache question

Concretely, if the goal is "my colleagues' work should save me context":

- **Now:** local extraction cache + vault checkpoints. Real savings, no new
  boundary, no identity needed.
- **Next:** the checkpoint artefact — provenance, reclassification, and an
  approval step — so there is something whose sharing is a decision rather than
  a side effect.
- **Before any team-wide store:** subject identity and a ledger that records it.
  A shared cache without identity is not a cache with a missing feature; it is
  a distribution channel with no policy in front of it.

The tempting shortcut is to ship the shared store first and add permissions
later. In this product that is backwards: the store *is* the permission
question, and the answer has to exist before the store does.
