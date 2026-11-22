"""Smoke tests for the config + db layer (lead-owned)."""

from __future__ import annotations

from pathlib import Path

import pytest

from relaxbook import db
from relaxbook.config import Settings, load_settings, save_settings


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_settings_roundtrip(data_dir: Path) -> None:
    settings = load_settings(data_dir)
    assert settings.db_path.parent == data_dir
    assert settings.books_dir.exists()
    assert settings.ffmpeg, "ffmpeg should be auto-detected"
    assert settings.ffprobe
    settings.port = 9999
    save_settings(settings)
    again = load_settings(data_dir)
    assert again.port == 9999
    assert Settings(**again.to_dict()).host == "127.0.0.1"


def test_db_lifecycle(data_dir: Path) -> None:
    settings = load_settings(data_dir)
    db.init_db(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        book_id = db.create_book(conn, sha256="a" * 64, format="epub", original_path="x.epub")
        assert db.find_book_by_sha(conn, "a" * 64)["id"] == book_id
        chapters = [
            {"title": "序言", "text": "清晨的列车", "read": True},
            {"title": "出发", "text": "午后的车站", "read": True},
            {"title": "出发", "text": "傍晚的灯光", "read": False},
        ]
        revision_id = db.create_revision(conn, book_id, chapters, note="initial")
        db.set_book_parsed(conn, book_id, title="示例书", author="作者", cover_path=None)
        book = db.get_book(conn, book_id)
        assert book["status"] == "ready" and book["title"] == "示例书"

        listed = db.list_chapters(conn, revision_id)
        assert [c["title"] for c in listed] == ["序言", "出发", "出发"]
        assert [c["read"] for c in listed] == [1, 1, 0]

        job_id = db.create_job(
            conn,
            book_id=book_id,
            revision_id=revision_id,
            engine="edge",
            model="edge-neural",
            voice="zh-CN-XiaoxiaoNeural",
            rate="+0%",
            formats=["m4b", "mp3"],
            total_segments=2,
            total_chapters=2,
            request_key="rk1",
        )
        assert db.find_job_by_request_key(conn, "rk1")["id"] == job_id
        db.create_segments(
            conn,
            job_id,
            [
                {"chapter_idx": 1, "ord": 1, "text": "清晨的列车", "cache_key": "k1"},
                {"chapter_idx": 2, "ord": 1, "text": "午后的车站", "cache_key": "k2"},
            ],
        )
        segs = db.list_segments(conn, job_id)
        db.mark_segment_done(conn, segs[0]["id"], audio_path="a.wav", duration_ms=1500)
        db.mark_segment_failed(conn, segs[1]["id"], "boom")
        assert db.segment_stats(conn, job_id) == {"total": 2, "done": 1, "failed": 1}
        assert db.reset_segment_failures(conn, job_id) == 1

        claimed = db.claim_next_job(conn)
        assert claimed is not None and claimed["state"] == "synthesizing"
        assert db.claim_next_job(conn) is None
        db.set_job_state(conn, job_id, "packaging")
        assert db.reset_running_jobs(conn) == [job_id]

        first = db.upsert_artifact(
            conn, job_id=job_id, kind="m4b", path="b.m4b", bytes=100, duration_ms=2000
        )
        db.upsert_artifact(
            conn, job_id=job_id, kind="m4b", path="b.m4b", bytes=120, duration_ms=2000
        )
        m4b_rows = [a for a in db.list_artifacts(conn, job_id) if a["kind"] == "m4b"]
        assert len(m4b_rows) == 1, "whole-book artifact upsert must not duplicate"
        assert m4b_rows[0]["bytes"] == 120
        assert first > 0
        mp3_id = db.upsert_artifact(
            conn, job_id=job_id, kind="mp3", chapter_idx=1, path="001 - 序言.mp3", bytes=50
        )
        arts = db.list_artifacts(conn, job_id)
        assert len(arts) == 2
        assert db.get_artifact(conn, mp3_id)["kind"] == "mp3"

        db.set_job_result(
            conn,
            job_id,
            m4b_bytes=120,
            m4b_duration_ms=2000,
            m4b_filename="示例书 - 晓晓 - v01.m4b",
            mp3_complete=True,
        )
        job = db.get_job(conn, job_id)
        assert job["mp3_complete"] == 1 and job["m4b_filename"].endswith(".m4b")

        db.create_preview(
            conn, preview_id="p1", engine="edge", model="m", voice="v", rate="+0%", text="hi"
        )
        db.set_preview_result(conn, "p1", path="p1.mp3", duration_ms=1000)
        assert db.get_preview(conn, "p1")["status"] == "ready"

        books = db.list_books(conn)
        assert books[0]["chapter_count"] == 3
    finally:
        conn.close()
