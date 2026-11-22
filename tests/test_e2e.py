"""A2/A3 端到端验收：三章样书 -> 真实 Edge 合成 -> MP3 + M4B -> ffprobe 核对。

本文件只用真实模块（``ingest`` / ``textnorm`` / ``tts`` / ``worker`` /
``packaging`` / ``audio`` / ``db`` / ``api``），不 mock 任何业务逻辑。除本机
FFmpeg 外唯一的真实外部依赖是 Edge 在线语音服务。

网络策略（Edge 公共端点在本机不稳定，连接可能被重置）：

* 开始前做 3 次单次尝试的连通性预检；**全部**是连接类错误时 ``pytest.skip``。
* 若任务第一轮以网络类错误结束，按 A5 的恢复流程最多续跑 2 轮：
  ``db.reset_segment_failures`` + ``db.set_job_state(job_id, "queued")`` 后再次
  ``run_job(job_id)``。这本身就在验证 A5 的断点续跑：断言后续轮次只补失败
  片段（缓存命中数 > 0，未命中数 == 上一轮失败片段数）。
* 两轮恢复后仍未完成：再探测 3 次；仍全部是连接类错误才 skip，否则 fail，
  并打印逐轮进度、最后 3 条错误与网络诊断。
* 任务完成之后的所有 A2/A3 断言（文件名、章节、时长、解码、缓存复用）都是
  硬断言，失败即测试失败，绝不静默跳过或吞掉。

运行方式（可看到最后的耗时与产物摘要）::

    cd D:\\dsh\\relaxbook
    .venv\\Scripts\\python.exe -m pytest tests/test_e2e.py -q -s
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from relaxbook import api, audio, db, ingest, naming, tts
from relaxbook import worker
from relaxbook.config import Settings
from relaxbook.tts import TTSError

#: 与 product 默认一致：Edge 引擎 + 中文女声 + 正常语速。
ENGINE = "edge"
VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+0%"

#: ``sample/预期清单.md`` 中固定的预期目录（第 2、3 章同名）。
EXPECTED_TITLES = ["序言", "出发", "出发"]
EXPECTED_MP3_NAMES = ["001 - 序言.mp3", "002 - 出发.mp3", "003 - 出发.mp3"]
EXPECTED_CHAPTER_COUNT = 3
EXPECTED_TOTAL_SEGMENTS = 17  # 样书三章按当前分段规则得到 17 片（不丢字）

#: A2/A3 允许的误差（毫秒）。
SEAM_TOLERANCE_MS = 20
DURATION_TOLERANCE_MS = 1000
WEB_DISPLAY_TOLERANCE_MS = 1000

#: 首次失败后最多再做 2 轮「模拟 A5 恢复」。
MAX_RECOVERY_ROUNDS = 2

#: 连接类错误特征（edge 引擎把底层错误归类后拼进 TTSError 的中文消息）。
NETWORK_ERROR_MARKERS = (
    "连接",
    "网络",
    "recv",
    "reset",
    "timed out",
    "timeout",
    "failed to connect",
    "curl:",
    "ssl",
    "语音服务",
    "no audio",
    "无音频",
    "合成失败",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _private_settings(settings: Settings) -> Settings:
    """``settings`` fixture + 一个私有数据目录（避免并行 pytest 互删临时目录）。

    ``tests/conftest.py`` 把 pytest 的 basetemp 固定成**共享**路径
    （``<safe-temp>/pytest-tmp``），而 pytest 在每个会话第一次使用 ``tmp_path``
    时会递归删除 basetemp。多个 agent 并行跑 pytest 时，别人的会话会在本测试
    中途删掉 ``settings`` 的数据目录（实测：M4B 刚生成就被删除，ffprobe 报
    No such file or directory）。因此在 conftest 保证「不在仓库内」的临时根目录
    下再建一个本次进程独有的目录；工具路径/音色等仍来自 ``settings`` fixture。
    """
    private_dir = Path(tempfile.mkdtemp(prefix="relaxbook-e2e-"))
    private = dataclasses.replace(settings, data_dir=str(private_dir))
    private.ensure_dirs()
    db.init_db(private.db_path)
    return private


def _is_network_error(message: str | None) -> bool:
    """判断一条错误消息是否属于网络/服务不可用（而不是业务或校验错误）。"""
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in NETWORK_ERROR_MARKERS)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """与 api/worker 相同口径：临时文件 + ``os.replace`` 原子改名。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            pass


def _connect(settings: Settings):
    return db.connect(settings.db_path)


# ---------------------------------------------------------------------------
# 链路步骤 1：导入样书（与 api.py 上传解析后落库的口径一致）
# ---------------------------------------------------------------------------
def _seed_book(settings: Settings, epub_path: Path) -> tuple[int, int]:
    """把样书放进数据目录，解析并落库；返回 ``(book_id, revision_id)``。"""
    data = epub_path.read_bytes()
    sha256 = _sha256_bytes(data)
    destination = settings.books_dir / sha256[:16] / "original.epub"
    _atomic_write(destination, data)

    parsed = ingest.parse_book(destination, format="epub", settings=settings)
    assert parsed.title, "解析结果缺少书名"
    assert len(parsed.chapters) == EXPECTED_CHAPTER_COUNT, (
        f"样书应有 {EXPECTED_CHAPTER_COUNT} 章，实际 {len(parsed.chapters)} 章"
    )

    chapters: list[dict[str, Any]] = []
    for chapter in parsed.chapters:
        chapters.append(
            {
                "title": chapter.title,
                "text": chapter.text,
                "read": bool(chapter.read),
                "source_locator": chapter.source_locator or None,
                "warnings": list(chapter.warnings or []),
            }
        )

    conn = _connect(settings)
    try:
        book_id = db.create_book(
            conn,
            sha256=sha256,
            format="epub",
            original_path=str(destination),
            title=parsed.title,
            status="parsing",
        )
        revision_id = db.create_revision(conn, book_id, chapters, note="A5 端到端验收")
        db.set_book_parsed(
            conn,
            book_id,
            title=parsed.title,
            author=parsed.author,
            cover_path=None,
            warnings=list(parsed.warnings or []),
        )
    finally:
        conn.close()
    return book_id, revision_id


# ---------------------------------------------------------------------------
# 链路步骤 2：按 api.py 的创建任务口径切片段并落库
# ---------------------------------------------------------------------------
def _plan_segments_like_api(
    settings: Settings,
    revision_id: int,
    *,
    engine_name: str = ENGINE,
    voice: str = VOICE,
    rate: str = RATE,
) -> tuple[list[dict[str, Any]], int, str]:
    """复用 ``relaxbook.api._plan_segments``，保证与网页创建任务的切片完全一致。"""
    planner = getattr(api, "_plan_segments", None)
    assert planner is not None, (
        "relaxbook.api._plan_segments 不存在：网页创建任务的分段口径已变化，"
        "请同步更新 tests/test_e2e.py。"
    )
    conn = _connect(settings)
    try:
        return planner(conn, settings, revision_id, engine_name, voice, rate)
    finally:
        conn.close()


def _create_job(
    settings: Settings,
    *,
    book_id: int,
    revision_id: int,
    items: list[dict[str, Any]],
    total_chapters: int,
    model: str,
    request_key: str | None,
    engine_name: str = ENGINE,
    voice: str = VOICE,
    rate: str = RATE,
) -> int:
    conn = _connect(settings)
    try:
        job_id = db.create_job(
            conn,
            book_id=book_id,
            revision_id=revision_id,
            engine=engine_name,
            model=model,
            voice=voice,
            rate=rate,
            formats=["m4b", "mp3"],
            total_segments=len(items),
            total_chapters=total_chapters,
            request_key=request_key,
        )
        db.create_segments(conn, job_id, items)
    finally:
        conn.close()
    return job_id


def _api_request_key(revision_id: int, *, voice: str = VOICE, rate: str = RATE) -> str:
    """与 ``api.py`` 的 ``POST /api/jobs`` 完全相同的去重键。"""
    return hashlib.sha256(
        f"{revision_id}|{ENGINE}|{voice}|{rate}".encode("utf-8")
    ).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 状态读取 / 单轮执行
# ---------------------------------------------------------------------------
def _snapshot(settings: Settings, job_id: int) -> dict[str, Any]:
    conn = _connect(settings)
    try:
        job_row = db.get_job(conn, job_id)
        assert job_row is not None, f"任务不存在：job={job_id}"
        job = dict(job_row)
        stats = db.segment_stats(conn, job_id)
        segments = db.list_segments(conn, job_id)
        artifacts = db.list_artifacts(conn, job_id)
    finally:
        conn.close()
    return {
        "job": job,
        "stats": stats,
        "segments": segments,
        "artifacts": artifacts,
        "failed_errors": [
            str(segment.get("error") or "") for segment in segments if segment["status"] == "failed"
        ],
    }


def _count_log(records: list[logging.LogRecord], needle: str) -> int:
    return sum(1 for record in records if needle in record.getMessage())


def _run_one_round(
    settings: Settings,
    job_id: int,
    caplog: pytest.LogCaptureFixture,
    *,
    round_index: int,
    previous_failed: int = 0,
) -> dict[str, Any]:
    """跑一轮 ``JobRunner.run_job``，返回本轮进度与缓存命中统计。"""
    caplog.set_level(logging.INFO, logger="relaxbook.worker")
    before = len(caplog.records)
    started = time.perf_counter()
    worker.JobRunner(settings).run_job(job_id)
    elapsed = time.perf_counter() - started
    records = caplog.records[before:]
    snapshot = _snapshot(settings, job_id)
    result = {
        "round": round_index,
        "previous_failed": previous_failed,
        "elapsed_s": elapsed,
        "state": snapshot["job"]["state"],
        "done_segments": snapshot["stats"]["done"],
        "total_segments": snapshot["stats"]["total"],
        "failed_segments": snapshot["stats"]["failed"],
        "done_chapters": int(snapshot["job"]["done_chapters"] or 0),
        "error": snapshot["job"].get("error"),
        "failed_errors": snapshot["failed_errors"],
        "cache_hit": _count_log(records, "cache=hit"),
        "cache_miss": _count_log(records, "cache=miss"),
    }
    print(
        f"[A5 第 {round_index} 轮] 状态={result['state']} "
        f"片段 {result['done_segments']}/{result['total_segments']} "
        f"失败={result['failed_segments']} 章={result['done_chapters']} "
        f"缓存命中={result['cache_hit']} 未命中={result['cache_miss']} "
        f"耗时={elapsed:.2f}s",
        flush=True,
    )
    return result


def _simulate_a5_recovery(settings: Settings, job_id: int) -> int:
    """A5 恢复：把失败片段重置为 pending，任务重新排队（缓存保留）。"""
    conn = _connect(settings)
    try:
        reset = db.reset_segment_failures(conn, job_id)
        db.set_job_state(conn, job_id, "queued")
    finally:
        conn.close()
    assert reset > 0, "A5 恢复时没有可重置的失败片段，无法验证断点续跑"
    print(f"[A5 恢复] 已把 {reset} 个失败片段重置为待合成，任务重新排队", flush=True)
    return reset


# ---------------------------------------------------------------------------
# 网络预检
# ---------------------------------------------------------------------------
def _probe_network(settings: Settings, attempts: int = 3) -> tuple[bool, list[str]]:
    """用真实 Edge 引擎做最多 ``attempts`` 次单次尝试的连通性预检。

    预检实例把引擎重试次数降到 1：诊断目的只是判断网络通不通，不应该为
    "网络不可用" 花掉 5 次 x 20 秒的连接超时（测试总时长预算 3-6 分钟）。
    """
    errors: list[str] = []
    for _ in range(max(1, attempts)):
        engine = tts.get_engine(ENGINE, settings)
        if hasattr(engine, "attempts"):  # pragma: no branch - 只影响诊断耗时
            engine.attempts = 1
        try:
            data = engine.synthesize("网络连通性测试", VOICE, RATE)
        except Exception as exc:  # noqa: BLE001 - 诊断：任何异常都记录下来
            errors.append(f"{exc.__class__.__name__}: {exc}")
            continue
        if isinstance(data, (bytes, bytearray)) and data:
            return True, []
        errors.append("合成返回空音频")
    return False, errors


def _require_network(settings: Settings) -> None:
    """网络不可用（3 次单次尝试全部连接类错误）时 skip，而不是 fail。"""
    available, errors = _probe_network(settings)
    if available:
        return
    if all(_is_network_error(error) for error in errors):
        pytest.skip(
            "Edge TTS 网络不可用（3 次单次尝试全部为连接类错误）："
            + (errors[-1] if errors else "未知错误")
        )
    pytest.fail("Edge TTS 预检失败（非连接类错误）：" + " | ".join(errors))


# ---------------------------------------------------------------------------
# 产物核对（A2/A3 硬断言）
# ---------------------------------------------------------------------------
def _artifacts_of(artifacts: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [artifact for artifact in artifacts if artifact["kind"] == kind]


def _check_mp3_artifacts(
    settings: Settings, artifacts: list[dict[str, Any]]
) -> tuple[list[Path], list[dict[str, Any]]]:
    mp3_artifacts = sorted(
        (
            artifact
            for artifact in artifacts
            if artifact["kind"] == "mp3" and artifact["chapter_idx"] is not None
        ),
        key=lambda artifact: int(artifact["chapter_idx"]),
    )
    assert len(mp3_artifacts) == EXPECTED_CHAPTER_COUNT, (
        f"MP3 成品应有 {EXPECTED_CHAPTER_COUNT} 个，实际 {len(mp3_artifacts)} 个："
        f"{[Path(a['path']).name for a in mp3_artifacts]}"
    )
    names = [Path(artifact["path"]).name for artifact in mp3_artifacts]
    assert names == EXPECTED_MP3_NAMES, f"MP3 文件名不符合预期：{names}"
    assert len(set(names)) == EXPECTED_CHAPTER_COUNT, f"MP3 文件名重复：{names}"

    paths: list[Path] = []
    probes: list[dict[str, Any]] = []
    for artifact in mp3_artifacts:
        path = Path(artifact["path"])
        assert path.is_file(), f"MP3 文件不存在：{path}"
        assert int(artifact["verified"]) == 1, f"MP3 未标记为已校验：{path.name}"
        # 可完整解码（AudioError 会让测试直接失败）
        audio.verify_decodable(path, ffmpeg=settings.ffmpeg)
        probe = audio.probe(path, ffprobe=settings.ffprobe)
        assert int(probe["duration_ms"]) > 0, f"MP3 探测时长为 0：{path.name}"
        assert probe["sample_rate"] == audio.FRAME_RATE, (
            f"MP3 采样率应为 {audio.FRAME_RATE}，实际 {probe['sample_rate']}：{path.name}"
        )
        assert int(artifact["bytes"]) == path.stat().st_size, (
            f"MP3 记录大小与文件大小不一致：{path.name}"
        )
        paths.append(path)
        probes.append(probe)
    return paths, probes


def _check_m4b_artifact(
    settings: Settings, artifacts: list[dict[str, Any]]
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    m4b_artifacts = _artifacts_of(artifacts, "m4b")
    assert len(m4b_artifacts) == 1, f"M4B 成品应有 1 个，实际 {len(m4b_artifacts)} 个"
    artifact = m4b_artifacts[0]
    path = Path(artifact["path"])
    assert path.is_file(), f"M4B 文件不存在：{path}"
    assert int(artifact["verified"]) == 1, "M4B 未标记为已校验"

    probe = audio.probe(path, ffprobe=settings.ffprobe)
    chapters = audio.probe_chapters(path, ffprobe=settings.ffprobe)
    assert probe["duration_ms"] > 0, "M4B 探测时长为 0"
    assert len(chapters) == EXPECTED_CHAPTER_COUNT, (
        f"M4B 内嵌章节应为 {EXPECTED_CHAPTER_COUNT} 个，实际 {len(chapters)} 个"
    )
    titles = [str(chapter["title"]) for chapter in chapters]
    assert titles == EXPECTED_TITLES, f"M4B 章节标题/顺序不符合预期：{titles}"
    assert chapters[0]["start_ms"] == 0, f"M4B 首章起点应为 0ms，实际 {chapters[0]['start_ms']}ms"
    for position, (previous, current) in enumerate(zip(chapters, chapters[1:]), start=2):
        gap = abs(int(current["start_ms"]) - int(previous["end_ms"]))
        assert gap <= SEAM_TOLERANCE_MS, (
            f"M4B 第 {position} 章与上一章未首尾相接："
            f"上一章末 {previous['end_ms']}ms，本章起 {current['start_ms']}ms"
        )
        assert int(current["end_ms"]) > int(current["start_ms"]), (
            f"M4B 第 {position} 章时长无效"
        )
    last_end = int(chapters[-1]["end_ms"])
    assert abs(int(probe["duration_ms"]) - last_end) <= DURATION_TOLERANCE_MS, (
        f"M4B 末章终点 {last_end}ms 与整书时长 {probe['duration_ms']}ms 相差超过 "
        f"{DURATION_TOLERANCE_MS}ms"
    )

    verification = audio_mod_verify_m4b(settings, path)
    assert verification["ok"] is True, f"packaging.verify_m4b 校验失败：{verification.get('error')}"
    assert int(verification["duration_ms"]) == int(probe["duration_ms"])
    return path, probe, chapters


def audio_mod_verify_m4b(settings: Settings, path: Path) -> dict[str, Any]:
    """``packaging.verify_m4b`` 的非抛出版本校验（含 ffmpeg -f null 完整解码）。"""
    from relaxbook import packaging

    return packaging.verify_m4b(
        path,
        ffmpeg=settings.ffmpeg,
        ffprobe=settings.ffprobe,
        expected_chapters=EXPECTED_CHAPTER_COUNT,
    )


def _check_decodes_with_null_muxer(settings: Settings, m4b_path: Path) -> None:
    """ACCEPTANCE A3 指定命令：``ffmpeg -v error -xerror -i m4b -map 0:a:0 -f null -``。"""
    command = [
        settings.ffmpeg,
        "-v",
        "error",
        "-xerror",
        "-i",
        str(m4b_path),
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    stderr = (completed.stderr or "").strip()
    assert completed.returncode == 0, (
        f"ffmpeg -f null 完整解码失败（退出码 {completed.returncode}）：{stderr[-800:]}"
    )


def _check_web_payload_matches_artifacts(
    settings: Settings,
    job_id: int,
    *,
    m4b_path: Path,
    m4b_probe: dict[str, Any],
    m4b_chapters: list[dict[str, Any]],
    mp3_paths: list[Path],
    mp3_probes: list[dict[str, Any]],
) -> dict[str, Any]:
    """A3：网页结果页显示值（``api._job_payload``）与成品探测值逐项对照。"""
    conn = _connect(settings)
    try:
        payload = api._job_payload(conn, settings, job_id)
    finally:
        conn.close()

    web_job = payload["job"]
    web_m4b = payload["m4b"]
    web_chapters = payload["chapters"]

    assert web_job["state"] == "done", f"结果页任务状态应为 done，实际 {web_job['state']}"
    assert web_job["done_chapters"] == web_job["total_chapters"] == EXPECTED_CHAPTER_COUNT, (
        f"结果页章数应为 {EXPECTED_CHAPTER_COUNT}/{EXPECTED_CHAPTER_COUNT}，"
        f"实际 {web_job['done_chapters']}/{web_job['total_chapters']}"
    )
    assert web_job["done_segments"] == web_job["total_segments"], "结果页片段进度不完整"
    assert web_m4b is not None, "结果页缺少 M4B 信息"
    assert web_m4b["verified"] is True, "结果页 M4B 未标记为已校验"
    assert web_m4b["filename"] == m4b_path.name, (
        f"结果页 M4B 文件名 {web_m4b['filename']} != 成品 {m4b_path.name}"
    )
    assert int(web_m4b["bytes"]) == m4b_path.stat().st_size, "结果页 M4B 大小与成品不一致"
    assert abs(int(web_m4b["duration_ms"]) - int(m4b_probe["duration_ms"])) <= WEB_DISPLAY_TOLERANCE_MS, (
        f"结果页 M4B 总时长 {web_m4b['duration_ms']}ms 与探测 {m4b_probe['duration_ms']}ms "
        f"相差超过 {WEB_DISPLAY_TOLERANCE_MS}ms"
    )

    assert len(web_chapters) == EXPECTED_CHAPTER_COUNT, "结果页章节表行数不对"
    for position, (web_chapter, mp3_path, mp3_probe, probed_chapter) in enumerate(
        zip(web_chapters, mp3_paths, mp3_probes, m4b_chapters), start=1
    ):
        assert web_chapter["title"] == EXPECTED_TITLES[position - 1], (
            f"结果页第 {position} 章标题不对：{web_chapter['title']}"
        )
        assert web_chapter["mp3_filename"] == mp3_path.name, (
            f"结果页第 {position} 章 MP3 文件名 {web_chapter['mp3_filename']} != {mp3_path.name}"
        )
        assert int(web_chapter["mp3_bytes"]) == mp3_path.stat().st_size, (
            f"结果页第 {position} 章 MP3 大小与成品不一致"
        )
        assert web_chapter["mp3_duration_ms"] is not None, "结果页缺少本章时长"
        assert (
            abs(int(web_chapter["mp3_duration_ms"]) - int(mp3_probe["duration_ms"]))
            <= WEB_DISPLAY_TOLERANCE_MS
        ), (
            f"结果页第 {position} 章时长 {web_chapter['mp3_duration_ms']}ms 与探测 "
            f"{mp3_probe['duration_ms']}ms 相差超过 {WEB_DISPLAY_TOLERANCE_MS}ms"
        )
        assert web_chapter["m4b_start_ms"] is not None and web_chapter["m4b_end_ms"] is not None, (
            "结果页缺少 M4B 起止位置"
        )
        assert (
            abs(int(web_chapter["m4b_start_ms"]) - int(probed_chapter["start_ms"]))
            <= WEB_DISPLAY_TOLERANCE_MS
        ), f"结果页第 {position} 章 M4B 起点与成品探测值相差超过 1 秒"
        assert (
            abs(int(web_chapter["m4b_end_ms"]) - int(probed_chapter["end_ms"]))
            <= WEB_DISPLAY_TOLERANCE_MS
        ), f"结果页第 {position} 章 M4B 终点与成品探测值相差超过 1 秒"
        assert web_chapter["state"] == "done", f"结果页第 {position} 章状态不是 done"
    return payload


# ---------------------------------------------------------------------------
# 缓存复用：计数包装引擎
# ---------------------------------------------------------------------------
class _CountingEngine:
    """包装真实引擎，统计 ``synthesize`` 调用次数（用于断言缓存命中不联网）。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", ENGINE)
        self.model = getattr(inner, "model", "")
        self.calls: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def voices(self):
        return self._inner.voices()

    def synthesize(self, text: str, voice: str, rate: str = RATE) -> bytes:
        self.calls.append(text)
        return self._inner.synthesize(text, voice, rate)


# ---------------------------------------------------------------------------
# 测试 1（不联网）：分段口径与文件名预期
# ---------------------------------------------------------------------------
def test_segment_plan_matches_acceptance_expectations(settings: Settings, sample_epub: Path) -> None:
    """不联网校验：三章切片口径、缓存键唯一、不丢字、MP3 文件名符合预期。"""
    settings = _private_settings(settings)
    book_id, revision_id = _seed_book(settings, sample_epub)
    items, total_chapters, model = _plan_segments_like_api(settings, revision_id)

    assert total_chapters == EXPECTED_CHAPTER_COUNT
    assert len(items) == EXPECTED_TOTAL_SEGMENTS, (
        f"样书应切成 {EXPECTED_TOTAL_SEGMENTS} 个片段，实际 {len(items)} 个"
    )
    assert model, "计划里缺少引擎模型名"
    assert len({item["cache_key"] for item in items}) == len(items), "缓存键应互不相同"
    assert {item["chapter_idx"] for item in items} == {1, 2, 3}

    conn = _connect(settings)
    try:
        chapters = [chapter for chapter in db.list_chapters(conn, revision_id) if chapter["read"]]
        job_id = db.create_job(
            conn,
            book_id=book_id,
            revision_id=revision_id,
            engine=ENGINE,
            model=model,
            voice=VOICE,
            rate=RATE,
            formats=["m4b", "mp3"],
            total_segments=len(items),
            total_chapters=total_chapters,
            request_key=_api_request_key(revision_id),
        )
        db.create_segments(conn, job_id, items)
        job = dict(db.get_job(conn, job_id))
    finally:
        conn.close()
    assert job["total_segments"] == len(items) and job["total_chapters"] == total_chapters

    # MP3 文件名按 naming 口径：同名章靠序号唯一。
    names = [
        naming.mp3_filename(position, chapter["title"], len(chapters))
        for position, chapter in enumerate(chapters, start=1)
    ]
    assert names == EXPECTED_MP3_NAMES, f"MP3 文件名不符合预期：{names}"
    assert len(set(names)) == len(names)

    # 不丢字：同一章所有片段拼回去（忽略空白）等于清理后的正文。
    for chapter in chapters:
        pieces = [item["text"] for item in items if item["chapter_idx"] == chapter["idx"]]
        assert pieces, f"第 {chapter['idx']} 章没有片段"
        joined = "".join("".join(pieces).split())
        assert joined == "".join(str(chapter["text"]).split()), (
            f"第 {chapter['idx']} 章切片丢字"
        )


# ---------------------------------------------------------------------------
# 测试 2（联网）：完整链路 + A2/A3 断言 + 缓存复用
# ---------------------------------------------------------------------------
@pytest.mark.network
def test_three_chapter_end_to_end_real_edge(
    settings: Settings, sample_epub: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """真实 Edge 合成一本三章样书，产出带内嵌目录的 M4B 与三个 MP3。"""
    settings = _private_settings(settings)
    _require_network(settings)

    book_id, revision_id = _seed_book(settings, sample_epub)
    items, total_chapters, model = _plan_segments_like_api(settings, revision_id)
    assert total_chapters == EXPECTED_CHAPTER_COUNT
    job_id = _create_job(
        settings,
        book_id=book_id,
        revision_id=revision_id,
        items=items,
        total_chapters=total_chapters,
        model=model,
        request_key=_api_request_key(revision_id),
    )
    print(
        f"任务 job={job_id}：{total_chapters} 章 / {len(items)} 片段，"
        f"引擎={ENGINE} 音色={VOICE} 并发={settings.concurrency}",
        flush=True,
    )
    print(f"测试数据目录：{settings.data_dir}", flush=True)

    # -- 生成：首次 + 最多 2 轮「模拟 A5 恢复」（只补失败片段） --------------
    rounds: list[dict[str, Any]] = []
    previous_failed = 0
    for round_index in range(1, MAX_RECOVERY_ROUNDS + 2):
        round_result = _run_one_round(
            settings,
            job_id,
            caplog,
            round_index=round_index,
            previous_failed=previous_failed,
        )
        rounds.append(round_result)
        if round_result["state"] == "done":
            break
        if not (
            round_result["failed_errors"]
            and all(_is_network_error(error) for error in round_result["failed_errors"])
        ):
            pytest.fail(
                "任务失败且不是网络类错误，属于真实缺陷：\n"
                f"状态={round_result['state']} 错误={round_result['error']}\n"
                f"失败片段错误={round_result['failed_errors'][-3:]}"
            )
        if round_index == MAX_RECOVERY_ROUNDS + 1:
            break
        previous_failed = _simulate_a5_recovery(settings, job_id)

    final_round = rounds[-1]
    if final_round["state"] != "done":
        available, probe_errors = _probe_network(settings)
        summary = "\n".join(
            f"  第 {item['round']} 轮: {item['done_segments']}/{item['total_segments']} 片段 "
            f"失败={item['failed_segments']} 命中={item['cache_hit']} "
            f"未命中={item['cache_miss']} 耗时={item['elapsed_s']:.1f}s "
            f"错误={item['error']}"
            for item in rounds
        )
        last_errors = final_round["failed_errors"][-3:]
        if not available and all(_is_network_error(error) for error in probe_errors):
            pytest.skip(
                "Edge TTS 网络不可用（两轮恢复后仍失败，且 3 次连通性探测全部为连接类错误）："
                f"{probe_errors[-1]}\n逐轮进度：\n{summary}"
            )
        pytest.fail(
            "两轮 A5 恢复后任务仍未完成（网络探测可用或存在非连接类错误），"
            f"最后 3 条片段错误：{last_errors}\n逐轮进度：\n{summary}"
        )

    # 恢复轮次必须只补缺失片段（A5：已完成的片段不重新合成）。
    if len(rounds) > 1:
        recovery_round = rounds[-1]
        assert recovery_round["cache_hit"] > 0, (
            "恢复轮次没有任何缓存命中：说明已成功的片段被重新合成了"
        )
        assert recovery_round["cache_miss"] == recovery_round["previous_failed"], (
            f"恢复轮次未命中 {recovery_round['cache_miss']} 个片段，"
            f"上一轮失败 {recovery_round['previous_failed']} 个，应只补失败片段"
        )

    # -- A2/A3 硬断言 --------------------------------------------------------
    snapshot = _snapshot(settings, job_id)
    job = snapshot["job"]
    stats = snapshot["stats"]
    assert job["state"] == "done", f"任务状态应为 done，实际 {job['state']}"
    assert job.get("error") in (None, ""), f"完成的任务仍带错误信息：{job.get('error')}"
    assert job["done_segments"] == job["total_segments"] == stats["total"] == len(items)
    assert stats["done"] == stats["total"] and stats["failed"] == 0
    assert job["done_chapters"] == job["total_chapters"] == EXPECTED_CHAPTER_COUNT
    assert job["mp3_complete"] == 1, "任务未标记 MP3 全部完成"
    assert job["m4b_filename"], "任务缺少 M4B 文件名"
    assert int(job["m4b_duration_ms"] or 0) > 0

    mp3_paths, mp3_probes = _check_mp3_artifacts(settings, snapshot["artifacts"])
    m4b_path, m4b_probe, m4b_chapters = _check_m4b_artifact(settings, snapshot["artifacts"])

    # M4B 时长 ≈ 三个 MP3 时长之和（AAC 填充允许少量误差）。
    mp3_sum_ms = sum(int(probe["duration_ms"]) for probe in mp3_probes)
    assert abs(int(m4b_probe["duration_ms"]) - mp3_sum_ms) <= DURATION_TOLERANCE_MS, (
        f"M4B 时长 {m4b_probe['duration_ms']}ms 与三个 MP3 之和 {mp3_sum_ms}ms "
        f"相差超过 {DURATION_TOLERANCE_MS}ms"
    )
    # 结果页记录的 M4B 数据必须来自成品。
    assert abs(int(job["m4b_duration_ms"]) - int(m4b_probe["duration_ms"])) <= 1
    assert int(job["m4b_bytes"] or 0) == m4b_path.stat().st_size
    assert job["m4b_filename"] == m4b_path.name

    # ACCEPTANCE A3：指定的完整解码命令退出码必须为 0。
    _check_decodes_with_null_muxer(settings, m4b_path)

    # ACCEPTANCE A3：网页结果页显示值与成品探测值对照（秒值误差 ≤ 1 秒）。
    _check_web_payload_matches_artifacts(
        settings,
        job_id,
        m4b_path=m4b_path,
        m4b_probe=m4b_probe,
        m4b_chapters=m4b_chapters,
        mp3_paths=mp3_paths,
        mp3_probes=mp3_probes,
    )

    # -- 缓存复用：同 revision/音色/语速的第二个任务必须 0 次合成调用 --------
    conn = _connect(settings)
    try:
        cached_before = {
            str(segment["cache_key"]): (
                settings.segments_dir / str(segment["cache_key"])[:2] / f"{segment['cache_key']}.wav"
            )
            for segment in db.list_segments(conn, job_id)
        }
    finally:
        conn.close()
    assert len(cached_before) == len(items)
    for cache_key, cache_path in cached_before.items():
        assert cache_path.is_file() and cache_path.stat().st_size > 0, (
            f"缓存片段不存在：{cache_key[:12]}… -> {cache_path}"
        )

    job2_items, job2_total_chapters, _ = _plan_segments_like_api(settings, revision_id)
    assert [item["cache_key"] for item in job2_items] == [item["cache_key"] for item in items], (
        "同一 revision/音色/语速的第二次计划缓存键应与第一次完全一致"
    )
    job2 = _create_job(
        settings,
        book_id=book_id,
        revision_id=revision_id,
        items=job2_items,
        total_chapters=job2_total_chapters,
        model=model,
        request_key=None,  # 避免 (revision,voice,rate) 唯一约束，模拟重新发起任务
    )
    counting_engine = _CountingEngine(tts.get_engine(ENGINE, settings))
    caplog.set_level(logging.INFO, logger="relaxbook.worker")
    before_records = len(caplog.records)
    started = time.perf_counter()
    worker.JobRunner(settings, engine=counting_engine).run_job(job2)
    elapsed_second = time.perf_counter() - started
    second_records = caplog.records[before_records:]

    snapshot2 = _snapshot(settings, job2)
    job2_row = snapshot2["job"]
    assert job2_row["state"] == "done", f"第二个任务应为 done，实际 {job2_row['state']}"
    assert snapshot2["stats"]["done"] == snapshot2["stats"]["total"] == len(job2_items)
    assert counting_engine.call_count == 0, (
        f"缓存命中时不得再次调用合成接口，实际调用 {counting_engine.call_count} 次："
        f"{counting_engine.calls[:3]}"
    )
    assert _count_log(second_records, "cache=hit") == len(job2_items), (
        "第二个任务的缓存命中日志数量与片段数不一致"
    )
    assert _count_log(second_records, "cache=miss") == 0, "第二个任务不应有缓存未命中"

    mp3_paths2, mp3_probes2 = _check_mp3_artifacts(settings, snapshot2["artifacts"])
    m4b_path2, m4b_probe2, m4b_chapters2 = _check_m4b_artifact(settings, snapshot2["artifacts"])
    assert [path.name for path in mp3_paths2] == EXPECTED_MP3_NAMES
    assert abs(int(m4b_probe2["duration_ms"]) - int(m4b_probe["duration_ms"])) <= 50, (
        "两个任务的 M4B 时长差异过大：缓存复用的成品应与首次一致"
    )

    # -- 摘要 ---------------------------------------------------------------
    generation_seconds = sum(item["elapsed_s"] for item in rounds)
    audio_seconds = int(m4b_probe["duration_ms"]) / 1000.0
    seconds_per_hour = generation_seconds * 3600.0 / audio_seconds if audio_seconds else 0.0
    print("\n===== A2/A3 端到端验收摘要 =====", flush=True)
    print(
        f"任务 job={job_id}（{len(rounds)} 轮生成；总耗时 {generation_seconds:.2f}s，"
        f"其中重试恢复 {len(rounds) - 1} 轮）",
        flush=True,
    )
    print(f"{'序号':<4}{'章名':<6}{'MP3 文件名':<18}{'时长(s)':>9}{'大小(KB)':>10}"
          f"{'M4B 起点(s)':>12}{'M4B 终点(s)':>12}", flush=True)
    for position, (mp3_path, mp3_probe, chapter) in enumerate(
        zip(mp3_paths, mp3_probes, m4b_chapters), start=1
    ):
        print(
            f"{position:<4}{EXPECTED_TITLES[position - 1]:<6}{mp3_path.name:<18}"
            f"{mp3_probe['duration_ms'] / 1000:>9.1f}"
            f"{mp3_path.stat().st_size / 1024:>10.1f}"
            f"{chapter['start_ms'] / 1000:>12.1f}{chapter['end_ms'] / 1000:>12.1f}",
            flush=True,
        )
    print(
        f"合计：音频 {audio_seconds:.1f}s；M4B {m4b_path.stat().st_size / 1024 / 1024:.2f} MB；"
        f"三个 MP3 合计 {sum(path.stat().st_size for path in mp3_paths) / 1024 / 1024:.2f} MB",
        flush=True,
    )
    print(
        f"生成效率：{generation_seconds:.2f}s 生成 {audio_seconds:.1f}s 音频 "
        f"-> 每 1 小时成品音频约需 {seconds_per_hour:.0f}s（{seconds_per_hour / 60:.1f} 分钟）",
        flush=True,
    )
    print(
        f"缓存复用：第二个任务 job={job2} 合成调用 {counting_engine.call_count} 次、"
        f"缓存命中 {_count_log(second_records, 'cache=hit')} 次，耗时 {elapsed_second:.2f}s",
        flush=True,
    )
