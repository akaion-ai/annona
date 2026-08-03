import { useState, useEffect, useCallback } from "react"
import { sync, SyncStatus } from "../../api/runner"
import { CloudUpIcon } from "../ui/Icons"

function StatCard({ number, label, color }: { number: number; label: string; color?: string }) {
  return (
    <div className="sync-stat">
      <div className="sync-stat-number" style={{ color: color ?? "var(--text)" }}>{number}</div>
      <div className="sync-stat-label">{label}</div>
    </div>
  )
}

export default function SyncView() {
  const [status, setStatus]     = useState<SyncStatus | null>(null)
  const [loading, setLoading]   = useState(true)
  const [pushing, setPushing]   = useState(false)
  const [lastResult, setLastResult] = useState<string | null>(null)
  const [runnerDown, setRunnerDown] = useState(false)

  const loadStatus = useCallback(async () => {
    try {
      const s = await sync.status()
      setStatus(s)
      setRunnerDown(false)
    } catch {
      setRunnerDown(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  const handlePush = async () => {
    setPushing(true)
    try {
      const res = await sync.push()
      setLastResult(`Push: ${res.synced} synced, ${res.errors} failed`)
      await loadStatus()
    } catch {
      setLastResult("Push failed — the daemon is not reachable")
    } finally {
      setPushing(false)
    }
  }

  const fmt = (dt: string | null) => {
    if (!dt) return "Never"
    return new Date(dt).toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
  }

  return (
    <>
      <div className="view-header">
        <div className="view-header-left">
          <div className="view-title">Sync</div>
          <div className="view-sub">Send local vault notes to the cloud (push only)</div>
        </div>
        <div className="flex gap-2">
          <button className="btn primary" onClick={handlePush} disabled={pushing}>
            <CloudUpIcon size={13} /> {pushing ? "Push…" : "Push pending"}
          </button>
        </div>
      </div>

      <div className="view-body">
        {runnerDown ? (
          <div className="card">
            <p className="text-muted">Daemon not reachable on <code>localhost:7070</code>.</p>
          </div>
        ) : loading ? (
          <p className="text-sub">Loading…</p>
        ) : status ? (
          <>
            <div className="sync-stat-grid">
              <StatCard number={status.local_only} label="Local only" />
              <StatCard number={status.pending}    label="Pending"    color="var(--yellow)" />
              <StatCard number={status.synced}     label="Synced"     color="var(--green)" />
              <StatCard number={status.errors}     label="Errors"     color="var(--red)" />
            </div>

            <div className="card mb-3">
              <div className="flex justify-between items-center mb-3">
                <span style={{ fontWeight: 600, fontSize: 13 }}>Sync status</span>
              </div>
              <div>
                <div className="text-sub" style={{ fontSize: 11, marginBottom: 3 }}>Last push</div>
                <div style={{ fontSize: 13 }}>{fmt(status.last_push)}</div>
              </div>
            </div>

            {status.pending > 0 && (
              <div className="card" style={{ borderColor: "rgba(210,153,34,0.3)", background: "rgba(210,153,34,0.05)" }}>
                <div className="flex items-center gap-2">
                  <span style={{ color: "var(--yellow)", fontWeight: 600 }}>
                    {status.pending} {status.pending === 1 ? "note" : "notes"} waiting
                  </span>
                  <span className="text-sub">— hit "Push pending" to send them</span>
                </div>
              </div>
            )}

            <div className="card" style={{ marginTop: 12 }}>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 10 }}>
                How sync works
              </div>
              <div style={{ color: "var(--text-muted)", fontSize: 12, lineHeight: 1.7 }}>
                <p style={{ marginBottom: 6 }}>
                  <strong style={{ color: "var(--text)" }}>Push</strong> — Notes marked "pending" are sent to the cloud as a <em>Thought</em>.
                  The cloud processes them, generates embeddings, and assigns each one to a cluster.
                </p>
                <p>
                  <strong style={{ color: "var(--text)" }}>Local only</strong> — Notes stay on this machine until you mark them
                  for sync explicitly, from the <em>Sync</em> button in the note editor.
                </p>
                <p style={{ marginTop: 6 }}>
                  <strong style={{ color: "var(--text)" }}>One-way sync</strong> — Annona publishes to the cloud, it never
                  pulls back. Cloud notes stay in the cloud; local notes live on your machine.
                </p>
              </div>
            </div>

            {lastResult && (
              <div className="card" style={{ marginTop: 12, borderColor: "var(--border)" }}>
                <span className="text-muted" style={{ fontSize: 12 }}>{lastResult}</span>
              </div>
            )}
          </>
        ) : null}
      </div>
    </>
  )
}
