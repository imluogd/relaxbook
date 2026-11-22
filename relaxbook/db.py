"""SQLite persistence layer.

This module is the single source of truth for the database schema and for the
queries used by the web API and the background worker.  Every function takes an
open ``sqlite3.Connection`` created by :func:`connect`; connections are cheap
and **must not** be shared across threads.

Conventions
-----------
* All timestamps are ISO-8601 UTC strings, e.g. ``2026-11-22T02:00:00+00:00``.
* ``chapter.idx`` is 1-based and gives the reading order inside one revision.
* JSON columns are stored as text; helpers decode them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS book (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256        TEXT NOT NULL UNIQUE,
    format        TEXT NOT NULL,
    original_path TEXT NOT NULL,
    work_path     TEXT,
    title         TEXT,
    author        TEXT,
    cover_path    TEXT,
    status        TEXT NOT NULL DEFAULT 'parsing',
    error         TEXT,
    warnings      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    parent_id   INTEGER,
    note        TEXT,
    frozen      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revision_book ON revision(book_id, id);

CREATE TABLE IF NOT EXISTS chapter (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id    INTEGER NOT NULL REFERENCES revision(id) ON DELETE CASCADE,
    idx            INTEGER NOT NULL,
    title          TEXT NOT NULL,
    source_locator TEXT,
    text           TEXT NOT NULL,
    read           INTEGER NOT NULL DEFAULT 1,
    warnings       TEXT,
    UNIQUE(revision_id, idx)
);

CREATE TABLE IF NOT EXISTS job (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id        INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    revision_id    INTEGER NOT NULL REFERENCES revision(id),
    engine         TEXT NOT NULL,
    model          TEXT,
    voice          TEXT NOT NULL,
    rate           TEXT NOT NULL DEFAULT '+0%',
    formats        TEXT NOT NULL DEFAULT 'm4b,mp3',
    request_key    TEXT UNIQUE,
    state          TEXT NOT NULL DEFAULT 'queued',
    error          TEXT,
    total_segments INTEGER NOT NULL DEFAULT 0,
    done_segments  INTEGER NOT NULL DEFAULT 0,
    failed_segments INTEGER NOT NULL DEFAULT 0,
    total_chapters INTEGER NOT NULL DEFAULT 0,
    done_chapters  INTEGER NOT NULL DEFAULT 0,
    m4b_bytes      INTEGER,
    m4b_duration_ms INTEGER,
    m4b_filename   TEXT,
    mp3_complete   INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_state ON job(state, id);

CREATE TABLE IF NOT EXISTS segment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    chapter_idx INTEGER NOT NULL,
    ord         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    cache_key   TEXT NOT NULL,
    audio_path  TEXT,
    duration_ms INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    UNIQUE(job_id, chapter_idx, ord)
);
CREATE INDEX IF NOT EXISTS idx_segment_job ON segment(job_id, chapter_idx, ord);

CREATE TABLE IF NOT EXISTS artifact (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    chapter_idx INTEGER,
    path        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    duration_ms INTEGER,
    sha256      TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, kind, chapter_idx)
);

CREATE TABLE IF NOT EXISTS preview (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'pending',
    engine     TEXT NOT NULL,
    model      TEXT,
    voice      TEXT NOT NULL,
    rate       TEXT NOT NULL DEFAULT '+0%',
    text       TEXT NOT NULL,
    path       TEXT,
    duration_ms INTEGER,
    error      TEXT,
    created_at TEXT NOT NULL
);
"""

BOOK_STATES = ("parsing", "ready", "failed")
JOB_STATES = ("queued", "synthesizing", "packaging", "done", "paused", "failed")
SEGMENT_STATES = ("pending", "done", "failed")
ARTIFACT_KINDS = ("m4b", "mp3", "zip")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with sane pragmas and row access by name."""
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | str) -> None:
    """Create the schema if needed and record the version."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        conn.close()


def rows(result: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in result]


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# book
# --------------------------------------------------------------------------
def find_book_by_sha(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM book WHERE sha256=?", (sha256,)).fetchone()


def create_book(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    format: str,
    original_path: str,
    work_path: str | None = None,
    title: str | None = None,
    author: str | None = None,
    cover_path: str | None = None,
    status: str = "parsing",
) -> int:
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO book (sha256, format, original_path, work_path, title, author,
                             cover_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sha256, format, original_path, work_path, title, author, cover_path, status, now, now),
    )
    return int(cur.lastrowid)


def set_book_parsed(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    title: str | None,
    author: str | None,
    cover_path: str | None,
    warnings: Sequence[str] = (),
    work_path: str | None = None,
) -> None:
    conn.execute(
        """UPDATE book SET title=COALESCE(?, title), author=COALESCE(?, author),
                           cover_path=COALESCE(?, cover_path), warnings=?, work_path=COALESCE(?, work_path),
                           status='ready', error=NULL, updated_at=?
           WHERE id=?""",
        (title, author, cover_path, json.dumps(list(warnings), ensure_ascii=False),
         work_path, utcnow(), book_id),
    )


def set_book_failed(conn: sqlite3.Connection, book_id: int, error: str) -> None:
    conn.execute(
        "UPDATE book SET status='failed', error=?, updated_at=? WHERE id=?",
        (error, utcnow(), book_id),
    )


def get_book(conn: sqlite3.Connection, book_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM book WHERE id=?", (book_id,)).fetchone()


def list_books(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            """SELECT b.*,
                      (SELECT MAX(id) FROM revision r WHERE r.book_id=b.id) AS revision_id,
                      (SELECT COUNT(*) FROM chapter c WHERE c.revision_id=
                         (SELECT MAX(id) FROM revision r2 WHERE r2.book_id=b.id)) AS chapter_count
               FROM book b ORDER BY b.id DESC LIMIT ?""",
            (limit,),
        )
    )


# --------------------------------------------------------------------------
# revision / chapter
# --------------------------------------------------------------------------
def create_revision(
    conn: sqlite3.Connection,
    book_id: int,
    chapters: Sequence[dict[str, Any]],
    *,
    parent_id: int | None = None,
    note: str = "",
) -> int:
    """Insert a full chapter list as a new revision.

    ``chapters`` items: ``{"title": str, "text": str, "read": bool,
    "source_locator": dict|None, "warnings": list[str]|None}``.
    """
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO revision (book_id, parent_id, note, frozen, created_at) VALUES (?,?,?,0,?)",
        (book_id, parent_id, note, now),
    )
    revision_id = int(cur.lastrowid)
    for i, ch in enumerate(chapters, start=1):
        conn.execute(
            """INSERT INTO chapter (revision_id, idx, title, source_locator, text, read, warnings)
               VALUES (?,?,?,?,?,?,?)""",
            (
                revision_id,
                i,
                (ch.get("title") or "").strip() or "未命名章节",
                json.dumps(ch["source_locator"], ensure_ascii=False) if ch.get("source_locator") else None,
                ch.get("text") or "",
                1 if ch.get("read", True) else 0,
                json.dumps(ch.get("warnings") or [], ensure_ascii=False),
            ),
        )
    return revision_id


def get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM revision WHERE id=?", (revision_id,)).fetchone()


def current_revision(conn: sqlite3.Connection, book_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revision WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)
    ).fetchone()


def list_chapters(conn: sqlite3.Connection, revision_id: int) -> list[dict[str, Any]]:
    result = rows(
        conn.execute(
            "SELECT * FROM chapter WHERE revision_id=? ORDER BY idx", (revision_id,)
        )
    )
    for ch in result:
        ch["source_locator"] = loads(ch.get("source_locator"), {})
        ch["warnings"] = loads(ch.get("warnings"), [])
    return result


def get_chapter(conn: sqlite3.Connection, chapter_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM chapter WHERE id=?", (chapter_id,)).fetchone()
    if row is None:
        return None
    ch = dict(row)
    ch["source_locator"] = loads(ch.get("source_locator"), {})
    ch["warnings"] = loads(ch.get("warnings"), [])
    return ch


def freeze_revision(conn: sqlite3.Connection, revision_id: int) -> None:
    conn.execute("UPDATE revision SET frozen=1 WHERE id=?", (revision_id,))


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------
def create_job(
    conn: sqlite3.Connection,
    *,
    book_id: int,
    revision_id: int,
    engine: str,
    model: str | None,
    voice: str,
    rate: str,
    formats: Sequence[str],
    total_segments: int,
    total_chapters: int,
    request_key: str | None = None,
) -> int:
    now = utcnow()
    cur = conn.execute(
        """INSERT INTO job (book_id, revision_id, engine, model, voice, rate, formats,
                            request_key, state, total_segments, total_chapters,
                            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?, 'queued', ?,?,?,?)""",
        (book_id, revision_id, engine, model, voice, rate, ",".join(formats),
         request_key, total_segments, total_chapters, now, now),
    )
    return int(cur.lastrowid)


def find_job_by_request_key(conn: sqlite3.Connection, request_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM job WHERE request_key=?", (request_key,)).fetchone()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, book_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if book_id is None:
        return rows(conn.execute("SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,)))
    return rows(
        conn.execute("SELECT * FROM job WHERE book_id=? ORDER BY id DESC LIMIT ?", (book_id, limit))
    )


def claim_next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically move the oldest queued job to ``synthesizing``."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM job WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE job SET state='synthesizing', updated_at=? WHERE id=? AND state='queued'",
            (utcnow(), row["id"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_job(conn, int(row["id"]))


def set_job_state(conn: sqlite3.Connection, job_id: int, state: str, error: str | None = None) -> None:
    if state not in JOB_STATES:
        raise ValueError(f"bad job state {state}")
    conn.execute(
        "UPDATE job SET state=?, error=?, updated_at=? WHERE id=?",
        (state, error, utcnow(), job_id),
    )


def update_job_progress(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    done_segments: int | None = None,
    failed_segments: int | None = None,
    done_chapters: int | None = None,
) -> None:
    sets, args = [], []
    if done_segments is not None:
        sets.append("done_segments=?")
        args.append(done_segments)
    if failed_segments is not None:
        sets.append("failed_segments=?")
        args.append(failed_segments)
    if done_chapters is not None:
        sets.append("done_chapters=?")
        args.append(done_chapters)
    if not sets:
        return
    sets.append("updated_at=?")
    args.extend([utcnow(), job_id])
    conn.execute(f"UPDATE job SET {', '.join(sets)} WHERE id=?", args)


def set_job_result(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    m4b_bytes: int | None = None,
    m4b_duration_ms: int | None = None,
    m4b_filename: str | None = None,
    mp3_complete: bool | None = None,
) -> None:
    conn.execute(
        """UPDATE job SET m4b_bytes=COALESCE(?, m4b_bytes),
                          m4b_duration_ms=COALESCE(?, m4b_duration_ms),
                          m4b_filename=COALESCE(?, m4b_filename),
                          mp3_complete=COALESCE(?, mp3_complete),
                          updated_at=?
           WHERE id=?""",
        (m4b_bytes, m4b_duration_ms, m4b_filename,
         None if mp3_complete is None else int(mp3_complete), utcnow(), job_id),
    )


def reset_running_jobs(conn: sqlite3.Connection) -> list[int]:
    """Called at worker start: running/synthesizing jobs become 'queued' again."""
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM job WHERE state IN ('synthesizing','packaging')"
    )]
    if ids:
        conn.execute(
            "UPDATE job SET state='queued', updated_at=? WHERE state IN ('synthesizing','packaging')",
            (utcnow(),),
        )
    return ids


# --------------------------------------------------------------------------
# segment
# --------------------------------------------------------------------------
def create_segments(conn: sqlite3.Connection, job_id: int, items: Sequence[dict[str, Any]]) -> None:
    conn.executemany(
        """INSERT INTO segment (job_id, chapter_idx, ord, text, cache_key, status)
           VALUES (?,?,?,?,?, 'pending')""",
        [(job_id, it["chapter_idx"], it["ord"], it["text"], it["cache_key"]) for it in items],
    )


def list_segments(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT * FROM segment WHERE job_id=? ORDER BY chapter_idx, ord", (job_id,)
        )
    )


def list_pending_segments(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT * FROM segment WHERE job_id=? AND status!='done' ORDER BY chapter_idx, ord",
            (job_id,),
        )
    )


def mark_segment_done(
    conn: sqlite3.Connection, segment_id: int, *, audio_path: str, duration_ms: int
) -> None:
    conn.execute(
        "UPDATE segment SET status='done', audio_path=?, duration_ms=?, error=NULL WHERE id=?",
        (audio_path, duration_ms, segment_id),
    )


def mark_segment_failed(conn: sqlite3.Connection, segment_id: int, error: str) -> None:
    conn.execute(
        "UPDATE segment SET status='failed', error=? WHERE id=?", (error, segment_id)
    )


def reset_segment_failures(conn: sqlite3.Connection, job_id: int) -> int:
    cur = conn.execute(
        "UPDATE segment SET status='pending', error=NULL WHERE job_id=? AND status='failed'",
        (job_id,),
    )
    return cur.rowcount


def segment_stats(conn: sqlite3.Connection, job_id: int) -> dict[str, int]:
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(status='done') AS done,
                  SUM(status='failed') AS failed
           FROM segment WHERE job_id=?""",
        (job_id,),
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "done": int(row["done"] or 0),
        "failed": int(row["failed"] or 0),
    }


# --------------------------------------------------------------------------
# artifact
# --------------------------------------------------------------------------
def upsert_artifact(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    kind: str,
    path: str,
    bytes: int,
    duration_ms: int | None = None,
    sha256: str | None = None,
    verified: bool = True,
    chapter_idx: int | None = None,
) -> int:
    """Insert or replace one artifact.

    SQLite's UNIQUE constraint treats NULLs as distinct, so a delete-then-insert
    is used instead of ``ON CONFLICT`` (whole-book artifacts have NULL chapter_idx).
    """
    conn.execute(
        "DELETE FROM artifact WHERE job_id=? AND kind=? AND chapter_idx IS ?",
        (job_id, kind, chapter_idx),
    )
    cur = conn.execute(
        """INSERT INTO artifact (job_id, kind, chapter_idx, path, bytes, duration_ms, sha256, verified)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, kind, chapter_idx, path, bytes, duration_ms, sha256, int(verified)),
    )
    return int(cur.lastrowid)


def list_artifacts(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    return rows(
        conn.execute(
            "SELECT * FROM artifact WHERE job_id=? ORDER BY kind, chapter_idx", (job_id,)
        )
    )


def get_artifact(conn: sqlite3.Connection, artifact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifact WHERE id=?", (artifact_id,)).fetchone()


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------
def create_preview(
    conn: sqlite3.Connection,
    *,
    preview_id: str,
    engine: str,
    model: str | None,
    voice: str,
    rate: str,
    text: str,
    status: str = "pending",
) -> None:
    conn.execute(
        """INSERT INTO preview (id, status, engine, model, voice, rate, text, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (preview_id, status, engine, model, voice, rate, text, utcnow()),
    )


def set_preview_result(
    conn: sqlite3.Connection,
    preview_id: str,
    *,
    path: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    if error is not None:
        conn.execute(
            "UPDATE preview SET status='failed', error=?, path=NULL WHERE id=?",
            (error, preview_id),
        )
    else:
        conn.execute(
            "UPDATE preview SET status='ready', path=?, duration_ms=?, error=NULL WHERE id=?",
            (path, duration_ms, preview_id),
        )


def get_preview(conn: sqlite3.Connection, preview_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM preview WHERE id=?", (preview_id,)).fetchone()
