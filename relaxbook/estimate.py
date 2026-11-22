"""按文本统计预测有声书的成品时长、文件大小与生成耗时。

本模块是**纯计算**：不联网、不生成音频、不调用 ffmpeg、不读取任何媒体文件，
只依据正文字符数与 :func:`relaxbook.textnorm.segment_text` 的真实切片做估算。
因此结果只适合用于「预计」与任务排队提示，不代表实测时长。

常量表（全部是**估算校准值**，不是本模块产生的实测结论）
=========================================================

中文朗读速度（去掉空白后的字符/秒）：

==========  =====  ===========================================================
引擎        字/秒  依据
==========  =====  ===========================================================
edge        4.6    项目既有《示例书》成品的字数与时长记录：
                   559 字 → 122.232 秒 ≈ 4.57 字/秒（作为校准值使用）
qwen        5.0    Qwen / CosyVoice 系列中文语速校准值
indextts    4.8    IndexTTS 中文语速校准值
kokoro      4.5    本地 Kokoro 中文语速校准值
其它        4.6    未知引擎按 edge 同档保守估算
==========  =====  ===========================================================

英文朗读速度：``chars_per_second(engine, language="en") = 15.7`` 字符/秒。
校准依据：英文正常朗读约 156 词/分钟，按「词数 × 5.5 个字母 + 空格」折算
约为每词 6.5 字符，即约 16.9 字符/秒，本模块取偏保守的 **15.7**。
**旧口径说明**：早期材料中写作「14.5 词/分钟 ≈ 2.6 字符/秒」，该写法有笔误，
已更正为 156 词/分钟口径，请勿再使用旧的 14.5 词/分钟 / 2.6 字符/秒 数值。

生成速度（``realtime_factor``：生成速度相对实时的倍数，即 1 秒墙钟约能生成
多少秒音频；同样是估算校准值）：

==========  ======  ==============================================
引擎        倍数    说明
==========  ======  ==============================================
edge        6.0    并发 3 时的估算值（单线程约 2.0）
qwen        5.0    估算值
indextts    1.0    估算值（本地模型，约等于实时）
kokoro      0.5    估算值（本地模型，比实时慢）
其它        1.0    未知引擎按实时保守估算
==========  ======  ==============================================

语速 ``rate``：``+20%`` 表示播放速度 ×1.2，音频时长 ÷1.2；``-10%`` 表示 ÷0.9；
无法解析时按 ``+0%`` 处理。

片段数：直接调用 :func:`relaxbook.textnorm.segment_text` 统计真实切片数，与
worker 完全同口径；只给总字数、没有正文时按平均 33 字/段估算。

预计生成耗时：``wall_seconds = 音频秒数 / realtime_factor(engine) × max(1, 3/并发数)``。
并发 3 为基准；低于 3 线性变慢，高于 3 按「不再提速」处理（Edge 高并发容易被
服务端重置），即并发修正系数最低为 1 倍。

文件大小：MP3 ≈ ``音频秒数 × 码率 / 8``；M4B 使用同样码率再乘 1.02 的容器开销；
``mp3_bytes`` 为全部参与朗读章节之和。

字符数口径：去掉所有空白字符后的正文字符数，中英文混排按字符计。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .textnorm import segment_text

__all__ = [
    "AVG_SEGMENT_CHARS",
    "CHARS_PER_SECOND_EN",
    "CHARS_PER_SECOND_ZH",
    "EMPTY_BOOK_NOTE",
    "REALTIME_FACTORS",
    "BookEstimate",
    "ChapterEstimate",
    "chars_per_second",
    "estimate_book",
    "estimate_book_from_text",
    "estimate_chapter",
    "format_estimate",
    "realtime_factor",
]

# ---------------------------------------------------------------------------
# 估算校准常量（详见模块 docstring；不是实测数据）
# ---------------------------------------------------------------------------

#: 中文朗读速度：引擎 -> 去掉空白后的字符/秒。
CHARS_PER_SECOND_ZH: dict[str, float] = {
    "edge": 4.6,
    "qwen": 5.0,
    "indextts": 4.8,
    "kokoro": 4.5,
}

#: 未知引擎的中文语速回退值（与 edge 同档，偏保守）。
DEFAULT_CHARS_PER_SECOND_ZH = 4.6

#: 英文朗读速度（字符/秒）：156 词/分钟 × ≈6.5 字符/词，取偏保守的 15.7。
CHARS_PER_SECOND_EN = 15.7

#: 生成速度相对实时的倍数：引擎 -> 倍数（并发 3 为基准）。
REALTIME_FACTORS: dict[str, float] = {
    "edge": 6.0,
    "qwen": 5.0,
    "indextts": 1.0,
    "kokoro": 0.5,
}

#: 未知引擎的实时倍数回退值（按实时估算，避免高估速度）。
DEFAULT_REALTIME_FACTOR = 1.0

#: 没有正文切片时用于估算片段数的平均每段字符数。
AVG_SEGMENT_CHARS = 33.0

#: M4B 容器相对同码率 MP3 的额外开销。
M4B_CONTAINER_OVERHEAD = 1.02

#: 实时倍数表的基准并发数。
CONCURRENCY_BASELINE = 3.0

#: 空书提示，测试与界面都会检查这句中文。
EMPTY_BOOK_NOTE = "没有可朗读正文，无法估算时长。"

_RATE_RE = re.compile(r"^\s*([+-]?)\s*(\d+(?:\.\d+)?)\s*%\s*$")
_BITRATE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*(?:bps|bit/s|b/s|bits?)?\s*$"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ChapterEstimate:
    """单章估算结果（``mp3_bytes`` 按该章 MP3 码率单独计算）。"""

    index: int
    title: str
    chars: int
    segments: int
    audio_ms: int
    mp3_bytes: int


@dataclass
class BookEstimate:
    """整书估算结果；``chapters`` 为空表示只按总字数估算。"""

    engine: str
    voice: str
    rate: str
    chapters: list[ChapterEstimate]
    chars: int
    segments: int
    audio_ms: int
    wall_seconds: float
    m4b_bytes: int
    mp3_bytes: int
    concurrency: int
    notes: list[str]


# ---------------------------------------------------------------------------
# 常量查询
# ---------------------------------------------------------------------------


def _engine_key(engine: str | None) -> str:
    return str(engine or "").strip().lower()


def chars_per_second(engine: str, *, language: str = "zh") -> float:
    """估算该引擎的朗读速度（去掉空白后的字符/秒）。

    ``language="zh"``（默认）使用 :data:`CHARS_PER_SECOND_ZH`，未知引擎回退到
    edge 同档的 4.6；``language="en"`` 返回 :data:`CHARS_PER_SECOND_EN`（15.7）。
    其它语言值一律按中文处理。
    """
    if str(language or "zh").strip().lower().startswith("en"):
        return float(CHARS_PER_SECOND_EN)
    return float(CHARS_PER_SECOND_ZH.get(_engine_key(engine), DEFAULT_CHARS_PER_SECOND_ZH))


def realtime_factor(engine: str) -> float:
    """估算该引擎的生成速度相对实时的倍数（并发 3 为基准）。

    未知引擎回退到 1.0（约等于实时），避免高估生成速度。
    """
    return float(REALTIME_FACTORS.get(_engine_key(engine), DEFAULT_REALTIME_FACTOR))


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _readable_chars(text: str) -> int:
    """去掉所有空白字符后的字符数（中英文混排按字符计）。"""
    return sum(1 for char in text if not char.isspace())


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # 日文假名
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF  # 韩文
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF  # 全角标点/字母
    )


def _detect_language(text: str) -> str:
    """按正文中中日韩字符与拉丁字母的数量判断主语言（zh / en）。"""
    cjk = 0
    latin = 0
    for char in text:
        if char.isspace():
            continue
        if _is_cjk(char):
            cjk += 1
        elif "a" <= char <= "z" or "A" <= char <= "Z":
            latin += 1
    return "en" if latin > cjk else "zh"


def _rate_speed(rate: str | None) -> tuple[float, bool]:
    """解析 ``+20%`` / ``-10%`` 形式语速，返回 (速度倍数, 是否解析成功)。"""
    match = _RATE_RE.match(str(rate or ""))
    if not match:
        return 1.0, False
    sign, number = match.groups()
    delta = float(number) / 100.0
    if sign == "-":
        delta = -delta
    factor = 1.0 + delta
    if factor <= 0:
        return 1.0, False
    return factor, True


def _bitrate_bps(bitrate: str | int | None) -> int:
    """把 ``64k`` / ``128k`` / ``64000`` / ``1M`` 解析成比特每秒。"""
    if isinstance(bitrate, int) and not isinstance(bitrate, bool):
        value = bitrate
    else:
        match = _BITRATE_RE.match(str(bitrate or ""))
        if not match:
            raise ValueError(
                f"码率格式无效：{bitrate!r}（示例：\"64k\"、\"128k\"、\"64000\"）"
            )
        number, unit = match.groups()
        multiplier = {"": 1, "k": 1000, "m": 1_000_000}[unit.lower()]
        value = float(number) * multiplier
    result = int(round(value))
    if result <= 0:
        raise ValueError(f"码率必须大于 0，收到：{bitrate!r}")
    return result


def _size_bytes(audio_seconds: float, bitrate: str | int | None, *, overhead: float = 1.0) -> int:
    """按码率估算文件字节数：音频秒数 × 比特率 / 8 × 开销。"""
    seconds = max(0.0, float(audio_seconds))
    return int(round(seconds * _bitrate_bps(bitrate) / 8.0 * overhead))


def _normalize_concurrency(concurrency: int) -> int:
    try:
        workers = int(concurrency)
    except (TypeError, ValueError):
        workers = 3
    return workers if workers >= 1 else 1


def _concurrency_factor(workers: int) -> float:
    """并发修正系数：并发 3 为 1 倍，低于 3 线性变慢，高于 3 不再提速。"""
    return max(1.0, CONCURRENCY_BASELINE / float(max(1, workers)))


def _clean_title(title: object) -> str:
    text = str(title or "").strip()
    return text or "未命名章节"


def _field(item: object, name: str, default: object = None) -> object:
    """从 dict / sqlite3.Row / dataclass 中取字段，便于直接喂数据库行。"""
    if isinstance(item, Mapping):
        return item.get(name, default)
    try:
        return item[name]  # type: ignore[index]
    except (TypeError, KeyError, IndexError, ValueError):
        pass
    return getattr(item, name, default)


def _assumption_notes(
    *,
    engine: str,
    rate: str,
    concurrency: int,
    mp3_bitrate: str,
    m4b_bitrate: str,
    segments_note: str,
) -> list[str]:
    """生成统一的中文估算假设说明。"""
    speed, parsed = _rate_speed(rate)
    rtf = realtime_factor(engine)
    shown_rate = str(rate or "+0%")
    notes = [
        "字符数按去掉空白后的正文字符统计，中英文混排按字符计。",
        (
            f"语速基准：中文按 {chars_per_second(engine):g} 字/秒、"
            f"英文按 {CHARS_PER_SECOND_EN:g} 字符/秒估算（估算校准值，非实测）。"
        ),
    ]
    if parsed:
        notes.append(f"语速参数 {shown_rate}：播放速度 ×{speed:g}，音频时长 ÷{speed:g}。")
    else:
        notes.append(f"语速参数「{shown_rate}」无法解析，按 +0% 处理。")
    notes.append(segments_note)
    notes.append(
        f"生成耗时按实时倍数 x{rtf:g} 与并发修正 max(1, 3/{concurrency}) 估算"
        f"（当前并发 {concurrency}）。"
    )
    notes.append(
        f"文件大小按音频时长 × 码率估算：MP3 {mp3_bitrate}、M4B {m4b_bitrate}，"
        f"M4B 另计 {int(round((M4B_CONTAINER_OVERHEAD - 1.0) * 100))}% 容器开销。"
    )
    if _engine_key(engine) not in CHARS_PER_SECOND_ZH:
        notes.append(
            f"引擎「{engine}」不在校准表内，按 edge 同档中文语速与 x1.0 实时倍数保守估算。"
        )
    notes.append("本估算仅按文本统计，未实测；实际时长与耗时会因音色、断句、网络和设备而变化。")
    return notes


def _empty_book(
    *,
    engine: str,
    voice: str,
    rate: str,
    concurrency: int,
    notes: list[str] | None = None,
) -> BookEstimate:
    return BookEstimate(
        engine=str(engine or ""),
        voice=str(voice or ""),
        rate=str(rate or "+0%"),
        chapters=[],
        chars=0,
        segments=0,
        audio_ms=0,
        wall_seconds=0.0,
        m4b_bytes=0,
        mp3_bytes=0,
        concurrency=_normalize_concurrency(concurrency),
        notes=list(notes) if notes else [EMPTY_BOOK_NOTE],
    )


# ---------------------------------------------------------------------------
# 对外估算接口
# ---------------------------------------------------------------------------


def estimate_chapter(
    text: str,
    *,
    index: int,
    title: str,
    engine: str,
    voice: str,
    rate: str = "+0%",
    mp3_bitrate: str = "64k",
) -> ChapterEstimate:
    """估算单章：字符数、片段数、音频毫秒与 MP3 字节数。

    片段数直接复用 :func:`relaxbook.textnorm.segment_text` 的真实切片，保证与
    转换进程同口径；``voice`` 只用于调用方记录（同一引擎不同音色的语速差异忽略）。
    """
    raw = "" if text is None else str(text)
    chars = _readable_chars(raw)
    segments = len(segment_text(raw)) if chars else 0
    language = _detect_language(raw)
    speed, _parsed = _rate_speed(rate)
    cps = chars_per_second(engine, language=language)
    if chars and cps > 0 and speed > 0:
        audio_seconds = chars / cps / speed
    else:
        audio_seconds = 0.0
    audio_ms = int(round(audio_seconds * 1000.0))
    return ChapterEstimate(
        index=int(index),
        title=_clean_title(title),
        chars=chars,
        segments=segments,
        audio_ms=audio_ms,
        mp3_bytes=_size_bytes(audio_ms / 1000.0, mp3_bitrate),
    )


def estimate_book(
    chapters: Sequence[dict],
    *,
    engine: str,
    voice: str,
    rate: str = "+0%",
    concurrency: int = 3,
    mp3_bitrate: str = "64k",
    m4b_bitrate: str = "64k",
) -> BookEstimate:
    """按章节列表估算整书。

    ``chapters`` 支持 dict / sqlite3.Row / ``ChapterDraft`` 风格的字段
    ``title``、``text``、``read``（缺省视为参与朗读）。``read`` 为假或正文为空白的
    章节不会计入估算，并在 ``notes`` 中说明。参与朗读的章节按顺序获得 1 起的
    ``index``（与 worker 的 MP3 编号顺序一致）。

    没有任何可朗读正文时返回全 0 的 :class:`BookEstimate`，``notes`` 含
    「没有可朗读正文」。
    """
    workers = _normalize_concurrency(concurrency)
    selected: list[tuple[str, str]] = []
    skipped_read = 0
    skipped_empty = 0
    for item in list(chapters or []):
        read = _field(item, "read", True)
        if read is not None and not bool(read):
            skipped_read += 1
            continue
        raw_text = _field(item, "text", "")
        text = "" if raw_text is None else str(raw_text)
        if not text.strip():
            skipped_empty += 1
            continue
        selected.append((_clean_title(_field(item, "title", "")), text))

    if not selected:
        notes = [EMPTY_BOOK_NOTE]
        if skipped_read:
            notes.append(f"{skipped_read} 章标记为跳过朗读，未计入估算。")
        if skipped_empty:
            notes.append(f"{skipped_empty} 章正文为空，未计入估算。")
        if len(notes) == 1:
            notes.append("请先导入书籍并确认至少一章有可朗读正文。")
        return _empty_book(
            engine=engine,
            voice=voice,
            rate=rate,
            concurrency=workers,
            notes=notes,
        )

    chapter_estimates: list[ChapterEstimate] = []
    languages: set[str] = set()
    for index, (title, text) in enumerate(selected, start=1):
        chapter_estimates.append(
            estimate_chapter(
                text,
                index=index,
                title=title,
                engine=engine,
                voice=voice,
                rate=rate,
                mp3_bitrate=mp3_bitrate,
            )
        )
        languages.add(_detect_language(text))

    chars = sum(chapter.chars for chapter in chapter_estimates)
    segments = sum(chapter.segments for chapter in chapter_estimates)
    audio_ms = sum(chapter.audio_ms for chapter in chapter_estimates)
    audio_seconds = audio_ms / 1000.0
    wall_seconds = (
        audio_seconds / realtime_factor(engine) * _concurrency_factor(workers)
    )
    mp3_bytes = sum(chapter.mp3_bytes for chapter in chapter_estimates)
    m4b_bytes = _size_bytes(
        audio_seconds, m4b_bitrate, overhead=M4B_CONTAINER_OVERHEAD
    )

    notes = _assumption_notes(
        engine=engine,
        rate=rate,
        concurrency=workers,
        mp3_bitrate=mp3_bitrate,
        m4b_bitrate=m4b_bitrate,
        segments_note=(
            "片段数按 textnorm.segment_text 的真实切片统计，与转换进程完全同口径。"
        ),
    )
    if skipped_read:
        notes.append(f"{skipped_read} 章标记为跳过朗读，未计入估算。")
    if skipped_empty:
        notes.append(f"{skipped_empty} 章正文为空，未计入估算。")
    if "en" in languages:
        notes.append("部分章节按英文语速估算；中英混排章节按该章占多数的语言统一计时。")

    return BookEstimate(
        engine=str(engine or ""),
        voice=str(voice or ""),
        rate=str(rate or "+0%"),
        chapters=chapter_estimates,
        chars=chars,
        segments=segments,
        audio_ms=audio_ms,
        wall_seconds=wall_seconds,
        m4b_bytes=m4b_bytes,
        mp3_bytes=mp3_bytes,
        concurrency=workers,
        notes=notes,
    )


def estimate_book_from_text(
    text_total_chars: int,
    *,
    engine: str,
    voice: str,
    rate: str = "+0%",
    concurrency: int = 3,
    mp3_bitrate: str = "64k",
    m4b_bitrate: str = "64k",
    language: str = "zh",
) -> BookEstimate:
    """只给全书总字数时估算整书（没有章节明细）。

    供网页在导入前按字数快速预估。``chapters`` 返回空列表，仅含整书汇总；
    片段数按平均 :data:`AVG_SEGMENT_CHARS` 字/段估算。总字数为 0 时返回全 0
    结果，``notes`` 含「没有可朗读正文」。
    """
    workers = _normalize_concurrency(concurrency)
    try:
        chars = max(0, int(text_total_chars or 0))
    except (TypeError, ValueError):
        chars = 0
    if chars == 0:
        return _empty_book(
            engine=engine,
            voice=voice,
            rate=rate,
            concurrency=workers,
        )

    speed, _parsed = _rate_speed(rate)
    cps = chars_per_second(engine, language=language)
    segments = max(1, int(math.ceil(chars / AVG_SEGMENT_CHARS)))
    audio_seconds = chars / cps / speed if cps > 0 and speed > 0 else 0.0
    audio_ms = int(round(audio_seconds * 1000.0))
    wall_seconds = (
        audio_seconds / realtime_factor(engine) * _concurrency_factor(workers)
    )
    notes = _assumption_notes(
        engine=engine,
        rate=rate,
        concurrency=workers,
        mp3_bitrate=mp3_bitrate,
        m4b_bitrate=m4b_bitrate,
        segments_note=(
            f"未提供正文切片，片段数按平均 {AVG_SEGMENT_CHARS:g} 字/段估算"
            "（有正文时按 textnorm.segment_text 真实切片统计）。"
        ),
    )
    notes.append("未提供章节明细，整书时长与大小仅按总字数估算。")
    if str(language or "zh").strip().lower().startswith("en"):
        notes.append("整书按英文语速估算。")

    return BookEstimate(
        engine=str(engine or ""),
        voice=str(voice or ""),
        rate=str(rate or "+0%"),
        chapters=[],
        chars=chars,
        segments=segments,
        audio_ms=audio_ms,
        wall_seconds=wall_seconds,
        m4b_bytes=_size_bytes(
            audio_seconds, m4b_bitrate, overhead=M4B_CONTAINER_OVERHEAD
        ),
        mp3_bytes=_size_bytes(audio_seconds, mp3_bitrate),
        concurrency=workers,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 中文摘要
# ---------------------------------------------------------------------------


def _format_bytes(size: int) -> str:
    value = float(max(0, int(size)))
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} TB"


def format_estimate(est: BookEstimate) -> str:
    """把 :class:`BookEstimate` 转成中文多行摘要（含「预计生成」与估算假设）。"""
    total_seconds = max(0, int(round(est.audio_ms / 1000.0)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    hours_value = total_seconds / 3600.0
    hours_text = f"{hours_value:.1f}" if total_seconds >= 3600 else f"{hours_value:.2f}"
    wall_seconds = max(0.0, float(est.wall_seconds))
    wall_minutes = wall_seconds / 60.0

    if est.chapters:
        chapter_text = f"{len(est.chapters)} 章参与朗读"
    elif est.chars:
        chapter_text = "未提供章节明细（按全书总字数估算）"
    else:
        chapter_text = "0 章"

    lines = [
        "全书估算（仅按文本统计，未实测）",
        f"引擎：{est.engine}｜音色：{est.voice}｜语速：{est.rate}",
        f"章节：{chapter_text}｜正文 {est.chars:,} 字｜片段 {est.segments:,} 个",
        (
            f"成品时长：约 {hours} 小时 {minutes} 分钟"
            f"（{total_seconds:,} 秒，约 {hours_text} 小时）"
        ),
        (
            f"预计生成：约 {wall_minutes:.1f} 分钟"
            f"（约 {wall_seconds:,.0f} 秒；按当前引擎 {est.engine}、并发 {est.concurrency} 估算）"
        ),
        (
            f"成品大小：M4B 约 {_format_bytes(est.m4b_bytes)}（{est.m4b_bytes:,} 字节）"
            f"｜MP3 合计约 {_format_bytes(est.mp3_bytes)}（{est.mp3_bytes:,} 字节）"
        ),
        "估算假设：",
    ]
    if est.notes:
        lines.extend(f"  {position}. {note}" for position, note in enumerate(est.notes, start=1))
    else:
        lines.append("  （无）")
    lines.append(
        "说明：本估算仅按文本统计，未实测，实际时长与耗时会因音色、断句和设备而不同。"
    )
    return "\n".join(lines)
