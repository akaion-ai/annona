import { useUpdater } from "../hooks/useUpdater";

// Non-blocking banner shown at the top of the app shell when a newer release
// is available on GitHub. Web (`npm run dev` in a browser) renders nothing.
export default function UpdateBanner() {
  const { phase, available, progress, error, dismissed, dismiss, installAndRestart } = useUpdater();

  // Hide for: web mode (phase stays "idle"), no update found, user-dismissed,
  // the "checking" first-load window, and a check that could not reach the
  // manifest — that last one is a fact about the network, not about the app.
  if (dismissed) return null;
  if (phase === "idle" || phase === "checking" || phase === "up-to-date") return null;
  if (phase === "check-failed") return null;
  if (phase === "available" && !available) return null;

  const busy = phase === "downloading" || phase === "installing";
  const errored = phase === "error";

  let label = "";
  if (available) {
    label = `Annona ${available.version} disponibile`;
  } else if (errored) {
    label = "Update failed";
  }

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "fixed",
        top: 8,
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "8px 14px",
        borderRadius: 8,
        background: "var(--bg-surface, #161b22)",
        border: "1px solid var(--border-sub, #30363d)",
        boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
        color: "var(--text, #e6edf3)",
        fontSize: 12,
        maxWidth: "calc(100% - 32px)",
      }}
    >
      <span aria-hidden style={{ fontSize: 14 }}>{errored ? "!" : "^"}</span>
      <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.3 }}>
        <span style={{ fontWeight: 500 }}>{label}</span>
        {available?.currentVersion && phase === "available" && (
          <span style={{ color: "var(--text-sub, #8b949e)", fontSize: 11 }}>
            Versione attuale: {available.currentVersion}
          </span>
        )}
        {phase === "downloading" && (
          <span style={{ color: "var(--text-sub, #8b949e)", fontSize: 11 }}>
            Download… {progress !== null ? `${progress}%` : ""}
          </span>
        )}
        {phase === "installing" && (
          <span style={{ color: "var(--text-sub, #8b949e)", fontSize: 11 }}>
            Installing…
          </span>
        )}
        {errored && error && (
          <span style={{ color: "var(--text-sub, #8b949e)", fontSize: 11 }}>{error}</span>
        )}
      </div>

      <div style={{ display: "flex", gap: 6, marginLeft: 8 }}>
        {phase === "available" && (
          <>
            <button
              onClick={installAndRestart}
              disabled={busy}
              style={{
                background: "var(--accent, #2f81f7)",
                color: "#fff",
                border: "none",
                borderRadius: 6,
                padding: "4px 10px",
                fontSize: 12,
                fontWeight: 500,
                cursor: busy ? "default" : "pointer",
              }}
            >
              Aggiorna ora
            </button>
            <button
              onClick={dismiss}
              style={{
                background: "transparent",
                color: "var(--text-sub, #8b949e)",
                border: "1px solid var(--border-sub, #30363d)",
                borderRadius: 6,
                padding: "4px 10px",
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              Later
            </button>
          </>
        )}
        {errored && (
          <button
            onClick={dismiss}
            style={{
              background: "transparent",
              color: "var(--text-sub, #8b949e)",
              border: "1px solid var(--border-sub, #30363d)",
              borderRadius: 6,
              padding: "4px 10px",
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            Close
          </button>
        )}
      </div>
    </div>
  );
}
