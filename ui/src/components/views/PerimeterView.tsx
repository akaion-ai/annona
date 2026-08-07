import { useCallback, useEffect, useState } from "react"
import {
  kernel,
  Decision,
  KernelError,
  PolicyDoc,
  SubstrateHealth,
} from "../../api/kernel"
import PolicyEditor from "./PolicyEditor"

/**
 * Perimeter — the policy, the substrates and the record, on one screen.
 *
 * The three things `annona policy show`, `annona substrates` and `annona audit`
 * print, for the people who will never open a terminal.
 *
 * It was read-only at first, on the argument that editing the perimeter from a
 * page every process on this machine can reach is a larger decision than being
 * able to see it. That is true, and it was still the wrong trade: the answer to
 * "how do I change this" was "open policy.yaml in an editor", which most people
 * do not do — so the policy stayed as first written whether or not it described
 * what anybody wanted. See `PolicyEditor` for what makes the write side safe.
 */

type Tab = "policy" | "substrates" | "ledger"

function classTone(klass: string): string {
  if (klass === "restricted") return "var(--red)"
  if (klass === "internal") return "var(--yellow)"
  return "var(--text-muted)"
}

function Empty({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="an-empty">
      <div className="an-empty__title">{title}</div>
      <div className="an-empty__sub">{sub}</div>
    </div>
  )
}

// ── Policy ────────────────────────────────────────────────────────────────────

function PolicyPanel({ doc }: { doc: PolicyDoc }) {
  return (
    <div className="an-panel">
      <div className="an-panel__path">{doc.source}</div>

      <h4 className="an-h4">Rules <span>first match wins</span></h4>
      <table className="an-table">
        <thead>
          <tr><th>class</th><th>may run on</th><th>if unavailable</th><th>prefers</th></tr>
        </thead>
        <tbody>
          {doc.rules.map((r, i) => (
            <tr key={r.id || i}>
              <td style={{ color: classTone(r.class) }}>{r.class}</td>
              <td><code>{r.allow.join(", ") || "—"}</code></td>
              <td className={r.on_unavailable === "hold" ? "an-strong" : ""}>{r.on_unavailable}</td>
              <td className="an-dim">{r.prefer}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4 className="an-h4">What earns a class</h4>
      <table className="an-table">
        <thead><tr><th>class</th><th>paths</th><th>patterns</th></tr></thead>
        <tbody>
          {doc.classes.map((c) => (
            <tr key={c.label}>
              <td style={{ color: classTone(c.label) }}>
                {c.label}{c.default ? <span className="an-dim"> · default</span> : null}
              </td>
              <td><code>{c.paths.join(", ") || "—"}</code></td>
              <td><code className="an-dim">{c.patterns.length ? `${c.patterns.length} pattern${c.patterns.length > 1 ? "s" : ""}` : "—"}</code></td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4 className="an-h4">Tools <span>default deny</span></h4>
      <table className="an-table">
        <thead><tr><th>tool</th><th>may touch</th></tr></thead>
        <tbody>
          {Object.entries(doc.tools.allow).map(([tool, paths]) => (
            <tr key={tool}><td><code>{tool}</code></td><td><code>{paths.join(", ") || "—"}</code></td></tr>
          ))}
          {Object.keys(doc.tools.allow).length === 0 && (
            <tr><td colSpan={2} className="an-dim">no tool is allowed — the model can reason, not act</td></tr>
          )}
        </tbody>
      </table>
      {doc.tools.deny_paths.length > 0 && (
        <div className="an-never">
          Never, for any tool: <code>{doc.tools.deny_paths.join(", ")}</code>
        </div>
      )}

      <div className="an-facts">
        <span>redaction: <b>{doc.redaction.enabled ? doc.redaction.provider : "off"}</b></span>
        <span>skills: <b>{doc.skills.length ? doc.skills.join(", ") : "none enabled"}</b></span>
        <span>default class: <b style={{ color: classTone(doc.default_class) }}>{doc.default_class}</b></span>
      </div>
    </div>
  )
}

// ── Substrates ────────────────────────────────────────────────────────────────

function SubstratesPanel({ rows, probed }: { rows: SubstrateHealth[]; probed: boolean }) {
  return (
    <div className="an-panel">
      <table className="an-table">
        <thead>
          <tr><th>substrate</th><th>jurisdiction</th><th>up to</th><th>state</th><th>detail</th></tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td>
                <code>{s.id}</code>
                <div className="an-dim an-sub">{s.kind}{s.model ? ` · ${s.model}` : ""}</div>
              </td>
              <td>{s.jurisdiction}</td>
              <td style={{ color: classTone(s.max_class) }}>{s.max_class}</td>
              <td style={{ color: s.up ? "var(--green)" : "var(--red)" }}>{s.up ? "up" : "down"}</td>
              <td className="an-dim">
                {s.reason || (s.latency_ms != null ? `${Math.round(s.latency_ms)} ms` : "")}
                {s.endpoint ? <div className="an-sub"><code>{s.endpoint}</code></div> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!probed && <div className="an-dim an-note">Liveness was not probed.</div>}
    </div>
  )
}

// ── Ledger ────────────────────────────────────────────────────────────────────

function LedgerPanel({
  entries, total, path, chain, onlyHeld, setOnlyHeld,
}: {
  entries: Decision[]
  total: number
  path: string
  chain: { ok: boolean; problem: string; entries: number } | null
  onlyHeld: boolean
  setOnlyHeld: (v: boolean) => void
}) {
  if (total === 0) {
    return <Empty title="Nothing recorded yet." sub="Every decision lands here the moment you ask the kernel something." />
  }
  return (
    <div className="an-panel">
      <div className="an-ledger-head">
        <div className="an-panel__path">{path}</div>
        {chain && (
          <span style={{ color: chain.ok ? "var(--green)" : "var(--red)" }}>
            {chain.ok ? `chain intact · ${chain.entries} entries` : `chain broken · ${chain.problem}`}
          </span>
        )}
        <label className="an-toggle">
          <input type="checkbox" checked={onlyHeld} onChange={(e) => setOnlyHeld(e.target.checked)} />
          refusals only
        </label>
      </div>

      <table className="an-table an-table--mono">
        <thead>
          <tr><th>#</th><th>when</th><th>step</th><th>kind</th><th>class</th><th>outcome</th><th>where</th><th>why</th></tr>
        </thead>
        <tbody>
          {[...entries].reverse().map((d) => {
            const held = d.outcome === "held" || d.outcome === "denied"
            return (
              <tr key={d.seq}>
                <td className="an-dim">{d.seq}</td>
                <td className="an-dim">{d.ts.slice(11, 19)}</td>
                <td><code>{d.step_id}</code></td>
                <td>{d.kind}</td>
                <td style={{ color: classTone(d.class) }}>{d.class}</td>
                <td style={{ color: held ? "var(--red)" : "var(--green)" }}>{d.outcome}</td>
                <td>{d.substrate || "—"}</td>
                <td className="an-dim">{String(d.detail?.reason ?? d.rule_id ?? "")}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {entries.length === 0 && onlyHeld && (
        <div className="an-dim an-note">Nothing was refused. {total} decisions recorded.</div>
      )}
    </div>
  )
}

// ── View ──────────────────────────────────────────────────────────────────────

export default function PerimeterView() {
  const [tab, setTab] = useState<Tab>("policy")
  const [doc, setDoc] = useState<PolicyDoc | null>(null)
  const [subs, setSubs] = useState<{ probed: boolean; substrates: SubstrateHealth[] } | null>(null)
  const [ledger, setLedger] = useState<{ path: string; total: number; entries: Decision[] } | null>(null)
  const [chain, setChain] = useState<{ ok: boolean; problem: string; entries: number } | null>(null)
  const [onlyHeld, setOnlyHeld] = useState(false)
  const [error, setError] = useState("")
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError("")
    try {
      if (tab === "policy") setDoc(await kernel.policy())
      if (tab === "substrates") setSubs(await kernel.substrates(true))
      if (tab === "ledger") {
        const [l, v] = await Promise.all([kernel.ledger({ limit: 200, held: onlyHeld }), kernel.verify()])
        setLedger(l)
        setChain({ ok: v.ok, problem: v.problem, entries: v.entries })
      }
    } catch (e) {
      setError(e instanceof KernelError ? e.detail : String(e))
    } finally {
      setLoading(false)
    }
  }, [tab, onlyHeld])

  useEffect(() => { void load() }, [load])

  return (
    <>
      <div className="view-header">
        <div className="view-header-left">
          <div className="ak-view-title">Perimeter</div>
          <div className="ak-view-sub">What is allowed, what is reachable, what happened</div>
        </div>
        <div className="view-header-actions">
          {tab === "policy" && !editing && doc && (
            <button className="btn" onClick={() => setEditing(true)}>Edit</button>
          )}
          <button className="btn" onClick={() => void load()} disabled={loading}>
            {loading ? "…" : "Refresh"}
          </button>
        </div>
      </div>

      <div className="ak-filter-tabs">
        {(["policy", "substrates", "ledger"] as Tab[]).map((t) => (
          <button
            key={t}
            className={`ak-filter-tab ${tab === t ? "active" : ""}`}
            onClick={() => { setEditing(false); setTab(t) }}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="view-body">
        {error && (
          <div className="an-error">
            {error}
            {error.includes("policy init") && (
              <div className="an-dim an-note">
                Until a policy exists nothing is enforced, and the kernel says so rather than
                showing you an empty table.
              </div>
            )}
          </div>
        )}

        {!error && tab === "policy" && editing && (
          <PolicyEditor
            onSaved={() => { setEditing(false); void load() }}
            onCancel={() => setEditing(false)}
          />
        )}
        {!error && tab === "policy" && !editing && doc && <PolicyPanel doc={doc} />}
        {!error && tab === "substrates" && subs && (
          <SubstratesPanel rows={subs.substrates} probed={subs.probed} />
        )}
        {!error && tab === "ledger" && ledger && (
          <LedgerPanel
            entries={ledger.entries}
            total={ledger.total}
            path={ledger.path}
            chain={chain}
            onlyHeld={onlyHeld}
            setOnlyHeld={setOnlyHeld}
          />
        )}
      </div>
    </>
  )
}
