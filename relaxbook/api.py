"""HTTP API for RelaxBook — see ``docs/CONTRACTS.md`` section 12.

All routes are created by :func:`build_router` so tests can inject a
``Settings`` pointing at a temporary data directory.  This module only uses
``db`` / ``ingest`` / ``textnorm`` / ``tts`` / ``naming`` / ``audio``; it must
never import ``worker``.

Long running work (book parsing, preview synthesis) happens in daemon threads
and communicates its state through the SQLite database, exactly like the
background worker does for full-book jobs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import audio, db, ingest, naming, textnorm, tts
from .config import Settings
from .ingest import parse_book

logger = logging.getLogger("relaxbook.api")

# Module level router (kept for symmetry with the contract); the application
# uses :func:`build_router` so that it can bind a concrete ``Settings``.
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_PREVIEW_TEXT = 4000

# job_id -> (artifact signature, probed chapter boundaries)
_M4B_CHAPTER_CACHE: dict[int, tuple[tuple[Any, ...], list[dict[str, Any]]]] = {}
_M4B_CACHE_LOCK = threading.Lock()

_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4b": "audio/mp4",
    ".m4a": "audio/mp4",
    ".zip": "application/zip",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _connect(settings: Settings) -> sqlite3.Connection:
    return db.connect(settings.db_path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _safe_unlink(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def _json(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _safe_name(filename: str | None) -> str:
    raw = (filename or "").replace("\\", "/")
    name = raw.rsplit("/", 1)[-1].strip()
    return name or "book"


def _detect_format(filename: str, data: bytes) -> str:
    """Detect the input format; falls back to the extension while ``ingest``
    is still being implemented in parallel."""
    try:
        return str(ingest.detect_format(filename, data[:4096]))
    except ingest.IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "不支持的文件格式。") from exc
    except NotImplementedError:
        pass
    except Exception:  # pragma: no cover - defensive during parallel development
        logger.warning("格式识别失败，回退到扩展名：%s", filename, exc_info=True)
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ingest.SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="不支持的文件格式，目前支持 EPUB、PDF、MOBI、AZW3。",
        )
    return ext


def _voice_dict(voice: Any) -> dict[str, Any]:
    if isinstance(voice, dict):
        data = voice
    else:
        data = {
            "id": getattr(voice, "id", ""),
            "name": getattr(voice, "name", ""),
            "language": getattr(voice, "language", ""),
            "gender": getattr(voice, "gender", ""),
            "engine": getattr(voice, "engine", ""),
        }
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("name") or data.get("id") or ""),
        "language": str(data.get("language") or ""),
        "gender": str(data.get("gender") or ""),
        "engine": str(data.get("engine") or ""),
    }


def _collect_voices(settings: Settings) -> list[dict[str, Any]]:
    raw: Sequence[Any] = []
    try:
        raw = list(tts.list_voices(settings) or [])
    except Exception:  # pragma: no cover - defensive during parallel development
        logger.warning("tts.list_voices 调用失败，回退到引擎内置清单", exc_info=True)
        raw = []
    if not raw:
        try:
            engine = tts.get_engine(settings.engine, settings)
            raw = list(engine.voices() or [])
        except Exception:
            raw = []
    return [_voice_dict(v) for v in raw]


def _voices_available(voices: Sequence[dict[str, Any]]) -> bool:
    zh = sum(1 for v in voices if str(v.get("language", "")).lower().startswith("zh"))
    en = sum(1 for v in voices if str(v.get("language", "")).lower().startswith("en"))
    return zh >= 3 and en >= 1


def _engine_rows(settings: Settings, voices: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    available = _voices_available(voices)
    primary = tts.resolve_engine_name(None, settings)
    model = settings.model or ""
    try:
        engine = tts.get_engine(primary, settings)
        model = getattr(engine, "model", model) or model
    except Exception:
        pass
    try:
        rows = tts.engine_status(settings)
    except Exception:  # pragma: no cover - defensive
        rows = [{"name": primary, "model": model, "available": available}]
    for row in rows:
        if row.get("name") == primary:
            row["model"] = model or row.get("model", "")
            row["available"] = bool(row.get("available")) and available
    return rows


def _default_voice_for(engine_name: str, settings: Settings) -> str:
    """Default voice id for an engine (Qwen and Edge use different catalogues)."""
    if (engine_name or "").strip().lower() == "qwen":
        return (getattr(settings, "qwen_voice", "") or "Cherry").strip()
    return (settings.voice or "zh-CN-XiaoxiaoNeural").strip()


def _book_row_payload(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "author": row["author"],
        "format": row["format"],
        "status": row["status"],
        "error": row["error"],
        "warnings": db.loads(row["warnings"], []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _chapter_summary(chapter: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(chapter["id"]),
        "idx": int(chapter["idx"]),
        "title": chapter["title"],
        "read": bool(int(chapter.get("read") or 0)),
        "warnings": list(chapter.get("warnings") or []),
        "locator": dict(chapter.get("source_locator") or {}),
    }


def _mp3_filename(index: int, title: str, total: int) -> str:
    try:
        return naming.mp3_filename(index, title, total)
    except Exception:  # pragma: no cover - naming is implemented in parallel
        clean = (title or "").strip() or "未命名章节"
        return f"{index:03d} - {clean}.mp3"


def _read_chapters(conn: sqlite3.Connection, revision_id: int) -> list[dict[str, Any]]:
    return [c for c in db.list_chapters(conn, revision_id) if int(c.get("read") or 0)]


# ---------------------------------------------------------------------------
# background workers (threads)
# ---------------------------------------------------------------------------
def _parse_book_worker(settings: Settings, book_id: int, path: Path, fmt: str) -> None:
    """Parse an uploaded book, create its first revision and update the row."""
    conn: sqlite3.Connection | None = None
    try:
        parsed = parse_book(path, format=fmt, settings=settings)
        chapters: list[dict[str, Any]] = []
        for chapter in list(getattr(parsed, "chapters", None) or []):
            chapters.append(
                {
                    "title": getattr(chapter, "title", "") or "",
                    "text": getattr(chapter, "text", "") or "",
                    "read": bool(getattr(chapter, "read", True)),
                    "source_locator": getattr(chapter, "source_locator", None) or None,
                    "warnings": list(getattr(chapter, "warnings", None) or []),
                }
            )
        if not chapters:
            raise ingest.IngestError("未提取到任何章节内容，请检查文件后重试。")
        warnings = list(getattr(parsed, "warnings", None) or [])
        cover_path: str | None = None
        cover_bytes = getattr(parsed, "cover_bytes", None)
        if cover_bytes:
            ext = str(getattr(parsed, "cover_ext", "jpg") or "jpg").lstrip(".").lower() or "jpg"
            cover = path.parent / f"cover.{ext}"
            _atomic_write_bytes(cover, bytes(cover_bytes))
            cover_path = str(cover)
        conn = _connect(settings)
        db.create_revision(conn, book_id, chapters, note="导入解析")
        db.set_book_parsed(
            conn,
            book_id,
            title=getattr(parsed, "title", None) or path.stem,
            author=getattr(parsed, "author", None),
            cover_path=cover_path,
            warnings=warnings,
        )
        logger.info("book=%s 解析完成，共 %d 章", book_id, len(chapters))
    except ingest.IngestError as exc:
        message = str(exc).strip() or "解析失败，请检查文件后重试。"
        logger.warning("book=%s 解析失败：%s", book_id, message)
        _fail_book(settings, book_id, message)
    except Exception as exc:  # noqa: BLE001 - user-visible Chinese reason
        logger.exception("book=%s 解析时发生异常", book_id)
        reason = str(exc).strip() or exc.__class__.__name__
        _fail_book(settings, book_id, f"解析失败：{reason}")
    finally:
        if conn is not None:
            conn.close()


def _fail_book(settings: Settings, book_id: int, message: str) -> None:
    try:
        conn = _connect(settings)
        try:
            db.set_book_failed(conn, book_id, message)
        finally:
            conn.close()
    except Exception:  # pragma: no cover - last resort
        logger.exception("book=%s 记录失败状态时出错", book_id)


def _preview_duration(settings: Settings, path: Path) -> int | None:
    if not settings.ffprobe:
        return None
    try:
        info = audio.probe(path, ffprobe=settings.ffprobe)
        return _as_int(info.get("duration_ms") if isinstance(info, dict) else None)
    except Exception:  # audio/probe may still be a stub, duration is optional
        return None


def _preview_worker(
    settings: Settings,
    preview_id: str,
    text: str,
    voice: str,
    rate: str,
    engine_name: str,
) -> None:
    conn: sqlite3.Connection | None = None
    try:
        engine = tts.get_engine(engine_name, settings)
        mp3 = engine.synthesize(text, voice, rate)
        if not isinstance(mp3, (bytes, bytearray)) or not mp3:
            raise RuntimeError("合成引擎返回了空音频。")
        path = settings.previews_dir / f"{preview_id}.mp3"
        _atomic_write_bytes(path, bytes(mp3))
        duration_ms = _preview_duration(settings, path)
        conn = _connect(settings)
        db.set_preview_result(conn, preview_id, path=str(path), duration_ms=duration_ms)
    except Exception as exc:  # noqa: BLE001 - user-visible Chinese reason
        logger.warning("试听 %s 合成失败：%s", preview_id, exc)
        reason = str(exc).strip() or exc.__class__.__name__
        if not reason.startswith("试听"):
            reason = f"试听合成失败：{reason}"
        try:
            if conn is None:
                conn = _connect(settings)
            db.set_preview_result(conn, preview_id, error=reason)
        except Exception:  # pragma: no cover - last resort
            logger.exception("记录试听 %s 失败状态时出错", preview_id)
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class RevisionChapterIn(BaseModel):
    title: str = ""
    text: str = ""
    read: bool = True
    source_locator: dict[str, Any] | None = None
    warnings: list[str] | None = None


class RevisionIn(BaseModel):
    base_revision_id: int
    note: str = ""
    chapters: list[RevisionChapterIn] = []


class PreviewIn(BaseModel):
    text: str
    voice: str
    rate: str = "+0%"
    engine: str | None = None


class JobIn(BaseModel):
    book_id: int
    revision_id: int
    voice: str
    engine: str | None = None
    rate: str | None = None
    formats: list[str] | None = None


# ---------------------------------------------------------------------------
# segment planning (must match worker.py exactly)
# ---------------------------------------------------------------------------
def _plan_segments(
    conn: sqlite3.Connection,
    settings: Settings,
    revision_id: int,
    engine_name: str,
    voice: str,
    rate: str,
) -> tuple[list[dict[str, Any]], int, str]:
    """Return ``(segment items, read chapter count, engine model)``.

    The segmentation rules are exactly the ones the worker uses: for every
    ``read=1`` chapter, ``textnorm.segment_text`` produces the pieces and
    ``textnorm.segment_cache_key`` produces the cache key.
    """
    model = settings.model or ""
    try:
        engine = tts.get_engine(engine_name, settings)
        model = getattr(engine, "model", None) or model
    except Exception:  # pragma: no cover - worker will also fall back to settings
        pass
    items: list[dict[str, Any]] = []
    total_chapters = 0
    for chapter in db.list_chapters(conn, revision_id):
        if not int(chapter.get("read") or 0):
            continue
        total_chapters += 1
        pieces = textnorm.segment_text(chapter.get("text") or "")
        clean_pieces = [(p or "").strip() for p in pieces if (p or "").strip()]
        for order, piece in enumerate(clean_pieces, start=1):
            try:
                key = textnorm.segment_cache_key(
                    piece,
                    engine=engine_name,
                    model=model,
                    voice=voice,
                    rate=rate,
                )
            except NotImplementedError:  # pragma: no cover - during parallel dev
                key = hashlib.sha256(
                    f"{engine_name}|{model}|{voice}|{rate}|{piece}".encode("utf-8")
                ).hexdigest()
            items.append(
                {
                    "chapter_idx": int(chapter["idx"]),
                    "ord": order,
                    "text": piece,
                    "cache_key": key,
                }
            )
    return items, total_chapters, model


# ---------------------------------------------------------------------------
# jobs payload
# ---------------------------------------------------------------------------
def _probe_m4b_boundaries(
    settings: Settings, job_id: int, artifact: sqlite3.Row
) -> list[dict[str, Any]] | None:
    """Probe chapter boundaries from the finished M4B, cached per job."""
    path = Path(artifact["path"])
    if not path.is_file():
        return None
    signature = (int(artifact["id"]), int(artifact["bytes"] or 0))
    with _M4B_CACHE_LOCK:
        cached = _M4B_CHAPTER_CACHE.get(job_id)
    if cached is not None and cached[0] == signature:
        return cached[1]
    if not settings.ffprobe:
        return None
    try:
        raw = audio.probe_chapters(path, ffprobe=settings.ffprobe) or []
    except Exception as exc:
        logger.warning("job=%s 探测 M4B 章节失败：%s", job_id, exc)
        return None
    boundaries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            title = item.get("title")
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
        else:  # pragma: no cover - future object based implementation
            title = getattr(item, "title", None)
            start_ms = getattr(item, "start_ms", None)
            end_ms = getattr(item, "end_ms", None)
        boundaries.append(
            {"title": title, "start_ms": _as_int(start_ms), "end_ms": _as_int(end_ms)}
        )
    with _M4B_CACHE_LOCK:
        _M4B_CHAPTER_CACHE[job_id] = (signature, boundaries)
    return boundaries


def _job_payload(
    conn: sqlite3.Connection, settings: Settings, job_id: int
) -> dict[str, Any]:
    job = db.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    book = db.get_book(conn, int(job["book_id"]))
    revision = db.get_revision(conn, int(job["revision_id"]))

    mp3_artifacts: dict[int, dict[str, Any]] = {}
    m4b_artifact: sqlite3.Row | None = None
    for artifact in db.list_artifacts(conn, job_id):
        if artifact["kind"] == "mp3" and artifact["chapter_idx"] is not None:
            mp3_artifacts[int(artifact["chapter_idx"])] = artifact
        elif artifact["kind"] == "m4b":
            m4b_artifact = artifact

    boundaries = (
        _probe_m4b_boundaries(settings, job_id, m4b_artifact) if m4b_artifact else None
    )
    chapters: list[dict[str, Any]] = []
    read_chapters = _read_chapters(conn, int(revision["id"])) if revision else []
    total = len(read_chapters)
    for position, chapter in enumerate(read_chapters, start=1):
        artifact = mp3_artifacts.get(int(chapter["idx"]))
        entry: dict[str, Any] = {
            "index": position,
            "title": chapter["title"],
            "read": True,
            "mp3_filename": _mp3_filename(position, chapter["title"], total),
            "mp3_artifact_id": int(artifact["id"]) if artifact else None,
            "mp3_bytes": int(artifact["bytes"]) if artifact else None,
            "mp3_duration_ms": _as_int(artifact["duration_ms"]) if artifact else None,
            "m4b_start_ms": None,
            "m4b_end_ms": None,
            "state": "done" if artifact and int(artifact["verified"]) else "pending",
        }
        if boundaries and position <= len(boundaries):
            probed = boundaries[position - 1]
            entry["m4b_start_ms"] = probed.get("start_ms")
            entry["m4b_end_ms"] = probed.get("end_ms")
        chapters.append(entry)

    m4b: dict[str, Any] | None = None
    if m4b_artifact is not None:
        duration_ms = job["m4b_duration_ms"]
        if duration_ms is None:
            duration_ms = m4b_artifact["duration_ms"]
        filename = job["m4b_filename"] or Path(m4b_artifact["path"]).name
        m4b_bytes = job["m4b_bytes"] or int(m4b_artifact["bytes"])
        m4b = {
            "artifact_id": int(m4b_artifact["id"]),
            "filename": filename,
            "bytes": int(m4b_bytes),
            "duration_ms": _as_int(duration_ms),
            "verified": bool(int(m4b_artifact["verified"])),
        }

    job_payload = {
        "id": int(job["id"]),
        "state": job["state"],
        "voice": job["voice"],
        "engine": job["engine"],
        "rate": job["rate"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "error": job["error"],
        "total_segments": int(job["total_segments"] or 0),
        "done_segments": int(job["done_segments"] or 0),
        "failed_segments": int(job["failed_segments"] or 0),
        "done_chapters": int(job["done_chapters"] or 0),
        "total_chapters": int(job["total_chapters"] or total),
    }
    book_payload = (
        {"id": int(book["id"]), "title": book["title"], "author": book["author"]}
        if book is not None
        else None
    )
    return {"job": job_payload, "book": book_payload, "m4b": m4b, "chapters": chapters}


# ---------------------------------------------------------------------------
# router factory
# ---------------------------------------------------------------------------
def build_router(settings: Settings) -> APIRouter:
    """Create the ``/api`` router bound to ``settings``."""
    settings.ensure_dirs()
    db.init_db(settings.db_path)

    api = APIRouter(prefix="/api", tags=["api"])

    # -- books --------------------------------------------------------------
    @api.post("/books")
    async def upload_book(file: UploadFile = File(...)) -> JSONResponse:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="上传文件为空，请重新选择。")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="文件过大，请选择 1 GiB 以内的电子书。")
        filename = _safe_name(file.filename)
        sha256 = hashlib.sha256(data).hexdigest()
        fmt = _detect_format(filename, data)
        conn = _connect(settings)
        try:
            existing = db.find_book_by_sha(conn, sha256)
            if existing is not None:
                return _json(
                    {
                        "book_id": int(existing["id"]),
                        "status": existing["status"],
                        "duplicate": True,
                    },
                    status_code=202,
                )
            destination = settings.books_dir / sha256[:16] / f"original.{fmt}"
            _atomic_write_bytes(destination, data)
            title = Path(filename).stem or None
            try:
                book_id = db.create_book(
                    conn,
                    sha256=sha256,
                    format=fmt,
                    original_path=str(destination),
                    title=title,
                    status="parsing",
                )
            except sqlite3.IntegrityError:
                # Lost a race with a concurrent identical upload.
                row = db.find_book_by_sha(conn, sha256)
                if row is None:
                    raise
                return _json(
                    {
                        "book_id": int(row["id"]),
                        "status": row["status"],
                        "duplicate": True,
                    },
                    status_code=202,
                )
            thread = threading.Thread(
                target=_parse_book_worker,
                args=(settings, book_id, destination, fmt),
                name=f"parse-book-{book_id}",
                daemon=True,
            )
            thread.start()
        finally:
            conn.close()
        return _json({"book_id": book_id, "status": "parsing", "duplicate": False}, 202)

    @api.get("/books")
    def list_books() -> dict[str, Any]:
        conn = _connect(settings)
        try:
            books = []
            for row in db.list_books(conn):
                books.append(
                    {
                        "id": int(row["id"]),
                        "title": row["title"],
                        "author": row["author"],
                        "format": row["format"],
                        "status": row["status"],
                        "revision_id": _as_int(row["revision_id"]),
                        "chapter_count": int(row["chapter_count"] or 0),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "error": row["error"],
                    }
                )
        finally:
            conn.close()
        return {"books": books}

    @api.get("/books/{book_id}")
    def get_book(book_id: int) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            row = db.get_book(conn, book_id)
            if row is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            book = _book_row_payload(row)
            revision = db.current_revision(conn, book_id)
            chapters: list[dict[str, Any]] = []
            revision_payload: dict[str, Any] | None = None
            if revision is not None:
                chapters = [
                    _chapter_summary(ch) for ch in db.list_chapters(conn, int(revision["id"]))
                ]
                revision_payload = {
                    "id": int(revision["id"]),
                    "chapter_count": len(chapters),
                }
        finally:
            conn.close()
        return {
            "book": book,
            "revision": revision_payload,
            "chapters": chapters,
            "warnings": book["warnings"],
        }

    @api.get("/books/{book_id}/estimate")
    def get_estimate(
        book_id: int,
        voice: str | None = None,
        engine: str | None = None,
        rate: str | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        """Predict duration / generation time from the text alone (no synthesis)."""
        from . import estimate as estimate_mod

        conn = _connect(settings)
        try:
            row = db.get_book(conn, book_id)
            if row is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            revision = db.current_revision(conn, book_id)
            if revision is None:
                raise HTTPException(status_code=409, detail="书籍尚未解析完成")
            chapters = db.list_chapters(conn, int(revision["id"]))
        finally:
            conn.close()
        engine_name = tts.resolve_engine_name((engine or "").strip() or settings.engine, settings)
        chosen_voice = (voice or "").strip() or _default_voice_for(engine_name, settings)
        result = estimate_mod.estimate_book(
            [{"index": ch["idx"], "title": ch["title"], "text": ch["text"], "read": ch["read"]} for ch in chapters],
            engine=engine_name,
            voice=chosen_voice,
            rate=(rate or settings.rate or "+0%"),
            concurrency=max(1, int(concurrency or settings.concurrency)),
            mp3_bitrate=settings.mp3_bitrate,
            m4b_bitrate=settings.m4b_bitrate,
        )
        return {
            "estimate": dataclasses.asdict(result),
            "summary": estimate_mod.format_estimate(result),
            "note": "本估算仅按文本统计，未实测。",
        }

    @api.get("/books/{book_id}/chapters/{chapter_id}")
    def get_chapter(book_id: int, chapter_id: int) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            chapter = db.get_chapter(conn, chapter_id)
            if chapter is None:
                raise HTTPException(status_code=404, detail="章节不存在")
            revision = db.get_revision(conn, int(chapter["revision_id"]))
            if revision is None or int(revision["book_id"]) != book_id:
                raise HTTPException(status_code=404, detail="章节不属于该书籍")
        finally:
            conn.close()
        return {
            "chapter": {
                "id": int(chapter["id"]),
                "idx": int(chapter["idx"]),
                "title": chapter["title"],
                "text": chapter["text"],
                "read": bool(int(chapter["read"] or 0)),
                "source_locator": dict(chapter.get("source_locator") or {}),
                "warnings": list(chapter.get("warnings") or []),
            }
        }

    @api.put("/books/{book_id}/revision")
    def save_revision(book_id: int, body: RevisionIn) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            book = db.get_book(conn, book_id)
            if book is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            current = db.current_revision(conn, book_id)
            if current is None or int(current["id"]) != int(body.base_revision_id):
                raise HTTPException(status_code=409, detail="版本已过期，请刷新后重试")
            if not body.chapters:
                raise HTTPException(status_code=400, detail="章节列表不能为空")
            chapters = []
            for chapter in body.chapters:
                chapters.append(
                    {
                        "title": chapter.title,
                        "text": chapter.text,
                        "read": bool(chapter.read),
                        "source_locator": chapter.source_locator,
                        "warnings": chapter.warnings,
                    }
                )
            revision_id = db.create_revision(
                conn,
                book_id,
                chapters,
                parent_id=int(current["id"]),
                note=body.note or "",
            )
        finally:
            conn.close()
        return {"revision_id": int(revision_id)}

    @api.get("/books/{book_id}/cover")
    def get_cover(book_id: int) -> FileResponse:
        conn = _connect(settings)
        try:
            row = db.get_book(conn, book_id)
            if row is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            cover_path = row["cover_path"]
        finally:
            conn.close()
        if not cover_path or not Path(cover_path).is_file():
            raise HTTPException(status_code=404, detail="暂无封面")
        path = Path(cover_path)
        media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(str(path), media_type=media_type)

    # -- voices / previews --------------------------------------------------
    @api.get("/voices")
    def list_voices() -> dict[str, Any]:
        voices = _collect_voices(settings)
        return {"engines": _engine_rows(settings, voices), "voices": voices}

    @api.post("/previews")
    def create_preview(body: PreviewIn) -> JSONResponse:
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="试听文本不能为空。")
        if len(text) > MAX_PREVIEW_TEXT:
            raise HTTPException(
                status_code=400, detail=f"试听文本过长，请控制在 {MAX_PREVIEW_TEXT} 字以内。"
            )
        voice = (body.voice or "").strip() or settings.voice
        rate = (body.rate or "").strip() or settings.rate or "+0%"
        engine_name = tts.resolve_engine_name((body.engine or "").strip() or settings.engine, settings)
        model = settings.model or ""
        try:
            engine = tts.get_engine(engine_name, settings)
            model = getattr(engine, "model", model) or model
        except Exception:  # pragma: no cover - the thread will report the error
            pass
        preview_id = uuid.uuid4().hex[:16]
        conn = _connect(settings)
        try:
            db.create_preview(
                conn,
                preview_id=preview_id,
                engine=engine_name,
                model=model,
                voice=voice,
                rate=rate,
                text=text,
            )
        finally:
            conn.close()
        thread = threading.Thread(
            target=_preview_worker,
            args=(settings, preview_id, text, voice, rate, engine_name),
            name=f"preview-{preview_id}",
            daemon=True,
        )
        thread.start()
        return _json({"preview_id": preview_id}, 202)

    @api.get("/previews/{preview_id}")
    def get_preview(preview_id: str) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            row = db.get_preview(conn, preview_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="试听不存在")
        ready = row["status"] == "ready" and row["path"]
        return {
            "status": row["status"],
            "audio_url": f"/api/previews/{preview_id}/audio" if ready else None,
            "duration_ms": _as_int(row["duration_ms"]),
            "error": row["error"],
        }

    @api.get("/previews/{preview_id}/audio")
    def get_preview_audio(preview_id: str) -> FileResponse:
        conn = _connect(settings)
        try:
            row = db.get_preview(conn, preview_id)
        finally:
            conn.close()
        if row is None or row["status"] != "ready" or not row["path"]:
            raise HTTPException(status_code=404, detail="试听音频不存在")
        path = Path(row["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="试听音频不存在")
        return FileResponse(str(path), media_type="audio/mpeg")

    # -- jobs ---------------------------------------------------------------
    @api.post("/jobs")
    def create_job(body: JobIn) -> JSONResponse:
        conn = _connect(settings)
        try:
            book = db.get_book(conn, body.book_id)
            if book is None:
                raise HTTPException(status_code=404, detail="书籍不存在")
            revision = db.get_revision(conn, body.revision_id)
            if revision is None or int(revision["book_id"]) != body.book_id:
                raise HTTPException(status_code=404, detail="编辑版本不存在")
            voice = (body.voice or "").strip() or settings.voice
            engine_name = tts.resolve_engine_name((body.engine or "").strip() or settings.engine, settings)
            rate = (body.rate or "").strip() or settings.rate or "+0%"
            formats = [f.strip() for f in (body.formats or ["m4b", "mp3"]) if f and f.strip()]
            if not formats:
                formats = ["m4b", "mp3"]
            request_key = hashlib.sha256(
                f"{body.revision_id}|{engine_name}|{voice}|{rate}".encode("utf-8")
            ).hexdigest()[:32]
            existing = db.find_job_by_request_key(conn, request_key)
            if existing is not None:
                if existing["state"] != "failed":
                    return _json(
                        {"job_id": int(existing["id"]), "duplicate": True}, status_code=202
                    )
                # A failed job must not block a fresh attempt: release the key
                # (the UNIQUE constraint would otherwise reject the new job).
                conn.execute("UPDATE job SET request_key=NULL WHERE id=?", (int(existing["id"]),))
            items, total_chapters, model = _plan_segments(
                conn, settings, body.revision_id, engine_name, voice, rate
            )
            if not items:
                raise HTTPException(status_code=400, detail="没有可朗读的正文，请先补全章节内容。")
            total_segments = len(items)
            try:
                job_id = db.create_job(
                    conn,
                    book_id=body.book_id,
                    revision_id=body.revision_id,
                    engine=engine_name,
                    model=model,
                    voice=voice,
                    rate=rate,
                    formats=formats,
                    total_segments=total_segments,
                    total_chapters=total_chapters,
                    request_key=request_key,
                )
            except sqlite3.IntegrityError:
                row = db.find_job_by_request_key(conn, request_key)
                if row is None:
                    raise
                return _json({"job_id": int(row["id"]), "duplicate": True}, status_code=202)
            db.create_segments(conn, job_id, items)
        finally:
            conn.close()
        return _json({"job_id": int(job_id), "duplicate": False}, status_code=202)

    @api.get("/jobs/{job_id}")
    def get_job(job_id: int) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            return _job_payload(conn, settings, job_id)
        finally:
            conn.close()

    def _transition(job_id: int, action: str) -> dict[str, Any]:
        conn = _connect(settings)
        try:
            job = db.get_job(conn, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            state = job["state"]
            if action == "pause":
                if state not in ("queued", "synthesizing"):
                    raise HTTPException(status_code=409, detail="当前状态不能暂停任务")
                db.set_job_state(conn, job_id, "paused")
            elif action == "resume":
                if state != "paused":
                    raise HTTPException(status_code=409, detail="仅已暂停的任务可以继续")
                db.set_job_state(conn, job_id, "queued")
            elif action == "retry":
                if state != "failed":
                    raise HTTPException(status_code=409, detail="仅失败的任务可以重试")
                db.reset_segment_failures(conn, job_id)
                db.set_job_state(conn, job_id, "queued")
            else:  # pragma: no cover - defensive
                raise HTTPException(status_code=400, detail="未知操作")
            fresh = db.get_job(conn, job_id)
            new_state = fresh["state"] if fresh is not None else state
        finally:
            conn.close()
        return {"job_id": int(job_id), "state": new_state}

    @api.post("/jobs/{job_id}/pause")
    def pause_job(job_id: int) -> dict[str, Any]:
        return _transition(job_id, "pause")

    @api.post("/jobs/{job_id}/resume")
    def resume_job(job_id: int) -> dict[str, Any]:
        return _transition(job_id, "resume")

    @api.post("/jobs/{job_id}/retry")
    def retry_job(job_id: int) -> dict[str, Any]:
        return _transition(job_id, "retry")

    @api.get("/jobs/{job_id}/zip")
    def download_zip(job_id: int) -> FileResponse:
        conn = _connect(settings)
        try:
            job = db.get_job(conn, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            book = db.get_book(conn, int(job["book_id"]))
            artifacts = [
                artifact
                for artifact in db.list_artifacts(conn, job_id)
                if artifact["kind"] == "mp3" and int(artifact["verified"] or 0)
            ]
            if not artifacts:
                raise HTTPException(status_code=404, detail="还没有可打包的 MP3 成品。")
            zip_dir = settings.jobs_dir / str(job_id)
            zip_dir.mkdir(parents=True, exist_ok=True)
            file_descriptor, temp_name = tempfile.mkstemp(suffix=".zip", dir=str(zip_dir))
            os.close(file_descriptor)
            try:
                with zipfile.ZipFile(temp_name, "w", zipfile.ZIP_DEFLATED) as archive:
                    for artifact in artifacts:
                        source = Path(artifact["path"])
                        if not source.is_file():
                            continue
                        archive.write(source, source.name)
                if not zipfile.is_zipfile(temp_name):
                    raise HTTPException(status_code=500, detail="打包 MP3 失败，请稍后重试。")
            except HTTPException:
                _safe_unlink(temp_name)
                raise
            except Exception as exc:  # noqa: BLE001 - Chinese user visible reason
                _safe_unlink(temp_name)
                logger.exception("job=%s 打包 ZIP 失败", job_id)
                raise HTTPException(
                    status_code=500, detail=f"打包 MP3 失败：{str(exc).strip() or exc.__class__.__name__}"
                ) from exc
            title = (book["title"] if book is not None else "") or f"任务{job_id}"
            filename = f"{naming.sanitize_title(title)} - {job['voice']} - 分章MP3.zip"
        finally:
            conn.close()
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")
        }
        return FileResponse(
            temp_name,
            media_type="application/zip",
            headers=headers,
            background=BackgroundTask(_safe_unlink, temp_name),
        )

    @api.get("/artifacts/{artifact_id}")
    def download_artifact(artifact_id: int) -> FileResponse:
        conn = _connect(settings)
        try:
            artifact = db.get_artifact(conn, artifact_id)
        finally:
            conn.close()
        if artifact is None:
            raise HTTPException(status_code=404, detail="成品不存在")
        path = Path(artifact["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="成品文件不存在或已被删除")
        filename = path.name
        media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe="")
        }
        return FileResponse(str(path), media_type=media_type, headers=headers)

    return api
