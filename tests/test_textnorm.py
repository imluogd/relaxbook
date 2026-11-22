# -*- coding: utf-8 -*-
"""``relaxbook.textnorm`` 的单元测试。"""

from __future__ import annotations

import hashlib
import json
import re

from relaxbook.textnorm import (
    RULES_VERSION,
    clean_text,
    normalize_title,
    segment_cache_key,
    segment_text,
)


def _strip_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------


def test_clean_text_normalizes_newlines() -> None:
    assert clean_text("Hello\r\nworld") == "Hello world"
    assert clean_text("中文\r\n继续") == "中文继续"
    assert clean_text("第一段\r\n\r\n第二段") == "第一段\n\n第二段"
    assert clean_text("a\rb") == "a b"


def test_clean_text_removes_zero_width_and_control_chars() -> None:
    assert clean_text("你\u200b好\ufeff。") == "你好。"
    assert clean_text("a\x00b") == "a b"
    assert clean_text(None) == ""
    assert clean_text("") == ""


def test_clean_text_fixes_hyphen_line_breaks() -> None:
    assert clean_text("inter-\nnational") == "international"
    assert clean_text("well-\n  known") == "wellknown"


def test_clean_text_drops_pure_page_number_lines() -> None:
    text = clean_text("正文开始\n12\n正文结束")
    assert "12" not in text
    assert "正文开始" in text and "正文结束" in text

    cleaned = clean_text("第一段\n\n第 3 页\n\n第二段")
    assert "第 3 页" not in cleaned
    assert cleaned == "第一段\n\n第二段"


def test_clean_text_compresses_whitespace_and_keeps_paragraphs() -> None:
    assert clean_text("a  b\tc") == "a b c"
    assert clean_text("段落一。\n\n\n段落二。") == "段落一。\n\n段落二。"
    assert clean_text("  \n\n  ") == ""


# ---------------------------------------------------------------------------
# normalize_title
# ---------------------------------------------------------------------------


def test_normalize_title_strips_ordinal_noise() -> None:
    assert normalize_title("  第一章   出发 ") == "出发"
    assert normalize_title("1. 序言") == "序言"
    assert normalize_title("（2）抵达") == "抵达"
    assert normalize_title("出发") == "出发"


def test_normalize_title_falls_back_when_empty() -> None:
    assert normalize_title("   ") == "未命名章节"
    assert normalize_title(None) == "未命名章节"
    assert normalize_title("第三章") == "未命名章节"


# ---------------------------------------------------------------------------
# segment_text：不丢字是硬约束
# ---------------------------------------------------------------------------


def test_segment_text_never_loses_characters() -> None:
    texts = [
        "清晨的列车缓缓驶出站台，窗外是连绵的田野和薄雾。" * 8,
        "第一段。\n\n第二段，有逗号；也有分号！真的吗？" * 5,
        "The quick brown fox jumps over the lazy dog. " * 20,
        "甲" * 500,
        "短句。短。再来一句很长很长很长很长很长很长很长很长很长很长的句子。",
        "a，b、c,d; e f " * 100,
    ]
    for text in texts:
        segments = segment_text(text, max_chars=120)
        assert segments
        assert _strip_ws("".join(segments)) == _strip_ws(text)
        assert all(part.strip() for part in segments)
        assert all(len(part) <= 120 for part in segments)


def test_segment_text_merges_short_sentences() -> None:
    text = "你好。今天天气不错。我们出发吧。"
    assert segment_text(text) == [text]

    long_text = "春" * 30 + "。" + "夏" * 30 + "。"
    segments = segment_text(long_text)
    assert segments == ["春" * 30 + "。", "夏" * 30 + "。"]


def test_segment_text_hard_cuts_long_unpunctuated_text() -> None:
    text = "无标点" * 100  # 300 字，没有任何标点或空格
    segments = segment_text(text, max_chars=100)
    assert len(segments) == 3
    assert _strip_ws("".join(segments)) == text
    assert all(len(part) <= 100 for part in segments)


def test_segment_text_respects_small_max_chars() -> None:
    text = "甲乙丙丁，戊己庚辛；壬癸。"
    segments = segment_text(text, max_chars=8)
    assert all(len(part) <= 8 for part in segments)
    assert _strip_ws("".join(segments)) == _strip_ws(text)


def test_segment_text_empty_input() -> None:
    assert segment_text("") == []
    assert segment_text("   \n\t ") == []
    assert segment_text(None) == []


def test_segment_text_on_cleaned_text_keeps_content() -> None:
    raw = "  他-\n走了。\r\n\r\n第二段  有 多余空格。  \n12\n第三段。"
    cleaned = clean_text(raw)
    segments = segment_text(cleaned)
    assert _strip_ws("".join(segments)) == _strip_ws(cleaned)


# ---------------------------------------------------------------------------
# segment_cache_key
# ---------------------------------------------------------------------------


def test_segment_cache_key_is_sha256_of_canonical_payload() -> None:
    key = segment_cache_key("你好", engine="edge", model="edge-neural", voice="v1", rate="+0%")
    payload = json.dumps(
        {
            "t": "你好",
            "e": "edge",
            "m": "edge-neural",
            "v": "v1",
            "r": "+0%",
            "rules": RULES_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert key == expected
    assert re.fullmatch(r"[0-9a-f]{64}", key)


def test_segment_cache_key_changes_with_every_parameter() -> None:
    base = segment_cache_key("你好", engine="edge", model="m", voice="v", rate="+0%")
    variants = {
        base,
        segment_cache_key("再见", engine="edge", model="m", voice="v", rate="+0%"),
        segment_cache_key("你好", engine="kokoro", model="m", voice="v", rate="+0%"),
        segment_cache_key("你好", engine="edge", model="m2", voice="v", rate="+0%"),
        segment_cache_key("你好", engine="edge", model="m", voice="v2", rate="+0%"),
        segment_cache_key("你好", engine="edge", model="m", voice="v", rate="+10%"),
    }
    assert len(variants) == 6


def test_rules_version_matches_contract() -> None:
    assert RULES_VERSION == "1"
