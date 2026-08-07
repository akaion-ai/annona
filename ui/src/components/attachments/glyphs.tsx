/**
 * One mark per family of file.
 *
 * Drawn rather than emoji: an emoji is a different typeface on every machine,
 * renders in colour it did not choose, and reads as decoration. These are
 * 16-pixel line marks in the current text colour, which is what the rest of this
 * interface is made of.
 *
 * They appear only when there is nothing better to show. A photo shows the
 * photo, a video shows a frame, a PDF shows its first page — a glyph is the
 * fallback for the files that have no face.
 */

import type { ReactElement } from "react"

type Props = { family: string }

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.4,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
}

const PATHS: Record<string, ReactElement> = {
  document: (
    <>
      <path d="M4 2.5h5.5L13 6v7.5H4z" {...stroke} />
      <path d="M9.5 2.5V6H13" {...stroke} />
      <path d="M6 9h5M6 11h3.5" {...stroke} />
    </>
  ),
  spreadsheet: (
    <>
      <rect x="3" y="3" width="11" height="10.5" rx="1" {...stroke} />
      <path d="M3 6.5h11M6.8 6.5v7M3 10h11" {...stroke} />
    </>
  ),
  presentation: (
    <>
      <rect x="2.8" y="3" width="11.4" height="7.5" rx="1" {...stroke} />
      <path d="M8.5 10.5v3M6 13.5h5" {...stroke} />
    </>
  ),
  text: (
    <>
      <path d="M4 2.5h5.5L13 6v7.5H4z" {...stroke} />
      <path d="M6 7.5h5M6 9.7h5M6 11.9h3" {...stroke} />
    </>
  ),
  structured: (
    <>
      <path d="M6 4.5 3 8.25 6 12" {...stroke} />
      <path d="M11 4.5 14 8.25 11 12" {...stroke} />
      <path d="M9.4 3.5 7.6 13" {...stroke} />
    </>
  ),
  signed: (
    <>
      <path d="M8.5 2.5 13.5 4.7v3.6c0 2.6-2 4.6-5 5.7-3-1.1-5-3.1-5-5.7V4.7z" {...stroke} />
      <path d="M6.3 8.3 7.8 9.8l3-3.2" {...stroke} />
    </>
  ),
  mail: (
    <>
      <rect x="2.5" y="4" width="12" height="9" rx="1.2" {...stroke} />
      <path d="m2.9 5 5.6 4.2L14.1 5" {...stroke} />
    </>
  ),
  archive: (
    <>
      <rect x="2.5" y="4.2" width="12" height="9.3" rx="1.2" {...stroke} />
      <path d="M2.5 6.9h12M7.2 4.2v2.7M9.8 4.2v2.7M8.5 9.4v2.2" {...stroke} />
    </>
  ),
  image: (
    <>
      <rect x="2.5" y="3.4" width="12" height="10" rx="1.4" {...stroke} />
      <circle cx="6.2" cy="6.9" r="1.15" {...stroke} />
      <path d="m3.2 12 3.3-3.2 2.4 2.3 2.2-2 3.2 3" {...stroke} />
    </>
  ),
  audio: (
    <>
      <path d="M4 6.5v4M6.6 4.4v8.2M9.2 5.8v5.4M11.8 3.6v9.8" {...stroke} />
    </>
  ),
  video: (
    <>
      <rect x="2.4" y="4.2" width="8.6" height="8.4" rx="1.3" {...stroke} />
      <path d="m11 8.4 3.6-2.3v6l-3.6-2.3z" {...stroke} />
    </>
  ),
  medical: (
    <>
      <circle cx="8.5" cy="8.4" r="5.4" {...stroke} />
      <path d="M8.5 5.6v5.6M5.7 8.4h5.6" {...stroke} />
    </>
  ),
  missing: (
    <>
      <circle cx="8.5" cy="8.4" r="5.4" {...stroke} />
      <path d="M6.4 6.3 10.6 10.5M10.6 6.3 6.4 10.5" {...stroke} />
    </>
  ),
}

export function FamilyGlyph({ family }: Props) {
  return (
    <svg viewBox="0 0 17 17" width="17" height="17" aria-hidden>
      {PATHS[family] ?? PATHS.text}
    </svg>
  )
}
