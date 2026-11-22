"""Thin FFmpeg / ffprobe wrappers (CONTRACTS section 8).

Every helper takes the absolute tool paths explicitly; nothing here depends on
the process ``PATH``.  Failures raise :class:`AudioError` carrying the tail of
the tool's stderr so the UI can show a Chinese explanation plus the real cause.

Sample format is always 24 000 Hz, mono, signed 16-bit PCM.  WAVs are joined
frame by frame with the standard library ``wave`` module instead of
``ffmpeg -f concat``: no resampling, no encoder delay and therefore a sample
count that is exactly the sum of the parts (chapter time stamps depend on it).
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import uuid
import wave
from pathlib import Path
from typing import Any, Sequence

FRAME_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample -> signed 16-bit PCM
_READ_CHUNK = 65536  # frames per read when copying standard WAVs

logger = logging.getLogger(__name__)


class AudioError(Exception):
    """User-visible audio failure (Chinese message, ffmpeg stderr tail included)."""


def ensure_chapter_capable(ffmpeg: str) -> None:
    """Refuse to write an M4B chapter track with an FFmpeg that cannot tag it.

    FFmpeg < 7 writes the chapter timings but silently drops the chapter title
    text track, which leaves a file whose chapters show up as untitled (and
    triggers "Referenced QT chapter track not found" in ffprobe).  Failing loud
    here is much better than publishing an audiobook without a usable TOC.
    """
    from . import config as _config

    exe = str(ffmpeg or "")
    if exe:
        try:
            major = _config.ffmpeg_major(exe)
        except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
            major = 0
        if major >= _config.MIN_FFMPEG_MAJOR:
            return
        version = _config.tool_version_line(exe) or exe
    else:
        version = "（未配置）"
    raise AudioError(
        f"FFmpeg 版本过低，无法写入带标题的 M4B 章节轨（当前：{version}）。"
        f"请升级到 FFmpeg {_config.MIN_FFMPEG_MAJOR} 及以上，"
        "或安装 imageio-ffmpeg（项目会自动使用其内置的新版 FFmpeg）。"
    )


# --------------------------------------------------------------------------- #
# process helpers
# --------------------------------------------------------------------------- #
def _tool(path: str | None, name: str) -> str:
    if not path:
        raise AudioError(f"未配置 {name} 可执行文件，请在 config.json 中设置路径。")
    return str(path)


def _tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        return "…" + text[-limit:]
    return text


def _run(cmd: Sequence[str], *, what: str, timeout: float | None = 600) -> subprocess.CompletedProcess:
    """Run *cmd*; raise :class:`AudioError` on launch failure or non-zero exit."""
    try:
        proc = subprocess.run(
            [str(part) for part in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise AudioError(f"找不到可执行文件：{cmd[0]}") from None
    except subprocess.TimeoutExpired:
        raise AudioError(f"{what} 超时（{timeout} 秒）。") from None
    except OSError as exc:
        raise AudioError(f"{what} 无法执行：{exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        code = proc.returncode
        if code >= 2**31:  # Windows reports negative exit codes as unsigned
            code -= 2**32
        raise AudioError(f"{what} 失败（退出码 {code}）：{_tail(stderr)}")
    return proc


def _seconds_to_ms(value: Any) -> int:
    """Convert an ffprobe seconds field to a non-negative integer millisecond."""
    if value is None or value == "":
        return 0
    try:
        return max(0, int(round(float(value) * 1000)))
    except (TypeError, ValueError):
        return 0


def _int_or_zero(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #
def probe(path: Path, *, ffprobe: str) -> dict:
    """``ffprobe -show_format -show_streams`` as a flat dict.

    Returns ``{"duration_ms", "sample_rate", "channels", "bit_rate", "format_name"}``.
    """
    path = Path(path)
    proc = _run(
        [
            _tool(ffprobe, "ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            "-i",
            str(path),
        ],
        what=f"探测音频 {path.name}",
    )
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise AudioError(f"无法解析 ffprobe 输出（{path.name}）：{exc}") from exc

    fmt: dict = data.get("format") or {}
    audio_streams = [s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"]
    stream: dict = audio_streams[0] if audio_streams else {}
    duration = fmt.get("duration", stream.get("duration"))
    bit_rate = _int_or_zero(fmt.get("bit_rate") or stream.get("bit_rate"))
    return {
        "duration_ms": _seconds_to_ms(duration),
        "sample_rate": _int_or_zero(stream.get("sample_rate")),
        "channels": _int_or_zero(stream.get("channels")),
        "bit_rate": bit_rate,
        "format_name": str(fmt.get("format_name") or ""),
    }


def probe_chapters(path: Path, *, ffprobe: str) -> list[dict]:
    """``ffprobe -show_chapters``; ``[]`` when the file has no chapters."""
    path = Path(path)
    proc = _run(
        [
            _tool(ffprobe, "ffprobe"),
            "-v",
            "error",
            "-show_chapters",
            "-of",
            "json",
            "-i",
            str(path),
        ],
        what=f"探测章节 {path.name}",
    )
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise AudioError(f"无法解析 ffprobe 章节输出（{path.name}）：{exc}") from exc

    chapters: list[dict] = []
    for position, raw in enumerate(data.get("chapters") or []):
        tags = raw.get("tags") or {}
        try:
            chapter_id = int(raw.get("id"))
        except (TypeError, ValueError):
            chapter_id = position + 1
        chapters.append(
            {
                "id": chapter_id,
                "title": str(tags.get("title") or ""),
                "start_ms": _seconds_to_ms(raw.get("start_time")),
                "end_ms": _seconds_to_ms(raw.get("end_time")),
            }
        )
    return chapters


def verify_decodable(path: Path, *, ffmpeg: str) -> None:
    """Fully decode the first audio stream; raise :class:`AudioError` if broken."""
    path = Path(path)
    _run(
        [
            _tool(ffmpeg, "ffmpeg"),
            "-nostdin",
            "-hide_banner",
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
        ],
        what=f"解码校验 {path.name}",
    )


# --------------------------------------------------------------------------- #
# WAV helpers
# --------------------------------------------------------------------------- #
def decode_to_wav(src: Path, dst: Path, *, ffmpeg: str) -> None:
    """Decode any audio file to 24 000 Hz mono signed 16-bit PCM WAV."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _tool(ffmpeg, "ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            str(CHANNELS),
            "-ar",
            str(FRAME_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(dst),
        ],
        what=f"解码为 WAV {src.name}",
    )


def wav_frames(path: Path) -> int:
    """Number of PCM frames in *path* (read from the WAV header)."""
    try:
        with wave.open(str(Path(path)), "rb") as wav:
            return int(wav.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"无法读取 WAV 头信息 {Path(path).name}：{exc}") from exc


def wav_duration_ms(path: Path) -> int:
    """Duration in whole milliseconds derived from the WAV frame count."""
    try:
        with wave.open(str(Path(path)), "rb") as wav:
            frames = int(wav.getnframes())
            rate = int(wav.getframerate())
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"无法读取 WAV 头信息 {Path(path).name}：{exc}") from exc
    if rate <= 0:
        raise AudioError(f"WAV 采样率无效：{Path(path).name}")
    return frames * 1000 // rate


def _decode_to_mono_int16(raw: bytes, width: int, channels: int) -> list[int]:
    """Decode raw PCM frames of arbitrary width/channel count into int16 mono."""
    if width == SAMPLE_WIDTH:
        count = len(raw) // 2
        samples = list(struct.unpack(f"<{count}h", raw[: count * 2]))
    elif width == 1:  # unsigned 8-bit -> signed 16-bit
        samples = [(value - 128) * 256 for value in raw]
    elif width == 3:  # packed signed 24-bit
        count = len(raw) // 3
        samples = []
        for pos in range(count):
            low, mid, high = raw[pos * 3 : pos * 3 + 3]
            value = low | (mid << 8) | (high << 16)
            if value & 0x800000:
                value -= 0x1000000
            samples.append(value >> 8)
    elif width == 4:  # signed 32-bit
        count = len(raw) // 4
        samples = [value >> 16 for value in struct.unpack(f"<{count}i", raw[: count * 4])]
    else:
        raise AudioError(f"不支持的 WAV 采样位宽：{width * 8} bit")

    if channels <= 1:
        return samples
    frames = len(samples) // channels
    mixed: list[int] = []
    for pos in range(frames):
        window = samples[pos * channels : (pos + 1) * channels]
        mixed.append(int(round(sum(window) / channels)))
    return mixed


def _resample_linear(samples: list[int], src_rate: int, dst_rate: int) -> list[int]:
    """Linear-interpolation resampling; only used for non-standard inputs."""
    if not samples or src_rate == dst_rate:
        return list(samples)
    if src_rate <= 0 or dst_rate <= 0:
        raise AudioError("WAV 采样率无效，无法重采样。")
    out_count = int(round(len(samples) * dst_rate / src_rate))
    if out_count <= 0:
        return []
    step = src_rate / dst_rate
    last = len(samples) - 1
    result: list[int] = []
    for pos in range(out_count):
        src_pos = pos * step
        base = int(src_pos)
        if base >= last:
            result.append(samples[last])
            continue
        fraction = src_pos - base
        value = samples[base] + (samples[base + 1] - samples[base]) * fraction
        result.append(int(round(value)))
    return result


def _copy_wav_into(source: Path, destination: wave.Wave_write) -> int:
    """Append one WAV's samples to *destination*; returns the frame count written."""
    try:
        with wave.open(str(source), "rb") as wav:
            channels = int(wav.getnchannels())
            width = int(wav.getsampwidth())
            rate = int(wav.getframerate())
            frames = int(wav.getnframes())
            standard = channels == CHANNELS and width == SAMPLE_WIDTH and rate == FRAME_RATE
            written = 0
            if standard:
                while True:
                    data = wav.readframes(_READ_CHUNK)
                    if not data:
                        break
                    destination.writeframes(data)
                    written += len(data) // (SAMPLE_WIDTH * CHANNELS)
                return written
            raw = wav.readframes(frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioError(f"无法读取 WAV 片段 {Path(source).name}：{exc}") from exc

    samples = _decode_to_mono_int16(raw, width, channels)
    samples = _resample_linear(samples, rate, FRAME_RATE)
    if samples:
        destination.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return len(samples)


def concat_wavs(wavs: Sequence[Path], out: Path) -> None:
    """Join WAVs sample by sample into one 24 000 Hz / mono / 16-bit WAV.

    The output is written to a sibling temporary file and ``os.replace``\\ d into
    place, so a crash never leaves a half-written WAV under a valid name.
    """
    sources = [Path(item) for item in wavs]
    if not sources:
        raise AudioError("没有可拼接的 WAV 文件。")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part.wav")
    try:
        total = 0
        with wave.open(str(temp), "wb") as destination:
            destination.setnchannels(CHANNELS)
            destination.setsampwidth(SAMPLE_WIDTH)
            destination.setframerate(FRAME_RATE)
            for source in sources:
                total += _copy_wav_into(source, destination)
        if total <= 0:
            raise AudioError("拼接后的 WAV 不含任何音频样本。")
        os.replace(temp, out)
    finally:
        temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #
def encode_mp3(wav: Path, out: Path, *, bitrate: str, ffmpeg: str) -> None:
    """Encode a 24 kHz mono WAV to MP3 with libmp3lame (writes *out* directly)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _tool(ffmpeg, "ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-i",
            str(Path(wav)),
            "-vn",
            "-ar",
            str(FRAME_RATE),
            "-ac",
            str(CHANNELS),
            "-c:a",
            "libmp3lame",
            "-b:a",
            str(bitrate),
            "-f",
            "mp3",
            str(out),
        ],
        what=f"编码 MP3 {out.name}",
    )


def _ffmetadata_value(value: Any) -> str:
    """Flatten a metadata value: FFMETADATA1 has no line continuation."""
    return str(value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def write_ffmetadata(
    path: Path,
    *,
    title: str,
    author: str | None,
    chapters: Sequence[dict],
) -> None:
    """Write an ``;FFMETADATA1`` sidecar with global tags and chapter ranges."""
    lines: list[str] = [";FFMETADATA1"]
    book_title = _ffmetadata_value(title)
    if book_title:
        lines.append(f"title={book_title}")
        lines.append(f"album={book_title}")
    author_name = _ffmetadata_value(author)
    if author_name:
        lines.append(f"artist={author_name}")
    for chapter in chapters:
        lines.append("")
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={max(0, int(chapter.get('start_ms') or 0))}")
        lines.append(f"END={max(0, int(chapter.get('end_ms') or 0))}")
        chapter_title = _ffmetadata_value(chapter.get("title"))
        if chapter_title:
            lines.append(f"title={chapter_title}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def encode_m4b(
    wav: Path,
    out: Path,
    *,
    title: str,
    author: str | None,
    chapters: Sequence[dict],
    bitrate: str,
    ffmpeg: str,
    cover: Path | str | None = None,
    warnings: list[str] | None = None,
) -> None:
    """Encode to M4B with an embedded chapter track and UTF-8 tags.

    ``chapters`` is ``[{"title", "start_ms", "end_ms"}]``.  When a *cover* is
    given it is attached as a ``attached_pic`` stream; if that fails the audio
    is re-encoded without the cover (a warning is logged, never a hard error).
    """
    wav, out = Path(wav), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ensure_chapter_capable(ffmpeg)
    meta_path = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.ffmetadata.txt")
    write_ffmetadata(meta_path, title=title, author=author, chapters=chapters)
    try:
        tool = _tool(ffmpeg, "ffmpeg")
        cover_value = str(cover) if cover is not None else ""
        use_cover = bool(cover_value) and Path(cover_value).is_file()

        prefix = [tool, "-nostdin", "-hide_banner", "-v", "error", "-y"]
        # Inputs (and their input options) come first: ffmpeg treats options
        # before an ``-i`` as applying to that input file.
        audio_and_meta_inputs = ["-i", str(wav), "-i", str(meta_path)]
        common_options = [
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-metadata",
            f"title={_ffmetadata_value(title)}",
        ]
        if _ffmetadata_value(author):
            common_options += ["-metadata", f"artist={_ffmetadata_value(author)}"]
        common_options += [
            "-map",
            "0:a",
            "-c:a",
            "aac",
            "-b:a",
            str(bitrate),
        ]
        plain_command = prefix + audio_and_meta_inputs + common_options + ["-f", "ipod", str(out)]

        if use_cover:
            with_cover = (
                prefix
                + audio_and_meta_inputs
                + ["-i", cover_value]
                + common_options
                + [
                    "-map",
                    "2:v:0",
                    "-c:v",
                    "copy",
                    "-disposition:v:0",
                    "attached_pic",
                    "-f",
                    "ipod",
                    str(out),
                ]
            )
            try:
                _run(with_cover, what=f"编码 M4B {out.name}（含封面）")
                return
            except AudioError as exc:
                message = f"封面写入失败，已改为无封面导出：{exc}"
                logger.warning("%s", message)
                if warnings is not None:
                    warnings.append(message)
                out.unlink(missing_ok=True)
        elif cover_value:
            message = f"封面文件不存在，已略过：{cover_value}"
            logger.warning("%s", message)
            if warnings is not None:
                warnings.append(message)

        _run(plain_command, what=f"编码 M4B {out.name}")
    finally:
        meta_path.unlink(missing_ok=True)
