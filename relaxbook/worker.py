"""Background audiobook conversion worker (CONTRACTS section 11).

Run as ``python -m relaxbook.worker [--data-dir DIR]``.

Life cycle of one job (state machine ``queued -> synthesizing -> packaging -> done``,
any stage may end in ``failed``; ``paused`` is only set by the web API):

1. Load the job, its frozen revision and the chapters marked ``read``.
2. Normalise each chapter into segments (``textnorm.segment_text``) and give
   every segment a content-addressed cache key.  Segments are persisted once per
   job, so a restart reuses them and only fills the gaps.
3. Synthesise with a ``ThreadPoolExecutor`` (default 4).  A segment whose WAV
   is already in ``segments/<key[:2]>/<key>.wav`` and decodes to a non-zero
   duration is marked done **without** calling the TTS engine at all.  After
   every finished segment the job's progress is persisted and before every new
   submission the job state is re-read, so ``paused`` stops new work while the
   in-flight segments are allowed to finish.
4. As soon as all segments of a chapter are done its MP3 is assembled and
   published atomically, so the first chapter is downloadable while the rest of
   the book is still being synthesised.
5. When every segment is done the job moves to ``packaging``; the M4B is built
   and verified, the result is stored and the job becomes ``done``.  A packaging
   failure keeps the finished MP3s and marks the job ``failed``; retrying such a
   job only redoes the missing packaging because all segments are already done.
6. Every synthesised segment is logged as
   ``job=<id> seg=<id> cache=hit|miss synth_ms=<n>`` to
   ``<data_dir>/logs/worker.log`` and stdout (A5 evidence).

A single-instance OS file lock on ``<data_dir>/worker.lock`` prevents two worker
processes from racing.  OS-level locks (``msvcrt.locking`` / ``fcntl.flock``) are
released by the kernel when a process is killed, so a forcefully terminated
worker can be started again while a *stale plain lock file* would have blocked it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import IO, Any, Iterable

from . import audio, db, naming, packaging, textnorm
from .config import Settings, load_settings, locate_tool
from .tts import TTSError, get_engine

logger = logging.getLogger("relaxbook.worker")

LOCK_NAME = "worker.lock"
LOG_NAME = "worker.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Output version directory under ``jobs/<job_id>/`` (writer is one per job).
VERSION = 1

#: Direction for ``wait()``: process the first segment that finishes.
_WAIT_FIRST = FIRST_COMPLETED


# --------------------------------------------------------------------------- #
# logging / single-instance lock
# --------------------------------------------------------------------------- #
def _setup_logging(settings: Settings) -> None:
    """Send ``relaxbook.*`` logs to ``<data_dir>/logs/worker.log`` and stdout."""
    logs_dir = Path(settings.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    base = logging.getLogger("relaxbook")
    base.setLevel(logging.INFO)
    if getattr(base, "_relaxbook_worker_handlers", False):
        return
    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(logs_dir / LOG_NAME, encoding="utf-8"))
    except OSError as exc:  # pragma: no cover - only on a broken data dir
        logger.warning("无法写入日志文件，仅输出到控制台：%s", exc)
    handlers.append(logging.StreamHandler(stream=sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)
        base.addHandler(handler)
    base._relaxbook_worker_handlers = True  # type: ignore[attr-defined]


def _acquire_lock(data_dir: Path | str) -> IO[str] | None:
    """Take the single-instance OS lock; ``None`` means another worker holds it.

    The PID is written before locking because Windows forbids truncating or
    writing the locked byte range; the lock itself is the authoritative check and
    the kernel drops it when the process dies, so a killed worker does not leave
    a blocking stale lock behind.
    """
    path = Path(data_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    # Open by path (not by fd) so ``handle.name`` stays a str and the release
    # path can be removed later.
    handle = open(path, "r+", encoding="utf-8", errors="replace")
    try:
        handle.seek(0)
        handle.write(str(os.getpid()).ljust(12))
        handle.flush()
    except OSError:
        # Another worker already holds the lock (writes to the locked byte
        # range are denied by Windows) or the file is not writable.  Closing a
        # handle with a failed buffered write raises again, so swallow that too.
        try:
            handle.close()
        except OSError:
            pass
        return None
    try:
        handle.seek(0)
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            handle.close()
        except OSError:
            pass
        return None
    return handle


def _release_lock(handle: IO[str] | None) -> None:
    if handle is None:
        return
    lock_path = Path(handle.name)
    try:
        handle.seek(0)
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:  # pragma: no cover - defensive
        pass
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - defensive
        pass


# --------------------------------------------------------------------------- #
# per-run context
# --------------------------------------------------------------------------- #
class _JobContext:
    """In-memory view of one job used by the main thread of :meth:`JobRunner.run_job`.

    Segment statuses live in this dictionary while the run is active and are
    written through to SQLite as they change.  Worker threads never touch the
    database; they only return synthesis results to the main thread.
    """

    def __init__(
        self,
        conn,
        job,
        chapters: Iterable[dict],
        segments: Iterable[dict],
    ) -> None:
        self.conn = conn
        self.job = job
        self.job_id = int(job["id"])
        self.chapters = list(chapters)
        self.chapter_map = {int(ch["idx"]): ch for ch in self.chapters}
        self.seq = {int(ch["idx"]): position + 1 for position, ch in enumerate(self.chapters)}
        self.segs: dict[int, list[dict]] = {int(ch["idx"]): [] for ch in self.chapters}
        for segment in segments:
            self.segs.setdefault(int(segment["chapter_idx"]), []).append(dict(segment))
        self.status: dict[int, str] = {int(s["id"]): s["status"] for s in segments}
        self.audio_paths: dict[int, str | None] = {
            int(s["id"]): s.get("audio_path") for s in segments
        }
        self.first_error: str | None = None
        self.assembly_errors: dict[int, str] = {}
        self.assembled: set[int] = set()
        for artifact in db.list_artifacts(conn, self.job_id):
            if (
                artifact["kind"] == "mp3"
                and artifact["chapter_idx"] is not None
                and artifact["verified"]
                and Path(artifact["path"]).is_file()
            ):
                self.assembled.add(int(artifact["chapter_idx"]))

    def done_count(self) -> int:
        return sum(1 for status in self.status.values() if status == "done")

    def failed_count(self) -> int:
        return sum(1 for status in self.status.values() if status == "failed")

    def chapter_segments(self, chapter_idx: int) -> list[dict]:
        return self.segs.get(int(chapter_idx)) or []

    def chapter_complete(self, chapter_idx: int) -> bool:
        segments = self.chapter_segments(chapter_idx)
        return bool(segments) and all(
            self.status.get(int(segment["id"])) == "done" for segment in segments
        )


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class JobRunner:
    """Convert one job at a time; :meth:`run_job` is safe to call directly in tests."""

    def __init__(self, settings: Settings, engine=None) -> None:
        self.settings = settings
        self._injected_engine = engine
        self._engines: dict[str, Any] = {}
        if not getattr(settings, "ffmpeg", ""):
            settings.ffmpeg = locate_tool("ffmpeg")
        if not getattr(settings, "ffprobe", ""):
            settings.ffprobe = locate_tool("ffprobe")
        # Packaging entry points are attributes on purpose: tests (and future
        # alternative packagers) can inject a stand-in without touching globals.
        self.assemble_chapter = packaging.assemble_chapter
        self.assemble_m4b = packaging.assemble_m4b
        self.verify_m4b = packaging.verify_m4b
        self.log = logger

    # -- paths ------------------------------------------------------------ #
    def job_dir(self, job_id: int) -> Path:
        return Path(self.settings.jobs_dir) / str(int(job_id))

    def version_dir(self, job_id: int) -> Path:
        return self.job_dir(job_id) / f"v{VERSION:02d}"

    def segment_path(self, cache_key: str) -> Path:
        key = str(cache_key)
        return Path(self.settings.segments_dir) / key[:2] / f"{key}.wav"

    # -- engines ---------------------------------------------------------- #
    def engine_for(self, job) -> Any:
        if self._injected_engine is not None:
            return self._injected_engine
        name = (job["engine"] or "edge") if job is not None else "edge"
        if name not in self._engines:
            self._engines[name] = get_engine(name, self.settings)
        return self._engines[name]

    def model_for(self, job, engine) -> str:
        return str(job["model"] or getattr(engine, "model", "") or "")

    # -- public API ------------------------------------------------------- #
    def run_job(self, job_id: int) -> None:
        """Run (or resume) one job to its next resting state."""
        conn = db.connect(self.settings.db_path)
        try:
            self._run_job(conn, int(job_id))
        finally:
            conn.close()

    def loop_forever(self) -> None:
        """Claim queued jobs forever, sleeping one second when the queue is empty."""
        conn = db.connect(self.settings.db_path)
        try:
            while True:
                job = db.claim_next_job(conn)
                if job is None:
                    time.sleep(1.0)
                    continue
                job_id = int(job["id"])
                logger.info(
                    "开始任务 job=%s（引擎 %s，音色 %s，语速 %s）",
                    job_id,
                    job["engine"],
                    job["voice"],
                    job["rate"],
                )
                try:
                    self.run_job(job_id)
                except Exception as exc:  # noqa: BLE001 - keep the worker alive
                    logger.exception("任务 job=%s 发生未预期错误，已标记失败", job_id)
                    try:
                        db.set_job_state(
                            conn, job_id, "failed", error=f"工作进程内部错误：{exc}"
                        )
                    except Exception:  # pragma: no cover - last resort
                        logger.exception("任务 job=%s 状态写入失败", job_id)
        finally:
            conn.close()

    # -- main thread steps ------------------------------------------------ #
    def _run_job(self, conn, job_id: int) -> None:
        job = db.get_job(conn, job_id)
        if job is None:
            logger.error("任务不存在：job=%s", job_id)
            return
        revision = db.get_revision(conn, int(job["revision_id"]))
        if revision is None:
            self._fail(conn, job_id, "任务对应的编辑版本不存在，无法生成。")
            return
        chapters = [
            chapter
            for chapter in db.list_chapters(conn, int(revision["id"]))
            if chapter.get("read")
        ]
        engine = self.engine_for(job)
        model = self.model_for(job, engine)
        voice = str(job["voice"] or self.settings.voice)
        rate = str(job["rate"] or "+0%")

        segments = [dict(item) for item in db.list_segments(conn, job_id)]
        if not segments:
            plan, chapters = self._build_plan(conn, job, chapters, model)
            if not plan:
                self._fail(conn, job_id, "任务没有可朗读的正文片段（各章正文为空），未生成音频。")
                return
            db.create_segments(conn, job_id, plan)
            db.freeze_revision(conn, int(job["revision_id"]))
            segments = [dict(item) for item in db.list_segments(conn, job_id)]
            logger.info(
                "job=%s 建立 %d 个片段，覆盖 %d 个朗读章", job_id, len(segments), len(chapters)
            )
        else:
            chapter_idxs = {int(item["chapter_idx"]) for item in segments}
            chapters = [chapter for chapter in chapters if int(chapter["idx"]) in chapter_idxs]

        conn.execute(
            "UPDATE job SET total_segments=?, total_chapters=?, updated_at=? WHERE id=?",
            (len(segments), len(chapters), db.utcnow(), job_id),
        )
        db.set_job_state(conn, job_id, "synthesizing")

        context = _JobContext(conn, job, chapters, segments)
        paused = self._synthesize(context, engine, voice, rate)
        stats = db.segment_stats(conn, job_id)
        if paused:
            logger.info(
                "job=%s 已暂停：完成 %d/%d 片段，在飞片段已收尾，等待继续。",
                job_id,
                stats["done"],
                stats["total"],
            )
            return
        if stats["failed"] > 0 or stats["done"] < stats["total"]:
            detail = context.first_error or "未知原因"
            self._fail(
                conn,
                job_id,
                f"{stats['failed']} 个片段合成失败（已完成 {stats['done']}/{stats['total']}），"
                f"已完成片段保留，可重试；首个错误：{detail}",
            )
            return

        # Re-assemble any chapter MP3 still missing (e.g. retry after a
        # packaging failure, when every segment is already done).
        self._assemble_chapters(context, [int(chapter["idx"]) for chapter in chapters])
        if context.assembly_errors:
            detail = "；".join(
                context.assembly_errors[idx] for idx in sorted(context.assembly_errors)
            )
            self._fail(conn, job_id, f"章节 MP3 封装失败，已完成片段保留，可重试：{detail}")
            return
        self._package(context)

    def _build_plan(self, conn, job, chapters: list[dict], model: str) -> tuple[list[dict], list[dict]]:
        """Segment every readable chapter and compute content-addressed cache keys."""
        plan: list[dict] = []
        planned: list[dict] = []
        engine_name = str(job["engine"] or self.settings.engine)
        voice = str(job["voice"])
        rate = str(job["rate"] or "+0%")
        for chapter in chapters:
            texts = textnorm.segment_text(chapter.get("text") or "")
            if not texts:
                logger.warning(
                    "job=%s 第 %s 章「%s」没有可朗读正文，已跳过。",
                    job["id"],
                    chapter["idx"],
                    chapter["title"],
                )
                continue
            planned.append(chapter)
            for order, text in enumerate(texts):
                cache_key = textnorm.segment_cache_key(
                    text,
                    engine=engine_name,
                    model=model,
                    voice=voice,
                    rate=rate,
                )
                plan.append(
                    {
                        "chapter_idx": int(chapter["idx"]),
                        "ord": order,
                        "text": text,
                        "cache_key": cache_key,
                    }
                )
        return plan, planned

    def _synthesize(self, context: _JobContext, engine, voice: str, rate: str) -> bool:
        """Fill all missing segments; returns ``True`` when paused mid-run."""
        conn = context.conn
        concurrency = max(1, int(getattr(self.settings, "concurrency", 1) or 1))
        pending = deque(
            dict(item)
            for item in db.list_segments(conn, context.job_id)
            if item["status"] != "done"
        )
        futures: dict[Future, dict] = {}
        paused = False
        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="relaxbook-tts"
        ) as pool:
            while pending or futures:
                if not paused:
                    while pending and len(futures) < concurrency:
                        # Required by the contract: check the state right before
                        # every new submission, so a pause stops new work fast.
                        state = self._job_state(conn, context.job_id)
                        if state == "paused":
                            paused = True
                            logger.info(
                                "job=%s 检测到暂停：不再提交新片段，等待 %d 个在飞片段完成。",
                                context.job_id,
                                len(futures),
                            )
                            break
                        if state in ("failed", "done"):
                            logger.warning(
                                "job=%s 状态为 %s，停止提交新片段。", context.job_id, state
                            )
                            paused = True
                            break
                        segment = pending.popleft()
                        target = self.segment_path(segment["cache_key"])
                        cached_ms = self._cached_duration(target)
                        if cached_ms is not None:
                            self._mark_done(context, segment, str(target), cached_ms, "hit", 0)
                            self._assemble_chapters(context, [int(segment["chapter_idx"])])
                            continue
                        futures[
                            pool.submit(self._synth_one, engine, segment, target, voice, rate)
                        ] = segment
                if not futures:
                    if paused or not pending:
                        break
                    continue
                done, _ = wait(list(futures.keys()), return_when=_WAIT_FIRST)
                for future in done:
                    segment = futures.pop(future)
                    try:
                        result = future.result()
                    except BaseException as exc:  # noqa: BLE001 - never lose a segment
                        result = {
                            "ok": False,
                            "error": f"合成线程异常：{exc}",
                            "synth_ms": 0,
                        }
                    if result.get("ok"):
                        self._mark_done(
                            context,
                            segment,
                            str(result["path"]),
                            int(result["duration_ms"]),
                            "miss",
                            int(result.get("synth_ms") or 0),
                        )
                    else:
                        self._mark_failed(context, segment, str(result.get("error")))
                        logger.warning(
                            "job=%s seg=%s cache=miss synth_ms=%s 合成失败：%s",
                            context.job_id,
                            segment["id"],
                            result.get("synth_ms") or 0,
                            result.get("error"),
                        )
                    self._assemble_chapters(context, [int(segment["chapter_idx"])])
        return paused

    def _synth_one(self, engine, segment: dict, target: Path, voice: str, rate: str) -> dict:
        """Worker-thread body: engine -> MP3 temp -> decode 24 kHz WAV -> atomic rename."""
        started = time.perf_counter()
        target = Path(target)
        token = uuid.uuid4().hex[:8]
        mp3_temp = target.with_name(f".{target.name}.{token}.mp3")
        wav_temp = target.with_name(f".{target.name}.{token}.wav")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = engine.synthesize(str(segment["text"]), voice, rate)
            if not data:
                raise TTSError("语音引擎返回空音频。")
            mp3_temp.write_bytes(data)
            audio.decode_to_wav(mp3_temp, wav_temp, ffmpeg=self.settings.ffmpeg)
            duration_ms = audio.wav_duration_ms(wav_temp)
            if duration_ms <= 0:
                raise RuntimeError("解码后的音频时长为 0，片段已丢弃。")
            os.replace(wav_temp, target)
            return {
                "ok": True,
                "path": str(target),
                "duration_ms": int(duration_ms),
                "synth_ms": int((time.perf_counter() - started) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 - reported as a failed segment
            return {
                "ok": False,
                "error": str(exc) or exc.__class__.__name__,
                "synth_ms": int((time.perf_counter() - started) * 1000),
            }
        finally:
            for temporary in (mp3_temp, wav_temp):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - best effort
                    pass

    # -- segment bookkeeping --------------------------------------------- #
    def _mark_done(
        self,
        context: _JobContext,
        segment: dict,
        path: str,
        duration_ms: int,
        cache: str,
        synth_ms: int,
    ) -> None:
        segment_id = int(segment["id"])
        db.mark_segment_done(
            context.conn, segment_id, audio_path=path, duration_ms=int(duration_ms)
        )
        context.status[segment_id] = "done"
        context.audio_paths[segment_id] = path
        logger.info(
            "job=%s seg=%s cache=%s synth_ms=%s",
            context.job_id,
            segment_id,
            cache,
            synth_ms,
        )
        self._progress(context)

    def _mark_failed(self, context: _JobContext, segment: dict, error: str) -> None:
        segment_id = int(segment["id"])
        db.mark_segment_failed(context.conn, segment_id, error)
        context.status[segment_id] = "failed"
        if context.first_error is None:
            context.first_error = error
        self._progress(context)

    def _progress(self, context: _JobContext) -> None:
        db.update_job_progress(
            context.conn,
            context.job_id,
            done_segments=context.done_count(),
            failed_segments=context.failed_count(),
        )

    def _cached_duration(self, path: Path) -> int | None:
        """A cache hit requires the WAV to exist and decode to a non-zero duration."""
        try:
            if not Path(path).is_file():
                return None
            duration_ms = int(audio.wav_duration_ms(Path(path)))
        except Exception as exc:  # noqa: BLE001 - a broken cache file is a miss
            logger.warning("缓存片段不可用，将重新合成：%s（%s）", path, exc)
            return None
        return duration_ms if duration_ms > 0 else None

    def _job_state(self, conn, job_id: int) -> str:
        row = conn.execute("SELECT state FROM job WHERE id=?", (job_id,)).fetchone()
        return str(row["state"]) if row is not None else "failed"

    # -- chapter MP3 ------------------------------------------------------ #
    def _assemble_chapters(self, context: _JobContext, chapter_idxs: Iterable[int]) -> None:
        for raw_idx in chapter_idxs:
            chapter_idx = int(raw_idx)
            if chapter_idx in context.assembled or chapter_idx in context.assembly_errors:
                continue
            chapter = context.chapter_map.get(chapter_idx)
            if chapter is None or not context.chapter_complete(chapter_idx):
                continue
            segments = context.chapter_segments(chapter_idx)
            wavs = [
                Path(str(context.audio_paths[int(seg["id"])]))
                for seg in segments
            ]
            missing = [str(path) for path in wavs if not path.is_file()]
            if missing:
                self._chapter_error(
                    context,
                    chapter_idx,
                    f"第 {context.seq.get(chapter_idx, chapter_idx)} 章「{chapter['title']}」"
                    f"的片段文件缺失：{missing[0]}",
                )
                continue
            out_path = self.version_dir(context.job_id) / naming.mp3_filename(
                context.seq.get(chapter_idx, chapter_idx),
                str(chapter["title"]),
                len(context.chapters),
            )
            try:
                info = self.assemble_chapter(
                    wavs,
                    out_path,
                    bitrate=self.settings.mp3_bitrate,
                    ffmpeg=self.settings.ffmpeg,
                    ffprobe=self.settings.ffprobe,
                )
            except Exception as exc:  # noqa: BLE001 - retried via /retry
                self._chapter_error(
                    context,
                    chapter_idx,
                    f"第 {context.seq.get(chapter_idx, chapter_idx)} 章「{chapter['title']}」"
                    f"MP3 封装失败：{exc}",
                )
                continue
            info = info or {}
            published = str(info.get("path") or out_path)
            size = int(info.get("bytes") or Path(published).stat().st_size)
            duration_ms = int(info.get("duration_ms") or 0)
            db.upsert_artifact(
                context.conn,
                job_id=context.job_id,
                kind="mp3",
                chapter_idx=chapter_idx,
                path=published,
                bytes=size,
                duration_ms=duration_ms,
                verified=True,
            )
            context.assembled.add(chapter_idx)
            db.update_job_progress(
                context.conn,
                context.job_id,
                done_chapters=len(context.assembled),
            )
            logger.info(
                "job=%s chapter=%s mp3 完成：%s（%s 字节，%s ms）",
                context.job_id,
                chapter_idx,
                Path(published).name,
                size,
                duration_ms,
            )

    def _chapter_error(self, context: _JobContext, chapter_idx: int, message: str) -> None:
        context.assembly_errors[chapter_idx] = message
        logger.error("job=%s %s", context.job_id, message)

    # -- final M4B -------------------------------------------------------- #
    def _package(self, context: _JobContext) -> None:
        conn = context.conn
        job = context.job
        job_id = context.job_id
        stats = db.segment_stats(conn, job_id)
        if stats["total"] == 0 or stats["done"] != stats["total"]:
            self._fail(conn, job_id, f"片段尚未全部完成（{stats['done']}/{stats['total']}），暂不封装 M4B。")
            return
        if stats["failed"]:
            self._fail(conn, job_id, f"{stats['failed']} 个片段合成失败，暂不封装 M4B。")
            return
        missing = [idx for idx in context.chapter_map if idx not in context.assembled]
        if missing:
            labels = "、".join(f"第{context.seq.get(idx, idx)}章" for idx in sorted(missing))
            self._fail(conn, job_id, f"章节 MP3 未全部生成（缺 {labels}），暂不封装 M4B。")
            return

        db.set_job_state(conn, job_id, "packaging")
        book = db.get_book(conn, int(job["book_id"]))
        title = str((book["title"] if book is not None else "") or "未命名书籍")
        author = book["author"] if book is not None else None
        cover_path: Path | None = None
        if book is not None and book["cover_path"]:
            candidate = Path(str(book["cover_path"]))
            if candidate.is_file():
                cover_path = candidate

        payload = []
        for chapter in context.chapters:
            chapter_idx = int(chapter["idx"])
            segments = context.chapter_segments(chapter_idx)
            payload.append(
                {
                    "index": context.seq.get(chapter_idx, chapter_idx),
                    "title": str(chapter["title"]),
                    "segment_wavs": [
                        Path(str(context.audio_paths[int(seg["id"])]))
                        for seg in segments
                    ],
                }
            )
        out_dir = self.version_dir(job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / naming.m4b_filename(title, self._voice_label(context, job), VERSION)
        try:
            info = self.assemble_m4b(
                payload,
                out_path,
                title=title,
                author=author,
                cover_path=cover_path,
                bitrate=self.settings.m4b_bitrate,
                ffmpeg=self.settings.ffmpeg,
                ffprobe=self.settings.ffprobe,
            )
        except Exception as exc:  # noqa: BLE001 - retried via /retry, MP3 kept
            self._fail(
                conn,
                job_id,
                f"封装 M4B 失败（已成功的 MP3 已保留，可直接重试封装）：{exc}",
            )
            return
        info = info or {}
        try:
            verification = self.verify_m4b(
                out_path,
                ffmpeg=self.settings.ffmpeg,
                ffprobe=self.settings.ffprobe,
                expected_chapters=len(payload),
            )
        except Exception as exc:  # noqa: BLE001 - treated as a verification failure
            verification = {"ok": False, "error": f"校验过程异常：{exc}"}
        if not verification.get("ok"):
            self._fail(
                conn,
                job_id,
                f"M4B 校验失败（已成功的 MP3 已保留，可直接重试封装）："
                f"{verification.get('error') or '未知原因'}",
            )
            return

        duration_ms = int(verification.get("duration_ms") or info.get("duration_ms") or 0)
        filename = Path(str(info.get("path") or out_path)).name
        size = int(info.get("bytes") or out_path.stat().st_size)
        # SQLite UNIQUE() treats NULLs as distinct, so the single M4B row is
        # replaced explicitly instead of relying on ON CONFLICT.
        conn.execute(
            "DELETE FROM artifact WHERE job_id=? AND kind='m4b' AND chapter_idx IS NULL",
            (job_id,),
        )
        db.upsert_artifact(
            conn,
            job_id=job_id,
            kind="m4b",
            path=str(out_path),
            bytes=size,
            duration_ms=duration_ms,
            verified=True,
        )
        db.set_job_result(
            conn,
            job_id,
            m4b_bytes=size,
            m4b_duration_ms=duration_ms,
            m4b_filename=filename,
            mp3_complete=True,
        )
        db.update_job_progress(
            conn,
            job_id,
            done_segments=stats["done"],
            failed_segments=0,
            done_chapters=len(context.assembled),
        )
        db.set_job_state(conn, job_id, "done")
        logger.info(
            "job=%s 完成：M4B %s（%s 字节，%s ms），%d 章 MP3 已就绪。",
            job_id,
            filename,
            size,
            duration_ms,
            len(context.assembled),
        )

    def _voice_label(self, context: _JobContext, job) -> str:
        """Chinese display name used in the M4B file name (falls back to the id)."""
        voice_id = str(job["voice"] or "")
        try:
            for voice in self.engine_for(job).voices():
                if voice.id == voice_id:
                    return voice.name or voice_id
        except Exception:  # noqa: BLE001 - a catalogue issue must not block export
            pass
        return voice_id or "未知音色"

    def _fail(self, conn, job_id: int, error: str) -> None:
        logger.error("job=%s 失败：%s", job_id, error)
        db.set_job_state(conn, job_id, "failed", error=error)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m relaxbook.worker",
        description="RelaxBook 转换工作进程：从队列认领任务，合成片段并导出 MP3 / M4B。",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（书库、缓存、任务和日志所在位置；默认使用系统默认目录）",
    )
    args = parser.parse_args(argv)

    settings = load_settings(args.data_dir)
    _setup_logging(settings)
    lock = _acquire_lock(settings.root)
    if lock is None:
        logger.error(
            "已有工作进程在运行（锁文件：%s），本次退出（退出码 2）。",
            Path(settings.root) / LOCK_NAME,
        )
        return 2
    try:
        settings.ensure_dirs()
        db.init_db(settings.db_path)
        conn = db.connect(settings.db_path)
        try:
            recovered = db.reset_running_jobs(conn)
        finally:
            conn.close()
        if recovered:
            logger.info("已把被中断的任务重新排队：job=%s", recovered)
        logger.info("工作进程启动，数据目录：%s", settings.root)
        JobRunner(settings).loop_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，工作进程退出。")
    finally:
        _release_lock(lock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
