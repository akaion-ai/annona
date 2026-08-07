/**
 * Where the daemon is, from the point of view of whoever is looking.
 *
 * This used to be `http://localhost:7070`, written out in four files. The
 * daemon takes a `--port`, and `annona run --port 7075` moved the server while
 * leaving the interface it was serving calling 7070 — so the page loaded,
 * rendered, and then said "Daemon stopped" while being served by the daemon it
 * could not find. Two instances on one machine, or anything behind a reverse
 * proxy, hit the same wall.
 *
 * When the page is served over http(s) from a host, the daemon *is* that host:
 * the interface ships inside the daemon, so same-origin is not a guess. The
 * Tauri shell is the one case where it is not — the window loads from
 * `tauri://localhost` (macOS/Linux) or `http://tauri.localhost` (Windows) and
 * the sidecar listens on 7070 — so those hostnames fall through to the default.
 *
 * VITE_ANNONA_API still wins, for `npm run dev` against a daemon started
 * separately, and for anyone pointing the interface at another machine.
 */

const DEFAULT_API = "http://localhost:7070";

function sameOrigin(): string {
  if (typeof window === "undefined") return DEFAULT_API;

  const { protocol, origin, hostname } = window.location;

  // The Tauri window: not served by the daemon, whatever the URL looks like.
  if (!protocol.startsWith("http")) return DEFAULT_API;
  if (hostname === "tauri.localhost") return DEFAULT_API;

  return origin;
}

export const API_ORIGIN: string = import.meta.env.VITE_ANNONA_API ?? sameOrigin();
