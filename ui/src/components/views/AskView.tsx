import { useEffect, useRef, useState } from "react"
import {
  kernel,
  AskResult,
  AttachmentInfo,
  Decision,
  FormatSupport,
  KernelError,
} from "../../api/kernel"
import { AttachmentCard } from "../attachments/AttachmentCard"
import { FormatsPopover } from "../attachments/FormatsPopover"
import Working from "./Working"
import { EgressPanel } from "../egress/EgressPanel"

/**
 * Ask — the conversation, with the placement attached to every answer.
 *
 * This is the only screen where the product's claim is visible while it is
 * being made: you type a request, and what comes back is the answer *and* where
 * it was computed, under which rule, with everything the run refused to do. A
 * chat that returned only prose would be a worse version of every other chat.
 *
 * A held run is not an error state here. It renders as an answer of its own —
 * the substrate was not permitted, nothing left the machine, and that is the
 * outcome the operator is paying for.
 *
 * Attachments are held to the same standard. A dropped file is stored on this
 * machine and read by a gated tool, so it appears in the decisions list like
 * every other read — and its card carries the class the perimeter gave it
 * before a single turn has run, which is the earliest moment that fact exists.
 * The line above the composer is the one sentence this product exists to be
 * able to say truthfully, said at the moment it matters.
 */

type Exchange = {
  id: number
  prompt: string
  attachments: AttachmentInfo[]
  result?: AskResult
  error?: string
  ms?: number
}

function classTone(klass?: string): string {
  if (klass === "restricted") return "var(--red)"
  if (klass === "internal") return "var(--yellow)"
  return "var(--text-muted)"
}

function Chip({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <span className="an-chip">
      <span className="an-chip__k">{label}</span>
      <span className="an-chip__v" style={tone ? { color: tone } : undefined}>{value}</span>
    </span>
  )
}

function DecisionRow({ d }: { d: Decision }) {
  const held = d.outcome === "held" || d.outcome === "denied"
  return (
    <div className="an-decision">
      <span className={`an-decision__dot ${held ? "held" : ""}`} />
      <code className="an-decision__step">{d.step_id}</code>
      <span className="an-decision__kind">{d.kind}</span>
      <span className="an-decision__outcome" style={{ color: held ? "var(--red)" : "var(--green)" }}>
        {d.outcome}
      </span>
      <span className="an-decision__class" style={{ color: classTone(d.class) }}>{d.class}</span>
      <span className="an-decision__where">{d.substrate || "—"}</span>
      <span className="an-decision__why">
        {String(d.detail?.reason ?? d.rule_id ?? "")}
      </span>
    </div>
  )
}

function Answer({ x }: { x: Exchange }) {
  const [open, setOpen] = useState(false)
  const r = x.result

  if (x.error) {
    return (
      <div className="an-answer an-answer--error">
        <div className="an-answer__text">{x.error}</div>
      </div>
    )
  }
  // Still running. The ledger is written as the run goes, so the wait shows
  // the same decisions the finished answer will carry — read early, not
  // narrated separately.
  if (!r) return <div className="an-answer an-answer--pending"><Working /></div>

  const held = r.placement?.outcome === "held"
  const denied = r.tool_calls.filter((t) => t.error)

  return (
    <div className="an-answer">
      {held ? (
        <div className="an-held">
          <b>Held.</b> Nothing ran, and nothing left this machine.
          {r.placement?.reason ? <> {r.placement.reason}</> : null}
        </div>
      ) : (
        <div className="an-answer__text">{r.response || "(no answer)"}</div>
      )}

      <div className="an-answer__meta">
        {r.enforced && r.placement ? (
          <>
            <Chip label="class" value={r.placement.class} tone={classTone(r.placement.class)} />
            <Chip
              label="ran on"
              value={r.placement.substrate || "—"}
              tone={held ? "var(--red)" : "var(--green)"}
            />
          </>
        ) : (
          <Chip label="perimeter" value="not enforcing" tone="var(--yellow)" />
        )}
        <Chip label="turns" value={String(r.iterations)} />
        {x.ms !== undefined && <Chip label="took" value={`${(x.ms / 1000).toFixed(1)}s`} />}
        {/* Read versus seen. On a policy with no vision substrate every
            attachment is read as text, and saying "3 read · 0 shown" is more
            honest than a paperclip that implies the model looked at them. */}
        {r.attachments && r.attachments.named > 0 && (
          <Chip
            label="files"
            value={`${r.attachments.named} read · ${r.attachments.shown} shown`}
          />
        )}
        {denied.length > 0 && (
          <Chip label="refused" value={`${denied.length} tool call${denied.length > 1 ? "s" : ""}`} tone="var(--red)" />
        )}
        {r.decisions.length > 0 && (
          <button className="an-link" onClick={() => setOpen(!open)}>
            {open ? "hide" : "show"} {r.decisions.length} decision{r.decisions.length > 1 ? "s" : ""}
          </button>
        )}
      </div>

      {/* Above the decisions, not inside them: what crossed is the fact an
          operator most needs and least expects to be shown. */}
      <EgressPanel crossings={r.egress ?? []} sealed={r.sealed} />

      {open && (
        <div className="an-decisions">
          {r.decisions.map((d) => <DecisionRow key={d.seq} d={d} />)}
        </div>
      )}
    </div>
  )
}

export default function AskView() {
  const [prompt, setPrompt] = useState("")
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<Exchange[]>([])
  const [enforcing, setEnforcing] = useState<boolean | null>(null)
  const [policyNote, setPolicyNote] = useState("")
  const [attached, setAttached] = useState<AttachmentInfo[]>([])
  const [taking, setTaking] = useState<string[]>([])
  const [dragging, setDragging] = useState(false)
  const [formats, setFormats] = useState<FormatSupport | null>(null)
  const [showFormats, setShowFormats] = useState(false)
  const [attachError, setAttachError] = useState("")
  const [escalate, setEscalate] = useState(false)
  const endRef = useRef<HTMLDivElement | null>(null)
  const picker = useRef<HTMLInputElement | null>(null)
  const depth = useRef(0)

  useEffect(() => {
    kernel.status()
      .then((s) => {
        setEnforcing(s.enforcing)
        setPolicyNote(s.enforcing ? `${s.substrates} substrates · ${s.rules} rules` : s.reason)
      })
      .catch(() => { setEnforcing(false); setPolicyNote("the daemon did not answer") })

    kernel.formats().then(setFormats).catch(() => setFormats(null))
  }, [])

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }) }, [history, busy])

  const take = async (files: FileList | File[]) => {
    const list = Array.from(files)
    if (!list.length) return
    setAttachError("")
    setTaking((n) => [...n, ...list.map((f) => f.name)])
    for (const file of list) {
      try {
        const info = await kernel.attach(file)
        // The same file twice is the same attachment: the id is its digest.
        setAttached((current) => [...current.filter((a) => a.id !== info.id), info])
      } catch (e) {
        setAttachError(e instanceof KernelError ? e.detail : String(e))
      } finally {
        setTaking((n) => n.filter((name) => name !== file.name))
      }
    }
  }

  const send = async () => {
    const text = prompt.trim()
    if ((!text && !attached.length) || busy) return
    const id = Date.now()
    const files = attached
    setHistory((h) => [...h, { id, prompt: text, attachments: files }])
    setPrompt("")
    setAttached([])
    setBusy(true)
    const started = performance.now()
    try {
      const result = await kernel.ask(
        text || "Read the attached files and tell me what they are.",
        files.map((a) => a.path),
        { escalate },
      )
      const ms = performance.now() - started
      setHistory((h) => h.map((x) => (x.id === id ? { ...x, result, ms } : x)))
    } catch (e) {
      const detail = e instanceof KernelError ? e.detail : String(e)
      setHistory((h) => h.map((x) => (x.id === id ? { ...x, error: detail } : x)))
    } finally {
      setBusy(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send() }
  }

  // Screenshots are the most common attachment there is and they live on the
  // clipboard, not on disk. Pasting one attaches it instead of pasting nothing.
  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files)
    if (files.length) { e.preventDefault(); void take(files) }
  }

  const blocked = attached.filter((a) => !a.readable)
  const restricted = attached.filter((a) => a.class === "restricted").length
  const unseeable = !formats?.vision && attached.some((a) => a.media.length > 0)
  const showTray = attached.length > 0 || taking.length > 0 || Boolean(attachError)

  return (
    <>
      <div className="view-header">
        <div className="view-header-left">
          <div className="ak-view-title">Ask</div>
          <div className="ak-view-sub">
            {enforcing === null ? "…"
              : enforcing ? `Every turn is placed before it runs · ${policyNote}`
              : `No policy is enforcing — ${policyNote}`}
          </div>
        </div>
      </div>

      <div
        className="an-chat"
        onDragEnter={(e) => { e.preventDefault(); depth.current += 1; setDragging(true) }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={() => { depth.current -= 1; if (depth.current <= 0) setDragging(false) }}
        onDrop={(e) => {
          e.preventDefault()
          depth.current = 0
          setDragging(false)
          if (e.dataTransfer.files.length) void take(e.dataTransfer.files)
        }}
      >
        {dragging && (
          <div className="an-drop">
            <div className="an-drop__inner">
              <div className="an-drop__title">Lascia qui i file</div>
              <div className="an-drop__sub">
                Restano su questa macchina, in <code>{formats?.inbox_short ?? "~/Documents/Annona/Inbox"}</code>
                <br />e vengono letti sotto policy, come qualsiasi altra lettura.
              </div>
            </div>
          </div>
        )}

        <div className="an-chat__scroll">
          {history.length === 0 && (
            <div className="an-empty">
              <div className="an-empty__title">Ask it something, or drop a file in.</div>
              <div className="an-empty__sub">
                Documents, scans, signed .p7m invoices, email, archives, photos, recordings,
                DICOM. Every answer carries where it was computed and under which rule —
                and an attachment gets its class before a turn even starts.
              </div>
              {formats && (
                <button className="an-link an-empty__link" onClick={() => setShowFormats(true)}>
                  what this machine can read
                </button>
              )}
            </div>
          )}

          {history.map((x) => (
            <div key={x.id} className="an-turn">
              {x.attachments.length > 0 && (
                <div className="an-cards an-cards--sent">
                  {x.attachments.map((a) => <AttachmentCard key={a.id} a={a} compact />)}
                </div>
              )}
              {x.prompt && <div className="an-prompt">{x.prompt}</div>}
              <Answer x={x} />
            </div>
          ))}
          <div ref={endRef} />
        </div>

        {showTray && (
          <div className="an-tray">
            <div className="an-tray__head">
              <span className="an-tray__count">
                {attached.length} {attached.length === 1 ? "file" : "file"}
                {restricted > 0 && <> · <span className="an-tray__hot">{restricted} restricted</span></>}
              </span>
              {/* The claim, at the moment it is true. Not a banner: one line of
                  the same 11px the rest of this surface is written in. */}
              <span className="an-tray__vow">
                sono su questa macchina, in <code>{formats?.inbox_short ?? "~/Documents/Annona/Inbox"}</code>
              </span>
            </div>

            <div className="an-cards">
              {attached.map((a) => (
                <AttachmentCard
                  key={a.id}
                  a={a}
                  onRemove={() => setAttached((c) => c.filter((x) => x.id !== a.id))}
                />
              ))}
              {taking.map((name) => (
                <div key={name} className="an-card an-card--reading">
                  <div className="an-card__body">
                    <span className="an-card__face an-card__face--wait" />
                    <span className="an-card__text">
                      <span className="an-card__name">{name}</span>
                      <span className="an-card__facts"><span className="an-card__dim">lettura…</span></span>
                    </span>
                  </div>
                </div>
              ))}
            </div>

            {attachError && <div className="an-tray__note an-tray__note--bad">{attachError}</div>}

            {blocked.length > 0 && (
              /* The upload worked and the policy still refuses the read. Saying
                 exactly that, with the lines to add, beats a run that comes back
                 with "permission denied" after the fact. */
              <div className="an-tray__note an-tray__note--bad">
                {blocked.length === 1 ? "Un file è" : `${blocked.length} file sono`} fuori da ciò che
                document_reader può leggere: la lettura verrà rifiutata.
              </div>
            )}

            {unseeable && (
              <div className="an-tray__note">
                Nessun substrato di questa policy può guardare un'immagine: i pixel verranno letti
                come testo (OCR e metadati), non visti.
              </div>
            )}
          </div>
        )}

        <div className="an-composer">
          <input
            ref={picker}
            type="file"
            multiple
            style={{ display: "none" }}
            onChange={(e) => { if (e.target.files) void take(e.target.files); e.target.value = "" }}
          />

          <div className="an-attach-wrap">
            <button
              className="an-attach"
              onClick={() => picker.current?.click()}
              onContextMenu={(e) => { e.preventDefault(); setShowFormats(true) }}
              disabled={busy}
              title="Allega file — restano su questa macchina e vengono letti sotto policy"
              aria-label="Allega file"
            >
              <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden>
                <path
                  d="M8 3.2v9.6M3.2 8h9.6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
            {formats && (
              <button
                className="an-attach__more"
                onClick={() => setShowFormats(true)}
                title="Cosa può leggere questa macchina"
                aria-label="Formati leggibili"
              >
                ?
              </button>
            )}
          </div>

          <button
            className={`an-escalate ${escalate ? "on" : ""}`}
            onClick={() => setEscalate(!escalate)}
            title={
              "Ask for the best substrate this policy already allows. " +
              "It adds none: material that may not leave still does not leave."
            }
          >
            ✦ best
          </button>
          <textarea
            className="an-composer__input"
            placeholder="Ask the kernel… (Enter to send, Shift+Enter for a new line)"
            value={prompt}
            rows={2}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={onKeyDown}
            onPaste={onPaste}
          />
          <button
            className="ak-pill-cta"
            onClick={send}
            disabled={busy || (!prompt.trim() && !attached.length)}
          >
            {busy ? "Running…" : "Send"}
          </button>
        </div>

        {showFormats && formats && (
          <FormatsPopover formats={formats} onClose={() => setShowFormats(false)} />
        )}
      </div>
    </>
  )
}
