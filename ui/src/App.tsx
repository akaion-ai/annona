import { useState, useEffect, useRef } from "react";
import { useRunner } from "./hooks/useRunner";
import { AskIcon, PerimeterIcon, BrainIcon, SyncIcon, TasksIcon, SettingsIcon } from "./components/ui/Icons";
import AskView       from "./components/views/AskView";
import PerimeterView from "./components/views/PerimeterView";
import BrainView    from "./components/views/BrainView";
import SyncView     from "./components/views/SyncView";
import TasksView    from "./components/views/TasksView";
import WelcomeView, { ONBOARDING_FLAG } from "./components/views/WelcomeView";
import SetupView from "./components/views/SetupView";
import UpdateBanner from "./components/UpdateBanner";
import { signIn, isSigninHandoff } from "./lib/signin";
import { auth as authApi, runner as runnerApi, sync as syncApi, AuthStatus, RunnerMode } from "./api/runner";
import { API_ORIGIN } from "./api/base";
import { kernel as kernelApi } from "./api/kernel";
import "./App.css";
import "./css/auth-animations.css";

type View = "ask" | "perimeter" | "brain" | "sync" | "tasks"

// The kernel first, the vault second. What somebody installed this for is
// deciding where their work runs; the notes are what the previous product did.
//
// No Plugins entry: there is no plugin system yet, and a nav item that leads to
// a list of things that do not exist teaches people the whole app might be a
// mock-up. It comes back when `annona plugin install` actually installs
// something.
const NAV: { id: View; label: string; icon: React.FC<{ size?: number }>; section: string }[] = [
  { id: "ask",       label: "Ask",       icon: AskIcon,       section: "Kernel" },
  { id: "perimeter", label: "Perimeter", icon: PerimeterIcon, section: "Kernel" },
  { id: "brain",     label: "Notes",     icon: BrainIcon,     section: "Workspace" },
  { id: "sync",      label: "Sync",      icon: SyncIcon,      section: "Workspace" },
  { id: "tasks",     label: "Tasks",     icon: TasksIcon,     section: "Workspace" },
]

function readOnboardingDone(): boolean {
  try { return localStorage.getItem(ONBOARDING_FLAG) === "true"; } catch { return false; }
}

export default function App() {
  const { status, start } = useRunner();
  const [view, setView]               = useState<View>("ask");
  const [authStatus, setAuthStatus]   = useState<AuthStatus | null>(null);
  const [mode, setMode]               = useState<RunnerMode | null>(null);
  const [bootChecked, setBootChecked] = useState(false);
  const [showWelcome, setShowWelcome] = useState(false);
  const [cloudSyncing, setCloudSyncing] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [noteCount, setNoteCount]       = useState<number | null>(null);
  // null until the daemon has been asked. Rendering the app before that is
  // known would flash the Ask box at somebody who has not chosen a policy.
  const [configured, setConfigured]     = useState<boolean | null>(null);
  const settingsRef = useRef<HTMLDivElement | null>(null);

  // Close popover when clicking outside.
  useEffect(() => {
    if (!settingsOpen) return;
    const onClick = (e: MouseEvent) => {
      if (settingsRef.current && !settingsRef.current.contains(e.target as Node)) {
        setSettingsOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [settingsOpen]);

  // Listen to BrainView count updates (statusbar info).
  useEffect(() => {
    const onCount = (e: Event) => {
      const ce = e as CustomEvent<{ count: number }>;
      if (ce?.detail && typeof ce.detail.count === "number") setNoteCount(ce.detail.count);
    };
    window.addEventListener("akaion:note-count", onCount);
    return () => window.removeEventListener("akaion:note-count", onCount);
  }, []);

  // Probe runner and decide if we need the welcome screen.
  useEffect(() => {
    if (status === "stopped") start();
  }, []); // eslint-disable-line

  // Does this machine have a policy at all? Until it does, the kernel is not
  // enforcing, and every answer the app could give would come from a machine
  // that has decided nothing. That question is asked before the app is shown,
  // not reported in small type at the bottom of it.
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const opts = await kernelApi.setupOptions();
        if (!cancelled) setConfigured(opts.configured);
      } catch {
        // The daemon is not up yet. Retry rather than assume either answer:
        // guessing "configured" hides the chooser, guessing the opposite shows
        // it to somebody who already has a policy.
        if (!cancelled) setTimeout(check, 1000);
      }
    };
    check();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const [s, m] = await Promise.all([authApi.status(), runnerApi.mode()]);
        if (cancelled) return;
        setAuthStatus(s);
        setMode(m);
        // Welcome rule: first launch and not yet authenticated — or this page
        // was opened by the desktop app to sign in, in which case the screen
        // with the button on it is the entire reason the tab exists. Without
        // this the handoff opened straight into Ask and there was nothing to
        // click; the onboarding flag is a fact about a previous visit, not
        // about what this tab was opened for.
        setShowWelcome(isSigninHandoff() || (!readOnboardingDone() && !s.authenticated));
        setBootChecked(true);
      } catch {
        if (!cancelled) setTimeout(check, 1000);
      }
    };
    check();
    return () => { cancelled = true; };
  }, []);

  const handleLoginFromWelcome = (s: AuthStatus) => {
    setAuthStatus(s);
    setShowWelcome(false);
    refreshMode();
  };

  const handleSkipFromWelcome = () => {
    setShowWelcome(false);
  };

  const refreshMode = async () => {
    try { setMode(await runnerApi.mode()); } catch { /* offline */ }
  };

  const handleLogout = async () => {
    await authApi.logout();
    setAuthStatus({ authenticated: false, email: null, runner_id: null });
    refreshMode();
  };

  const handleSidebarCloudLogin = async () => {
    setCloudSyncing(true);
    try {
      // Same handoff as the welcome screen: a popup cannot open here.
      const s = await signIn();
      setAuthStatus(s);
      try { localStorage.setItem(ONBOARDING_FLAG, "true"); } catch { /* */ }
      refreshMode();

      // Auto-push once auth lands: send every pending local note to the cloud.
      // Failure here must never roll back the login.
      try {
        const res = await syncApi.push();
        console.info(`Auto-push after login: synced=${res.synced} errors=${res.errors}`);
      } catch (syncErr) {
        console.warn("Auto-push after login failed (login still ok):", syncErr);
      }
    } catch (e: any) {
      if (e?.code !== "auth/popup-closed-by-user" && e?.code !== "auth/cancelled-popup-request") {
        // No noisy alert; logged silently — sidebar will retry on next click.
        console.warn("Cloud login failed:", e?.message ?? e);
      }
    } finally {
      setCloudSyncing(false);
    }
  };

  const handleShowWelcomeAgain = () => {
    try { localStorage.removeItem(ONBOARDING_FLAG); } catch { /* */ }
    setShowWelcome(true);
  };

  // Names the state of the daemon, not of a component the user has never heard
  // of. "Runner" was the product's previous name and means nothing here.
  const statusLabel: Record<string, string> = {
    running: "Daemon active",
    stopped: "Daemon stopped",
    starting: "Starting…",
    error: "Daemon unreachable",
  };

  // ── Render ──────────────────────────────────────────────────────────────────
  // The policy comes before the vault. Somebody who has not chosen where their
  // work may run has not finished installing this, whatever else they signed
  // into — and the signin handoff tab is about one thing, so it is exempt.
  if (configured === false && !isSigninHandoff()) {
    return (
      <>
        <UpdateBanner />
        <SetupView onDone={() => setConfigured(true)} />
      </>
    );
  }

  if (showWelcome) {
    return (
      <>
        <UpdateBanner />
        <WelcomeView
          vaultPath={mode?.vault_path}
          onLogin={handleLoginFromWelcome}
          onSkip={handleSkipFromWelcome}
        />
      </>
    );
  }

  const isAuthed = !!authStatus?.authenticated;

  return (
    <div className="app-shell">
      {/* Auto-update banner (Tauri-only; web mode = no-op). Position:fixed so
          it doesn't disturb the grid layout. */}
      <UpdateBanner />
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="ak-sidebar-logo">
          <span className="ak-sidebar-logo__badge">
            {/* The mascot rather than the vendor's mark: whoever installed this
                installed Annona, and the sidebar is the one place they look to
                know what they are running. */}
            <img
              src="/annona-mascot.png"
              alt=""
              width={28}
              height={28}
              style={{ objectFit: "contain", display: "block" }}
            />
          </span>
          <div>
            <div className="ak-sidebar-logo__name">Annona</div>
            <div className="ak-sidebar-logo__sub">{isAuthed ? "Cloud" : "Local"}</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          {NAV.map(({ id, label, icon: Icon, section }, i) => (
            <div key={id}>
              {(i === 0 || NAV[i - 1].section !== section) && (
                <div className="ak-nav-section">{section}</div>
              )}
              <button
                className={`ak-nav-item ${view === id ? "active" : ""}`}
                onClick={() => setView(id)}
                style={{ width: "100%" }}
              >
                <Icon size={15} />
                {label}
              </button>
            </div>
          ))}

          <div style={{ flex: 1 }} />

          {/* Cloud sync badge — visible when in local mode */}
          {!isAuthed && (
            <div className="ak-cloud-badge" role="region" aria-label="Local mode">
              <div className="ak-cloud-badge__row">
                <span className="ak-cloud-badge__dot" />
                <span style={{ fontSize: 12, fontWeight: 500 }}>Local</span>
              </div>
              <div style={{ fontSize: 11, color: "rgba(255,255,255,0.45)", lineHeight: 1.4, marginTop: -2 }}>
                Notes stay on this machine
              </div>
              <button
                className="ak-cloud-badge__cta"
                onClick={handleSidebarCloudLogin}
                disabled={cloudSyncing}
              >
                {cloudSyncing ? "Connecting…" : "Sync →"}
              </button>
            </div>
          )}

          {/* Account block — authed */}
          {isAuthed && (
            <div className="ak-cloud-badge" role="region" aria-label="Cloud mode">
              <div className="ak-cloud-badge__row">
                <span className="ak-cloud-badge__dot ak-cloud-badge__dot--online" />
                <span style={{ fontSize: 12, fontWeight: 500 }}>Cloud sync</span>
              </div>
              {authStatus?.email && (
                <div style={{ fontSize: 11, color: "rgba(255,255,255,0.45)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", marginTop: -2 }}>
                  {authStatus.email}
                </div>
              )}
              <button
                className="ak-cloud-badge__cta"
                onClick={handleLogout}
                style={{ borderColor: "rgba(255,255,255,0.08)", background: "rgba(255,255,255,0.04)" }}
              >
                Logout
              </button>
            </div>
          )}
        </nav>

        {/* Settings row (gear popover) */}
        <div className="ak-settings-row" ref={settingsRef}>
          <button
            className="ak-icon-btn"
            onClick={() => setSettingsOpen((v) => !v)}
            title="Settings"
            aria-label="Settings"
          >
            <SettingsIcon size={14} />
          </button>
          <span style={{ fontSize: 11, color: "rgba(255,255,255,0.4)" }}>
            v0.1.0
          </span>
          {settingsOpen && (
            <div className="ak-settings-popover" role="menu">
              <button
                className="ak-settings-item"
                onClick={() => { setSettingsOpen(false); handleShowWelcomeAgain(); }}
              >
                Mostra benvenuto
              </button>
              <button
                className="ak-settings-item ak-settings-item--sub"
                disabled
                title="Coming soon"
                style={{ cursor: "not-allowed", opacity: 0.55 }}
              >
                Open vault folder
              </button>
              <button
                className="ak-settings-item ak-settings-item--sub"
                disabled
                title="Coming soon"
                style={{ cursor: "not-allowed", opacity: 0.55 }}
              >
                About Annona
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        {!bootChecked ? (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--text-sub)", fontSize: 13 }}>
            Connecting to the daemon…
          </div>
        ) : (
          <>
            {view === "ask"       && <AskView />}
            {view === "perimeter" && <PerimeterView />}
            {view === "brain"   && <BrainView />}
            {view === "sync"    && <SyncView />}
            {view === "tasks"   && <TasksView />}
          </>
        )}
      </main>

      {/* Status bar */}
      <footer className="ak-statusbar">
        <div className="ak-statusbar__item">
          <span className={`status-dot ${status}`} />
          <span>{statusLabel[status] ?? status}</span>
        </div>
        <div className="ak-statusbar__sep" />
        <div className="ak-statusbar__item ak-statusbar__item--muted">
          {/* The daemon this window is actually talking to. It was written out
              as 127.0.0.1:7070, which is a claim rather than a reading the
              moment anybody uses --port. */}
          <span>{API_ORIGIN.replace(/^https?:\/\//, "")}</span>
        </div>
        <div className="ak-statusbar__sep" />
        <div className="ak-statusbar__item">
          <span style={{ color: isAuthed ? "var(--green)" : "rgba(255,255,255,0.45)" }}>
            {isAuthed ? "● cloud" : "● local"}
          </span>
        </div>

        <div className="ak-statusbar__item ak-statusbar__item--right ak-statusbar__item--muted" title={mode?.vault_path ?? ""}>
          <span style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {mode?.vault_path ?? "~/akaion-brain"}
          </span>
        </div>
        {noteCount !== null && (
          <>
            <div className="ak-statusbar__sep" />
            <div className="ak-statusbar__item ak-statusbar__item--muted">
              <span>{noteCount} {noteCount === 1 ? "note" : "notes"}</span>
            </div>
          </>
        )}
        <div className="ak-statusbar__sep" />
        <div className="ak-statusbar__item ak-statusbar__item--muted">
          <span>Annona v0.1.0</span>
        </div>
      </footer>
    </div>
  );
}
