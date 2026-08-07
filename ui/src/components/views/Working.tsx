/**
 * What the kernel is doing, while it does it.
 *
 * The placeholder was the words "Placing and running…", static, for however long
 * the model took — thirty seconds of nothing on a 14B, and no way to tell a slow
 * answer from a hung one. For a product whose claim is *you can watch it decide*,
 * that was the wrong screen.
 *
 * The source is the **ledger**, polled while the request is in flight, and that
 * choice is the point rather than a shortcut. Every step this shows — the tool
 * that was cleared, the read that raised the class, the substrate the turn was
 * placed on — is an entry the daemon already wrote and hash-chained. So the
 * progress display cannot drift from the audit trail: it is not a second,
 * friendlier account of the run, it is the same account, read early. Anything it
 * shows, `annona audit` will show later; anything it cannot show did not happen.
 *
 * It also means no new endpoint, no streaming protocol, and no callback threaded
 * down through the agent loop — three things that would each have to stay in
 * step with the record and would each eventually not.
 *
 * Entries are filtered by sequence number against a baseline taken before the
 * request started, because the ledger is the whole machine's, not this run's.
 */
import { useEffect, useRef, useState } from "react"
import { kernel, Decision } from "../../api/kernel"

/** How often to look. Fast enough to feel live, slow enough to be invisible on
 *  a machine that is busy generating tokens. */
const POLL_MS = 600

/** The last path-ish argument, shortened to something a person recognises. */
function basename(entry: Decision): string {
  const paths = (entry.detail?.paths as string[] | undefined) ?? []
  const last = paths[paths.length - 1]
  if (!last) return ""
  const name = last.split("/").pop() || last
  return name.length > 34 ? `${name.slice(0, 31)}…` : name
}

/** One line of prose for what is happening now, from the newest entry.
 *
 * Deliberately in the present continuous and deliberately specific: "Reading
 * fattura.pdf" tells you the run is alive and what it is touching, which is the
 * question a spinner is being asked and cannot answer. */
function phrase(latest: Decision | undefined): string {
  if (!latest) return "Classifying"

  switch (latest.kind) {
    case "tool_call": {
      const tool = (latest.detail?.tool as string) ?? "a tool"
      const file = basename(latest)
      if (latest.outcome !== "cleared" && latest.outcome !== "permitted") {
        return `Refused ${tool}`
      }
      return file ? `Reading ${file}` : `Running ${tool}`
    }
    case "taint":
      return `Reclassifying as ${latest.class}`
    case "inference":
      if (latest.outcome === "held") return "Held"
      return latest.substrate ? `Thinking on ${latest.substrate}` : "Thinking"
    case "brief":
      return "Writing a brief"
    case "egress":
      return latest.outcome === "held" ? "Held at the perimeter" : "Sending"
    default:
      return "Working"
  }
}

function tone(entry: Decision): string {
  if (entry.outcome === "held" || entry.outcome === "denied" || entry.outcome === "refused") {
    return "var(--red)"
  }
  if (entry.kind === "taint") return "var(--yellow)"
  return "var(--green)"
}

/** A step, as one line. Kept to one line on purpose: this is peripheral vision
 *  while somebody waits, not the record — that is the Perimeter view. */
function Step({ entry }: { entry: Decision }) {
  const tool = entry.detail?.tool as string | undefined
  const file = basename(entry)

  let what = entry.kind as string
  if (entry.kind === "tool_call") what = tool ?? "tool"
  if (entry.kind === "inference") what = entry.substrate || "inference"
  if (entry.kind === "taint") what = "class raised"

  return (
    <div className="an-working__step">
      <span className="an-working__dot" style={{ background: tone(entry) }} />
      <code className="an-working__what">{what}</code>
      {file && <span className="an-working__file">{file}</span>}
      <span className="an-working__outcome" style={{ color: tone(entry) }}>{entry.outcome}</span>
    </div>
  )
}

export default function Working() {
  const [steps, setSteps] = useState<Decision[]>([])
  const [elapsed, setElapsed] = useState(0)
  const baseline = useRef<number | null>(null)

  // Elapsed time, because "is it stuck" is the actual question and a number
  // answers it better than any animation.
  useEffect(() => {
    const started = Date.now()
    const t = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    let stopped = false

    const tick = async () => {
      try {
        const { entries, total } = await kernel.ledger({ limit: 40 })
        if (stopped) return
        // The first read fixes the watermark. Everything at or below it belongs
        // to some earlier run and is none of this component's business.
        if (baseline.current === null) baseline.current = total - entries.length
        const mine = entries
          .filter((e) => e.seq > (baseline.current ?? 0))
          .sort((a, b) => a.seq - b.seq)
        setSteps(mine)
      } catch {
        // The daemon is busy answering; a missed poll is not worth a message.
      }
    }

    void tick()
    const t = setInterval(tick, POLL_MS)
    return () => { stopped = true; clearInterval(t) }
  }, [])

  const latest = steps[steps.length - 1]
  const held = latest?.outcome === "held"

  return (
    <div className="an-working">
      <div className="an-working__head">
        <span className={`an-working__label ${held ? "is-held" : ""}`}>{phrase(latest)}</span>
        {elapsed >= 2 && <span className="an-working__clock">{elapsed}s</span>}
      </div>

      {steps.length > 0 && (
        <div className="an-working__steps">
          {steps.map((e) => <Step key={e.seq} entry={e} />)}
        </div>
      )}
    </div>
  )
}
