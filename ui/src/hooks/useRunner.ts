import { useEffect, useState, useCallback } from "react";

export type RunnerStatus = "running" | "stopped" | "starting" | "error";

export interface RunnerLog {
  type: "log" | "error";
  message: string;
  ts: number;
}

import { API_ORIGIN } from "../api/base";

const API_BASE = API_ORIGIN;

// ─────────────────────────────────────────────────────────────────────────────
// Tauri detection + safe invoke wrapper
// Tauri 2 exposes window.__TAURI_INTERNALS__ (and window.__TAURI__ when
// withGlobalTauri:true is set in tauri.conf.json — which we do). We probe both.
// ─────────────────────────────────────────────────────────────────────────────
function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as unknown as Record<string, unknown>;
  return "__TAURI__" in w || "__TAURI_INTERNALS__" in w;
}

async function tauriInvoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  // Lazy import so the web bundle doesn't need @tauri-apps/api at runtime.
  const mod = await import("@tauri-apps/api/core");
  return mod.invoke<T>(cmd, args);
}

interface TauriRunnerStatus {
  process_alive: boolean;
  http_ok: boolean;
  url: string;
}

/**
 * Status + lifecycle hook for the runner daemon.
 *
 * Two modes:
 *  • Tauri (desktop bundle): the sidecar is owned by the Rust shell. We talk
 *    to it via `invoke('runner_status' | 'start_runner' | 'stop_runner')`.
 *  • Web (served by the daemon itself on :7070): the daemon is already up.
 *    Fall back to polling `/health` every 5 seconds.
 */
export function useRunner() {
  const [status, setStatus] = useState<RunnerStatus>("stopped");
  const [logs, setLogs] = useState<RunnerLog[]>([]);

  const probe = useCallback(async () => {
    if (isTauri()) {
      try {
        const s = await tauriInvoke<TauriRunnerStatus>("runner_status");
        setStatus(s.http_ok ? "running" : s.process_alive ? "starting" : "stopped");
        return;
      } catch (e) {
        // Asking the shell failed — the IPC bridge, not the daemon. Since this
        // page is served *by* the daemon, the honest thing is to ask the daemon
        // directly rather than paint the status bar red about a process that is
        // demonstrably answering requests.
        console.warn("[runner] status via IPC failed, falling back to /health:", e);
      }
    }
    try {
      const res = await fetch(`${API_BASE}/health`);
      setStatus(res.ok ? "running" : "stopped");
    } catch {
      setStatus("stopped");
    }
  }, []);

  useEffect(() => {
    probe();
    const id = setInterval(probe, 5000);
    return () => clearInterval(id);
  }, [probe]);

  const start = useCallback(async () => {
    if (isTauri()) {
      try {
        await tauriInvoke("start_runner");
        setLogs((p) => [...p, { type: "log", message: "Runner started.", ts: Date.now() }]);
        probe();
      } catch (e) {
        setLogs((p) => [
          ...p,
          { type: "error", message: `Start failed: ${String(e)}`, ts: Date.now() },
        ]);
      }
      return;
    }
    setLogs((prev) => [
      ...prev,
      {
        type: "log",
        message:
          "Runner is already up (this UI is served by the daemon). Start/stop from the shell with ./start.sh.",
        ts: Date.now(),
      },
    ]);
  }, [probe]);

  const stop = useCallback(async () => {
    if (isTauri()) {
      try {
        await tauriInvoke("stop_runner");
        setLogs((p) => [...p, { type: "log", message: "Runner stopped.", ts: Date.now() }]);
        probe();
      } catch (e) {
        setLogs((p) => [
          ...p,
          { type: "error", message: `Stop failed: ${String(e)}`, ts: Date.now() },
        ]);
      }
      return;
    }
    setLogs((prev) => [
      ...prev,
      {
        type: "log",
        message: "Stop is not available in the web UI — kill the daemon process from your terminal.",
        ts: Date.now(),
      },
    ]);
  }, [probe]);

  return { status, logs, start, stop };
}
