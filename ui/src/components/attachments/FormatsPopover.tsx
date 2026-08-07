import { FormatSupport } from "../../api/kernel"
import { FamilyGlyph } from "./glyphs"

/**
 * "What can this machine read?" — asked of the daemon, not of a marketing page.
 *
 * Half the readers depend on something optional, so the answer differs from
 * install to install. A window that listed formats from a constant would be
 * wrong on the first machine that lacks ffmpeg, and wrong in the direction that
 * costs the most: offering to read a recording and coming back with nothing.
 *
 * A family that is not ready shows the command that makes it ready. That is the
 * whole feature — the list is not a boast, it is a to-do.
 */

const ORDER = [
  "document", "spreadsheet", "presentation", "text", "structured",
  "signed", "mail", "archive", "image", "audio", "video", "medical",
]

const LABELS: Record<string, string> = {
  document: "Documents",
  spreadsheet: "Spreadsheets",
  presentation: "Presentations",
  text: "Text and code",
  structured: "XML · FatturaPA · iCal",
  signed: "Signed .p7m envelopes",
  mail: "Saved email",
  archive: "Archives",
  image: "Images",
  audio: "Audio",
  video: "Video",
  medical: "DICOM",
}

const EXTRAS: Record<string, string> = {
  ocr: "OCR on images",
  transcription: "Local transcription",
  heic: "HEIC photos",
  outlook_msg: "Outlook .msg",
  pdf_rasteriser: "OCR on scanned PDFs",
}

export function FormatsPopover({ formats, onClose }: { formats: FormatSupport; onClose: () => void }) {
  const families = ORDER.filter((key) => formats.families[key])

  return (
    <>
      <div className="an-pop__scrim" onClick={onClose} />
      <div className="an-pop" role="dialog" aria-label="Readable formats">
        <div className="an-pop__head">
          <span>What this machine can read</span>
          <span className="an-pop__count">{formats.extensions.length} extensions</span>
        </div>

        <div className="an-pop__grid">
          {families.map((key) => {
            const entry = formats.families[key]
            return (
              <div key={key} className={`an-pop__row ${entry.ready ? "" : "an-pop__row--off"}`}>
                <span className="an-pop__glyph"><FamilyGlyph family={key} /></span>
                <span className="an-pop__label">{LABELS[key] ?? key}</span>
                {entry.ready
                  ? <span className="an-pop__ok">ready</span>
                  : <code className="an-pop__install">{entry.install}</code>}
              </div>
            )
          })}
        </div>

        <div className="an-pop__extras">
          {Object.entries(EXTRAS).map(([key, label]) => (
            <span key={key} className={formats.extras[key] ? "on" : "off"}>
              {formats.extras[key] ? "●" : "○"} {label}
            </span>
          ))}
        </div>

        <div className="an-pop__foot">
          <div>
            Files stay on this machine, in <code>{formats.inbox_short}</code>
          </div>
          <div className={formats.vision ? "" : "an-pop__foot--dim"}>
            {formats.vision
              ? "A substrate in this policy can look at images."
              : "No substrate can look at images: they will be read as text."}
          </div>
        </div>
      </div>
    </>
  )
}
