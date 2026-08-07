import { useState, useEffect, useCallback, useRef, useMemo } from "react"
import CodeMirror from "@uiw/react-codemirror"
import { markdown } from "@codemirror/lang-markdown"
import { oneDark } from "@codemirror/theme-one-dark"
import { brain, BrainNote } from "../../api/runner"
import { PlusIcon, SearchIcon, CloudUpIcon, TrashIcon, TagIcon, ClusterIcon, BrainIcon } from "../ui/Icons"
import { API_ORIGIN } from "../../api/base";

// ── helpers ─────────────────────────────────────────────────────────────────

type SyncFilter = "all" | "local_only" | "pending_sync" | "synced" | "sync_error"

function stripMarkdown(s: string): string {
  // Best-effort plain-text preview: drop common markdown noise.
  return s
    .replace(/^#+\s+/gm, "")
    .replace(/[*_`~>]/g, "")
    .replace(/!\[[^\]]*\]\([^)]+\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/\n+/g, " ")
    .trim()
}

function relativeTime(iso: string): string {
  if (!iso) return ""
  const now = Date.now()
  const then = new Date(iso).getTime()
  if (isNaN(then)) return ""
  const diff = Math.max(0, now - then)
  const min = Math.floor(diff / 60_000)
  if (min < 1) return "ora"
  if (min < 60) return `${min}m fa`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h}h fa`
  const d = Math.floor(h / 24)
  if (d === 1) return "ieri"
  if (d < 7) return `${d} giorni fa`
  if (d < 30) return `${Math.floor(d / 7)} sett. fa`
  if (d < 365) return `${Math.floor(d / 30)} mesi fa`
  return `${Math.floor(d / 365)} anni fa`
}

const STATUS_DOT_CLASS: Record<string, string> = {
  local_only:   "ak-note-card__status-dot--local",
  pending_sync: "ak-note-card__status-dot--pending",
  synced:       "ak-note-card__status-dot--synced",
  sync_error:   "ak-note-card__status-dot--error",
}

const STATUS_LABEL: Record<string, string> = {
  local_only:   "local",
  pending_sync: "pending",
  synced:       "synced",
  sync_error:   "error",
}

// Legacy sync-pill kept for editor toolbar
function SyncPill({ status }: { status: string }) {
  return <span className={`sync-pill ${status}`}>{STATUS_LABEL[status] ?? status}</span>
}

// ── Note list ──────────────────────────────────────────────────────────────

function NoteCard({ note, selected, onSelect }: {
  note: BrainNote
  selected: boolean
  onSelect: (id: string) => void
}) {
  const preview = useMemo(() => stripMarkdown(note.content).slice(0, 120), [note.content])
  const dotClass = STATUS_DOT_CLASS[note.sync_status] ?? STATUS_DOT_CLASS.local_only
  const label    = STATUS_LABEL[note.sync_status] ?? note.sync_status
  return (
    <div
      className={`ak-note-card ${selected ? "selected" : ""}`}
      onClick={() => onSelect(note.id)}
    >
      <div className="ak-note-card__title">{note.title || "Untitled"}</div>
      <div className="ak-note-card__preview">
        {preview || <span style={{ color: "rgba(255,255,255,0.30)" }}>Nessun contenuto…</span>}
      </div>
      <div className="ak-note-card__footer">
        <span className="ak-note-card__time">{relativeTime(note.updated_at)}</span>
        <span className="ak-note-card__status">
          <span className={`ak-note-card__status-dot ${dotClass}`} />
          {label}
        </span>
      </div>
      {note.tags.length > 0 && (
        <div className="ak-note-card__tags">
          {note.tags.slice(0, 4).map((t) => (
            <span className="ak-note-tag" key={t}>#{t}</span>
          ))}
          {note.tags.length > 4 && (
            <span className="ak-note-tag" style={{ opacity: 0.6 }}>+{note.tags.length - 4}</span>
          )}
        </div>
      )}
    </div>
  )
}

function NoteList({ notes, selected, onSelect }: {
  notes: BrainNote[]
  selected: string | null
  onSelect: (id: string) => void
}) {
  return (
    <div className="ak-note-list">
      {notes.map((n) => (
        <NoteCard key={n.id} note={n} selected={selected === n.id} onSelect={onSelect} />
      ))}
    </div>
  )
}

// ── Editor ─────────────────────────────────────────────────────────────────

function NoteEditor({ note, onSave, onMarkSync, onDelete }: {
  note: BrainNote
  onSave: (id: string, title: string, content: string, tags: string[]) => void
  onMarkSync: (id: string) => void
  onDelete: (id: string) => void
}) {
  const [title, setTitle]       = useState(note.title)
  const [content, setContent]   = useState(note.content)
  const [tagInput, setTagInput] = useState(note.tags.join(", "))
  const [dirty, setDirty]       = useState(false)
  const saveTimer               = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    setTitle(note.title)
    setContent(note.content)
    setTagInput(note.tags.join(", "))
    setDirty(false)
  }, [note.id])

  const triggerSave = useCallback((t: string, c: string, tg: string) => {
    if (saveTimer.current) clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(() => {
      const tags = tg.split(",").map((x) => x.trim()).filter(Boolean)
      onSave(note.id, t, c, tags)
      setDirty(false)
    }, 800)
  }, [note.id, onSave])

  const handleTitle   = (v: string) => { setTitle(v);    setDirty(true); triggerSave(v, content, tagInput) }
  const handleContent = (v: string) => { setContent(v);  setDirty(true); triggerSave(title, v, tagInput) }
  const handleTags    = (v: string) => { setTagInput(v); setDirty(true); triggerSave(title, content, v) }

  return (
    <div className="ak-editor-wrap">
      <div className="editor-toolbar" style={{ borderBottom: "1px solid rgba(255,255,255,0.04)", paddingBottom: 10, marginBottom: 14 }}>
        <input
          className="ak-editor-title"
          value={title}
          onChange={(e) => handleTitle(e.target.value)}
          placeholder="Note title..."
        />
        <div className="flex gap-2 items-center" style={{ flexShrink: 0 }}>
          {dirty && <span className="text-sub" style={{ fontSize: 11 }}>Saving…</span>}
          {note.sync_status === "local_only" || note.sync_status === "sync_error" ? (
            <button className="btn sm" onClick={() => onMarkSync(note.id)} title="Marca per sync">
              <CloudUpIcon size={12} /> Sync
            </button>
          ) : (
            <SyncPill status={note.sync_status} />
          )}
          <button className="btn sm" onClick={() => onDelete(note.id)} title="Delete note"
            style={{ color: "var(--red)", borderColor: "rgba(248,81,73,0.3)" }}>
            <TrashIcon size={12} />
          </button>
        </div>
      </div>

      {note.cot_cluster_name && (
        <div className="flex items-center gap-2 mb-3">
          <span className="cluster-badge"><ClusterIcon size={10} /> {note.cot_cluster_name}</span>
        </div>
      )}

      <div className="flex items-center gap-2 mb-3" style={{ borderBottom: "1px solid rgba(255,255,255,0.04)", paddingBottom: 10 }}>
        <TagIcon size={12} />
        <input
          className="field"
          style={{ background: "transparent", border: "none", padding: 0, color: "rgba(255,255,255,0.85)" }}
          value={tagInput}
          onChange={(e) => handleTags(e.target.value)}
          placeholder="tag1, tag2, tag3"
        />
      </div>

      <div className="editor-body cm-host" style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
        <CodeMirror
          value={content}
          theme={oneDark}
          extensions={[markdown()]}
          onChange={handleContent}
          basicSetup={{
            lineNumbers: false,
            foldGutter: false,
            highlightActiveLine: false,
            highlightActiveLineGutter: false,
          }}
          placeholder="Scrivi in markdown..."
          style={{ fontSize: 14 }}
        />
      </div>
    </div>
  )
}

// ── Main view ──────────────────────────────────────────────────────────────

export default function BrainView() {
  const [notes, setNotes]           = useState<BrainNote[]>([])
  const [selected, setSelected]     = useState<string | null>(null)
  const [search, setSearch]         = useState("")
  const [filter, setFilter]         = useState<SyncFilter>("all")
  const [loading, setLoading]       = useState(true)
  const [runnerDown, setRunnerDown] = useState(false)

  const selectedNote = notes.find((n) => n.id === selected) ?? null

  const load = useCallback(async () => {
    try {
      const data = await brain.list()
      setNotes(data)
      setRunnerDown(false)
    } catch (_) {
      setRunnerDown(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Notify shell of the latest note count (statusbar info).
  useEffect(() => {
    window.dispatchEvent(new CustomEvent("akaion:note-count", { detail: { count: notes.length } }))
  }, [notes.length])

  // ⌘N → create new note. (Shortcut hint visible in empty state.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") {
        e.preventDefault()
        handleCreate()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleCreate = async () => {
    try {
      const note = await brain.create({ title: "New note", content: "", tags: [] })
      setNotes((prev) => [note, ...prev])
      setSelected(note.id)
    } catch { /* runner down */ }
  }

  const handleSave = async (id: string, title: string, content: string, tags: string[]) => {
    try {
      const updated = await brain.update(id, { title, content, tags })
      setNotes((prev) => prev.map((n) => (n.id === id ? updated : n)))
    } catch { /* ignore */ }
  }

  const handleMarkSync = async (id: string) => {
    try {
      const updated = await brain.markSync(id)
      setNotes((prev) => prev.map((n) => (n.id === id ? updated : n)))
    } catch { /* ignore */ }
  }

  const handleDelete = async (id: string) => {
    try {
      await brain.delete(id)
      setNotes((prev) => prev.filter((n) => n.id !== id))
      if (selected === id) setSelected(null)
    } catch { /* ignore */ }
  }

  // Counts per filter (header tabs + statusbar).
  const counts = useMemo(() => {
    return {
      all:          notes.length,
      local_only:   notes.filter((n) => n.sync_status === "local_only").length,
      pending_sync: notes.filter((n) => n.sync_status === "pending_sync").length,
      synced:       notes.filter((n) => n.sync_status === "synced").length,
      sync_error:   notes.filter((n) => n.sync_status === "sync_error").length,
    }
  }, [notes])

  // Filter + search (single pipeline).
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return notes.filter((n) => {
      if (filter !== "all" && n.sync_status !== filter) return false
      if (!q) return true
      return (
        n.title.toLowerCase().includes(q) ||
        n.content.toLowerCase().includes(q) ||
        n.tags.some((t) => t.toLowerCase().includes(q))
      )
    })
  }, [notes, search, filter])

  return (
    <>
      <div className="ak-view-header">
        <div>
          <div className="ak-view-title">Brain</div>
          <div className="ak-view-sub">
            {counts.all} {counts.all === 1 ? "note" : "notes"} · {counts.synced} synced
          </div>
        </div>
        <button className="ak-pill-cta" onClick={handleCreate} aria-label="New note">
          <PlusIcon size={14} /> New note
        </button>
      </div>

      {/* Filter tabs */}
      {!runnerDown && !loading && (
        <div className="ak-filter-tabs" role="tablist">
          <FilterTab id="all"          active={filter} onClick={setFilter} label="All"        count={counts.all} />
          <FilterTab id="local_only"   active={filter} onClick={setFilter} label="Locali"       count={counts.local_only}   dot="local"   />
          <FilterTab id="pending_sync" active={filter} onClick={setFilter} label="Pending"      count={counts.pending_sync} dot="pending" />
          <FilterTab id="synced"       active={filter} onClick={setFilter} label="Sincronizzate" count={counts.synced}      dot="synced"  />
          <FilterTab id="sync_error"   active={filter} onClick={setFilter} label="Errors"       count={counts.sync_error}   dot="error"   />
        </div>
      )}

      {runnerDown ? (
        <div className="view-body">
          <div className="card">
            <p className="text-muted">Daemon not reachable on <code>{API_ORIGIN.replace(/^https?:\/\//, "")}</code>.</p>
            <p className="text-sub" style={{ marginTop: 4, fontSize: 12 }}>Start the runner from the status bar.</p>
          </div>
        </div>
      ) : loading ? (
        <div className="view-body"><p className="text-sub">Loading…</p></div>
      ) : (
        <div className="brain-split" style={{ flex: 1, overflow: "hidden" }}>
          {/* List panel */}
          <div className="brain-list-panel">
            <div className="ak-search-wrap">
              <span className="ak-search-wrap__icon"><SearchIcon size={13} /></span>
              <input
                className="ak-search-input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search notes…"
              />
            </div>
            {filtered.length === 0 ? (
              <div className="ak-empty" style={{ padding: "32px 16px" }}>
                <div style={{ fontSize: 12, color: "rgba(255,255,255,0.5)" }}>
                  {search.trim() ? "No results" : "No notes in this view"}
                </div>
              </div>
            ) : (
              <NoteList notes={filtered} selected={selected} onSelect={setSelected} />
            )}
          </div>

          {/* Editor panel */}
          <div className="brain-editor-panel">
            {selectedNote ? (
              <NoteEditor
                note={selectedNote}
                onSave={handleSave}
                onMarkSync={handleMarkSync}
                onDelete={handleDelete}
              />
            ) : (
              <div className="ak-empty">
                <span className="ak-empty__icon"><BrainIcon size={64} /></span>
                <h2 className="ak-empty__title">
                  {counts.all === 0 ? "Start writing" : "No note selected"}
                </h2>
                <p className="ak-empty__subtitle">
                  {counts.all === 0
                    ? "Create your first note — it lives in the local vault on this machine."
                    : "Pick a note from the list, or create a new one."}
                </p>
                <button className="ak-pill-cta" onClick={handleCreate} style={{ marginTop: 4 }}>
                  <PlusIcon size={14} /> New note
                </button>
                <div className="ak-empty__hint">
                  <kbd>⌘N</kbd> new note &nbsp;·&nbsp; <kbd>⌘K</kbd> search <span style={{ opacity: 0.6 }}>(soon)</span>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}

// ── Filter tab ──────────────────────────────────────────────────────────────

type DotKind = "local" | "pending" | "synced" | "error"

function FilterTab({ id, active, onClick, label, count, dot }: {
  id: SyncFilter
  active: SyncFilter
  onClick: (id: SyncFilter) => void
  label: string
  count: number
  dot?: DotKind
}) {
  return (
    <button
      className={`ak-filter-tab ${active === id ? "active" : ""}`}
      onClick={() => onClick(id)}
      role="tab"
      aria-selected={active === id}
    >
      {dot && <span className={`ak-filter-tab__dot ak-filter-tab__dot--${dot}`} />}
      <span>{label}</span>
      <span className="ak-filter-tab__count">{count}</span>
    </button>
  )
}
