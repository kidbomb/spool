"""SQLite job store. Small and synchronous; guarded by a lock so worker threads can share it."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,
    title         TEXT,
    thumbnail     TEXT,
    status        TEXT NOT NULL,          -- queued | downloading | processing | paused | done | error
    progress      REAL DEFAULT 0,
    speed         REAL,
    eta           INTEGER,
    size          INTEGER,
    downloaded    INTEGER,
    preset        TEXT,
    subfolder     TEXT,
    release_name  TEXT,
    playlist      INTEGER DEFAULT 0,
    arr           TEXT,                   -- NULL | sonarr | radarr
    arr_status    TEXT,
    output_path   TEXT,
    files         TEXT DEFAULT '[]',      -- JSON list of produced files
    error         TEXT,
    log           TEXT DEFAULT '',
    created_at    REAL,
    updated_at    REAL,
    finished_at   REAL
);
"""

LOG_LIMIT = 20_000  # chars kept per job
PUBLIC_FIELDS = (
    "id", "url", "title", "thumbnail", "status", "progress", "speed", "eta", "size",
    "downloaded", "preset", "subfolder", "release_name", "playlist", "arr", "arr_status",
    "output_path", "files", "error", "created_at", "updated_at", "finished_at",
)


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = {k: r[k] for k in PUBLIC_FIELDS}
        d["files"] = json.loads(d["files"] or "[]")
        d["playlist"] = bool(d["playlist"])
        return d

    def create(self, **fields) -> dict:
        now = time.time()
        fields.setdefault("status", "queued")
        fields["created_at"] = fields["updated_at"] = now
        cols = ", ".join(fields)
        qs = ", ".join("?" for _ in fields)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", tuple(fields.values()))
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone())

    def get(self, job_id: int) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list(self) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.conn.execute("SELECT * FROM jobs ORDER BY id DESC")]

    def update(self, job_id: int, **fields) -> dict | None:
        if "files" in fields and not isinstance(fields["files"], str):
            fields["files"] = json.dumps(fields["files"])
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
            return self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def append_log(self, job_id: int, line: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET log = substr(coalesce(log,'') || ? || char(10), -?) WHERE id=?",
                (line, LOG_LIMIT, job_id),
            )

    def get_log(self, job_id: int) -> str:
        with self.lock:
            r = self.conn.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
            return (r["log"] or "") if r else ""

    def delete(self, job_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def next_queued(self, exclude: list[int]) -> dict | None:
        q = "SELECT * FROM jobs WHERE status='queued'"
        if exclude:
            q += f" AND id NOT IN ({','.join('?' for _ in exclude)})"
        q += " ORDER BY id ASC LIMIT 1"
        with self.lock:
            return self._row(self.conn.execute(q, tuple(exclude)).fetchone())

    def requeue_interrupted(self) -> None:
        """Jobs that were running when the container stopped go back to the queue (partials resume)."""
        with self.lock:
            self.conn.execute(
                "UPDATE jobs SET status='queued', speed=NULL, eta=NULL WHERE status IN ('downloading','processing')"
            )
