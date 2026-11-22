"""Tests for the conversion worker (CONTRACTS section 11, ACCEPTANCE A5).

The tests drive :class:`relaxbook.worker.JobRunner` with a ``FakeEngine`` so no
network is involved, while the audio / packaging layers are used **for real when
the parallel owners have already implemented them** (they are contract-shaped).
If a module is still a stub at the time the tests run, a contract-shaped
stand-in is injected, so the worker logic stays testable independently of the
other workstreams.

Covered here: full job -> done, cache hits never calling the engine again,
pause stopping new submissions while the first chapter's MP3 is already
downloadable, failed segments retried without redoing finished ones, a killed
worker restarting and only filling the gaps, and packaging failures that keep
the MP3s and are retried without any re-synthesis.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path

import pytest

import relaxbook.audio as audio
import relaxbook.db as db
import relaxbook.naming as naming
import relaxbook.packaging as packaging
import relaxbook.textnorm as textnorm
import relaxbook.worker as worker
from relaxbook.config import Settings, locate_tool
from relaxbook.tts import TTSError, Voice

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A real, ~0.5 s 24 kHz mono 48 kbps MP3 (generated once with ffmpeg).  The
#: FakeEngine returns it unchanged, so a real decode/package pipeline can be
#: exercised without network access.
FAKE_MP3 = base64.b64decode(
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYzLjEuMTAyAAAAAAAAAAAAAAD/84TAAAAAAAAAAAAASW5mbwAAAA8AAAAXAAANsAAYGBgYIyMjIy0tLS0tODg4OEJCQkJNTU1NTVdXV1diYmJibGxsbGx3d3d3gYGBgYyMjIyMlpaWlqGhoaGrq6urq7a2trbAwMDAy8vLy8vV1dXV4ODg4Orq6urq9fX19f////8AAAAATGF2YzYzLjEuAAAAAAAAAAAAAAAAJAKgAAAAAAAADbAe8yBmAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/82TEAAAAA0gAAAAATEFNRVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/82TEfAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU="
)

DEFAULT_TITLES = ("序言", "出发", "出发")
DEFAULT_TEXTS = (
    "清晨的列车缓缓驶出站台。窗外的田野一层层铺开。我们安静地坐着，听铁轨有节奏地响。",
    "午后的车站人来人往。广播里反复报着同一班车的名字。她提着行李，在人群里寻找出口。",
    "傍晚的灯光落在长椅上。风把树叶吹得沙沙作响。远处的钟声敲了三下，天就慢慢暗了。",
)


# --------------------------------------------------------------------------- #
# contract-shaped stand-ins for modules the parallel owners may still be writing
# --------------------------------------------------------------------------- #
def _implemented(function) -> bool:
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):  # pragma: no cover - builtins / C functions
        return True
    return "NotImplementedError" not in source


def _real_pipeline_available() -> bool:
    required = (
        audio.decode_to_wav,
        audio.wav_duration_ms,
        packaging.assemble_chapter,
        packaging.assemble_m4b,
        packaging.verify_m4b,
    )
    return (
        all(_implemented(function) for function in required)
        and bool(locate_tool("ffmpeg"))
        and bool(locate_tool("ffprobe"))
    )


def _fake_segment_text(text, *, max_chars=220):
    cleaned = "".join(str(text or "").split())
    if not cleaned:
        return []
    size = 13
    return [cleaned[index : index + size] for index in range(0, len(cleaned), size)]


def _fake_segment_cache_key(text, *, engine, model, voice, rate):
    payload = json.dumps(
        {"t": text, "e": engine, "m": model, "v": voice, "r": rate, "rules": "fake"},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _wav_frames(path) -> int:
    with wave.open(str(path), "rb") as handle:
        return int(handle.getnframes())


def _write_silent_wav(path, frames: int = 2400) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * frames)


def _fake_decode_to_wav(src, dst, *, ffmpeg):
    _write_silent_wav(dst)


def _fake_wav_duration_ms(path):
    return _wav_frames(path) * 1000 // 24000


def _fake_assemble_chapter(segment_wavs, out_mp3, *, bitrate, ffmpeg, ffprobe):
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    duration_ms = sum(_fake_wav_duration_ms(path) for path in segment_wavs)
    out.write_bytes(FAKE_MP3 + b"chapter")
    return {"path": str(out), "bytes": out.stat().st_size, "duration_ms": duration_ms}


def _fake_assemble_m4b(chapters, out_path, *, title, author, cover_path, bitrate, ffmpeg, ffprobe):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cursor = 0
    ranges = []
    for position, chapter in enumerate(chapters, start=1):
        frames = sum(_wav_frames(path) for path in chapter["segment_wavs"])
        start_ms = cursor * 1000 // 24000
        cursor += frames
        end_ms = cursor * 1000 // 24000
        ranges.append(
            {
                "index": chapter.get("index", position),
                "title": chapter["title"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
            }
        )
    out.write_bytes(FAKE_MP3 + b"m4b")
    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "duration_ms": cursor * 1000 // 24000,
        "chapters": ranges,
    }


def _fake_verify_m4b(path, *, ffmpeg, ffprobe, expected_chapters=None):
    return {"ok": True, "duration_ms": 1000, "chapters": []}


@pytest.fixture
def pipeline(monkeypatch):
    """Real implementations where available, contract-shaped fakes otherwise."""
    fakes: list[str] = []
    if not _implemented(textnorm.segment_text):
        monkeypatch.setattr(textnorm, "segment_text", _fake_segment_text)
        fakes.append("textnorm.segment_text")
    if not _implemented(textnorm.segment_cache_key):
        monkeypatch.setattr(textnorm, "segment_cache_key", _fake_segment_cache_key)
        fakes.append("textnorm.segment_cache_key")
    if not _real_pipeline_available():
        monkeypatch.setattr(audio, "decode_to_wav", _fake_decode_to_wav)
        monkeypatch.setattr(audio, "wav_duration_ms", _fake_wav_duration_ms)
        monkeypatch.setattr(packaging, "assemble_chapter", _fake_assemble_chapter)
        monkeypatch.setattr(packaging, "assemble_m4b", _fake_assemble_m4b)
        monkeypatch.setattr(packaging, "verify_m4b", _fake_verify_m4b)
        fakes.append("audio/packaging")
    return fakes


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path):
    # resolve_tools() picks the FFmpeg 7+ binary (imageio-ffmpeg wheel) needed
    # for M4B chapter tracks; locate_tool("ffmpeg") may return an old system one.
    value = Settings(data_dir=str(tmp_path))
    value.ensure_dirs()
    value.resolve_tools()
    value.concurrency = 2
    value.mp3_bitrate = "64k"
    value.m4b_bitrate = "64k"
    return value


@pytest.fixture
def conn(settings):
    db.init_db(settings.db_path)
    connection = db.connect(settings.db_path)
    yield connection
    connection.close()


def _create_job(
    conn,
    settings,
    *,
    titles=DEFAULT_TITLES,
    texts=DEFAULT_TEXTS,
    engine="edge",
    model="edge-neural",
    voice="zh-CN-XiaoxiaoNeural",
    rate="+0%",
    request_key=None,
):
    book_id = db.create_book(
        conn,
        sha256=uuid.uuid4().hex,
        format="epub",
        original_path=str(Path(settings.root) / "book.epub"),
    )
    db.set_book_parsed(
        conn, book_id, title="示例书", author="测试作者", cover_path=None
    )
    revision_id = db.create_revision(
        conn,
        book_id,
        [
            {"title": title, "text": text, "read": True}
            for title, text in zip(titles, texts)
        ],
    )
    total_segments = sum(len(textnorm.segment_text(text)) for text in texts)
    job_id = db.create_job(
        conn,
        book_id=book_id,
        revision_id=revision_id,
        engine=engine,
        model=model,
        voice=voice,
        rate=rate,
        formats=("m4b", "mp3"),
        total_segments=total_segments,
        total_chapters=len(texts),
        request_key=request_key or uuid.uuid4().hex,
    )
    return book_id, revision_id, job_id


class FakeEngine:
    """Deterministic engine: counts calls, can fail or pause the job on demand."""

    name = "edge"
    model = "edge-neural"

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_on_calls: dict[int, int] | None = None,
        pause_after: int | None = None,
        settings: Settings | None = None,
        job_id: int | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self.delay = delay
        self.fail_on_calls = dict(fail_on_calls or {})
        self.pause_after = pause_after
        self.settings = settings
        self.job_id = job_id

    def voices(self):
        return [Voice("zh-CN-XiaoxiaoNeural", "晓晓（女·温暖）", "zh-CN", "Female", "edge")]

    def synthesize(self, text: str, voice: str, rate: str) -> bytes:
        with self._lock:
            self.calls.append(text)
            call_number = len(self.calls)
        if self.delay:
            time.sleep(self.delay)
        if (
            self.pause_after is not None
            and call_number == self.pause_after
            and self.settings is not None
            and self.job_id is not None
        ):
            pause_conn = db.connect(self.settings.db_path)
            try:
                db.set_job_state(pause_conn, int(self.job_id), "paused")
            finally:
                pause_conn.close()
        if self.fail_on_calls.get(call_number, 0) > 0:
            self.fail_on_calls[call_number] -= 1
            raise TTSError("模拟网络中断导致的合成失败")
        return FAKE_MP3


def _job(conn, job_id: int) -> dict:
    return dict(db.get_job(conn, job_id))


def _stats(conn, job_id: int) -> dict:
    return db.segment_stats(conn, job_id)


def _artifacts(conn, job_id: int, kind: str | None = None) -> list[dict]:
    items = db.list_artifacts(conn, job_id)
    return [item for item in items if kind is None or item["kind"] == kind]


def _cache_log_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "cache=" in record.getMessage()
    ]


def _remove_logging_handlers() -> None:
    base = logging.getLogger("relaxbook")
    for handler in list(base.handlers):
        base.removeHandler(handler)
        handler.close()
    if hasattr(base, "_relaxbook_worker_handlers"):
        delattr(base, "_relaxbook_worker_handlers")
    base.setLevel(logging.NOTSET)


# --------------------------------------------------------------------------- #
# 1. full pipeline
# --------------------------------------------------------------------------- #
def test_full_pipeline_reaches_done(settings, conn, pipeline, caplog):
    caplog.set_level(logging.INFO)
    _, _, job_id = _create_job(conn, settings)
    engine = FakeEngine()
    worker.JobRunner(settings, engine=engine).run_job(job_id)

    job = _job(conn, job_id)
    stats = _stats(conn, job_id)
    assert job["state"] == "done", job["error"]
    assert job["error"] is None
    assert stats["total"] == job["total_segments"] > 0
    assert stats["done"] == stats["total"]
    assert job["done_segments"] == stats["total"]
    assert job["failed_segments"] == 0
    assert job["done_chapters"] == 3

    # MP3 available per chapter + one M4B, all verified and touching real files.
    mp3s = _artifacts(conn, job_id, "mp3")
    m4bs = _artifacts(conn, job_id, "m4b")
    assert len(mp3s) == 3
    assert len(m4bs) == 1
    assert {item["chapter_idx"] for item in mp3s} == {1, 2, 3}
    for artifact in mp3s + m4bs:
        path = Path(artifact["path"])
        assert path.is_file() and path.stat().st_size > 0
        assert artifact["bytes"] > 0
        assert artifact["duration_ms"] > 0
        assert artifact["verified"]
    names = sorted(Path(item["path"]).name for item in mp3s)
    assert names[0].startswith("001 - "), names
    assert names[1].startswith("002 - "), names
    assert names[2].startswith("003 - "), names
    assert job["m4b_bytes"] > 0
    assert job["m4b_duration_ms"] > 0
    assert job["m4b_filename"].endswith(".m4b")
    assert job["mp3_complete"] == 1

    # cache directory got one WAV per segment, named by cache key prefix
    cache_files = sorted(Path(settings.segments_dir).rglob("*.wav"))
    assert len(cache_files) == stats["total"]
    assert all(path.parent.name == path.stem[:2] for path in cache_files)

    # engine called exactly once per segment; every segment logged as a miss
    assert len(engine.calls) == stats["total"]
    lines = _cache_log_lines(caplog)
    assert len(lines) == stats["total"]
    assert all(re.fullmatch(r"job=\d+ seg=\d+ cache=miss synth_ms=\d+", line) for line in lines)


# --------------------------------------------------------------------------- #
# 2. cache hits
# --------------------------------------------------------------------------- #
def test_cache_hit_never_calls_synthesize(settings, conn, pipeline, caplog):
    caplog.set_level(logging.INFO)
    book_id, revision_id, job1 = _create_job(conn, settings)
    first_engine = FakeEngine()
    worker.JobRunner(settings, engine=first_engine).run_job(job1)
    total = _job(conn, job1)["total_segments"]
    assert len(first_engine.calls) == total
    assert _job(conn, job1)["state"] == "done"

    # A second job for the same revision/voice/rate shares every cache key.
    job2 = db.create_job(
        conn,
        book_id=book_id,
        revision_id=revision_id,
        engine="edge",
        model="edge-neural",
        voice="zh-CN-XiaoxiaoNeural",
        rate="+0%",
        formats=("m4b", "mp3"),
        total_segments=total,
        total_chapters=3,
        request_key=uuid.uuid4().hex,
    )
    second_engine = FakeEngine()
    worker.JobRunner(settings, engine=second_engine).run_job(job2)

    assert second_engine.calls == [], "缓存命中时绝不能再次调用合成接口"
    assert _job(conn, job2)["state"] == "done"
    hits = [line for line in _cache_log_lines(caplog) if f"job={job2} " in line]
    assert len(hits) == total
    assert all("cache=hit" in line and "synth_ms=0" in line for line in hits)
    assert not any("cache=miss" in line for line in hits)


def test_broken_cache_file_is_resynthesized(settings, conn, pipeline):
    book_id, revision_id, job1 = _create_job(conn, settings)
    worker.JobRunner(settings, engine=FakeEngine()).run_job(job1)
    total = _job(conn, job1)["total_segments"]
    cache_files = sorted(Path(settings.segments_dir).rglob("*.wav"))
    assert len(cache_files) == total
    cache_files[0].write_bytes(b"this is not a wav file")

    job2 = db.create_job(
        conn,
        book_id=book_id,
        revision_id=revision_id,
        engine="edge",
        model="edge-neural",
        voice="zh-CN-XiaoxiaoNeural",
        rate="+0%",
        formats=("m4b", "mp3"),
        total_segments=total,
        total_chapters=3,
        request_key=uuid.uuid4().hex,
    )
    engine = FakeEngine()
    worker.JobRunner(settings, engine=engine).run_job(job2)
    assert len(engine.calls) == 1, "损坏的缓存片段必须重新合成"
    assert _job(conn, job2)["state"] == "done"


# --------------------------------------------------------------------------- #
# 3. pause / resume
# --------------------------------------------------------------------------- #
def test_pause_stops_new_submissions_and_keeps_first_chapter(settings, conn, pipeline):
    settings.concurrency = 1
    _, _, job_id = _create_job(conn, settings)
    total = _job(conn, job_id)["total_segments"]
    first_chapter_segments = len(textnorm.segment_text(DEFAULT_TEXTS[0]))
    assert 0 < first_chapter_segments < total

    engine = FakeEngine(pause_after=first_chapter_segments, settings=settings, job_id=job_id)
    runner = worker.JobRunner(settings, engine=engine)
    runner.run_job(job_id)

    job = _job(conn, job_id)
    stats = _stats(conn, job_id)
    assert job["state"] == "paused", "暂停后状态必须保持 paused"
    assert stats["done"] == first_chapter_segments
    assert stats["failed"] == 0
    assert len(engine.calls) == first_chapter_segments, "暂停后不得再提交新片段"
    # The first chapter's MP3 was published before the rest was synthesised.
    chapter_artifacts = _artifacts(conn, job_id, "mp3")
    assert [item["chapter_idx"] for item in chapter_artifacts] == [1]
    assert _job(conn, job_id)["done_chapters"] == 1

    # API /resume: paused -> queued, then the worker picks it up again.
    db.set_job_state(conn, job_id, "queued")
    engine.pause_after = None
    worker.JobRunner(settings, engine=engine).run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "done"
    assert job["done_chapters"] == 3
    assert len(_artifacts(conn, job_id, "mp3")) == 3
    assert _stats(conn, job_id)["done"] == total
    assert len(engine.calls) == total, "恢复后每个片段只合成一次（不重复已完成片段）"


# --------------------------------------------------------------------------- #
# 4. failed segment retry
# --------------------------------------------------------------------------- #
def test_failed_segment_is_retried_without_redoing_done_segments(settings, conn, pipeline):
    settings.concurrency = 1
    _, _, job_id = _create_job(conn, settings)
    total = _job(conn, job_id)["total_segments"]
    assert total >= 2

    engine = FakeEngine(fail_on_calls={2: 1})
    runner = worker.JobRunner(settings, engine=engine)
    runner.run_job(job_id)

    job = _job(conn, job_id)
    stats = _stats(conn, job_id)
    assert job["state"] == "failed"
    assert "合成失败" in (job["error"] or "")
    assert stats["failed"] == 1
    assert stats["done"] == total - 1
    assert job["done_segments"] == total - 1, "已成功片段必须保留"
    assert len(engine.calls) == total

    # API /retry: failed -> queued and reset the failed segments.
    assert db.reset_segment_failures(conn, job_id) == 1
    db.set_job_state(conn, job_id, "queued")
    calls_before = len(engine.calls)
    runner.run_job(job_id)

    assert _job(conn, job_id)["state"] == "done"
    assert len(engine.calls) - calls_before == 1, "重试只补失败的片段"
    assert _stats(conn, job_id)["failed"] == 0
    assert _stats(conn, job_id)["done"] == total


# --------------------------------------------------------------------------- #
# 5. killed worker restarts and only fills the gaps
# --------------------------------------------------------------------------- #
def test_restart_after_kill_only_fills_missing_segments(settings, conn, pipeline):
    settings.concurrency = 1
    _, _, job_id = _create_job(conn, settings)
    total = _job(conn, job_id)["total_segments"]
    completed_before_kill = len(textnorm.segment_text(DEFAULT_TEXTS[0]))
    assert 0 < completed_before_kill < total

    # Stop the "process" mid-job (pause is the deterministic injection point).
    engine = FakeEngine(
        pause_after=completed_before_kill, settings=settings, job_id=job_id
    )
    worker.JobRunner(settings, engine=engine).run_job(job_id)
    assert _job(conn, job_id)["state"] == "paused"
    assert _stats(conn, job_id)["done"] == completed_before_kill

    # Simulate the crash: the state is left in 'synthesizing' and the restart
    # path re-queues it (db.reset_running_jobs at worker start).
    db.set_job_state(conn, job_id, "synthesizing")
    assert job_id in db.reset_running_jobs(conn)
    assert _job(conn, job_id)["state"] == "queued"

    engine.pause_after = None
    calls_before = len(engine.calls)
    worker.JobRunner(settings, engine=engine).run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "done"
    assert len(engine.calls) - calls_before == total - completed_before_kill, (
        "重启后只补缺失片段，已完成片段不得重新合成"
    )
    assert _stats(conn, job_id)["done"] == total


# --------------------------------------------------------------------------- #
# 6. packaging failure -> retry only packages
# --------------------------------------------------------------------------- #
def test_m4b_failure_keeps_mp3_and_retry_only_packages(settings, conn, pipeline):
    _, _, job_id = _create_job(conn, settings)
    engine = FakeEngine()
    runner = worker.JobRunner(settings, engine=engine)
    working_assemble = runner.assemble_m4b

    def broken_assemble(*args, **kwargs):
        raise RuntimeError("模拟磁盘空间不足")

    runner.assemble_m4b = broken_assemble
    runner.run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "failed"
    assert "M4B" in (job["error"] or "")
    assert "模拟磁盘空间不足" in (job["error"] or "")
    assert job["done_segments"] == job["total_segments"]
    assert len(_artifacts(conn, job_id, "mp3")) == 3, "封装失败时已成功的 MP3 必须保留"
    assert not _artifacts(conn, job_id, "m4b")
    for artifact in _artifacts(conn, job_id, "mp3"):
        assert Path(artifact["path"]).is_file()

    calls_before = len(engine.calls)
    runner.assemble_m4b = working_assemble
    db.set_job_state(conn, job_id, "queued")  # API /retry
    runner.run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "done", job["error"]
    assert len(_artifacts(conn, job_id, "m4b")) == 1
    assert len(engine.calls) == calls_before, "所有片段已 done，重试只重做封装"
    assert len(_artifacts(conn, job_id, "mp3")) == 3


def test_chapter_mp3_failure_retry_without_synthesis(settings, conn, pipeline):
    _, _, job_id = _create_job(conn, settings)
    engine = FakeEngine()
    runner = worker.JobRunner(settings, engine=engine)
    working_assemble = runner.assemble_chapter

    def broken_assemble(*args, **kwargs):
        raise RuntimeError("模拟章节写入失败")

    runner.assemble_chapter = broken_assemble
    runner.run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "failed"
    assert "MP3 封装失败" in (job["error"] or "")
    assert job["done_segments"] == job["total_segments"]
    assert not _artifacts(conn, job_id, "mp3")
    assert not _artifacts(conn, job_id, "m4b")

    calls_before = len(engine.calls)
    runner.assemble_chapter = working_assemble
    db.set_job_state(conn, job_id, "queued")
    runner.run_job(job_id)

    job = _job(conn, job_id)
    assert job["state"] == "done", job["error"]
    assert len(_artifacts(conn, job_id, "mp3")) == 3
    assert len(_artifacts(conn, job_id, "m4b")) == 1
    assert len(engine.calls) == calls_before, "封装重试不得重新合成"


# --------------------------------------------------------------------------- #
# 7. logging file + engine resolution from the job row
# --------------------------------------------------------------------------- #
def test_worker_log_file_and_engine_resolution(settings, conn, pipeline, monkeypatch):
    engine = FakeEngine()
    resolved: list[str] = []

    def fake_get_engine(name, settings_arg):
        resolved.append(name)
        return engine

    monkeypatch.setattr(worker, "get_engine", fake_get_engine)
    _, _, job_id = _create_job(conn, settings)
    try:
        worker._setup_logging(settings)
        worker.JobRunner(settings).run_job(job_id)
    finally:
        _remove_logging_handlers()

    assert resolved == ["edge"], "未注入 engine 时必须按 job.engine 解析引擎"
    assert _job(conn, job_id)["state"] == "done"
    total = _job(conn, job_id)["total_segments"]
    log_path = Path(settings.logs_dir) / "worker.log"
    log_text = log_path.read_text(encoding="utf-8")
    lines = [line for line in log_text.splitlines() if "cache=" in line]
    assert len(lines) == total
    assert all(
        re.search(r"job=\d+ seg=\d+ cache=(hit|miss) synth_ms=\d+", line) for line in lines
    )


# --------------------------------------------------------------------------- #
# 8. single instance + CLI
# --------------------------------------------------------------------------- #
def test_worker_help_exits_zero():
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "relaxbook.worker", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--data-dir" in proc.stdout
    assert "工作进程" in proc.stdout


def test_single_instance_lock(tmp_path):
    first = worker._acquire_lock(tmp_path)
    assert first is not None
    try:
        assert worker._acquire_lock(tmp_path) is None, "第二个实例必须拿不到锁"
        assert worker.main(["--data-dir", str(tmp_path)]) == 2
    finally:
        _remove_logging_handlers()
        worker._release_lock(first)
    again = worker._acquire_lock(tmp_path)
    assert again is not None
    worker._release_lock(again)
