"""Chapter / M4B assembly and verification (CONTRACTS section 9).

Every published artifact follows the same pipeline: build under a temporary
name beside the target, validate it, then ``os.replace`` it into place.  A
failure therefore leaves either the previous good file or nothing at all --
never a truncated file that the UI might offer for download.

M4B chapter boundaries are computed from the *actual PCM frame counts* of the
segment WAVs (24 000 frames = 1 000 ms).  Integer millisecond accumulation is
done once at the end, so rounding never accumulates per segment and adjacent
chapters always share the exact same boundary value.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Sequence

from . import audio
from .audio import FRAME_RATE, AudioError

logger = logging.getLogger(__name__)

#: Maximum distance (ms) allowed between the last chapter end and the file's
#: probed duration; covers AAC priming / container padding.
_LAST_CHAPTER_TOLERANCE_MS = 1000

#: Maximum rounding slop when checking that chapters are back-to-back.
_SEAM_TOLERANCE_MS = 2


def _temp_beside(final: Path, suffix: str) -> Path:
    """A hidden temporary path next to *final* (same filesystem for os.replace)."""
    return final.with_name(f".{final.name}.{uuid.uuid4().hex[:8]}{suffix}")


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            logger.debug("临时文件清理失败：%s", path, exc_info=True)


# --------------------------------------------------------------------------- #
# chapter MP3
# --------------------------------------------------------------------------- #
def assemble_chapter(
    segment_wavs: Sequence[Path],
    out_mp3: Path,
    *,
    bitrate: str,
    ffmpeg: str,
    ffprobe: str,
) -> dict:
    """Concat segments -> MP3 -> decode check -> probe -> atomic publish."""
    segments = [Path(item) for item in segment_wavs]
    if not segments:
        raise AudioError("没有可拼接的音频片段，无法生成 MP3。")
    out = Path(out_mp3)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_wav = _temp_beside(out, ".wav")
    temp_mp3 = _temp_beside(out, ".mp3")
    try:
        audio.concat_wavs(segments, temp_wav)
        if audio.wav_frames(temp_wav) <= 0:
            raise AudioError("拼接后的 WAV 不含音频样本，MP3 未生成。")
        audio.encode_mp3(temp_wav, temp_mp3, bitrate=bitrate, ffmpeg=ffmpeg)
        audio.verify_decodable(temp_mp3, ffmpeg=ffmpeg)
        info = audio.probe(temp_mp3, ffprobe=ffprobe)
        duration_ms = int(info.get("duration_ms") or 0)
        bit_rate = int(info.get("bit_rate") or 0)
        if duration_ms <= 0:
            raise AudioError("MP3 校验失败：成品时长为 0。")
        if bit_rate <= 0:
            raise AudioError("MP3 校验失败：成品码率为 0。")
        os.replace(temp_mp3, out)
        return {
            "path": str(out),
            "bytes": out.stat().st_size,
            "duration_ms": duration_ms,
        }
    finally:
        _cleanup(temp_wav, temp_mp3)


# --------------------------------------------------------------------------- #
# M4B
# --------------------------------------------------------------------------- #
def _chapter_ranges(chapters: Sequence[dict]) -> tuple[list[dict], list[Path], int]:
    """Walk chapters in order; return (ranges, flat segment list, total frames).

    ``ranges`` is ``[{"title", "start_ms", "end_ms"}]`` derived from cumulative
    PCM frame counts (``frames * 1000 // 24000``), never from per-segment ms.
    """
    ranges: list[dict] = []
    all_segments: list[Path] = []
    cursor = 0
    for position, chapter in enumerate(chapters):
        label = chapter.get("index", position + 1)
        title = str(chapter.get("title") or "").strip() or f"第{position + 1}章"
        segments = [Path(item) for item in (chapter.get("segment_wavs") or [])]
        if not segments:
            raise AudioError(f"第 {label} 章「{title}」没有可用的音频片段，无法封装 M4B。")
        frames = 0
        for segment in segments:
            frames += audio.wav_frames(segment)
        if frames <= 0:
            raise AudioError(f"第 {label} 章「{title}」的音频样本为 0，无法封装 M4B。")
        ranges.append(
            {
                "title": title,
                "start_ms": cursor * 1000 // FRAME_RATE,
                "end_ms": (cursor + frames) * 1000 // FRAME_RATE,
            }
        )
        all_segments.extend(segments)
        cursor += frames
    return ranges, all_segments, cursor


def _check_chapters(expected: Sequence[dict], actual: Sequence[dict], duration_ms: int) -> None:
    """Raise :class:`AudioError` unless the probed chapter track matches *expected*."""
    if len(actual) != len(expected):
        raise AudioError(
            f"M4B 章节核对失败：成品内嵌 {len(actual)} 章，期望 {len(expected)} 章。"
        )
    if not actual:
        raise AudioError("M4B 章节核对失败：成品没有内嵌章节。")
    if duration_ms <= 0:
        raise AudioError("M4B 校验失败：无法从成品读取总时长。")
    for position, (want, got) in enumerate(zip(expected, actual), start=1):
        if str(got.get("title") or "") != str(want.get("title") or ""):
            raise AudioError(
                "M4B 章节核对失败：第 "
                f"{position} 章标题应为「{want.get('title')}」，成品为「{got.get('title')}」。"
            )
        start_ms = int(got.get("start_ms") or 0)
        end_ms = int(got.get("end_ms") or 0)
        if end_ms <= start_ms:
            raise AudioError(
                f"M4B 章节核对失败：第 {position} 章起止无效（start={start_ms}ms，end={end_ms}ms）。"
            )
        if position > 1:
            previous = actual[position - 2]
            if start_ms <= int(previous.get("start_ms") or 0):
                raise AudioError(
                    f"M4B 章节核对失败：第 {position} 章起点未严格递增"
                    f"（{start_ms}ms ≤ 上一章 {previous.get('start_ms')}ms）。"
                )
            previous_end = int(previous.get("end_ms") or 0)
            if abs(start_ms - previous_end) > _SEAM_TOLERANCE_MS:
                raise AudioError(
                    f"M4B 章节未首尾相接：第 {position} 章起点 {start_ms}ms"
                    f" 与上一章终点 {previous_end}ms 不一致。"
                )
    if int(actual[0].get("start_ms") or 0) > _LAST_CHAPTER_TOLERANCE_MS:
        raise AudioError(f"M4B 第一章起点不是 0ms（实际 {actual[0].get('start_ms')}ms）。")
    last_end = int(actual[-1].get("end_ms") or 0)
    if abs(duration_ms - last_end) > _LAST_CHAPTER_TOLERANCE_MS:
        raise AudioError(
            f"M4B 总时长核对失败：成品总时长 {duration_ms}ms，"
            f"最后一章终点 {last_end}ms，相差超过 {_LAST_CHAPTER_TOLERANCE_MS}ms。"
        )


def assemble_m4b(
    chapters: Sequence[dict],
    out_path: Path,
    *,
    title: str,
    author: str | None,
    cover_path: Path | None,
    bitrate: str,
    ffmpeg: str,
    ffprobe: str,
) -> dict:
    """Assemble one M4B with an embedded chapter track and publish it atomically.

    ``chapters`` is ``[{"index", "title", "segment_wavs"}]`` in reading order.
    Returns the measured values of the published file:
    ``{"path", "bytes", "duration_ms", "chapters": [{"index","title",
    "start_ms","end_ms","duration_ms"}]}``.
    """
    entries = list(chapters)
    if not entries:
        raise AudioError("没有可封装的章节，M4B 未生成。")
    # Fail fast on FFmpeg builds that cannot write the chapter text track: their
    # output keeps chapter timings but loses the chapter names.
    audio.ensure_chapter_capable(ffmpeg)

    ranges, all_segments, total_frames = _chapter_ranges(entries)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_wav = _temp_beside(out, ".wav")
    temp_m4b = _temp_beside(out, ".m4b")
    warnings: list[str] = []
    try:
        audio.concat_wavs(all_segments, temp_wav)
        joined_frames = audio.wav_frames(temp_wav)
        if joined_frames != total_frames:
            raise AudioError(
                "拼接样本数核对失败："
                f"拼接结果 {joined_frames} 帧，各章片段合计 {total_frames} 帧。"
            )
        audio.encode_m4b(
            temp_wav,
            temp_m4b,
            title=title,
            author=author,
            chapters=ranges,
            bitrate=bitrate,
            ffmpeg=ffmpeg,
            cover=cover_path,
            warnings=warnings,
        )
        info = audio.probe(temp_m4b, ffprobe=ffprobe)
        duration_ms = int(info.get("duration_ms") or 0)
        probed = audio.probe_chapters(temp_m4b, ffprobe=ffprobe)
        _check_chapters(ranges, probed, duration_ms)
        os.replace(temp_m4b, out)
        for message in warnings:
            logger.warning("M4B 导出提示：%s", message)
        return {
            "path": str(out),
            "bytes": out.stat().st_size,
            "duration_ms": duration_ms,
            "chapters": [
                {
                    "index": int(entry.get("index", position + 1)),
                    "title": str(got.get("title") or ""),
                    "start_ms": int(got.get("start_ms") or 0),
                    "end_ms": int(got.get("end_ms") or 0),
                    "duration_ms": int(got.get("end_ms") or 0) - int(got.get("start_ms") or 0),
                }
                for position, (entry, got) in enumerate(zip(entries, probed))
            ],
        }
    except AudioError as exc:
        logger.error("M4B 封装校验失败：%s", exc)
        raise
    finally:
        _cleanup(temp_wav, temp_m4b)


def verify_m4b(
    path: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    expected_chapters: int | None = None,
) -> dict:
    """Non-throwing M4B check: ``{"ok", "duration_ms", "chapters", ["error"]}``."""
    path = Path(path)
    result: dict = {"ok": False, "duration_ms": 0, "chapters": []}
    try:
        audio.verify_decodable(path, ffmpeg=ffmpeg)
        info = audio.probe(path, ffprobe=ffprobe)
        duration_ms = int(info.get("duration_ms") or 0)
        probed = audio.probe_chapters(path, ffprobe=ffprobe)
        result["duration_ms"] = duration_ms
        result["chapters"] = [
            {
                "index": position + 1,
                "title": str(chapter.get("title") or ""),
                "start_ms": int(chapter.get("start_ms") or 0),
                "end_ms": int(chapter.get("end_ms") or 0),
                "duration_ms": int(chapter.get("end_ms") or 0)
                - int(chapter.get("start_ms") or 0),
            }
            for position, chapter in enumerate(probed)
        ]
        if duration_ms <= 0:
            result["error"] = "M4B 成品时长为 0。"
            return result
        if expected_chapters is not None and len(probed) != int(expected_chapters):
            result["error"] = (
                f"M4B 内嵌章节数为 {len(probed)}，期望 {int(expected_chapters)}。"
            )
            return result
        if not probed:
            result["error"] = "M4B 没有内嵌章节。"
            return result
        for position, chapter in enumerate(probed):
            start_ms = int(chapter.get("start_ms") or 0)
            end_ms = int(chapter.get("end_ms") or 0)
            if end_ms <= start_ms:
                result["error"] = f"第 {position + 1} 章起止无效。"
                return result
            if position and start_ms <= int(probed[position - 1].get("start_ms") or 0):
                result["error"] = f"第 {position + 1} 章起点未严格递增。"
                return result
            if position and abs(start_ms - int(probed[position - 1].get("end_ms") or 0)) > _SEAM_TOLERANCE_MS:
                result["error"] = f"第 {position + 1} 章与上一章未首尾相接。"
                return result
        last_end = int(probed[-1].get("end_ms") or 0)
        if abs(duration_ms - last_end) > _LAST_CHAPTER_TOLERANCE_MS:
            result["error"] = (
                f"M4B 最后一章终点 {last_end}ms 与总时长 {duration_ms}ms 相差超过 "
                f"{_LAST_CHAPTER_TOLERANCE_MS}ms。"
            )
            return result
        result["ok"] = True
    except AudioError as exc:
        result["error"] = str(exc)
    except OSError as exc:
        result["error"] = f"读取 M4B 成品失败：{exc}"
    return result
