"""Tests for :mod:`relaxbook.estimate`（A7：按文本预测成品时长与生成耗时）。

全部是纯计算：不联网、不生成音频、不调用 ffmpeg。测试只依赖样书正文、
``textnorm.segment_text`` 的真实切片和本模块的估算校准常量。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from relaxbook import estimate
from relaxbook.config import Settings
from relaxbook.ingest import parse_book
from relaxbook.textnorm import segment_text

ROOT = Path(__file__).resolve().parent.parent
ESTIMATE_SOURCE = Path(estimate.__file__)

ENGINE = "edge"
VOICE = "zh-CN-XiaoxiaoNeural"


def _sample_chapters(settings: Settings, epub_path: Path) -> list[dict]:
    """用 ingest 读《示例书》，得到与 worker 相同形态的章节 dict。"""
    book = parse_book(epub_path, format="epub", settings=settings)
    return [
        {"title": chapter.title, "text": chapter.text, "read": True}
        for chapter in book.chapters
    ]


@pytest.fixture()
def sample_estimate(
    settings: Settings, sample_epub: Path
) -> estimate.BookEstimate:
    return estimate.estimate_book(
        _sample_chapters(settings, sample_epub), engine=ENGINE, voice=VOICE
    )


# ---------------------------------------------------------------------------
# 1. 《示例书》：已知校准数据 559 字 → 122.232 秒，允许 ±5 秒
# ---------------------------------------------------------------------------


def test_sample_book_audio_matches_known_calibration(
    sample_estimate: estimate.BookEstimate,
) -> None:
    est = sample_estimate
    # 三章正文（含 parse_book 放在正文首行的章名）在 550 字上下。
    assert 500 <= est.chars <= 600
    # 559 字 / 4.6 字/秒 ≈ 121.5 秒；允差 ±5 秒。
    assert 118_000 <= est.audio_ms <= 126_000
    assert est.engine == ENGINE
    assert est.voice == VOICE
    assert est.rate == "+0%"
    assert [chapter.index for chapter in est.chapters] == [1, 2, 3]
    assert [chapter.title for chapter in est.chapters][0] == "序言"
    assert est.audio_ms == sum(chapter.audio_ms for chapter in est.chapters)
    assert est.segments == sum(chapter.segments for chapter in est.chapters)
    assert est.mp3_bytes == sum(chapter.mp3_bytes for chapter in est.chapters)
    assert est.m4b_bytes > 0 and est.wall_seconds > 0


def test_sample_book_segments_match_textnorm_exactly(
    settings: Settings, sample_epub: Path, sample_estimate: estimate.BookEstimate
) -> None:
    chapters = _sample_chapters(settings, sample_epub)
    assert len(chapters) == len(sample_estimate.chapters)
    for chapter_estimate, chapter in zip(sample_estimate.chapters, chapters):
        assert chapter_estimate.segments == len(segment_text(chapter["text"]))
        assert chapter_estimate.chars == len(
            "".join(chapter["text"].split())
        )


# ---------------------------------------------------------------------------
# 2. 10 万字：约 6.0 小时音频、M4B 约 174MB、生成耗时约 1 小时（x6）
# ---------------------------------------------------------------------------


def test_100k_chars_matches_expected_magnitude() -> None:
    est = estimate.estimate_book_from_text(100_000, engine=ENGINE, voice=VOICE)
    assert est.chars == 100_000
    assert est.chapters == []
    # 100_000 / 4.6 ≈ 21_739 秒 ≈ 6.04 小时。
    assert est.audio_ms / 3_600_000 == pytest.approx(6.0, rel=0.05)
    # 64k 码率、6 小时音频 → 约 174MB（M4B 另加 2% 容器开销），±10%。
    assert est.m4b_bytes == pytest.approx(174_000_000, rel=0.10)
    # 实时倍数 x6 → 约 1 小时墙钟。
    assert est.wall_seconds == pytest.approx(3600.0, rel=0.10)
    # M4B 与同码率 MP3 只差 1.02 倍的容器开销。
    assert est.m4b_bytes == pytest.approx(est.mp3_bytes * 1.02, rel=0.01)


def test_repeated_text_matches_total_chars_estimate() -> None:
    unit = "这是一段用于估算的中文测试正文，包含逗号和句号。"
    text = (unit * 5000)[:100_000]
    assert len("".join(text.split())) == 100_000
    from_text = estimate.estimate_book_from_text(100_000, engine=ENGINE, voice=VOICE)
    from_chapters = estimate.estimate_book(
        [{"title": "长书", "text": text}], engine=ENGINE, voice=VOICE
    )
    assert from_chapters.chars == 100_000
    assert from_chapters.audio_ms == pytest.approx(from_text.audio_ms, rel=0.001)
    assert from_chapters.wall_seconds == pytest.approx(
        from_text.wall_seconds, rel=0.001
    )
    assert from_chapters.m4b_bytes == pytest.approx(from_text.m4b_bytes, rel=0.01)
    assert from_chapters.mp3_bytes == pytest.approx(from_text.mp3_bytes, rel=0.01)


# ---------------------------------------------------------------------------
# 3. rate 参数：+20% → 时长 ÷1.2；-20% → ÷0.8；单调关系正确
# ---------------------------------------------------------------------------


def _chapter_with_rate(text: str, rate: str) -> estimate.ChapterEstimate:
    return estimate.estimate_chapter(
        text, index=1, title="语速", engine=ENGINE, voice=VOICE, rate=rate
    )


def test_rate_scaling_is_inverse_and_monotonic() -> None:
    text = "这是一段用于语速测试的中文正文，包含逗号和句号。" * 100
    base = _chapter_with_rate(text, "+0%")
    faster = _chapter_with_rate(text, "+20%")
    slower = _chapter_with_rate(text, "-20%")
    slower_10 = _chapter_with_rate(text, "-10%")

    assert faster.audio_ms == pytest.approx(base.audio_ms / 1.2, rel=1e-3)
    assert slower.audio_ms == pytest.approx(base.audio_ms / 0.8, rel=1e-3)
    assert slower_10.audio_ms == pytest.approx(base.audio_ms / 0.9, rel=1e-3)
    assert faster.audio_ms < base.audio_ms < slower.audio_ms
    # 语速只影响时长，不改变切片数。
    assert faster.segments == base.segments == slower.segments


def test_unparseable_rate_falls_back_to_zero_percent() -> None:
    text = "无法解析的语速参数应回退到 +0%。" * 50
    base = _chapter_with_rate(text, "+0%")
    unknown = _chapter_with_rate(text, "很快")
    assert unknown.audio_ms == base.audio_ms
    assert any("无法解析" in note for note in _book_with_rate("快速").notes)


def _book_with_rate(rate: str) -> estimate.BookEstimate:
    return estimate.estimate_book(
        [{"title": "测速", "text": "语速测试正文。" * 20}],
        engine=ENGINE,
        voice=VOICE,
        rate=rate,
    )


# ---------------------------------------------------------------------------
# 4. 空章节列表：全 0 + 「没有可朗读正文」
# ---------------------------------------------------------------------------


def test_empty_chapters_return_zeros_and_explanation() -> None:
    est = estimate.estimate_book([], engine=ENGINE, voice=VOICE)
    assert est.chapters == []
    assert est.chars == 0
    assert est.segments == 0
    assert est.audio_ms == 0
    assert est.mp3_bytes == 0
    assert est.m4b_bytes == 0
    assert est.wall_seconds == 0.0
    assert est.concurrency == 3
    assert any("没有可朗读正文" in note for note in est.notes)
    # 格式化空书不能抛异常。
    assert "预计生成" in estimate.format_estimate(est)


def test_all_skipped_or_blank_chapters_are_treated_as_empty() -> None:
    cases = [
        [{"title": "跳过", "text": "这一章不朗读。", "read": False}],
        [{"title": "空白", "text": " \n\t\u3000 ", "read": True}],
    ]
    for chapters in cases:
        est = estimate.estimate_book(chapters, engine=ENGINE, voice=VOICE)
        assert est.audio_ms == 0
        assert est.chars == 0
        assert any("没有可朗读正文" in note for note in est.notes)
    assert any(
        "跳过朗读" in note
        for note in estimate.estimate_book(cases[0], engine=ENGINE, voice=VOICE).notes
    )


def test_estimate_from_zero_chars_is_empty_but_formattable() -> None:
    est = estimate.estimate_book_from_text(0, engine=ENGINE, voice=VOICE)
    assert est.audio_ms == 0 and est.chars == 0
    assert any("没有可朗读正文" in note for note in est.notes)
    assert "预计生成" in estimate.format_estimate(est)


# ---------------------------------------------------------------------------
# 5. format_estimate 中文关键词与多行结构
# ---------------------------------------------------------------------------


def test_format_estimate_contains_chinese_keywords(
    sample_estimate: estimate.BookEstimate,
) -> None:
    text = estimate.format_estimate(sample_estimate)
    for keyword in ("小时", "分钟", "预计生成", "成品时长", "估算假设", "未实测"):
        assert keyword in text, f"摘要缺少关键词：{keyword}"
    assert "仅按文本统计" in text
    assert len(text.splitlines()) >= 5


def test_format_estimate_for_large_book_has_hour_scale() -> None:
    est = estimate.estimate_book_from_text(100_000, engine=ENGINE, voice=VOICE)
    text = estimate.format_estimate(est)
    assert "小时" in text and "分钟" in text and "预计生成" in text
    assert "100,000" in text


# ---------------------------------------------------------------------------
# 6. 模块纯洁性：不导入网络 / 子进程库
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = {
    "aiohttp",
    "asyncio",
    "curl_cffi",
    "edge_tts",
    "http",
    "httpx",
    "multiprocessing",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}
ALLOWED_IMPORTS = {
    "__future__",
    "collections",
    "dataclasses",
    "math",
    "re",
    "textnorm",
}


def _imported_top_level_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_module_does_not_import_network_or_subprocess_libraries() -> None:
    imported = _imported_top_level_modules(ESTIMATE_SOURCE.read_text(encoding="utf-8"))
    assert imported & FORBIDDEN_IMPORTS == set(), (
        f"estimate.py 不得导入网络/子进程库：{sorted(imported & FORBIDDEN_IMPORTS)}"
    )
    assert imported <= ALLOWED_IMPORTS, (
        f"estimate.py 只应依赖纯计算模块，实际导入了：{sorted(imported - ALLOWED_IMPORTS)}"
    )


# ---------------------------------------------------------------------------
# 7. 常量表、并发修正、码率与 read 标记
# ---------------------------------------------------------------------------


def test_calibration_tables() -> None:
    assert estimate.chars_per_second("edge") == pytest.approx(4.6)
    assert estimate.chars_per_second("qwen") == pytest.approx(5.0)
    assert estimate.chars_per_second("indextts") == pytest.approx(4.8)
    assert estimate.chars_per_second("kokoro") == pytest.approx(4.5)
    assert estimate.chars_per_second("edge", language="en") == pytest.approx(15.7)
    assert estimate.realtime_factor("edge") == pytest.approx(6.0)
    assert estimate.realtime_factor("qwen") == pytest.approx(5.0)
    assert estimate.realtime_factor("indextts") == pytest.approx(1.0)
    assert estimate.realtime_factor("kokoro") == pytest.approx(0.5)
    # 未知引擎按保守值回退。
    assert estimate.chars_per_second("unknown") == pytest.approx(4.6)
    assert estimate.realtime_factor("unknown") == pytest.approx(1.0)


def test_higher_concurrency_does_not_slow_estimation() -> None:
    base = estimate.estimate_book_from_text(
        100_000, engine=ENGINE, voice=VOICE, concurrency=3
    )
    single = estimate.estimate_book_from_text(
        100_000, engine=ENGINE, voice=VOICE, concurrency=1
    )
    many = estimate.estimate_book_from_text(
        100_000, engine=ENGINE, voice=VOICE, concurrency=6
    )
    assert single.wall_seconds == pytest.approx(base.wall_seconds * 3, rel=1e-6)
    assert many.wall_seconds <= base.wall_seconds + 1e-9
    assert many.concurrency == 6
    # 并发只影响生成耗时，不影响音频时长与大小。
    assert single.audio_ms == base.audio_ms == many.audio_ms
    assert single.m4b_bytes == many.m4b_bytes


def test_bitrate_scales_file_size_and_rejects_garbage() -> None:
    text = "码率测试正文。" * 1000
    chapter_64 = estimate.estimate_chapter(
        text, index=1, title="码率", engine=ENGINE, voice=VOICE, mp3_bitrate="64k"
    )
    chapter_128 = estimate.estimate_chapter(
        text, index=1, title="码率", engine=ENGINE, voice=VOICE, mp3_bitrate="128k"
    )
    assert chapter_64.mp3_bytes == pytest.approx(
        chapter_64.audio_ms / 1000 * 64000 / 8, rel=0.01
    )
    assert chapter_128.mp3_bytes == pytest.approx(
        chapter_64.mp3_bytes * 2, rel=1e-3
    )
    with pytest.raises(ValueError):
        estimate.estimate_chapter(
            text, index=1, title="码率", engine=ENGINE, voice=VOICE, mp3_bitrate="很快"
        )


def test_read_flag_skips_chapters_and_renumbers() -> None:
    chapters = [
        {"title": "第一章", "text": "甲乙丙丁。" * 50, "read": True},
        {"title": "跳过", "text": "不应计入。" * 100, "read": False},
        {"title": "第三章", "text": "戊己庚辛。" * 50, "read": True},
    ]
    est = estimate.estimate_book(chapters, engine=ENGINE, voice=VOICE)
    assert [chapter.index for chapter in est.chapters] == [1, 2]
    assert [chapter.title for chapter in est.chapters] == ["第一章", "第三章"]
    assert est.chars == sum(chapter.chars for chapter in est.chapters)
    assert est.audio_ms == sum(chapter.audio_ms for chapter in est.chapters)
    assert any("跳过朗读" in note for note in est.notes)


def test_english_text_uses_english_rate() -> None:
    text = "The train left the platform at dawn and the carriage smelled of coffee. " * 100
    chapter = estimate.estimate_chapter(
        text, index=1, title="English", engine=ENGINE, voice=VOICE
    )
    chars = len("".join(text.split()))
    assert chars == len(text.replace(" ", ""))
    assert chapter.audio_ms == pytest.approx(chars / 15.7 * 1000, rel=0.01)
