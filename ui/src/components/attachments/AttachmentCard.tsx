import { useState } from "react"
import { AttachmentInfo, THUMBNAIL_URL } from "../../api/kernel"
import { FamilyGlyph } from "./glyphs"

/**
 * One attached file, as the operator sees it before anything has run.
 *
 * The card carries four facts and no ornament:
 *
 * 1. **What it is** — a real thumbnail when the file has a face (a photo, a
 *    video frame, the first page of a PDF, a DICOM slice), a drawn glyph when
 *    it does not. Recognition is the job: people attach the wrong file, and a
 *    letterhead tells them so faster than a filename.
 * 2. **What it holds** — pages, duration, sheets, invoice number, the name on
 *    the signing certificate. One line, from the reader that already parsed it.
 * 3. **What class it carries** — assigned by the policy at intake, which is the
 *    earliest moment that fact exists. This is the product, on the card.
 * 4. **What is wrong with it** — a missing OCR binary, an unverified signature,
 *    a path the policy will not let a tool read. Never hidden behind a toast.
 *
 * Clicking opens the first few hundred characters the reader actually got. It
 * is the answer to "is this really the file I think it is" and it costs nothing,
 * because the preview was extracted at intake.
 */

type Props = {
  a: AttachmentInfo
  onRemove?: () => void
  compact?: boolean
}

function size(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export function AttachmentCard({ a, onRemove, compact }: Props) {
  const [open, setOpen] = useState(false)
  const [thumbFailed, setThumbFailed] = useState(false)
  const showThumb = a.thumbnail && !thumbFailed && a.id

  const trouble = !a.readable ? a.reason : a.warnings[0]
  const facts = [a.headline, size(a.bytes)].filter(Boolean).join(" · ")

  return (
    <div
      className={`an-card ${compact ? "an-card--compact" : ""} ${a.readable ? "" : "an-card--blocked"}`}
      data-class={a.class || "unknown"}
    >
      <button
        className="an-card__body"
        onClick={() => !compact && a.preview && setOpen(!open)}
        title={a.path}
        aria-expanded={open}
      >
        <span className="an-card__face">
          {showThumb ? (
            <img
              src={THUMBNAIL_URL(a.id)}
              alt=""
              loading="lazy"
              onError={() => setThumbFailed(true)}
            />
          ) : (
            <FamilyGlyph family={a.family} />
          )}
        </span>

        <span className="an-card__text">
          <span className="an-card__name">{a.name}</span>
          <span className="an-card__facts">
            <span className="an-card__format">{a.format || a.family}</span>
            {facts && <span className="an-card__dim">{facts}</span>}
          </span>
        </span>

        {a.class && (
          <span className={`an-card__class an-card__class--${a.class}`}>{a.class}</span>
        )}
      </button>

      {onRemove && (
        <button className="an-card__x" onClick={onRemove} aria-label={`remove ${a.name}`}>
          <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden>
            <path
              d="M2.5 2.5 9.5 9.5M9.5 2.5 2.5 9.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
            />
          </svg>
        </button>
      )}

      {trouble && !compact && (
        <div className={`an-card__note ${a.readable ? "" : "an-card__note--bad"}`}>
          {trouble}
          {a.fix && <pre className="an-card__fix">{a.fix}</pre>}
        </div>
      )}

      {open && !compact && a.preview && (
        <pre className="an-card__preview">{a.preview}</pre>
      )}
    </div>
  )
}
