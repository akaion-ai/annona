import { useState } from "react"
import { Egress } from "../../api/kernel"

/**
 * What left this machine, verbatim.
 *
 * Every other surface in this app answers "where did it run". This one answers
 * the question that decides whether the rest is worth anything: **what did you
 * actually send?** — and it answers with the bytes. A count of replaced
 * identifiers is a reassurance; the text is evidence, and only the text can
 * show an operator that the codename of their deal survived a redaction the
 * detector performed flawlessly.
 *
 * So the panel is built to be read, not to be dismissed:
 *
 * - the destination and its jurisdiction, because "redacted" means nothing
 *   without "to whom";
 * - what was replaced, by kind and count, from the redactor's own vocabulary;
 * - the payload itself, with every placeholder marked, one click away;
 * - and a sentence about what redaction does *not* do, next to the evidence
 *   that makes it concrete rather than a disclaimer nobody reads.
 */

const KIND_LABEL: Record<string, string> = {
  redacted: "Anonymised before it left",
  briefed: "Summarised locally before it left",
  verbatim: "Left as it was",
}

const PLACEHOLDER = /\[[A-Z_]+_\d+\]/g

/** The crossed text with every placeholder marked, so the substitutions are countable by eye. */
function Marked({ text }: { text: string }) {
  const parts: (string | { token: string })[] = []
  let last = 0
  for (const match of text.matchAll(PLACEHOLDER)) {
    const at = match.index ?? 0
    if (at > last) parts.push(text.slice(last, at))
    parts.push({ token: match[0] })
    last = at + match[0].length
  }
  if (last < text.length) parts.push(text.slice(last))

  return (
    <pre className="an-egress__text">
      {parts.map((part, index) =>
        typeof part === "string"
          ? part
          : <mark key={index} className="an-egress__ph">{part.token}</mark>,
      )}
    </pre>
  )
}

export function EgressPanel({ crossings, sealed }: { crossings: Egress[]; sealed?: string }) {
  const [open, setOpen] = useState<number | null>(null)

  if (sealed) {
    return (
      <div className="an-egress an-egress--sealed">
        <div className="an-egress__head">
          <span className="an-egress__kind">Sealed material</span>
          <span className="an-egress__why">{sealed}</span>
        </div>
        <div className="an-egress__note">
          No transformation gets it out: not a summary, not anonymisation, because both
          leave <em>what it is about</em> intact.
        </div>
      </div>
    )
  }

  if (!crossings.length) return null

  return (
    <>
      {crossings.map((crossing, index) => {
        const labels = Object.entries(crossing.labels ?? {})
        const isOpen = open === index
        return (
          <div key={index} className={`an-egress an-egress--${crossing.kind}`}>
            <div className="an-egress__head">
              <span className="an-egress__kind">{KIND_LABEL[crossing.kind] ?? crossing.kind}</span>
              <span className="an-egress__where">
                → {crossing.substrate}
                {crossing.jurisdiction && <span className="an-egress__juris"> · {crossing.jurisdiction}</span>}
              </span>
              <button className="an-link an-egress__toggle" onClick={() => setOpen(isOpen ? null : index)}>
                {isOpen ? "hide" : "show"} what left
              </button>
            </div>

            {crossing.kind === "redacted" && (
              <div className="an-egress__labels">
                <strong>{crossing.replaced ?? 0}</strong> identifiers replaced
                {crossing.redactor && <span className="an-egress__by"> by {crossing.redactor}</span>}
                {labels.length > 0 && (
                  <span className="an-egress__tags">
                    {labels.map(([label, count]) => (
                      <span key={label} className="an-egress__tag">{label} ×{count}</span>
                    ))}
                  </span>
                )}
              </div>
            )}

            {crossing.kind === "briefed" && (
              <div className="an-egress__labels">
                Written by <strong>{crossing.written_by}</strong>; reclassified{" "}
                <strong>{crossing.class}</strong> before it left.
              </div>
            )}

            {isOpen && (
              <>
                <Marked text={crossing.text} />
                {crossing.kind === "redacted" && (
                  <div className="an-egress__note">
                    Read it. Anonymisation removes <em>who</em>, not <em>what it is about</em>:
                    if a matter's code name survives here, or enough context to recognise it,
                    whoever answers still knows what you are working on — and knows it is you
                    asking. That material needs the seal, not the redactor.
                  </div>
                )}
              </>
            )}
          </div>
        )
      })}
    </>
  )
}
