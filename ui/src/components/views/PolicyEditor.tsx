/**
 * Editing the policy from the window.
 *
 * The Perimeter view could show the policy and not change it, and the answer to
 * "how do I change this" was: open `~/.annona/policy.yaml` in an editor. Most
 * people do not, which is a worse outcome than it sounds — a perimeter nobody
 * adjusts stops describing what anybody wants, and the failure mode of that is
 * the tool being switched off, or widened once in a hurry and never narrowed.
 *
 * Two modes over the same file:
 *
 * - **Fields** — the things the tables show: which substrates a class may run
 *   on, what happens when none is available, what each tool may touch. Saved as
 *   a document; the daemon puts the explanatory header back.
 * - **Text** — the file itself. The only way to change something the fields do
 *   not cover, and the only way to keep comments written inside the body. The
 *   editor switches to it on its own when the file has such comments, rather
 *   than offering a save that would delete them.
 *
 * Nothing here validates. The daemon parses the replacement before it writes,
 * and its refusal carries the sentence naming the offending key — reimplementing
 * that check in TypeScript would produce a second, subtly different opinion
 * about what a valid policy is, and the wrong one would be the one that let a
 * bad document through.
 */
import { useEffect, useState } from "react"
import { kernel, KernelError, PolicySource } from "../../api/kernel"

interface Props {
  onSaved: () => void
  onCancel: () => void
}

type Mode = "fields" | "text"

const UNAVAILABLE = ["hold", "queue", "brief", "redact"]
const PREFER = ["privacy", "cost", "latency", "quality"]

/** Comma-separated text ⇄ list, for the fields that are lists of globs. */
const toList = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean)
const toText = (xs: unknown) => (Array.isArray(xs) ? xs.join(", ") : "")

export default function PolicyEditor({ onSaved, onCancel }: Props) {
  const [source, setSource] = useState<PolicySource | null>(null)
  const [doc, setDoc]       = useState<any>(null)
  const [text, setText]     = useState("")
  const [mode, setMode]     = useState<Mode>("fields")
  const [error, setError]   = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    kernel
      .policySource()
      .then((s) => {
        if (cancelled) return
        setSource(s)
        setDoc(structuredClone(s.document))
        setText(s.text)
        // A structured save would drop those comments. Start where it will not.
        if (s.body_has_comments) setMode("text")
      })
      .catch((e) => !cancelled && setError(e?.detail ?? e?.message ?? "Could not read the policy"))
    return () => { cancelled = true }
  }, [])

  const save = async () => {
    if (!source) return
    setSaving(true)
    setError(null)
    try {
      await kernel.replacePolicy({
        ...(mode === "text" ? { yaml: text } : { document: doc }),
        expected_digest: source.digest,
      })
      onSaved()
    } catch (e) {
      const err = e as KernelError
      setError(err?.detail ?? "Could not save the policy")
    } finally {
      setSaving(false)
    }
  }

  if (error && !source) return <div className="an-panel"><div className="an-edit__error">{error}</div></div>
  if (!source || !doc) return <div className="an-panel"><div className="an-dim">Reading the policy…</div></div>

  const substrateIds: string[] = (doc.substrates ?? []).map((s: any) => String(s.id))

  /** Replace one substrate/rule/tool in place, immutably. */
  const patch = (path: (string | number)[], value: unknown) => {
    const next = structuredClone(doc)
    let node = next
    for (const key of path.slice(0, -1)) node = node[key]
    node[path[path.length - 1]] = value
    setDoc(next)
  }

  return (
    <div className="an-panel an-edit">
      <div className="an-edit__bar">
        <div className="an-edit__modes">
          <button
            className={`an-edit__mode ${mode === "fields" ? "on" : ""}`}
            onClick={() => setMode("fields")}
            disabled={source.body_has_comments}
            title={
              source.body_has_comments
                ? "This file has comments in the body. Saving from fields would delete them."
                : undefined
            }
          >
            Fields
          </button>
          <button
            className={`an-edit__mode ${mode === "text" ? "on" : ""}`}
            onClick={() => { setText(source.text); setMode("text") }}
          >
            Text
          </button>
        </div>
        <div className="an-edit__actions">
          <button className="an-edit__cancel" onClick={onCancel} disabled={saving}>Cancel</button>
          <button className="ak-btn-primary an-edit__save" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save policy"}
          </button>
        </div>
      </div>

      {source.body_has_comments && mode === "text" && (
        <div className="an-edit__note">
          This file has comments in the body, so it is edited as text — a save
          from the fields would rewrite the document and delete them.
        </div>
      )}

      {error && <div className="an-edit__error">{error}</div>}

      {mode === "text" ? (
        <textarea
          className="an-edit__text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
        />
      ) : (
        <>
          <h4 className="an-h4">Rules <span>first match wins</span></h4>
          <table className="an-table an-edit__table">
            <thead>
              <tr><th>class</th><th>may run on</th><th>if unavailable</th><th>prefers</th></tr>
            </thead>
            <tbody>
              {(doc.rules ?? []).map((rule: any, i: number) => (
                <tr key={i}>
                  <td className="an-edit__fixed">{rule?.match?.class ?? "—"}</td>
                  <td>
                    {/* Checkboxes rather than free text: a rule may only name a
                        substrate the policy declares, and the daemon refuses the
                        document if it does not. Offering the real list turns
                        that refusal into something that cannot happen. */}
                    <div className="an-edit__checks">
                      {substrateIds.map((id) => {
                        const on = (rule.allow ?? []).includes(id)
                        return (
                          <label key={id} className={`an-edit__check ${on ? "on" : ""}`}>
                            <input
                              type="checkbox"
                              checked={on}
                              onChange={() =>
                                patch(
                                  ["rules", i, "allow"],
                                  on
                                    ? (rule.allow ?? []).filter((x: string) => x !== id)
                                    : [...(rule.allow ?? []), id],
                                )
                              }
                            />
                            {id}
                          </label>
                        )
                      })}
                    </div>
                  </td>
                  <td>
                    <select
                      value={rule.on_unavailable ?? "hold"}
                      onChange={(e) => patch(["rules", i, "on_unavailable"], e.target.value)}
                    >
                      {UNAVAILABLE.map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  </td>
                  <td>
                    <select
                      value={rule.prefer ?? "privacy"}
                      onChange={(e) => patch(["rules", i, "prefer"], e.target.value)}
                    >
                      {PREFER.map((v) => <option key={v} value={v}>{v}</option>)}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4 className="an-h4">Substrates</h4>
          <table className="an-table an-edit__table">
            <thead>
              <tr><th>id</th><th>model</th><th>endpoint</th><th>jurisdiction</th><th>up to</th></tr>
            </thead>
            <tbody>
              {(doc.substrates ?? []).map((s: any, i: number) => (
                <tr key={i}>
                  <td className="an-edit__fixed"><code>{s.id}</code></td>
                  <td>
                    <input
                      value={s.model ?? ""}
                      onChange={(e) => patch(["substrates", i, "model"], e.target.value)}
                    />
                  </td>
                  <td>
                    <input
                      value={s.endpoint ?? ""}
                      onChange={(e) => patch(["substrates", i, "endpoint"], e.target.value)}
                    />
                  </td>
                  <td>
                    <input
                      value={s.jurisdiction ?? ""}
                      onChange={(e) => patch(["substrates", i, "jurisdiction"], e.target.value)}
                    />
                  </td>
                  <td>
                    {/* The field the whole product turns on, so it is a list of
                        the three real values rather than a text box somebody can
                        typo into something the parser rejects. */}
                    <select
                      value={s.max_class ?? "internal"}
                      onChange={(e) => patch(["substrates", i, "max_class"], e.target.value)}
                    >
                      {["restricted", "internal", "public"].map((v) => (
                        <option key={v} value={v}>{v}</option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4 className="an-h4">Tools <span>default deny · empty means the tool does not run</span></h4>
          <table className="an-table an-edit__table">
            <thead><tr><th>tool</th><th>may touch</th></tr></thead>
            <tbody>
              {Object.entries(doc.tools?.allow ?? {}).map(([tool, paths]) => (
                <tr key={tool}>
                  <td className="an-edit__fixed"><code>{tool}</code></td>
                  <td>
                    <input
                      value={toText(paths)}
                      placeholder="~/Documents/**, ~/Downloads/**"
                      onChange={(e) => patch(["tools", "allow", tool], toList(e.target.value))}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4 className="an-h4">Never, for any tool</h4>
          <input
            className="an-edit__wide"
            value={toText(doc.tools?.deny_paths)}
            onChange={(e) => patch(["tools", "deny_paths"], toList(e.target.value))}
          />
          <div className="an-edit__note">
            This list wins over every allow above it. Emptying it does not make
            anything work that did not; it removes the guarantee that credentials
            and keys are unreadable whatever else a rule permits.
          </div>
        </>
      )}

      <div className="an-edit__foot">
        Saving keeps a copy of the current file beside it, and records the change
        in the ledger — <code>annona audit</code> and this view's ledger tab both
        show it. An invalid policy is refused before anything is written.
      </div>
    </div>
  )
}
