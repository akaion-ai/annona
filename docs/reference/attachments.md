# Attachments

Drop a file into the Ask window, or name a path in a prompt. Either way the
kernel reads it the same way, and the read is a decision like any other.

## What happens to a file you attach

1. **It is stored.** The upload is written into an inbox directory on this
   machine — `~/Documents/Annona/Inbox` by default. It is a real folder you can
   open in Finder; nothing is held in a database and nothing is uploaded
   anywhere.
2. **It is classified immediately.** Before a single turn runs, the file is
   given a class by the policy — from its path, and from what a cheap preview
   of its content matches. The chip in the window shows it. An invoice with a
   codice fiscale in it comes back `restricted` the moment it lands.
3. **It is read by a tool.** The run calls `document_reader` on the path, which
   goes through the default-deny gate and into the ledger. There is no path by
   which attached material enters a model without a recorded decision.
4. **It may be *shown*, if something can see.** When the policy has a substrate
   with `vision: true`, images and PDFs are also attached to the turn as
   references and encoded by the adapter *after* placement. When it does not,
   they are read as text (OCR and metadata) and the answer says so: the chip
   reads `3 read · 0 shown`.

If the inbox is outside what `document_reader` may touch, the upload still
succeeds and the window tells you at once, with the lines to add:

```yaml
tools:
  allow:
    document_reader:
      - ~/Documents/Annona/Inbox/**
```

Move the inbox with `ANNONA_INBOX`, or in `config.yaml`:

```yaml
attachments:
  dir: ~/Pratiche/Inbox
  max_size_mb: 512
```

## What can be read

| Family | Formats | Needs |
|---|---|---|
| Documents | PDF, DOCX, ODT, RTF, EPUB | in the box |
| Spreadsheets | XLSX, XLSM, XLS, ODS, CSV | in the box |
| Presentations | PPTX, ODP | `python-pptx` |
| Text and code | TXT, MD, JSON, YAML, and ~40 source extensions | in the box |
| Structured | XML, **FatturaPA**, iCalendar | in the box |
| Signed envelopes | **P7M / CAdES**, P7S | `asn1crypto` or `openssl` |
| Email | EML, MSG | `extract-msg` for MSG |
| Archives | ZIP, TAR, TGZ | in the box |
| Images | PNG, JPEG, GIF, WEBP, TIFF, HEIC, AVIF | `pillow`, `pillow-heif` |
| Audio | MP3, WAV, M4A, FLAC, OGG, OPUS | `ffmpeg` |
| Video | MP4, MOV, MKV, WEBM, AVI | `ffmpeg` |
| Medical imaging | DICOM | `pydicom` |

Ask the daemon what *this* machine can do rather than trusting the table:

```bash
curl -s localhost:7070/api/kernel/formats | jq '.families | map_values(.ready)'
```

Install the extras you need:

```bash
pip install 'annona[formats]'   # presentations, p7m, Outlook mail, images, OCR
pip install 'annona[media]'     # local speech-to-text (faster-whisper)
pip install 'annona[medical]'   # DICOM
```

Three of these are worth their own paragraph.

**Signed invoices.** A `.p7m` is opened and the document inside it — almost
always a FatturaPA XML — is read and rendered as an invoice: who billed whom,
the lines, the VAT summary, the payment terms. The signature is **not**
verified, and every read says so. Opening an envelope and checking a
qualified-trust chain are different claims, and only one of them is being made.

**Recordings.** Duration, codecs and tags come from `ffprobe`. A transcript is
produced only if a local speech model is installed, and it is produced *on this
machine* — there is deliberately no code path here that sends audio to a
transcription API, because that would be exactly the egress this kernel exists
to prevent. Video also yields keyframes, for a substrate permitted to look at
them.

**DICOM.** The header is read as text — modality, study, and the patient
identifiers a DICOM file carries whether or not anyone thought about it. The
shipped policy classifies `**/*.dcm` as **restricted** for that reason, so a
study cannot be placed outside the machine even if a remote substrate is
registered later.

## Degraded reads are reported, never hidden

Every read returns `warnings`, and the window shows them. A scanned PDF says it
has no text layer instead of coming back empty; a format whose library is not
installed names the library and the install command; an archive that hit its
member ceiling says how many it did not open. The failure this avoids is the
one that costs the most: an answer confidently built on a document nothing
actually read.

## HTTP

```
GET    /api/kernel/formats             what this installation can read
POST   /api/kernel/attachments         multipart upload → stored file + class
GET    /api/kernel/attachments         what is in the inbox
DELETE /api/kernel/attachments/{id}    remove one
POST   /api/kernel/ask                 {"prompt": "...", "attachments": ["/abs/path", ...]}
```

`ask` takes **paths, not contents**. That is the whole design: an attachment is
a file the perimeter can reason about, not a payload that arrived beside it.
