import { useRunner } from "../../hooks/useRunner"

export default function TasksView() {
  const { status, logs, start, stop } = useRunner()

  return (
    <>
      <div className="view-header">
        <div className="view-header-left">
          <div className="view-title">Runner</div>
          <div className="view-sub">Daemon status · local task execution</div>
        </div>
        <div className="flex gap-2">
          <button className="btn" onClick={stop} disabled={status === "stopped"}>Stop</button>
          <button className="btn primary" onClick={start} disabled={status === "running" || status === "starting"}>
            {status === "starting" ? "Starting…" : "Start"}
          </button>
        </div>
      </div>

      <div className="view-body">
        <div className="card mb-3">
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 10 }}>Daemon status</div>
          <div className="flex items-center gap-2">
            <span className={`status-dot ${status}`} />
            <span style={{ textTransform: "capitalize" }}>{status}</span>
            {status === "running" && (
              <span className="text-sub" style={{ fontSize: 11 }}>· polling · API on localhost:7070</span>
            )}
          </div>
        </div>

        {logs.length > 0 && (
          <div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>Daemon log</div>
            <div className="log-panel">
              {logs.map((l, i) => (
                <div key={i} className={`log-line ${l.type === "error" ? "err" : ""}`}>
                  {l.message}
                </div>
              ))}
            </div>
          </div>
        )}

        {logs.length === 0 && status !== "running" && (
          <div className="card">
            <p className="text-muted">The daemon is stopped. Start it to run tasks locally and to enable sync.</p>
          </div>
        )}
      </div>
    </>
  )
}
