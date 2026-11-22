"""A3 验收证据生成器：三章样书 -> 真实 Edge 合成 -> MP3 + M4B -> ffprobe 核对。

用法::

    cd D:\\dsh\\relaxbook
    .venv\\Scripts\\python.exe tools\\acceptance.py --data-dir D:\\dsh\\tmp\\relaxbook-acceptance --book sample\\示例书.epub

工具内部直接调用 :class:`relaxbook.worker.JobRunner` 跑完整链路（不启动网页
和常驻 worker），然后：

* 重新执行 ACCEPTANCE A3 的三条命令，并把 **原始 ffprobe JSON** 与
  ``ffmpeg -f null`` 的退出码/输出写入 ``<data-dir>/acceptance/``；
* 生成章节表（序号/章名/MP3 文件名/M4B 起止/时长/大小）；
* 把网页结果页显示值（``relaxbook.api._job_payload``，即 ``GET /api/jobs/{id}``）
  与成品探测值逐项对照，秒值误差要求 ≤ 1 秒；
* 在 stdout 打印中文摘要表格；全部通过退出码 0，否则 1。

失败自动重试：任务失败时默认按 A5 恢复流程
（``db.reset_segment_failures`` + ``db.set_job_state('queued')``）再跑一次，
两轮证据分别写入 ``acceptance/round1/``、``acceptance/round2/``；``--no-retry``
可关闭。重复运行会复用已完成的书籍/任务与既有 artifact，不会覆盖已完成的成品。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import platform
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from relaxbook import api, audio, db, ingest, tts  # noqa: E402
from relaxbook import worker  # noqa: E402
from relaxbook.config import Settings, load_settings  # noqa: E402
from relaxbook.ingest import IngestError  # noqa: E402

#: 与 ``sample/预期清单.md`` 一致的预期目录。
EXPECTED_TITLES = ["序言", "出发", "出发"]
EXPECTED_MP3_NAMES = ["001 - 序言.mp3", "002 - 出发.mp3", "003 - 出发.mp3"]
EXPECTED_CHAPTER_COUNT = 3

SEAM_TOLERANCE_MS = 20
DURATION_TOLERANCE_MS = 1000
WEB_DISPLAY_TOLERANCE_MS = 1000

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 连接类错误特征（网络不可用时给出可操作的提示，而不是笼统的失败）。
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
# 小工具
# ---------------------------------------------------------------------------
def _is_network_error(message: str | None) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in NETWORK_ERROR_MARKERS)


def _display_width(text: str) -> int:
    """终端显示宽度（CJK/全角算 2 列），用于对齐中文摘要表格。"""
    width = 0
    for char in str(text):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: Any, width: int, *, align: str = "left") -> str:
    value = str(text)
    padding = max(0, width - _display_width(value))
    if align == "right":
        return " " * padding + value
    return value + " " * padding


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]], *, aligns: Sequence[str] | None = None) -> str:
    """按显示宽度对齐的纯文本表格（不依赖第三方库）。"""
    rows = [list(row) for row in rows]
    aligns = list(aligns or ["left"] * len(headers))
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for position, cell in enumerate(row):
            widths[position] = max(widths[position], _display_width(cell))
    lines = ["  ".join(_pad(header, widths[i]) for i, header in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append(
            "  ".join(_pad(cell, widths[i], align=aligns[i]) for i, cell in enumerate(row))
        )
    return "\n".join(lines)


def _atomic_write(path: Path, data: bytes) -> None:
    """契约要求：临时文件 + ``os.replace`` 原子改名。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            pass


def _write_text(path: Path, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _connect(settings: Settings):
    return db.connect(settings.db_path)


def _run_command(command: Sequence[str], *, timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_commit() -> str:
    try:
        result = _run_command(["git", "rev-parse", "--short", "HEAD"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout or "").strip() or "unknown"


def _tool_version(exe: str) -> str:
    if not exe:
        return "（未配置）"
    try:
        result = _run_command([exe, "-version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return exe
    first = (result.stdout or result.stderr or "").splitlines()
    return first[0].strip() if first else exe


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        resolved = path.resolve()
        base = parent.resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    return resolved == base or base in resolved.parents


def _configure_console() -> None:
    """Windows 控制台默认 GBK：把无法编码的字符替换掉，绝不让打印崩掉退出码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - 非 TextIOWrapper
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 已被重定向的流
            pass


# ---------------------------------------------------------------------------
# 链路：导入样书
# ---------------------------------------------------------------------------
def _prepare_book(settings: Settings, book_path: Path) -> dict[str, Any]:
    """把样书放进数据目录并解析落库；已存在时复用，返回书籍信息。"""
    data = book_path.read_bytes()
    sha256 = _sha256(data)
    destination = settings.books_dir / sha256[:16] / f"original{book_path.suffix.lower() or '.epub'}"
    if not destination.is_file() or destination.read_bytes() != data:
        _atomic_write(destination, data)

    conn = _connect(settings)
    try:
        existing = db.find_book_by_sha(conn, sha256)
        revision = db.current_revision(conn, int(existing["id"])) if existing is not None else None
        if existing is not None and revision is not None:
            return {
                "book_id": int(existing["id"]),
                "revision_id": int(revision["id"]),
                "title": existing["title"],
                "author": existing["author"],
                "chapters": len(db.list_chapters(conn, int(revision["id"]))),
                "reused": True,
                "warnings": db.loads(existing["warnings"], []) or [],
            }

        parsed = ingest.parse_book(destination, format="epub", settings=settings)
        cover_path: str | None = None
        if parsed.cover_bytes:
            cover_ext = (parsed.cover_ext or "jpg").lstrip(".").lower() or "jpg"
            cover_file = destination.parent / f"cover.{cover_ext}"
            _atomic_write(cover_file, bytes(parsed.cover_bytes))
            cover_path = str(cover_file)
        chapters = [
            {
                "title": chapter.title,
                "text": chapter.text,
                "read": bool(chapter.read),
                "source_locator": chapter.source_locator or None,
                "warnings": list(chapter.warnings or []),
            }
            for chapter in parsed.chapters
        ]
        if existing is None:
            book_id = db.create_book(
                conn,
                sha256=sha256,
                format="epub",
                original_path=str(destination),
                title=parsed.title,
                status="parsing",
            )
        else:
            book_id = int(existing["id"])
        revision_id = db.create_revision(conn, book_id, chapters, note="A3 验收导入")
        db.set_book_parsed(
            conn,
            book_id,
            title=parsed.title,
            author=parsed.author,
            cover_path=cover_path,
            warnings=list(parsed.warnings or []),
        )
    finally:
        conn.close()
    return {
        "book_id": book_id,
        "revision_id": revision_id,
        "title": parsed.title,
        "author": parsed.author,
        "chapters": len(chapters),
        "reused": False,
        "warnings": list(parsed.warnings or []),
    }


# ---------------------------------------------------------------------------
# 链路：创建/复用任务
# ---------------------------------------------------------------------------
def _plan_segments(
    settings: Settings,
    revision_id: int,
    *,
    engine_name: str,
    voice: str,
    rate: str,
) -> tuple[list[dict[str, Any]], int, str]:
    """复用网页创建任务的分段口径（``relaxbook.api._plan_segments``）。"""
    planner = getattr(api, "_plan_segments", None)
    if planner is None:  # pragma: no cover - 防御性检查
        raise SystemExit(
            "relaxbook.api._plan_segments 不存在：网页创建任务的分段口径已变化，"
            "请同步更新 tools/acceptance.py。"
        )
    conn = _connect(settings)
    try:
        return planner(conn, settings, revision_id, engine_name, voice, rate)
    finally:
        conn.close()


def _request_key(revision_id: int, engine_name: str, voice: str, rate: str) -> str:
    """与 ``api.py`` 的 ``POST /api/jobs`` 完全相同的去重键。"""
    return hashlib.sha256(
        f"{revision_id}|{engine_name}|{voice}|{rate}".encode("utf-8")
    ).hexdigest()[:32]


def _artifacts_complete(settings: Settings, artifacts: list[dict[str, Any]]) -> bool:
    mp3s = [
        artifact
        for artifact in artifacts
        if artifact["kind"] == "mp3" and artifact["chapter_idx"] is not None
    ]
    m4bs = [artifact for artifact in artifacts if artifact["kind"] == "m4b"]
    if len(mp3s) != EXPECTED_CHAPTER_COUNT or len(m4bs) != 1:
        return False
    for artifact in mp3s + m4bs:
        if not int(artifact["verified"]):
            return False
        if not Path(artifact["path"]).is_file():
            return False
    return True


def _ensure_job(
    settings: Settings,
    book: dict[str, Any],
    *,
    engine_name: str,
    voice: str,
    rate: str,
    new_job: bool,
) -> dict[str, Any]:
    """复用已完成的同参数任务，否则按网页口径新建任务并落片段。"""
    conn = _connect(settings)
    try:
        if not new_job:
            done_rows = conn.execute(
                "SELECT * FROM job WHERE revision_id=? AND engine=? AND voice=? AND rate=? "
                "AND state='done' ORDER BY id DESC LIMIT 1",
                (book["revision_id"], engine_name, voice, rate),
            ).fetchall()
            for row in done_rows:
                job_id = int(row["id"])
                if _artifacts_complete(settings, db.list_artifacts(conn, job_id)):
                    return {
                        "job_id": job_id,
                        "reused": True,
                        "items": db.list_segments(conn, job_id),
                        "total_chapters": int(row["total_chapters"] or EXPECTED_CHAPTER_COUNT),
                    }

        request_key: str | None = None
        if not new_job:
            request_key = _request_key(book["revision_id"], engine_name, voice, rate)
            existing = db.find_job_by_request_key(conn, request_key)
            if existing is not None:
                if existing["state"] == "failed":
                    # 失败任务不能阻塞新任务：先释放唯一键（与 api.py 相同的处理）。
                    conn.execute("UPDATE job SET request_key=NULL WHERE id=?", (int(existing["id"]),))
                else:
                    return {
                        "job_id": int(existing["id"]),
                        "reused": True,
                        "items": db.list_segments(conn, int(existing["id"])),
                        "total_chapters": int(existing["total_chapters"] or EXPECTED_CHAPTER_COUNT),
                    }

        items, total_chapters, model = _plan_segments(
            settings,
            book["revision_id"],
            engine_name=engine_name,
            voice=voice,
            rate=rate,
        )
        if not items:
            raise SystemExit("没有可朗读的正文片段，无法生成音频。")
        job_id = db.create_job(
            conn,
            book_id=book["book_id"],
            revision_id=book["revision_id"],
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
        return {"job_id": job_id, "reused": False, "items": items, "total_chapters": total_chapters}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 链路：执行任务（含默认一次 A5 恢复重试）
# ---------------------------------------------------------------------------
class _RecordCollector(logging.Handler):
    """收集 ``relaxbook.worker`` 的日志行，用于统计缓存命中/未命中。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102 - logging API
        self.records.append(record)


def _snapshot(settings: Settings, job_id: int) -> dict[str, Any]:
    conn = _connect(settings)
    try:
        job_row = db.get_job(conn, job_id)
        if job_row is None:
            raise SystemExit(f"任务不存在：job={job_id}")
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


def _run_attempt(
    settings: Settings,
    job_id: int,
    *,
    attempt: int,
    previous_failed: int,
    verbose: bool = False,
) -> dict[str, Any]:
    collector = _RecordCollector()
    target_logger = logging.getLogger("relaxbook.worker")
    target_logger.addHandler(collector)
    previous_level = target_logger.level
    previous_propagate = target_logger.propagate
    target_logger.setLevel(logging.INFO)
    if not verbose:
        # 收集日志只是为了统计缓存命中；不把 INFO 日志漏到 stdout/stderr。
        target_logger.propagate = False
    try:
        started = time.perf_counter()
        worker.JobRunner(settings).run_job(job_id)
        elapsed = time.perf_counter() - started
    finally:
        target_logger.removeHandler(collector)
        target_logger.setLevel(previous_level)
        target_logger.propagate = previous_propagate

    snapshot = _snapshot(settings, job_id)
    messages = [record.getMessage() for record in collector.records]
    cache_hit = sum(1 for message in messages if "cache=hit" in message)
    cache_miss = sum(1 for message in messages if "cache=miss" in message)
    result = {
        "attempt": attempt,
        "previous_failed": previous_failed,
        "job_id": job_id,
        "state": snapshot["job"]["state"],
        "done_segments": snapshot["stats"]["done"],
        "total_segments": snapshot["stats"]["total"],
        "failed_segments": snapshot["stats"]["failed"],
        "done_chapters": int(snapshot["job"]["done_chapters"] or 0),
        "elapsed_seconds": round(elapsed, 3),
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        "error": snapshot["job"].get("error"),
        "failed_segment_errors": snapshot["failed_errors"][-10:],
    }
    print(
        f"[第 {attempt} 轮] 状态={result['state']} "
        f"片段 {result['done_segments']}/{result['total_segments']} "
        f"失败={result['failed_segments']} 章={result['done_chapters']} "
        f"缓存命中={cache_hit} 未命中={cache_miss} 耗时={elapsed:.2f}s",
        flush=True,
    )
    return result


def _simulate_a5_recovery(settings: Settings, job_id: int) -> int:
    """A5 恢复：失败片段重置为 pending、任务重新排队（已完成的缓存保留）。"""
    conn = _connect(settings)
    try:
        reset = db.reset_segment_failures(conn, job_id)
        db.set_job_state(conn, job_id, "queued")
    finally:
        conn.close()
    print(
        f"[A5 恢复] 已把 {reset} 个失败片段重置为待合成，任务重新排队；"
        "已成功片段的缓存不会重新合成。",
        flush=True,
    )
    return reset


# ---------------------------------------------------------------------------
# 证据采集
# ---------------------------------------------------------------------------
def _ffprobe_raw(settings: Settings, path: Path, *, chapters: bool) -> dict[str, Any]:
    """执行 ACCEPTANCE A3 的 ffprobe 命令并返回原始 JSON。"""
    command = [settings.ffprobe, "-v", "error", "-show_format"]
    if chapters:
        command.append("-show_chapters")
    command += ["-of", "json", str(path)]
    completed = _run_command(command)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffprobe 失败（退出码 {completed.returncode}）：{(completed.stderr or '').strip()[-500:]}"
        )
    try:
        return json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError(f"无法解析 ffprobe 输出：{exc}") from exc


def _decode_check(settings: Settings, path: Path) -> dict[str, Any]:
    """执行 ``ffmpeg -v error -xerror -i <file> -map 0:a:0 -f null -``。"""
    command = [
        settings.ffmpeg,
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        "null",
        "-",
    ]
    completed = _run_command(command, timeout=180)
    return {
        "command": " ".join(command),
        "returncode": int(completed.returncode),
        "stdout": (completed.stdout or "").strip(),
        "stderr": (completed.stderr or "").strip(),
        "ok": completed.returncode == 0,
    }


def _ms_to_s(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(int(value) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def _format_mb(size_bytes: Any) -> str:
    try:
        return f"{int(size_bytes) / 1024 / 1024:.2f}"
    except (TypeError, ValueError):
        return "-"


def _build_evidence(settings: Settings, job_id: int) -> dict[str, Any]:
    """探测全部成品，构造章节表、检查项与网页↔成品对照数据。"""
    snapshot = _snapshot(settings, job_id)
    job = snapshot["job"]
    artifacts = snapshot["artifacts"]

    mp3_artifacts = sorted(
        (
            artifact
            for artifact in artifacts
            if artifact["kind"] == "mp3" and artifact["chapter_idx"] is not None
        ),
        key=lambda artifact: int(artifact["chapter_idx"]),
    )
    m4b_artifacts = [artifact for artifact in artifacts if artifact["kind"] == "m4b"]
    checks: list[dict[str, Any]] = []

    def add_check(check_id: str, name: str, ok: bool, expected: Any, actual: Any, detail: str = "") -> None:
        checks.append(
            {
                "id": check_id,
                "name": name,
                "ok": bool(ok),
                "expected": expected,
                "actual": actual,
                "detail": detail,
            }
        )

    # -- 任务层 --------------------------------------------------------------
    add_check("A2-1", "任务状态为 done", job["state"] == "done", "done", job["state"])
    add_check(
        "A2-2",
        "片段全部完成且无失败",
        snapshot["stats"]["failed"] == 0
        and snapshot["stats"]["done"] == snapshot["stats"]["total"]
        and int(job["done_segments"]) == int(job["total_segments"]),
        "done=total, failed=0",
        f"done={snapshot['stats']['done']}, total={snapshot['stats']['total']}, "
        f"failed={snapshot['stats']['failed']}",
    )
    add_check(
        "A2-3",
        f"完成章数为 {EXPECTED_CHAPTER_COUNT}/{EXPECTED_CHAPTER_COUNT}",
        int(job["done_chapters"]) == int(job["total_chapters"]) == EXPECTED_CHAPTER_COUNT,
        EXPECTED_CHAPTER_COUNT,
        f"{job['done_chapters']}/{job['total_chapters']}",
    )

    # -- MP3 ----------------------------------------------------------------
    mp3_names = [Path(artifact["path"]).name for artifact in mp3_artifacts]
    add_check(
        "A2-4",
        "MP3 成品数量为 3",
        len(mp3_artifacts) == EXPECTED_CHAPTER_COUNT,
        EXPECTED_CHAPTER_COUNT,
        len(mp3_artifacts),
    )
    add_check(
        "A2-5",
        "MP3 文件名与排序唯一且符合预期",
        mp3_names == EXPECTED_MP3_NAMES,
        EXPECTED_MP3_NAMES,
        mp3_names,
    )

    mp3_probes: list[dict[str, Any]] = []
    for artifact in mp3_artifacts:
        path = Path(artifact["path"])
        name = path.name
        exists = path.is_file()
        probe: dict[str, Any] = {}
        decode: dict[str, Any] = {"ok": False, "returncode": None, "stderr": "文件不存在"}
        if exists:
            try:
                probe = audio.probe(path, ffprobe=settings.ffprobe)
            except Exception as exc:  # noqa: BLE001 - 记录为检查失败
                probe = {"error": str(exc), "duration_ms": 0}
            decode = _decode_check(settings, path)
        mp3_probes.append({"path": path, "probe": probe, "decode": decode})
        add_check(
            f"A2-6-{artifact['chapter_idx']}",
            f"MP3 可完整解码：{name}",
            bool(decode.get("ok")),
            "ffmpeg -f null 退出码 0",
            f"退出码 {decode.get('returncode')}",
            str(decode.get("stderr") or "")[-300:],
        )
        add_check(
            f"A2-7-{artifact['chapter_idx']}",
            f"MP3 时长有效：{name}",
            int(probe.get("duration_ms") or 0) > 0,
            "> 0 ms",
            f"{probe.get('duration_ms')} ms",
        )
        add_check(
            f"A2-8-{artifact['chapter_idx']}",
            f"MP3 大小与数据库记录一致：{name}",
            exists and int(artifact["bytes"]) == path.stat().st_size,
            int(artifact["bytes"]),
            path.stat().st_size if exists else "文件不存在",
        )

    # -- M4B ----------------------------------------------------------------
    m4b_artifact: dict[str, Any] | None = m4b_artifacts[0] if m4b_artifacts else None
    m4b_path: Path | None = Path(m4b_artifact["path"]) if m4b_artifact else None
    m4b_probe: dict[str, Any] = {}
    m4b_chapters: list[dict[str, Any]] = []
    m4b_decode: dict[str, Any] = {"ok": False, "returncode": None, "stderr": "未生成 M4B"}
    raw_m4b: dict[str, Any] = {}
    if m4b_path is not None and m4b_path.is_file():
        raw_m4b = _ffprobe_raw(settings, m4b_path, chapters=True)
        m4b_probe = audio.probe(m4b_path, ffprobe=settings.ffprobe)
        m4b_chapters = audio.probe_chapters(m4b_path, ffprobe=settings.ffprobe)
        m4b_decode = _decode_check(settings, m4b_path)

    add_check(
        "A2-9",
        "M4B 成品数量为 1 且已校验",
        len(m4b_artifacts) == 1 and bool(int(m4b_artifacts[0]["verified"])) if m4b_artifacts else False,
        1,
        len(m4b_artifacts),
    )
    titles = [str(chapter.get("title") or "") for chapter in m4b_chapters]
    add_check(
        "A2-10",
        "M4B 内嵌章节数为 3",
        len(m4b_chapters) == EXPECTED_CHAPTER_COUNT,
        EXPECTED_CHAPTER_COUNT,
        len(m4b_chapters),
    )
    add_check(
        "A2-11",
        "M4B 章节标题与顺序符合预期",
        titles == EXPECTED_TITLES,
        EXPECTED_TITLES,
        titles,
    )
    first_start = int(m4b_chapters[0]["start_ms"]) if m4b_chapters else None
    add_check("A2-12", "M4B 首章起点为 0ms", first_start == 0, 0, first_start)
    max_seam_gap = 0
    seams_ok = True
    for previous, current in zip(m4b_chapters, m4b_chapters[1:]):
        gap = abs(int(current["start_ms"]) - int(previous["end_ms"]))
        max_seam_gap = max(max_seam_gap, gap)
        seams_ok = seams_ok and gap <= SEAM_TOLERANCE_MS
    add_check(
        "A2-13",
        "M4B 相邻章首尾相接（误差 ≤ 20ms）",
        seams_ok,
        f"≤ {SEAM_TOLERANCE_MS} ms",
        f"最大间隙 {max_seam_gap} ms",
    )
    m4b_duration_ms = int(m4b_probe.get("duration_ms") or 0)
    last_end = int(m4b_chapters[-1]["end_ms"]) if m4b_chapters else None
    last_diff = abs(m4b_duration_ms - int(last_end)) if last_end is not None else None
    add_check(
        "A2-14",
        "M4B 末章终点与整书时长一致（误差 ≤ 1000ms）",
        last_diff is not None and last_diff <= DURATION_TOLERANCE_MS,
        f"≤ {DURATION_TOLERANCE_MS} ms",
        f"相差 {last_diff} ms（末章 {last_end} ms / 整书 {m4b_duration_ms} ms）",
    )
    mp3_sum_ms = sum(int(item["probe"].get("duration_ms") or 0) for item in mp3_probes)
    sum_diff = abs(m4b_duration_ms - mp3_sum_ms)
    add_check(
        "A2-15",
        "M4B 时长 ≈ 三个 MP3 时长之和（误差 ≤ 1000ms）",
        sum_diff <= DURATION_TOLERANCE_MS,
        f"≤ {DURATION_TOLERANCE_MS} ms",
        f"相差 {sum_diff} ms（M4B {m4b_duration_ms} ms / MP3 合计 {mp3_sum_ms} ms）",
    )
    add_check(
        "A3-1",
        "M4B 可完整解码（ffmpeg -xerror -f null）",
        bool(m4b_decode.get("ok")),
        "ffmpeg -f null 退出码 0",
        f"退出码 {m4b_decode.get('returncode')}",
        str(m4b_decode.get("stderr") or "")[-500:],
    )

    # -- 网页结果页显示值（GET /api/jobs/{id}）vs 成品探测值 -----------------
    payload: dict[str, Any] = {}
    try:
        conn = _connect(settings)
        try:
            payload = api._job_payload(conn, settings, job_id)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 记录为检查失败
        payload = {"error": str(exc), "job": {}, "m4b": None, "chapters": []}
    web_job = payload.get("job") or {}
    web_m4b = payload.get("m4b")
    web_chapters = payload.get("chapters") or []

    add_check(
        "A3-2",
        "结果页任务状态为 done 且片段进度完整",
        web_job.get("state") == "done"
        and web_job.get("done_segments") == web_job.get("total_segments")
        and int(web_job.get("total_segments") or 0) > 0,
        "state=done, done_segments=total_segments",
        f"state={web_job.get('state')}, "
        f"done={web_job.get('done_segments')}/{web_job.get('total_segments')}",
    )
    add_check(
        "A3-3",
        "结果页章数为 3/3",
        web_job.get("done_chapters") == web_job.get("total_chapters") == EXPECTED_CHAPTER_COUNT,
        "3/3",
        f"{web_job.get('done_chapters')}/{web_job.get('total_chapters')}",
    )
    web_m4b_duration = int(web_m4b["duration_ms"]) if web_m4b and web_m4b.get("duration_ms") is not None else None
    m4b_duration_diff = (
        abs(web_m4b_duration - m4b_duration_ms) if web_m4b_duration is not None else None
    )
    add_check(
        "A3-4",
        "结果页 M4B 总时长与成品探测值一致（≤1 秒）",
        m4b_duration_diff is not None and m4b_duration_diff <= WEB_DISPLAY_TOLERANCE_MS,
        f"≤ {WEB_DISPLAY_TOLERANCE_MS} ms",
        f"相差 {m4b_duration_diff} ms" if m4b_duration_diff is not None else "结果页缺少 M4B 时长",
    )
    add_check(
        "A3-5",
        "结果页 M4B 文件名/大小与成品一致",
        bool(
            web_m4b
            and m4b_path is not None
            and web_m4b.get("filename") == m4b_path.name
            and int(web_m4b.get("bytes") or 0) == m4b_path.stat().st_size
        ),
        "文件名与字节数一致",
        f"{web_m4b.get('filename') if web_m4b else None} / "
        f"{web_m4b.get('bytes') if web_m4b else None}",
    )

    # 章节表：把网页值、成品探测值、误差放到同一行。
    chapter_rows: list[dict[str, Any]] = []
    for position in range(EXPECTED_CHAPTER_COUNT):
        web_chapter = web_chapters[position] if position < len(web_chapters) else {}
        mp3 = mp3_artifacts[position] if position < len(mp3_artifacts) else {}
        mp3_probe_item = mp3_probes[position] if position < len(mp3_probes) else {"path": None, "probe": {}}
        m4b_chapter = m4b_chapters[position] if position < len(m4b_chapters) else {}
        mp3_path = Path(mp3["path"]) if mp3.get("path") else None
        probed_mp3_duration = int(mp3_probe_item["probe"].get("duration_ms") or 0)
        web_mp3_duration = (
            int(web_chapter["mp3_duration_ms"])
            if web_chapter.get("mp3_duration_ms") is not None
            else None
        )
        web_start = web_chapter.get("m4b_start_ms")
        web_end = web_chapter.get("m4b_end_ms")
        start_diff = (
            abs(int(web_start) - int(m4b_chapter["start_ms"])) if web_start is not None and m4b_chapter else None
        )
        end_diff = (
            abs(int(web_end) - int(m4b_chapter["end_ms"])) if web_end is not None and m4b_chapter else None
        )
        mp3_duration_diff = (
            abs(web_mp3_duration - probed_mp3_duration) if web_mp3_duration is not None else None
        )
        row = {
            "index": position + 1,
            "title": web_chapter.get("title") or (EXPECTED_TITLES[position]),
            "mp3_filename": mp3_path.name if mp3_path else (web_chapter.get("mp3_filename") or ""),
            "mp3_bytes": int(mp3["bytes"]) if mp3.get("bytes") is not None else None,
            "mp3_size_mb": _format_mb(mp3.get("bytes")),
            "mp3_duration_ms": probed_mp3_duration,
            "mp3_duration_s": _ms_to_s(probed_mp3_duration),
            "m4b_start_ms": int(m4b_chapter["start_ms"]) if m4b_chapter else None,
            "m4b_end_ms": int(m4b_chapter["end_ms"]) if m4b_chapter else None,
            "m4b_start_s": _ms_to_s(m4b_chapter.get("start_ms")) if m4b_chapter else None,
            "m4b_end_s": _ms_to_s(m4b_chapter.get("end_ms")) if m4b_chapter else None,
            "m4b_duration_ms": (
                int(m4b_chapter["end_ms"]) - int(m4b_chapter["start_ms"]) if m4b_chapter else None
            ),
            "web_mp3_duration_ms": web_mp3_duration,
            "web_mp3_duration_s": _ms_to_s(web_mp3_duration),
            "mp3_duration_diff_ms": mp3_duration_diff,
            "web_m4b_start_ms": int(web_start) if web_start is not None else None,
            "web_m4b_end_ms": int(web_end) if web_end is not None else None,
            "web_m4b_start_s": _ms_to_s(web_start),
            "web_m4b_end_s": _ms_to_s(web_end),
            "m4b_start_diff_ms": start_diff,
            "m4b_end_diff_ms": end_diff,
            "web_state": web_chapter.get("state"),
            "probed_m4b_title": str(m4b_chapter.get("title") or "") if m4b_chapter else "",
        }
        chapter_rows.append(row)

        add_check(
            f"A3-6-{position + 1}",
            f"第 {position + 1} 章 MP3 文件名一致（网页 vs 成品）",
            bool(mp3_path) and web_chapter.get("mp3_filename") == (mp3_path.name if mp3_path else None),
            mp3_path.name if mp3_path else EXPECTED_MP3_NAMES[position],
            web_chapter.get("mp3_filename"),
        )
        add_check(
            f"A3-7-{position + 1}",
            f"第 {position + 1} 章时长一致（网页 vs 探测，≤1 秒）",
            mp3_duration_diff is not None and mp3_duration_diff <= WEB_DISPLAY_TOLERANCE_MS,
            f"≤ {WEB_DISPLAY_TOLERANCE_MS} ms",
            f"相差 {mp3_duration_diff} ms（网页 {web_mp3_duration} / 探测 {probed_mp3_duration}）",
        )
        add_check(
            f"A3-8-{position + 1}",
            f"第 {position + 1} 章 M4B 起止一致（网页 vs 探测，≤1 秒）",
            start_diff is not None
            and end_diff is not None
            and start_diff <= WEB_DISPLAY_TOLERANCE_MS
            and end_diff <= WEB_DISPLAY_TOLERANCE_MS,
            f"起止误差 ≤ {WEB_DISPLAY_TOLERANCE_MS} ms",
            f"起点差 {start_diff} ms / 终点差 {end_diff} ms",
        )
        add_check(
            f"A3-9-{position + 1}",
            f"第 {position + 1} 章网页状态为 done",
            web_chapter.get("state") == "done",
            "done",
            web_chapter.get("state"),
        )

    evidence = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "settings": {
            "data_dir": str(settings.data_dir),
            "voice": job["voice"],
            "engine": job["engine"],
            "engine_resolved": tts.resolve_engine_name(job["engine"], settings),
            "rate": job["rate"],
            "concurrency": settings.concurrency,
            "ffmpeg": settings.ffmpeg,
            "ffprobe": settings.ffprobe,
        },
        "job": job,
        "stats": snapshot["stats"],
        "mp3_artifacts": [
            {
                "chapter_idx": artifact["chapter_idx"],
                "path": artifact["path"],
                "filename": Path(artifact["path"]).name,
                "bytes": artifact["bytes"],
                "duration_ms": artifact["duration_ms"],
                "probed_duration_ms": int(item["probe"].get("duration_ms") or 0),
                "decode_ok": bool(item["decode"].get("ok")),
                "decode_returncode": item["decode"].get("returncode"),
                "decode_command": item["decode"].get("command"),
                "decode_stderr": item["decode"].get("stderr"),
            }
            for artifact, item in zip(mp3_artifacts, mp3_probes)
        ],
        "m4b": {
            "path": str(m4b_path) if m4b_path else None,
            "filename": m4b_path.name if m4b_path else None,
            "bytes": int(m4b_artifact["bytes"]) if m4b_artifact else None,
            "duration_ms": m4b_duration_ms,
            "chapters": m4b_chapters,
            "decode": m4b_decode,
        },
        "chapters": chapter_rows,
        "checks": checks,
        "raw_ffprobe_m4b": raw_m4b,
        "web_payload": payload,
        "totals": {
            "audio_duration_ms": m4b_duration_ms,
            "audio_duration_s": _ms_to_s(m4b_duration_ms),
            "mp3_sum_ms": mp3_sum_ms,
            "m4b_bytes": int(m4b_artifact["bytes"]) if m4b_artifact else None,
            "mp3_bytes_sum": sum(int(artifact["bytes"]) for artifact in mp3_artifacts),
            "max_seam_gap_ms": max_seam_gap,
        },
    }
    return evidence


# ---------------------------------------------------------------------------
# 证据落盘
# ---------------------------------------------------------------------------
def _clean_stale_mp3_probe_files(probe_dir: Path, expected_names: Sequence[str]) -> None:
    """只删除本工具上次生成、本次不再使用的单个 JSON 文件（不做递归删除）。"""
    if not probe_dir.is_dir():
        return
    keep = {f"{name}.json" for name in expected_names}
    for item in probe_dir.glob("*.json"):
        if item.name not in keep:
            try:
                item.unlink()
            except OSError:  # pragma: no cover - best effort
                pass


def _write_evidence(settings: Settings, evidence: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    # 1) 原始 ffprobe JSON（M4B：-show_format -show_chapters）。
    paths["ffprobe_m4b"] = out_dir / "ffprobe_m4b.json"
    _write_json(paths["ffprobe_m4b"], evidence["raw_ffprobe_m4b"])

    # 2) 每个 MP3 的原始 ffprobe JSON（-show_format）。
    probe_dir = out_dir / "ffprobe_mp3"
    probe_dir.mkdir(parents=True, exist_ok=True)
    expected_names = [row["mp3_filename"] for row in evidence["chapters"] if row["mp3_filename"]]
    _clean_stale_mp3_probe_files(probe_dir, expected_names)
    for artifact in evidence["mp3_artifacts"]:
        path = Path(artifact["path"])
        name = artifact["filename"]
        raw = _ffprobe_raw(settings, path, chapters=False)
        target = probe_dir / f"{name}.json"
        _write_json(target, raw)
        artifact["raw_ffprobe"] = str(target.relative_to(out_dir))
        paths[f"ffprobe_mp3_{artifact['chapter_idx']}"] = target

    # 3) ffmpeg -f null 校验结果（M4B + 每个 MP3 的退出码/输出）。
    lines = [
        "# ffmpeg -f null 完整解码校验（ACCEPTANCE A3 指定命令）",
        "",
        f"生成时间：{evidence['generated_at']}",
        "",
        "## M4B",
        f"命令：{evidence['m4b']['decode']['command']}",
        f"退出码：{evidence['m4b']['decode']['returncode']}",
        "stderr：",
        evidence["m4b"]["decode"]["stderr"] or "（空）",
        "",
    ]
    for artifact in evidence["mp3_artifacts"]:
        lines += [
            f"## MP3：{artifact['filename']}",
            f"命令：{artifact.get('decode_command') or '（未执行）'}",
            f"退出码：{artifact.get('decode_returncode')}",
            "stderr：",
            (artifact.get("decode_stderr") or "（空）").strip() or "（空）",
            "",
        ]
    paths["ffmpeg_decode_check"] = out_dir / "ffmpeg_decode_check.txt"
    _write_text(paths["ffmpeg_decode_check"], "\n".join(lines))

    # 4) 章节表 CSV（utf-8-sig，Excel 可直接打开）。
    paths["chapters_csv"] = out_dir / "chapters.csv"
    fieldnames = [
        "index",
        "title",
        "mp3_filename",
        "mp3_bytes",
        "mp3_size_mb",
        "mp3_duration_ms",
        "mp3_duration_s",
        "m4b_start_ms",
        "m4b_end_ms",
        "m4b_start_s",
        "m4b_end_s",
        "m4b_duration_ms",
        "web_mp3_duration_ms",
        "web_mp3_duration_s",
        "mp3_duration_diff_ms",
        "web_m4b_start_ms",
        "web_m4b_end_ms",
        "web_m4b_start_s",
        "web_m4b_end_s",
        "m4b_start_diff_ms",
        "m4b_end_diff_ms",
        "web_state",
        "probed_m4b_title",
    ]
    temporary = paths["chapters_csv"].with_name(f".{paths['chapters_csv'].name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(evidence["chapters"])
        os.replace(temporary, paths["chapters_csv"])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    # 5) 检查项 JSON + 汇总 JSON。
    paths["checks_json"] = out_dir / "checks.json"
    _write_json(paths["checks_json"], evidence["checks"])
    paths["summary_json"] = out_dir / "summary.json"
    _write_json(paths["summary_json"], evidence)

    # 6) 中文报告（Markdown）。
    paths["report_md"] = out_dir / "report.md"
    _write_text(paths["report_md"], _render_report(evidence))
    return paths


def _render_report(evidence: dict[str, Any]) -> str:
    job = evidence["job"]
    totals = evidence["totals"]
    passed = sum(1 for check in evidence["checks"] if check["ok"])
    failed = [check for check in evidence["checks"] if not check["ok"]]
    lines = [
        "# RelaxBook A3 验收证据",
        "",
        f"- 生成时间：{evidence['generated_at']}",
        f"- Git 提交：{evidence['git_commit']}",
        f"- 平台：{evidence['platform']} / Python {evidence['python']}",
        f"- 数据目录：{evidence['settings']['data_dir']}",
        f"- 引擎/音色/语速：{evidence['settings']['engine']}"
        f"（解析为 {evidence['settings'].get('engine_resolved', 'edge')}） / "
        f"{evidence['settings']['voice']} / {evidence['settings']['rate']}"
        f"（并发 {evidence['settings']['concurrency']}）",
        f"- ffmpeg：{evidence['settings']['ffmpeg']}",
        f"- ffprobe：{evidence['settings']['ffprobe']}",
        "",
        "## 任务与成品",
        "",
        f"- 任务号：{job['id']}，状态：**{job['state']}**",
        f"- 片段：{evidence['stats']['done']}/{evidence['stats']['total']} 完成，"
        f"{evidence['stats']['failed']} 失败",
        f"- 章数：{job['done_chapters']}/{job['total_chapters']}",
        f"- M4B：`{evidence['m4b']['filename']}`，{_format_mb(evidence['m4b']['bytes'])} MB，"
        f"{totals['audio_duration_s']} 秒（探测），内嵌 {len(evidence['m4b']['chapters'])} 章",
        f"- 三个 MP3 合计：{_format_mb(totals['mp3_bytes_sum'])} MB，"
        f"{totals['mp3_sum_ms']} 毫秒（探测合计）",
        f"- 章节衔接最大间隙：{totals['max_seam_gap_ms']} 毫秒",
        "",
        "## 章节表（序号 / 章名 / MP3 文件名 / 大小 / 时长 / M4B 起止）",
        "",
        "| 序号 | 章名 | MP3 文件名 | 大小(MB) | 时长(s) | M4B 起点(s) | M4B 终点(s) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in evidence["chapters"]:
        lines.append(
            f"| {row['index']} | {row['title']} | {row['mp3_filename']} | "
            f"{row['mp3_size_mb']} | {row['mp3_duration_s']} | {row['m4b_start_s']} | "
            f"{row['m4b_end_s']} |"
        )
    lines += [
        "",
        "## 网页结果页显示值 vs 成品探测值（秒值误差要求 ≤ 1 秒）",
        "",
        "| 序号 | 网页时长(s) | 探测时长(s) | 时长差(ms) | 网页 M4B 起止(s) | 探测 M4B 起止(s) | 起止差(ms) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in evidence["chapters"]:
        lines.append(
            f"| {row['index']} | {row['web_mp3_duration_s']} | {row['mp3_duration_s']} | "
            f"{row['mp3_duration_diff_ms']} | "
            f"{row['web_m4b_start_s']}-{row['web_m4b_end_s']} | "
            f"{row['m4b_start_s']}-{row['m4b_end_s']} | "
            f"{row['m4b_start_diff_ms']}/{row['m4b_end_diff_ms']} |"
        )
    run = evidence.get("run") or {}
    generation_seconds = run.get("generation_seconds")
    cache_misses = int(run.get("cache_misses") or 0)
    cache_hits = int(run.get("cache_hits") or 0)
    if run.get("reused_job"):
        efficiency_mode = "复用已完成任务，本次未重新合成（耗时为历史记录）"
    elif cache_misses == 0 and cache_hits:
        efficiency_mode = "全部片段命中缓存，耗时仅包含封装"
    elif cache_hits == 0:
        efficiency_mode = "真实合成（全部片段重新合成）"
    else:
        efficiency_mode = "真实合成（部分片段命中缓存）"
    efficiency_lines = [
        f"- 本次生成耗时：{generation_seconds:.2f} 秒（{efficiency_mode}）"
        if generation_seconds is not None
        else f"- 本次生成耗时：未重新合成（{efficiency_mode}）",
        f"- 缓存：命中 {cache_hits} 个片段，未命中 {cache_misses} 个片段",
    ]
    if generation_seconds and totals["audio_duration_s"]:
        seconds_per_hour = generation_seconds * 3600.0 / float(totals["audio_duration_s"])
        efficiency_lines.append(
            f"- 成品音频 {totals['audio_duration_s']} 秒；每 1 小时成品音频约需 "
            f"{seconds_per_hour:.0f} 秒（{seconds_per_hour / 60:.1f} 分钟）"
            + ("（历史记录，本次复用）" if run.get("reused_job") else "")
        )
    lines += [
        "",
        "## 生成效率（每小时成品音频所需生成时间）",
        "",
        *efficiency_lines,
        "",
        "## 检查项",
        "",
        f"通过 {passed}/{len(evidence['checks'])} 项"
        + ("" if not failed else f"，失败 {len(failed)} 项："),
        "",
    ]
    if failed:
        for check in failed:
            lines.append(
                f"- [FAIL] {check['id']} {check['name']}：期望 {check['expected']}，"
                f"实际 {check['actual']} {check['detail']}"
            )
        lines.append("")
    lines += [
        "## 复现命令",
        "",
        "```sh",
        f'ffprobe -v error -show_format -show_chapters -of json "{evidence["m4b"]["filename"]}"',
    ]
    for artifact in evidence["mp3_artifacts"]:
        lines.append(f'ffprobe -v error -show_format -of json "{artifact["filename"]}"')
    lines += [
        f'ffmpeg -v error -xerror -i "{evidence["m4b"]["filename"]}" -map 0:a:0 -f null -',
        "```",
        "",
        "## 证据文件",
        "",
        "- `ffprobe_m4b.json`：M4B 的原始 ffprobe JSON（含 format 与 chapters）",
        "- `ffprobe_mp3/*.json`：每个 MP3 的原始 ffprobe JSON",
        "- `ffmpeg_decode_check.txt`：`ffmpeg -f null` 校验命令与退出码",
        "- `chapters.csv`：章节表（含网页值与探测值对照）",
        "- `checks.json`：逐项检查结果",
        "- `summary.json`：本次验收的机器可读汇总",
        "- `round1/`、`round2/`：每次执行任务的进度证据（含 A5 恢复轮次）",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# stdout 摘要
# ---------------------------------------------------------------------------
def _print_summary(evidence: dict[str, Any], rounds: list[dict[str, Any]], *, generation_seconds: float | None) -> None:
    job = evidence["job"]
    totals = evidence["totals"]
    checks = evidence["checks"]
    passed = sum(1 for check in checks if check["ok"])
    failed = [check for check in checks if not check["ok"]]

    print("", flush=True)
    print("=" * 78, flush=True)
    print("RelaxBook A3 验收摘要（三章样书）", flush=True)
    print("=" * 78, flush=True)
    print(
        f"任务 job={job['id']}  状态={job['state']}  "
        f"片段 {evidence['stats']['done']}/{evidence['stats']['total']}  "
        f"章 {job['done_chapters']}/{job['total_chapters']}",
        flush=True,
    )
    print(
        f"引擎={evidence['settings']['engine']}"
        f"（{evidence['settings'].get('engine_resolved', 'edge')}） "
        f"音色={evidence['settings']['voice']} "
        f"语速={evidence['settings']['rate']} 并发={evidence['settings']['concurrency']}",
        flush=True,
    )
    print("", flush=True)
    print("[成品章节表]", flush=True)
    print(
        _table(
            ["序号", "章名", "MP3 文件名", "大小(MB)", "时长(s)", "M4B 起点(s)", "M4B 终点(s)"],
            [
                [
                    row["index"],
                    row["title"],
                    row["mp3_filename"],
                    row["mp3_size_mb"],
                    row["mp3_duration_s"],
                    row["m4b_start_s"],
                    row["m4b_end_s"],
                ]
                for row in evidence["chapters"]
            ],
            aligns=["right", "left", "left", "right", "right", "right", "right"],
        ),
        flush=True,
    )
    print("", flush=True)
    print("[网页结果页显示值 vs 成品探测值]", flush=True)
    print(
        _table(
            [
                "序号",
                "网页时长(s)",
                "探测时长(s)",
                "时长差(ms)",
                "网页 M4B 起止(s)",
                "探测 M4B 起止(s)",
                "起止差(ms)",
            ],
            [
                [
                    row["index"],
                    row["web_mp3_duration_s"],
                    row["mp3_duration_s"],
                    row["mp3_duration_diff_ms"],
                    f"{row['web_m4b_start_s']}-{row['web_m4b_end_s']}",
                    f"{row['m4b_start_s']}-{row['m4b_end_s']}",
                    f"{row['m4b_start_diff_ms']}/{row['m4b_end_diff_ms']}",
                ]
                for row in evidence["chapters"]
            ],
            aligns=["right", "right", "right", "right", "right", "right", "right"],
        ),
        flush=True,
    )
    print("", flush=True)
    print(
        f"整书：M4B {_format_mb(evidence['m4b']['bytes'])} MB / "
        f"{totals['audio_duration_s']} 秒；三个 MP3 合计 {_format_mb(totals['mp3_bytes_sum'])} MB / "
        f"{totals['mp3_sum_ms']} 毫秒；章节衔接最大间隙 {totals['max_seam_gap_ms']} 毫秒",
        flush=True,
    )
    print(
        f"解码：M4B ffmpeg -f null 退出码 {evidence['m4b']['decode']['returncode']}；"
        f"MP3 逐章解码 "
        + "、".join(
            f"{artifact['filename']}={artifact['decode_ok']}" for artifact in evidence["mp3_artifacts"]
        ),
        flush=True,
    )
    print("", flush=True)
    print("[执行轮次]", flush=True)
    for round_result in rounds:
        label = "复用已有成品" if round_result["attempt"] == 0 else f"第 {round_result['attempt']} 轮"
        print(
            f"  {label}：状态={round_result['state']} "
            f"片段 {round_result['done_segments']}/{round_result['total_segments']} "
            f"失败={round_result['failed_segments']} 缓存命中={round_result['cache_hit']} "
            f"未命中={round_result['cache_miss']} 耗时={round_result['elapsed_seconds']:.2f}s",
            flush=True,
        )
    reused_job = bool((evidence.get("run") or {}).get("reused_job"))
    total_miss = sum(int(round_result.get("cache_miss") or 0) for round_result in rounds)
    total_hit = sum(int(round_result.get("cache_hit") or 0) for round_result in rounds)
    if generation_seconds is not None and totals["audio_duration_s"]:
        seconds_per_hour = generation_seconds * 3600.0 / float(totals["audio_duration_s"])
        if reused_job:
            print(
                f"  生成效率（历史）：上次生成耗时 {generation_seconds:.2f}s，"
                f"成品音频 {totals['audio_duration_s']}s，"
                f"每 1 小时约需 {seconds_per_hour:.0f}s；本次复用成品，未重新合成",
                flush=True,
            )
        elif total_miss == 0:
            print(
                f"  生成效率：本次 {total_hit} 个片段全部命中缓存、未重新合成，"
                f"仅封装耗时 {generation_seconds:.2f}s；"
                f"真实合成效率请用空数据目录（或 --new-job 且清除片段缓存）重跑",
                flush=True,
            )
        else:
            print(
                f"  生成效率：{generation_seconds:.2f}s 生成 {totals['audio_duration_s']}s 音频"
                f"（缓存命中 {total_hit}、未命中 {total_miss}）"
                f" -> 每 1 小时成品音频约需 {seconds_per_hour:.0f}s"
                f"（{seconds_per_hour / 60:.1f} 分钟）",
                flush=True,
            )
    else:
        print(
            f"  生成效率：本次复用已完成的成品，未重新合成"
            f"（缓存命中 {total_hit}、未命中 {total_miss}；--new-job 可强制重新生成）",
            flush=True,
        )
    print("", flush=True)
    print(f"[检查项] 通过 {passed}/{len(checks)}", flush=True)
    for check in failed:
        print(
            f"  [FAIL] {check['id']} {check['name']}：期望 {check['expected']}，实际 {check['actual']}"
            + (f"（{check['detail']}）" if check["detail"] else ""),
            flush=True,
        )
    if not failed:
        print("  全部通过（PASS）", flush=True)
    print("", flush=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(settings: Settings, book_path: Path, args: argparse.Namespace) -> int:
    acceptance_dir = Path(settings.data_dir) / "acceptance"
    acceptance_dir.mkdir(parents=True, exist_ok=True)
    engine_name = (args.engine or settings.engine or "edge").strip()
    voice = (args.voice or settings.voice or "zh-CN-XiaoxiaoNeural").strip()
    rate = (args.rate or settings.rate or "+0%").strip()
    if args.concurrency:
        settings.concurrency = max(1, int(args.concurrency))

    print(f"数据目录：{settings.data_dir}", flush=True)
    print(f"样书：{book_path}", flush=True)
    print(f"ffmpeg：{settings.ffmpeg}", flush=True)
    print(f"ffprobe：{settings.ffprobe}", flush=True)
    resolved_engine = tts.resolve_engine_name(engine_name, settings)
    print(
        f"引擎/音色/语速：{engine_name}（解析为 {resolved_engine}） / {voice} / {rate}"
        f"（并发 {settings.concurrency}）",
        flush=True,
    )

    print("", flush=True)
    print("[1/4] 导入样书并解析 ...", flush=True)
    try:
        book = _prepare_book(settings, book_path)
    except IngestError as exc:
        print(f"解析失败：{exc}", flush=True)
        return EXIT_FAIL
    print(
        f"      书名《{book['title']}》，作者 {book['author'] or '（未知）'}，"
        f"{book['chapters']} 章（{'复用已有书籍' if book['reused'] else '新导入'}）",
        flush=True,
    )
    for warning in book.get("warnings") or []:
        print(f"      提示：{warning}", flush=True)

    print("[2/4] 规划片段并创建/复用任务 ...", flush=True)
    job_info = _ensure_job(
        settings,
        book,
        engine_name=engine_name,
        voice=voice,
        rate=rate,
        new_job=bool(args.new_job),
    )
    job_id = int(job_info["job_id"])
    if job_info["reused"]:
        print(f"      复用任务 job={job_id}（已完成且 artifact 完整）", flush=True)
    else:
        print(
            f"      新建任务 job={job_id}：{job_info['total_chapters']} 章 / "
            f"{len(job_info['items'])} 片段",
            flush=True,
        )

    print("[3/4] 执行转换（真实 Edge 合成 + FFmpeg 封装）...", flush=True)
    rounds: list[dict[str, Any]] = []
    generation_seconds: float | None = None
    max_attempts = 1 if args.no_retry else 2
    previous_failed = 0
    snapshot = _snapshot(settings, job_id)
    already_done = snapshot["job"]["state"] == "done" and _artifacts_complete(
        settings, snapshot["artifacts"]
    )
    if already_done:
        generation_seconds = None
        rounds.append(
            {
                "attempt": 0,
                "state": "done",
                "done_segments": snapshot["stats"]["done"],
                "total_segments": snapshot["stats"]["total"],
                "failed_segments": 0,
                "cache_hit": 0,
                "cache_miss": 0,
                "elapsed_seconds": 0.0,
                "note": "复用已完成任务，未重新合成",
            }
        )
        print("      任务已完成且成品完整，直接复用（不覆盖已有 artifact）", flush=True)
    else:
        for attempt in range(1, max_attempts + 1):
            round_result = _run_attempt(
                settings,
                job_id,
                attempt=attempt,
                previous_failed=previous_failed,
                verbose=bool(args.verbose),
            )
            rounds.append(round_result)
            generation_seconds = (generation_seconds or 0.0) + round_result["elapsed_seconds"]
            if round_result["state"] == "done":
                break
            if attempt >= max_attempts:
                break
            if round_result["previous_failed"] and round_result["failed_segments"] == 0:
                break  # 没有片段失败（如封装失败），重试一次即可
            previous_failed = _simulate_a5_recovery(settings, job_id)

    # 每轮证据分别落到 acceptance/roundN/round.json。
    # 复用已完成任务时保留之前生成轮次的证据，不清掉。
    if not already_done:
        for stale in (acceptance_dir / "round1", acceptance_dir / "round2", acceptance_dir / "reused"):
            stale_file = stale / "round.json"
            try:
                stale_file.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                pass
    for round_result in rounds:
        if round_result["attempt"] == 0:
            round_dir = acceptance_dir / "reused"
        else:
            round_dir = acceptance_dir / f"round{round_result['attempt']}"
        round_dir.mkdir(parents=True, exist_ok=True)
        _write_json(round_dir / "round.json", round_result)

    snapshot = _snapshot(settings, job_id)
    if snapshot["job"]["state"] != "done":
        failed_errors = snapshot["failed_errors"][-3:]
        network_hint = "（错误信息显示为网络/服务类问题，缓存已保留；网络恢复后重跑本工具即可只补缺失片段）" if (
            failed_errors and all(_is_network_error(error) for error in failed_errors)
        ) else ""
        print("", flush=True)
        print(f"[结论] 任务未完成：状态={snapshot['job']['state']}{network_hint}", flush=True)
        print(f"任务错误：{snapshot['job'].get('error')}", flush=True)
        for error in failed_errors:
            print(f"  失败片段：{error[-300:]}", flush=True)
        _write_json(
            acceptance_dir / "failure.json",
            {
                "job_id": job_id,
                "state": snapshot["job"]["state"],
                "error": snapshot["job"].get("error"),
                "failed_segment_errors": snapshot["failed_errors"],
                "rounds": rounds,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        print(f"失败证据已写入：{acceptance_dir / 'failure.json'}", flush=True)
        return EXIT_FAIL

    print("[4/4] 探测成品、生成 A3 证据 ...", flush=True)
    evidence = _build_evidence(settings, job_id)
    evidence["rounds"] = rounds
    evidence["run"] = {
        "book_path": str(book_path),
        "arguments": sys.argv,
        "generation_seconds": generation_seconds,
        "reused_job": bool(job_info["reused"]),
        "retry_enabled": not args.no_retry,
        "cache_hits": sum(int(item.get("cache_hit") or 0) for item in rounds),
        "cache_misses": sum(int(item.get("cache_miss") or 0) for item in rounds),
        "round_count": len([item for item in rounds if item.get("attempt")]),
    }
    if generation_seconds is None:
        # 复用任务时保留历史耗时（如果 summary.json 里有）。
        try:
            previous_summary = json.loads((acceptance_dir / "summary.json").read_text(encoding="utf-8"))
            generation_seconds = previous_summary.get("run", {}).get("generation_seconds")
        except (OSError, ValueError):
            generation_seconds = None
        evidence["run"]["generation_seconds"] = generation_seconds
    paths = _write_evidence(settings, evidence, acceptance_dir)
    _print_summary(evidence, rounds, generation_seconds=generation_seconds)

    failed = [check for check in evidence["checks"] if not check["ok"]]
    print(f"证据目录：{acceptance_dir}", flush=True)
    for key in ("report_md", "chapters_csv", "summary_json", "ffprobe_m4b", "ffmpeg_decode_check"):
        print(f"  {key}: {paths[key]}", flush=True)
    if failed:
        print(f"\n[结论] 失败 {len(failed)} 项，退出码 {EXIT_FAIL}", flush=True)
        return EXIT_FAIL
    print(f"\n[结论] A3 证据全部通过，退出码 {EXIT_OK}", flush=True)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools/acceptance.py",
        description="RelaxBook A3 验收证据生成器（三章样书端到端转换 + ffprobe 核对）",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="数据目录（必须位于仓库之外，例如 D:\\dsh\\tmp\\relaxbook-acceptance）",
    )
    parser.add_argument(
        "--book",
        default=str(Path("sample") / "示例书.epub"),
        help="EPUB 样书路径（默认 sample/示例书.epub）",
    )
    parser.add_argument("--voice", default=None, help="音色（默认取配置文件）")
    parser.add_argument("--rate", default=None, help="语速（默认取配置文件，如 +0%%）")
    parser.add_argument("--engine", default=None, help="引擎（默认取配置文件，如 edge）")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="本次运行的合成并发数（默认取配置文件；本机 Edge 端点建议 2-3）",
    )
    parser.add_argument(
        "--new-job",
        action="store_true",
        help="忽略已有任务，强制新建任务（已有成品不会被覆盖）",
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help="任务失败后不自动按 A5 恢复流程重试（默认会重试一次）",
    )
    parser.add_argument("--verbose", action="store_true", help="打印 worker 的详细日志")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_dir = Path(args.data_dir).expanduser()
    if _is_inside(data_dir, ROOT):
        print(
            f"数据目录不能位于仓库内：{data_dir.resolve() if data_dir.exists() else data_dir}\n"
            f"请使用仓库之外的目录，例如 D:\\dsh\\tmp\\relaxbook-acceptance",
            file=sys.stderr,
        )
        return EXIT_USAGE
    book_path = Path(args.book)
    if not book_path.is_absolute() and not book_path.is_file():
        candidate = ROOT / book_path
        if candidate.is_file():
            book_path = candidate
    if not book_path.is_file():
        print(f"样书不存在：{args.book}", file=sys.stderr)
        return EXIT_USAGE

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings(data_dir)
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    if not settings.ffmpeg or not settings.ffprobe:
        print(
            "未找到 ffmpeg/ffprobe：请安装 FFmpeg 7+ 或在 config.json 中配置路径\n"
            f"  ffmpeg={settings.ffmpeg or '（未找到）'}\n"
            f"  ffprobe={settings.ffprobe or '（未找到）'}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    try:
        return run(settings, book_path, args)
    except KeyboardInterrupt:
        print("\n已中断（已完成片段的缓存仍保留）。", file=sys.stderr)
        return EXIT_FAIL
    except (RuntimeError, OSError) as exc:
        print(f"验收执行失败：{exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
