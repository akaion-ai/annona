"""
Brain Manager

Local persistence for notes:
- SQLite per metadata e indice full-text
- File markdown in <brain_dir>/notes/<id>.md

Struttura su disco:
  ~/akaion-brain/
  ├── notes/          ← <uuid>.md
  └── .akaion/
      └── index.db    ← SQLite
"""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from . import frontmatter
from .models import SYNC_ERROR, SYNC_LOCAL_ONLY, SYNC_PENDING, SYNC_SYNCED, Note, SyncStats, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    tags             TEXT NOT NULL DEFAULT '[]',   -- JSON array
    sync_status      TEXT NOT NULL DEFAULT 'local_only',
    cot_message_id   TEXT,
    cot_cluster_id   TEXT,
    cot_cluster_name TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    synced_at        TEXT,
    sync_error       TEXT
);

-- Note: the FTS5 table is NOT contentless. With `content=''` SQLite returns
-- NULL per le colonne (incluso `id UNINDEXED`), rendendo impossibili sia
-- JOIN su notes.id sia DELETE by id. Manteniamo il contenuto in-fts.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    id UNINDEXED,
    title,
    content
);
"""


# Words, not punctuation: unicode letters and digits, nothing else.
_FTS_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _fts_query(raw: str) -> str:
    """Turn what somebody typed into an expression FTS5 will actually accept.

    The query used to reach `MATCH` verbatim, which makes every FTS5 operator
    live ammunition against the person using the search box. Searching for
    `end-to-end` raised

        sqlite3.OperationalError: no such column: to

    because `-` is FTS5's NOT and `to` was read as a column name — a 500 from
    `GET /api/brain/search` for a perfectly ordinary word. Quotes, `*`, `:`,
    `(`, and `NEAR` all had their own version of this.

    A search box is not a query language. Each word is extracted and quoted as a
    literal, and the words are ANDed, which is what someone typing two words
    into a box means. Returns "" when nothing searchable was typed; callers
    treat that as no results rather than passing an empty MATCH to SQLite,
    which is itself a syntax error.
    """
    return " ".join(f'"{word}"' for word in _FTS_WORD.findall(raw))


def _now() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


class BrainManager:
    """Local note CRUD backed by a SQLite index."""

    def __init__(self, brain_dir: Path):
        self.brain_dir = Path(brain_dir).expanduser()
        self.notes_dir = self.brain_dir / "notes"
        self.db_path = self.brain_dir / ".akaion" / "index.db"

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._backfill_frontmatter()
        logger.info(f"BrainManager ready: {self.brain_dir}")

    def _migrate(self):
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ── Markdown helpers ──────────────────────────────────────────────────────

    def _note_path(self, note_id: str) -> Path:
        return self.notes_dir / f"{note_id}.md"

    def _write_md(self, note_id: str, content: str):
        """Write the body, preserving whatever frontmatter the note carries.

        Metadata is written by :meth:`_write_frontmatter`, which knows the note's
        fields. Here we only ever touch the body, so a hand-edited `project:` key
        survives a content update.
        """
        path = self._note_path(note_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        metadata, _ = frontmatter.parse(existing)
        path.write_text(frontmatter.dump(metadata, content), encoding="utf-8")

    def _read_md(self, note_id: str) -> str:
        """Read the body, without frontmatter.

        `Note.content` has always meant "the prose", and every caller and test
        depends on that. Frontmatter is metadata, and metadata belongs in the
        note's fields, not in the middle of its text.
        """
        p = self._note_path(note_id)
        if not p.exists():
            return ""
        _, body = frontmatter.parse(p.read_text(encoding="utf-8"))
        return body

    def _write_frontmatter(self, note_id: str) -> None:
        """Refresh a note's frontmatter from the index.

        Called after any change to title, tags or sync state, so the file on disk
        is always self-describing: delete the index and the vault still knows what
        each note is.
        """
        row = self._conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return

        path = self._note_path(note_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        custom, body = frontmatter.parse(existing)

        # Runner-owned fields win; anything a human added is carried through.
        metadata = {k: v for k, v in custom.items() if k not in frontmatter.FIELD_ORDER}
        metadata.update(
            {
                "id": row["id"],
                "title": row["title"],
                "tags": json.loads(row["tags"]),
                "created": row["created_at"],
                "updated": row["updated_at"],
                "sync": row["sync_status"],
                "synced_at": row["synced_at"],
                "cloud_message_id": row["cot_message_id"],
                "cloud_cluster_id": row["cot_cluster_id"],
                "cloud_cluster_name": row["cot_cluster_name"],
            }
        )
        path.write_text(frontmatter.dump(metadata, body), encoding="utf-8")

    def _backfill_frontmatter(self) -> int:
        """Add frontmatter to notes written before the vault carried metadata.

        Idempotent and cheap: it only rewrites files that lack a frontmatter
        block, so opening an already-migrated vault costs one read per note.
        Returns how many notes were migrated, for the log line.
        """
        migrated = 0
        for row in self._conn.execute("SELECT id FROM notes").fetchall():
            path = self._note_path(row["id"])
            if not path.exists():
                continue
            if frontmatter.has_frontmatter(path.read_text(encoding="utf-8")):
                continue
            self._write_frontmatter(row["id"])
            migrated += 1

        if migrated:
            logger.info(f"Vault migrated: frontmatter written to {migrated} note(s)")
        return migrated

    def _delete_md(self, note_id: str):
        p = self._note_path(note_id)
        if p.exists():
            p.unlink()

    # ── Row → Note ────────────────────────────────────────────────────────────

    def _row_to_note(self, row: sqlite3.Row) -> Note:
        content = self._read_md(row["id"])
        return Note(
            id=row["id"],
            title=row["title"],
            content=content,
            tags=json.loads(row["tags"]),
            sync_status=row["sync_status"],
            cot_message_id=row["cot_message_id"],
            cot_cluster_id=row["cot_cluster_id"],
            cot_cluster_name=row["cot_cluster_name"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            synced_at=_parse_dt(row["synced_at"]),
            sync_error=row["sync_error"],
        )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, title: str, content: str = "", tags: Optional[List[str]] = None) -> Note:
        note = Note(
            id=new_id(),
            title=title,
            content=content,
            tags=tags or [],
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._write_md(note.id, content)
        now = _now()
        self._conn.execute(
            """INSERT INTO notes (id, title, tags, sync_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (note.id, note.title, json.dumps(note.tags), note.sync_status, now, now),
        )
        self._conn.execute(
            "INSERT INTO notes_fts (id, title, content) VALUES (?, ?, ?)",
            (note.id, note.title, content),
        )
        self._conn.commit()
        self._write_frontmatter(note.id)
        logger.debug(f"Note created: {note.id} [{note.title}]")
        return note

    def get(self, note_id: str) -> Optional[Note]:
        row = self._conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return self._row_to_note(row) if row else None

    def list(
        self,
        sync_status: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Note]:
        query = "SELECT * FROM notes"
        params: list = []
        conditions: list = []

        if sync_status:
            conditions.append("sync_status = ?")
            params.append(sync_status)
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_note(r) for r in rows]

    def find_note_by_tag(self, tag: str) -> Optional[Note]:
        """
        Finds the FIRST note carrying the exact tag.

        Used for idempotency checks, e.g. avoiding two notes for the same
        task_id. The match is exact against the JSON value in the tags column.
        """
        row = self._conn.execute(
            "SELECT * FROM notes WHERE tags LIKE ? ORDER BY created_at ASC LIMIT 1",
            (f'%"{tag}"%',),
        ).fetchone()
        return self._row_to_note(row) if row else None

    def search(self, query: str, limit: int = 20) -> List[Note]:
        match = _fts_query(query)
        if not match:
            return []
        rows = self._conn.execute(
            """SELECT n.* FROM notes n
               JOIN notes_fts f ON n.id = f.id
               WHERE notes_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (match, limit),
        ).fetchall()
        return [self._row_to_note(r) for r in rows]

    def update(
        self,
        note_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[Note]:
        note = self.get(note_id)
        if not note:
            return None

        now = _now()
        updates: dict = {"updated_at": now}
        if title is not None:
            updates["title"] = title
            note.title = title
        if tags is not None:
            updates["tags"] = json.dumps(tags)
            note.tags = tags
        if content is not None:
            self._write_md(note_id, content)
            note.content = content
            # Aggiorna FTS
            self._conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
            self._conn.execute(
                "INSERT INTO notes_fts (id, title, content) VALUES (?, ?, ?)",
                (note_id, updates.get("title", note.title), content),
            )

        # Se era synced e il contenuto cambia → torna pending
        if content is not None and note.sync_status == SYNC_SYNCED:
            updates["sync_status"] = SYNC_PENDING

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self._conn.execute(
            f"UPDATE notes SET {set_clause} WHERE id = ?",
            list(updates.values()) + [note_id],
        )
        self._conn.commit()
        self._write_frontmatter(note_id)
        return self.get(note_id)

    def delete(self, note_id: str) -> bool:
        self._delete_md(note_id)
        self._conn.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
        cur = self._conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ── Sync status helpers ───────────────────────────────────────────────────

    def mark_pending(self, note_id: str) -> bool:
        """Mark the note to be sent on the next push."""
        cur = self._conn.execute(
            "UPDATE notes SET sync_status = ?, updated_at = ? WHERE id = ? AND sync_status != ?",
            (SYNC_PENDING, _now(), note_id, SYNC_PENDING),
        )
        self._conn.commit()
        self._write_frontmatter(note_id)
        return cur.rowcount > 0

    def mark_synced(
        self,
        note_id: str,
        cot_message_id: str,
        cot_cluster_id: Optional[str] = None,
        cot_cluster_name: Optional[str] = None,
    ):
        """Update the note after a successful sync."""
        self._conn.execute(
            """UPDATE notes SET
               sync_status = ?, cot_message_id = ?, cot_cluster_id = ?,
               cot_cluster_name = ?, synced_at = ?, sync_error = NULL, updated_at = ?
               WHERE id = ?""",
            (
                SYNC_SYNCED,
                cot_message_id,
                cot_cluster_id,
                cot_cluster_name,
                _now(),
                _now(),
                note_id,
            ),
        )
        self._conn.commit()
        self._write_frontmatter(note_id)

    def mark_sync_error(self, note_id: str, error: str):
        self._conn.execute(
            "UPDATE notes SET sync_status = ?, sync_error = ?, updated_at = ? WHERE id = ?",
            (SYNC_ERROR, error[:500], _now(), note_id),
        )
        self._conn.commit()
        self._write_frontmatter(note_id)

    def update_cluster_info(self, cot_message_id: str, cot_cluster_id: str, cot_cluster_name: str):
        """Update cluster info after a pull from the cloud."""
        self._conn.execute(
            "UPDATE notes SET cot_cluster_id = ?, cot_cluster_name = ? WHERE cot_message_id = ?",
            (cot_cluster_id, cot_cluster_name, cot_message_id),
        )
        self._conn.commit()
        for row in self._conn.execute(
            "SELECT id FROM notes WHERE cot_message_id = ?", (cot_message_id,)
        ).fetchall():
            self._write_frontmatter(row["id"])

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> SyncStats:
        rows = self._conn.execute(
            "SELECT sync_status, COUNT(*) as cnt FROM notes GROUP BY sync_status"
        ).fetchall()
        counts = {r["sync_status"]: r["cnt"] for r in rows}

        last_push = self._conn.execute(
            "SELECT MAX(synced_at) as ts FROM notes WHERE sync_status = 'synced'"
        ).fetchone()["ts"]
        last_pull = self._conn.execute(
            "SELECT MAX(synced_at) as ts FROM notes WHERE cot_cluster_id IS NOT NULL"
        ).fetchone()["ts"]

        return SyncStats(
            pending=counts.get(SYNC_PENDING, 0),
            synced=counts.get(SYNC_SYNCED, 0),
            local_only=counts.get(SYNC_LOCAL_ONLY, 0),
            errors=counts.get(SYNC_ERROR, 0),
            last_push=_parse_dt(last_push),
            last_pull=_parse_dt(last_pull),
        )

    def close(self):
        self._conn.close()
