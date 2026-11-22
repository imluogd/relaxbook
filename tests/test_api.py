"""A4 网页接口测试：FastAPI TestClient + 临时数据目录。

并行开发约定：
* ``relaxbook.ingest.parse_book`` 用 monkeypatch 替换（上传解析在后台线程）。
* ``relaxbook.tts.get_engine`` / ``list_voices`` 用假引擎替换（不联网）。
* ``relaxbook.textnorm.segment_text`` / ``segment_cache_key`` 用确定性替身，
  避免在 A1 完成前阻塞；接口本身仍然通过模块属性调用它们，所以 A1 实现后
  生产代码行为不变。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

# 必须在导入 relaxbook.main 之前指定默认数据目录，避免模块级 app 在真实用户
# 目录建库（本机沙箱可能不可写）。数据目录放在仓库外的 D:\dsh\tmp（或系统临时
# 目录），绝不在仓库根下创建。
def _test_data_home() -> str:
    repo_root = Path(__file__).resolve().parent.parent
    sibling_tmp = repo_root.parent / "tmp"
    try:
        sibling_tmp.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="rb-api-home-", dir=str(sibling_tmp))
    except OSError:
        return tempfile.mkdtemp(prefix="rb-api-home-")


os.environ["RELAXBOOK_DATA_DIR"] = _test_data_home()

from fastapi.testclient import TestClient  # noqa: E402

from relaxbook import api as api_module  # noqa: E402
from relaxbook import db, ingest, textnorm, tts  # noqa: E402
from relaxbook.config import Settings  # noqa: E402
from relaxbook.ingest.types import ChapterDraft, ParsedBook  # noqa: E402
from relaxbook.main import create_app  # noqa: E402

BOOK_BYTES = b"%PDF-1.4 fake relaxbook test payload " + b"\x00" * 64
# 假引擎返回的固定“MP3”字节（接口只做字节透传，不真正解码）。
FAKE_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00" + b"\x00" * 96

FAKE_VOICES = [
    SimpleNamespace(
        id="zh-CN-XiaoxiaoNeural", name="晓晓（温暖女声）", language="zh-CN", gender="Female", engine="edge"
    ),
    SimpleNamespace(
        id="zh-CN-XiaoyiNeural", name="晓伊（活泼女声）", language="zh-CN", gender="Female", engine="edge"
    ),
    SimpleNamespace(
        id="zh-CN-YunxiNeural", name="云希（年轻男声）", language="zh-CN", gender="Male", engine="edge"
    ),
    SimpleNamespace(
        id="zh-CN-YunjianNeural", name="云健（沉稳男声）", language="zh-CN", gender="Male", engine="edge"
    ),
    SimpleNamespace(
        id="zh-CN-YunyangNeural", name="云扬（新闻男声）", language="zh-CN", gender="Male", engine="edge"
    ),
    SimpleNamespace(id="en-US-AriaNeural", name="Aria（英文女声）", language="en-US", gender="Female", engine="edge"),
    SimpleNamespace(id="en-US-GuyNeural", name="Guy（英文男声）", language="en-US", gender="Male", engine="edge"),
]

CHAPTER_TEXTS = [
    "清晨的列车缓缓驶出站台。窗外的田野在薄雾里铺展开来。这是故事的开始。",
    "午后的车站人来人往。他提着旧皮箱走上月台。远处传来汽笛声。",
    "傍晚的灯光亮起。她在小城的街道上慢慢走着。夜色渐深。",
]


# ---------------------------------------------------------------------------
# 并行模块替身
# ---------------------------------------------------------------------------
def fake_parse_book(path, *, format, settings, **kwargs):
    """替身解析：三章，其中两章同名「出发」，一章「序言」。"""
    return ParsedBook(
        title="示例书",
        author="测试作者",
        format=format,
        chapters=[
            ChapterDraft(
                title="序言",
                text=CHAPTER_TEXTS[0],
                read=True,
                source_locator={"pages": [1]},
                warnings=[],
            ),
            ChapterDraft(
                title="出发",
                text=CHAPTER_TEXTS[1],
                read=True,
                source_locator={"pages": [2]},
                warnings=["目录未覆盖，按正文补入"],
            ),
            ChapterDraft(
                title="出发",
                text=CHAPTER_TEXTS[2],
                read=True,
                source_locator={"pages": [3]},
                warnings=[],
            ),
        ],
        cover_bytes=b"\x89PNG\r\n\x1a\nfake-cover",
        cover_ext="png",
        warnings=["PDF 低文本量页：2"],
    )


def fake_segment_text(text, *, max_chars=220, **kwargs):
    """按句号/问号/感叹号切分，行为确定，便于核对 total_segments。"""
    pieces: list[str] = []
    for line in (text or "").split("\n"):
        for match in re.findall(r"[^。！？；!?;]+[。！？；!?;]?", line):
            piece = match.strip()
            if piece:
                pieces.append(piece)
    return pieces


def fake_segment_cache_key(text, *, engine, model, voice, rate, **kwargs):
    payload = f"{engine}|{model}|{voice}|{rate}|{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FakeEngine:
    name = "edge"
    model = "edge-neural"

    def __init__(self) -> None:
        self.synth_calls: list[tuple[str, str, str]] = []

    def voices(self):
        return list(FAKE_VOICES)

    def synthesize(self, text, voice, rate):
        self.synth_calls.append((text, voice, rate))
        return FAKE_MP3


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def settings(tmp_path):
    result = Settings(data_dir=str(tmp_path))
    result.ensure_dirs()
    db.init_db(result.db_path)
    return result


@pytest.fixture()
def fake_engine(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(tts, "get_engine", lambda name, settings: engine)
    monkeypatch.setattr(tts, "list_voices", lambda settings: list(FAKE_VOICES))
    return engine


@pytest.fixture()
def client(settings, fake_engine, monkeypatch):
    monkeypatch.setattr(api_module, "parse_book", fake_parse_book)
    monkeypatch.setattr(textnorm, "segment_text", fake_segment_text)
    monkeypatch.setattr(textnorm, "segment_cache_key", fake_segment_cache_key)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def upload(client, content=BOOK_BYTES, filename="示例书.epub"):
    return client.post(
        "/api/books",
        files={"file": (filename, content, "application/epub+zip")},
    )


def wait_for(fetch, predicate, timeout=6.0, interval=0.05, message="等待超时"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fetch()
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"{message}；最后一次结果：{last!r}")


def make_ready_book(client):
    response = upload(client)
    assert response.status_code == 202, response.text
    book_id = response.json()["book_id"]

    def fetch_detail():
        return client.get(f"/api/books/{book_id}").json()

    detail = wait_for(
        fetch_detail,
        lambda data: data.get("book", {}).get("status") in ("ready", "failed"),
        message="书籍解析超时",
    )
    assert detail["book"]["status"] == "ready", detail["book"].get("error")
    return book_id, detail


def chapter_texts(client, book_id, detail):
    texts = []
    for chapter in detail["chapters"]:
        payload = client.get(f"/api/books/{book_id}/chapters/{chapter['id']}").json()
        texts.append(payload["chapter"]["text"])
    return texts


def expected_segment_count(texts):
    return sum(len(fake_segment_text(text)) for text in texts)


def latest_segments(settings, job_id):
    conn = db.connect(settings.db_path)
    try:
        return db.list_segments(conn, job_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 上传 / 书库 / 章节读取
# ---------------------------------------------------------------------------
def test_upload_duplicate_library_and_chapters(client):
    response = upload(client)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["duplicate"] is False
    assert payload["status"] == "parsing"
    book_id = payload["book_id"]
    assert isinstance(book_id, int)

    detail = wait_for(
        lambda: client.get(f"/api/books/{book_id}").json(),
        lambda data: data.get("book", {}).get("status") in ("ready", "failed"),
        message="书籍解析超时",
    )
    assert detail["book"]["status"] == "ready", detail["book"].get("error")
    assert detail["book"]["title"] == "示例书"
    assert detail["book"]["author"] == "测试作者"
    assert detail["book"]["format"] == "epub"
    assert detail["warnings"] == ["PDF 低文本量页：2"]
    assert detail["revision"]["chapter_count"] == 3
    assert [chapter["title"] for chapter in detail["chapters"]] == ["序言", "出发", "出发"]
    assert [chapter["idx"] for chapter in detail["chapters"]] == [1, 2, 3]
    assert all(chapter["read"] is True for chapter in detail["chapters"])
    assert detail["chapters"][0]["locator"] == {"pages": [1]}
    assert detail["chapters"][1]["warnings"] == ["目录未覆盖，按正文补入"]

    chapter_id = detail["chapters"][0]["id"]
    chapter_payload = client.get(f"/api/books/{book_id}/chapters/{chapter_id}").json()
    assert chapter_payload["chapter"]["title"] == "序言"
    assert "清晨的列车" in chapter_payload["chapter"]["text"]
    assert chapter_payload["chapter"]["read"] is True

    # 重复上传相同内容：返回已有书籍，不重复解析。
    duplicate = upload(client)
    assert duplicate.status_code == 202
    assert duplicate.json() == {"book_id": book_id, "status": "ready", "duplicate": True}

    # 书库列表字段。
    books = client.get("/api/books").json()["books"]
    assert len(books) == 1
    book = books[0]
    for key in (
        "id",
        "title",
        "author",
        "format",
        "status",
        "revision_id",
        "chapter_count",
        "created_at",
        "updated_at",
        "error",
    ):
        assert key in book
    assert book["chapter_count"] == 3
    assert book["revision_id"] is not None

    # 封面与 404。
    cover = client.get(f"/api/books/{book_id}/cover")
    assert cover.status_code == 200
    assert cover.headers["content-type"].startswith("image/png")
    assert cover.content.startswith(b"\x89PNG")
    assert client.get("/api/books/999999").status_code == 404
    assert client.get(f"/api/books/{book_id}/chapters/999999").status_code == 404

    # 空文件 / 不支持的格式。
    empty = client.post("/api/books", files={"file": ("空.epub", b"", "application/epub+zip")})
    assert empty.status_code == 400
    bad = upload(client, content=b"not a book", filename="说明.txt")
    assert bad.status_code == 400
    assert isinstance(bad.json()["detail"], str) and bad.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 编辑版本（PUT revision）与 409
# ---------------------------------------------------------------------------
def test_save_revision_and_conflict(client):
    book_id, detail = make_ready_book(client)
    base_revision_id = detail["revision"]["id"]
    texts = chapter_texts(client, book_id, detail)

    revised = [
        {"title": "序言（修改）", "text": texts[0], "read": True, "source_locator": {"pages": [1]}},
        {"title": "出发", "text": texts[1], "read": False, "source_locator": {"pages": [2]}},
        {"title": "出发", "text": texts[2] + "新增一句。", "read": True, "source_locator": {"pages": [3]}},
    ]
    response = client.put(
        f"/api/books/{book_id}/revision",
        json={"base_revision_id": base_revision_id, "note": "测试编辑", "chapters": revised},
    )
    assert response.status_code == 200, response.text
    new_revision_id = response.json()["revision_id"]
    assert new_revision_id > base_revision_id

    updated = client.get(f"/api/books/{book_id}").json()
    assert updated["revision"]["id"] == new_revision_id
    assert updated["revision"]["chapter_count"] == 3
    assert [chapter["title"] for chapter in updated["chapters"]] == ["序言（修改）", "出发", "出发"]
    assert updated["chapters"][1]["read"] is False

    # 用过期版本再次提交 -> 409。
    stale = client.put(
        f"/api/books/{book_id}/revision",
        json={"base_revision_id": base_revision_id, "note": "过期", "chapters": revised},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == "版本已过期，请刷新后重试"

    # 空章节列表 -> 400。
    empty = client.put(
        f"/api/books/{book_id}/revision",
        json={"base_revision_id": new_revision_id, "note": "", "chapters": []},
    )
    assert empty.status_code == 400


# ---------------------------------------------------------------------------
# 3. 音色与试听
# ---------------------------------------------------------------------------
def test_voices_and_preview(client):
    voices_payload = client.get("/api/voices").json()
    voices = voices_payload["voices"]
    assert len(voices) >= 4
    chinese = [voice for voice in voices if voice["language"].startswith("zh")]
    english = [voice for voice in voices if voice["language"].startswith("en")]
    assert len(chinese) >= 3
    assert len(english) >= 1
    for voice in voices:
        assert voice["id"] and voice["name"] and voice["gender"] and voice["engine"]
    engines = voices_payload["engines"]
    assert engines and engines[0]["name"] == "edge"
    assert engines[0]["available"] is True

    created = client.post(
        "/api/previews",
        json={"text": "试听文本：清晨的列车。", "voice": chinese[0]["id"], "rate": "+0%"},
    )
    assert created.status_code == 202, created.text
    preview_id = created.json()["preview_id"]

    status_payload = wait_for(
        lambda: client.get(f"/api/previews/{preview_id}").json(),
        lambda data: data.get("status") in ("ready", "failed"),
        timeout=4.0,
        message="试听合成超时",
    )
    assert status_payload["status"] == "ready", status_payload.get("error")
    assert status_payload["error"] is None
    assert status_payload["audio_url"] == f"/api/previews/{preview_id}/audio"

    audio = client.get(status_payload["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert audio.content == FAKE_MP3

    assert client.get("/api/previews/not-exist").status_code == 404


# ---------------------------------------------------------------------------
# 4. 任务创建去重 / 详情形状 / 状态转换
# ---------------------------------------------------------------------------
def test_job_create_detail_and_transitions(client, settings):
    book_id, detail = make_ready_book(client)
    revision_id = detail["revision"]["id"]
    texts = chapter_texts(client, book_id, detail)
    voice = FAKE_VOICES[0].id

    body = {"book_id": book_id, "revision_id": revision_id, "voice": voice, "engine": "edge"}
    created = client.post("/api/jobs", json=body)
    assert created.status_code == 202, created.text
    assert created.json()["duplicate"] is False
    job_id = created.json()["job_id"]

    again = client.post("/api/jobs", json=body)
    assert again.status_code == 202
    assert again.json() == {"job_id": job_id, "duplicate": True}

    payload = client.get(f"/api/jobs/{job_id}").json()
    assert set(payload) == {"job", "book", "m4b", "chapters"}
    job = payload["job"]
    for key in (
        "id",
        "state",
        "voice",
        "engine",
        "rate",
        "created_at",
        "updated_at",
        "error",
        "total_segments",
        "done_segments",
        "failed_segments",
        "done_chapters",
        "total_chapters",
    ):
        assert key in job
    assert job["id"] == job_id
    assert job["state"] == "queued"
    assert job["voice"] == voice
    assert job["engine"] == "edge"
    assert job["total_chapters"] == 3
    assert job["done_chapters"] == 0
    assert job["done_segments"] == 0
    assert job["failed_segments"] == 0
    assert job["total_segments"] == expected_segment_count(texts)
    assert job["total_segments"] > 0
    assert payload["book"]["id"] == book_id
    assert payload["book"]["title"] == "示例书"
    assert payload["m4b"] is None
    chapters = payload["chapters"]
    assert len(chapters) == 3
    assert [chapter["index"] for chapter in chapters] == [1, 2, 3]
    assert [chapter["title"] for chapter in chapters] == ["序言", "出发", "出发"]
    assert [chapter["mp3_filename"] for chapter in chapters] == [
        "001 - 序言.mp3",
        "002 - 出发.mp3",
        "003 - 出发.mp3",
    ]
    assert len({chapter["mp3_filename"] for chapter in chapters}) == 3
    for chapter in chapters:
        assert chapter["read"] is True
        assert chapter["mp3_artifact_id"] is None
        assert chapter["mp3_bytes"] is None
        assert chapter["mp3_duration_ms"] is None
        assert chapter["m4b_start_ms"] is None
        assert chapter["m4b_end_ms"] is None
        assert chapter["state"] == "pending"

    # 暂停：仅 queued/synthesizing 合法。
    paused = client.post(f"/api/jobs/{job_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert client.post(f"/api/jobs/{job_id}/pause").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    resumed = client.post(f"/api/jobs/{job_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "queued"
    assert client.post(f"/api/jobs/{job_id}/resume").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409

    # 制造失败再重试：失败片段应被重置为 pending。
    conn = db.connect(settings.db_path)
    try:
        segments = db.list_segments(conn, job_id)
        db.mark_segment_failed(conn, segments[0]["id"], "测试失败")
        db.set_job_state(conn, job_id, "failed", error="测试失败")
    finally:
        conn.close()
    retried = client.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["state"] == "queued"
    assert all(segment["status"] == "pending" for segment in latest_segments(settings, job_id))

    # 失败任务再次 POST 同一请求：应创建新任务而不是返回失败旧任务。
    conn = db.connect(settings.db_path)
    try:
        db.set_job_state(conn, job_id, "failed", error="再次失败")
    finally:
        conn.close()
    fresh = client.post("/api/jobs", json=body)
    assert fresh.status_code == 202
    assert fresh.json()["duplicate"] is False
    assert fresh.json()["job_id"] != job_id

    # done 状态不接受暂停/继续/重试。
    conn = db.connect(settings.db_path)
    try:
        db.set_job_state(conn, job_id, "done")
    finally:
        conn.close()
    for action in ("pause", "resume", "retry"):
        response = client.post(f"/api/jobs/{job_id}/{action}")
        assert response.status_code == 409, (action, response.text)

    assert client.get("/api/jobs/999999").status_code == 404


# ---------------------------------------------------------------------------
# 5. 成品信息、下载与 ZIP
# ---------------------------------------------------------------------------
def test_artifacts_result_shape_and_zip(client, settings, monkeypatch):
    book_id, detail = make_ready_book(client)
    revision_id = detail["revision"]["id"]
    voice = FAKE_VOICES[0].id
    created = client.post(
        "/api/jobs", json={"book_id": book_id, "revision_id": revision_id, "voice": voice}
    )
    job_id = created.json()["job_id"]

    names = ["001 - 序言.mp3", "002 - 出发.mp3", "003 - 出发.mp3"]
    conn = db.connect(settings.db_path)
    try:
        job = db.get_job(conn, job_id)
        chapters = [c for c in db.list_chapters(conn, int(job["revision_id"])) if c["read"]]
        mp3_dir = settings.jobs_dir / str(job_id) / "v01"
        mp3_dir.mkdir(parents=True, exist_ok=True)
        mp3_ids = []
        for index, (chapter, name) in enumerate(zip(chapters, names), start=1):
            path = mp3_dir / name
            path.write_bytes(FAKE_MP3 + bytes([index]))
            mp3_ids.append(
                db.upsert_artifact(
                    conn,
                    job_id=job_id,
                    kind="mp3",
                    chapter_idx=int(chapter["idx"]),
                    path=str(path),
                    bytes=path.stat().st_size,
                    duration_ms=120000 + index * 1000,
                    verified=True,
                )
            )
        m4b_path = settings.jobs_dir / str(job_id) / "示例书 - 夏夏 - v01.m4b"
        m4b_path.write_bytes(FAKE_MP3 * 3)
        m4b_id = db.upsert_artifact(
            conn,
            job_id=job_id,
            kind="m4b",
            chapter_idx=None,
            path=str(m4b_path),
            bytes=m4b_path.stat().st_size,
            duration_ms=600000,
            verified=True,
        )
        db.set_job_result(
            conn,
            job_id,
            m4b_bytes=m4b_path.stat().st_size,
            m4b_duration_ms=600000,
            m4b_filename="示例书 - 夏夏 - v01.m4b",
            mp3_complete=True,
        )
        db.update_job_progress(conn, job_id, done_segments=job["total_segments"], done_chapters=3)
        db.set_job_state(conn, job_id, "done")
    finally:
        conn.close()

    payload = client.get(f"/api/jobs/{job_id}").json()
    m4b = payload["m4b"]
    assert m4b is not None
    assert m4b["artifact_id"] == m4b_id
    assert m4b["filename"] == "示例书 - 夏夏 - v01.m4b"
    assert m4b["duration_ms"] == 600000
    assert m4b["verified"] is True
    chapters = payload["chapters"]
    assert payload["job"]["done_chapters"] == 3
    assert payload["job"]["done_segments"] == payload["job"]["total_segments"]
    for index, chapter in enumerate(chapters):
        assert chapter["state"] == "done"
        assert chapter["mp3_artifact_id"] == mp3_ids[index]
        assert chapter["mp3_bytes"] == len(FAKE_MP3) + 1
        assert chapter["mp3_duration_ms"] == 120000 + (index + 1) * 1000
        # 测试环境没有 ffprobe/真实成品，探测失败时章节起止为空。
        assert chapter["m4b_start_ms"] is None
        assert chapter["m4b_end_ms"] is None

    # M4B 章节起止：探测失败 -> null；探测成功 -> 按朗读顺序映射且按任务缓存。
    settings.ffprobe = "ffprobe-fake"
    probe_calls = []

    def failing_probe(path, *, ffprobe, **kwargs):
        probe_calls.append(path)
        raise RuntimeError("ffprobe 探测失败")

    monkeypatch.setattr(api_module.audio, "probe_chapters", failing_probe)
    failed_probe = client.get(f"/api/jobs/{job_id}").json()
    assert all(chapter["m4b_start_ms"] is None for chapter in failed_probe["chapters"])

    def good_probe(path, *, ffprobe, **kwargs):
        probe_calls.append(path)
        return [
            {"id": "1", "title": "序言", "start_ms": 0, "end_ms": 120000},
            {"id": "2", "title": "出发", "start_ms": 120000, "end_ms": 240000},
            {"id": "3", "title": "出发", "start_ms": 240000, "end_ms": 360000},
        ]

    monkeypatch.setattr(api_module.audio, "probe_chapters", good_probe)
    probed = client.get(f"/api/jobs/{job_id}").json()
    assert [chapter["m4b_start_ms"] for chapter in probed["chapters"]] == [0, 120000, 240000]
    assert [chapter["m4b_end_ms"] for chapter in probed["chapters"]] == [120000, 240000, 360000]
    call_count = len(probe_calls)
    client.get(f"/api/jobs/{job_id}")
    assert len(probe_calls) == call_count

    # 单章 MP3 下载：UTF-8 文件名头 + 字节一致。
    download = client.get(f"/api/artifacts/{mp3_ids[0]}")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/mpeg")
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=UTF-8''")
    assert quote("001 - 序言.mp3", safe="") in disposition
    assert download.content == FAKE_MP3 + b"\x01"
    assert client.get("/api/artifacts/999999").status_code == 404

    # M4B 下载。
    m4b_download = client.get(f"/api/artifacts/{m4b_id}")
    assert m4b_download.status_code == 200
    assert m4b_download.headers["content-type"].startswith("audio/mp4")

    # 记录不存在但文件已被删除 -> 404。
    conn = db.connect(settings.db_path)
    try:
        missing_id = db.upsert_artifact(
            conn,
            job_id=job_id,
            kind="mp3",
            chapter_idx=99,
            path=str(settings.jobs_dir / "missing.mp3"),
            bytes=1,
            verified=True,
        )
    finally:
        conn.close()
    assert client.get(f"/api/artifacts/{missing_id}").status_code == 404

    # ZIP：打包全部 MP3。
    zip_response = client.get(f"/api/jobs/{job_id}/zip")
    assert zip_response.status_code == 200
    assert zip_response.headers["content-type"].startswith("application/zip")
    archive = zipfile.ZipFile(io.BytesIO(zip_response.content))
    assert sorted(archive.namelist()) == sorted(names)
    assert archive.read("001 - 序言.mp3") == FAKE_MP3 + b"\x01"

    # 另一个没有成品的任务 -> 404。
    other = client.post(
        "/api/jobs",
        json={"book_id": book_id, "revision_id": revision_id, "voice": FAKE_VOICES[1].id},
    )
    other_job_id = other.json()["job_id"]
    assert client.get(f"/api/jobs/{other_job_id}/zip").status_code == 404


# ---------------------------------------------------------------------------
# 6. 三个页面与静态资源
# ---------------------------------------------------------------------------
def test_pages_render(client):
    book_id, detail = make_ready_book(client)
    job_id = client.post(
        "/api/jobs",
        json={"book_id": book_id, "revision_id": detail["revision"]["id"], "voice": FAKE_VOICES[0].id},
    ).json()["job_id"]

    library = client.get("/")
    assert library.status_code == 200
    assert "书库" in library.text
    assert "示例书" in library.text
    assert "上传并解析" in library.text

    book_page = client.get(f"/books/{book_id}")
    assert book_page.status_code == 200
    assert "目录与正文" in book_page.text
    assert "音色试听与生成" in book_page.text
    assert "保存新编辑版本" in book_page.text

    job_page = client.get(f"/jobs/{job_id}")
    assert job_page.status_code == 200
    assert "转换结果" in job_page.text
    assert "下载有章节的 M4B" in job_page.text
    assert "章节清单" in job_page.text

    for asset in ("/static/style.css", "/static/common.js", "/static/book.js", "/static/job.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert response.text.strip()

    assert client.get("/books/999999").status_code == 404
    assert client.get("/jobs/999999").status_code == 404


# ---------------------------------------------------------------------------
# 7. 解析失败（扫描件等）返回中文原因，且不建任何编辑版本
# ---------------------------------------------------------------------------
def test_parse_failure_reports_chinese_error(client, monkeypatch):
    def failing_parse(path, *, format, settings, **kwargs):
        raise ingest.IngestError("未提取到可读正文，疑似扫描件，需要先 OCR。")

    monkeypatch.setattr(api_module, "parse_book", failing_parse)
    response = upload(client, content=b"scanned-pdf-" + b"\x02" * 48, filename="扫描示例.pdf")
    assert response.status_code == 202
    book_id = response.json()["book_id"]

    detail = wait_for(
        lambda: client.get(f"/api/books/{book_id}").json(),
        lambda data: data.get("book", {}).get("status") != "parsing",
        message="解析失败状态超时",
    )
    assert detail["book"]["status"] == "failed"
    assert "OCR" in detail["book"]["error"]
    assert detail["revision"] is None
    assert detail["chapters"] == []
    assert client.get(f"/api/books/{book_id}/cover").status_code == 404

    # 失败书籍仍然出现在书库列表中，并带中文错误原因。
    books = client.get("/api/books").json()["books"]
    failed = [book for book in books if book["id"] == book_id][0]
    assert failed["status"] == "failed"
    assert "OCR" in failed["error"]
