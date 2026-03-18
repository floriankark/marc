"""SQLite state tracking for processed emails and run history."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class StateManager:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id          TEXT PRIMARY KEY,
                subject             TEXT,
                from_addr           TEXT,
                date_received       TEXT,
                thread_id           TEXT,
                obsidian_note_path  TEXT,
                archived_at         TEXT,
                deleted_from_server INTEGER DEFAULT 0,
                deletion_verified   INTEGER DEFAULT 0,
                backup_path         TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id          TEXT PRIMARY KEY,
                started_at      TEXT,
                completed_at    TEXT,
                emails_fetched  INTEGER DEFAULT 0,
                emails_archived INTEGER DEFAULT 0,
                emails_deleted  INTEGER DEFAULT 0,
                quota_before    REAL,
                quota_after     REAL,
                errors          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_thread_id ON processed_emails(thread_id);
        """)
        # Migrate existing DBs that predate the backup_path column
        try:
            self._conn.execute("ALTER TABLE processed_emails ADD COLUMN backup_path TEXT")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    def is_processed(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def get_all_processed_ids(self) -> set:
        rows = self._conn.execute("SELECT message_id FROM processed_emails").fetchall()
        return {r["message_id"] for r in rows}

    def get_processed_ids_for_thread(self, thread_id: str) -> list:
        rows = self._conn.execute(
            "SELECT message_id FROM processed_emails WHERE thread_id = ?", (thread_id,)
        ).fetchall()
        return [r["message_id"] for r in rows]

    def record_archived(self, message_id: str, subject: str, from_addr: str,
                        date_received: str, thread_id: str, obsidian_note_path: str,
                        backup_path: str = None):
        self._conn.execute("""
            INSERT OR REPLACE INTO processed_emails
                (message_id, subject, from_addr, date_received, thread_id,
                 obsidian_note_path, archived_at, backup_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, subject, from_addr, date_received, thread_id,
              obsidian_note_path, _now(), backup_path))
        self._conn.commit()

    def get_backup_paths_by_note(self) -> dict:
        """Return {obsidian_note_path: [backup_path, ...]} for all entries with both paths set."""
        rows = self._conn.execute("""
            SELECT obsidian_note_path, backup_path
            FROM processed_emails
            WHERE obsidian_note_path IS NOT NULL AND backup_path IS NOT NULL
        """).fetchall()
        result: dict[str, list[str]] = {}
        for r in rows:
            result.setdefault(r["obsidian_note_path"], []).append(r["backup_path"])
        return result

    def record_deleted(self, message_id: str):
        self._conn.execute("""
            UPDATE processed_emails
            SET deleted_from_server = 1, deletion_verified = 1
            WHERE message_id = ?
        """, (message_id,))
        self._conn.commit()

    def get_thread_id_for_message_id(self, message_id: str) -> Optional[str]:
        """Return the canonical thread_id for a known message_id, or None."""
        row = self._conn.execute(
            "SELECT thread_id FROM processed_emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row["thread_id"] if row else None

    def get_note_path_for_thread(self, thread_id: str) -> Optional[str]:
        """Return the obsidian note path for a known thread_id, or None."""
        row = self._conn.execute(
            "SELECT obsidian_note_path FROM processed_emails "
            "WHERE thread_id = ? AND obsidian_note_path IS NOT NULL LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row["obsidian_note_path"] if row else None

    def get_undeleted_archived(self) -> list:
        """Return archived emails not yet deleted from server."""
        rows = self._conn.execute("""
            SELECT * FROM processed_emails
            WHERE deleted_from_server = 0 AND obsidian_note_path IS NOT NULL
        """).fetchall()
        return [dict(r) for r in rows]

    def start_run(self, run_id: str, quota_before: float):
        self._conn.execute("""
            INSERT INTO runs (run_id, started_at, quota_before)
            VALUES (?, ?, ?)
        """, (run_id, _now(), quota_before))
        self._conn.commit()

    def finish_run(self, run_id: str, fetched: int, archived: int,
                   deleted: int, quota_after: float, errors: list):
        self._conn.execute("""
            UPDATE runs
            SET completed_at = ?, emails_fetched = ?, emails_archived = ?,
                emails_deleted = ?, quota_after = ?, errors = ?
            WHERE run_id = ?
        """, (_now(), fetched, archived, deleted, quota_after,
              json.dumps(errors), run_id))
        self._conn.commit()

    def close(self):
        self._conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
