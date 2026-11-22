"""文本清理、标题规范化与朗读分段。

本模块是纯函数集合，不依赖数据库或网络。契约见 ``docs/CONTRACTS.md`` 第 6 节。

分段的核心约束是 **不丢字**：把 ``segment_text`` 的返回片段按顺序拼起来，
去掉所有空白字符后必须与输入文本去掉空白后的内容完全一致。
"""

from __future__ import annotations

import hashlib
import json
import re

RULES_VERSION = "1"

# 零宽 / 软连字符等不可见字符。
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e"

# 需要统一成 \n 的换行符。
_NEWLINE_MAP = {
    "\r\n": "\n",
    "\r": "\n",
    "\u2028": "\n",
    "\u2029": "\n",
    "\u0085": "\n",
}

# 纯页码行：12 / - 12 - / IV / 第 12 页。
_PAGE_NUMBER_RE = re.compile(r"^[\s\-—–]*\d{1,4}[\s\-—–]*$")
_PAGE_ROMAN_RE = re.compile(r"^[\s\-—–]*[ivxlcdmIVXLCDM]{1,7}[\s\-—–]*$")
_PAGE_LABEL_RE = re.compile(
    r"^[\s\-—–]*第\s*\d{1,4}\s*页(?:\s*[/／]\s*共\s*\d{1,4}\s*页)?[\s\-—–]*$"
)

# 中日韩字符与全角标点（用于判断软换行是否该补空格）。
_CJK_CHARS = (
    "\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
)
_CJK_RE = re.compile(f"[{_CJK_CHARS}]")

# 标题开头的序号噪声。
_TITLE_ORDINAL_RES = (
    # 第一章 / 第 1 节 / 第一百二十三回
    re.compile(r"^第\s*[一二三四五六七八九十百千零〇两\d]+\s*[章节回卷篇部集]\s*[\.、,，:：\-—]?\s*"),
    # 1. / 1、/ 1）/ 1)
    re.compile(r"^\d{1,4}\s*[\.、,，:：）)\-—]\s*"),
    # (1) / 【1】 / [1]
    re.compile(r"^[\(（\[【]\s*\d{1,4}\s*[\)）\]】]\s*"),
    # 项目符号
    re.compile(r"^[\-–—•·●◇◆■□*]+\s*"),
)

_SENTENCE_TERMINATORS = "。！？；…：!?"
_SECONDARY_SEPARATORS = "，、,；;"
_SPACE_CHARS = " \t\u3000"


def clean_text(raw: str | None) -> str:
    """把原始正文转成可朗读的清理稿。

    - 统一换行、去掉零宽字符与控制字符；
    - 修复连字符断行（``-\\n`` 直接拼接）；
    - 合并软换行（中文直接相连，拉丁文补一个空格）；
    - 去掉纯页码行；
    - 压缩空白，段落之间保留两个 ``\\n``。
    """
    if not raw:
        return ""

    text = str(raw)
    for source, target in _NEWLINE_MAP.items():
        text = text.replace(source, target)

    # 零宽字符直接删除；其它控制字符（保留 \n \t）替换为空格，避免粘字。
    text = text.translate({ord(ch): None for ch in _ZERO_WIDTH})
    text = "".join(
        ch if ch in "\n\t" or ord(ch) >= 32 else " " for ch in text
    )

    # 英文连字符断行：inter-\nnational -> international
    text = re.sub(r"(?<=[^\s])[\-\u2010\u2011]\n[ \t]*", "", text)

    blocks = re.split(r"\n[ \t]*\n+", text)
    cleaned_blocks: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n")]
        lines = [
            line
            for line in lines
            if not (
                _PAGE_NUMBER_RE.match(line)
                or _PAGE_ROMAN_RE.match(line)
                or _PAGE_LABEL_RE.match(line)
            )
        ]
        merged = _merge_soft_lines(lines)
        merged = re.sub(r"[ \t\u3000]+", " ", merged).strip()
        if merged:
            cleaned_blocks.append(merged)

    result = "\n\n".join(cleaned_blocks)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _merge_soft_lines(lines: list[str]) -> str:
    """把段落内的软换行合并成一行。"""
    if not lines:
        return ""
    out = lines[0]
    for line in lines[1:]:
        if not out:
            out = line
            continue
        if not line:
            continue
        if _CJK_RE.match(out[-1]) or _CJK_RE.match(line[0]):
            out += line
        else:
            out += " " + line
    return out


def normalize_title(raw: str | None) -> str:
    """清理章节标题：去空白、去序号噪声，空标题回退为「未命名章节」。"""
    if not raw:
        return "未命名章节"
    title = str(raw)
    title = title.replace("\u3000", " ").replace("\u00a0", " ")
    title = re.sub(r"\s+", " ", title).strip()
    for _ in range(5):
        before = title
        for pattern in _TITLE_ORDINAL_RES:
            title = pattern.sub("", title, count=1).strip()
        if title == before:
            break
    title = title.strip(" \t\u3000")
    return title or "未命名章节"


def segment_text(text: str, *, max_chars: int = 220) -> list[str]:
    """把正文切成适合朗读的片段，保证不丢字。

    切分顺序：句末标点 → 次级标点（，、,；;）→ 空格 → 硬切；
    过短的句子与相邻句合并，直到长度 >= 20 字或到达末尾。
    """
    if not text:
        return []
    limit = max(1, int(max_chars))

    sentences = _split_sentences(text)
    pieces: list[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
        else:
            pieces.extend(_split_long(sentence, limit))

    merged: list[str] = []
    buffer = ""
    for piece in pieces:
        if not piece:
            continue
        if not piece.strip():
            # 纯空白片段：保留在缓冲里即可（空白在“不丢字”比较中被忽略）。
            buffer += piece
            continue
        if not buffer:
            buffer = piece
            continue
        fits = len(buffer) + len(piece) <= limit
        if fits and (len(buffer.strip()) < 20 or len(piece.strip()) < 8):
            buffer += piece
        else:
            candidate = buffer.strip()
            if candidate:
                merged.append(candidate)
            buffer = piece
    tail = buffer.strip()
    if tail:
        merged.append(tail)
    return [part for part in merged if part]


def _split_sentences(text: str) -> list[str]:
    """按句末标点切分，标点保留在前一句末尾。"""
    chunks = re.split(f"([{re.escape(_SENTENCE_TERMINATORS)}]+)", text)
    sentences: list[str] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        if index + 1 < len(chunks):
            chunk += chunks[index + 1]
            index += 2
        else:
            index += 1
        if chunk:
            sentences.append(chunk)
    return sentences


def _split_long(sentence: str, limit: int) -> list[str]:
    """超长单句：优先在 ，、,；; 处切，再退到空格，最后硬切。"""
    out: list[str] = []
    rest = sentence
    while len(rest) > limit:
        cut = -1
        for sep in _SECONDARY_SEPARATORS:
            cut = max(cut, rest.rfind(sep, 0, limit))
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            out.append(rest[:limit])
            rest = rest[limit:]
        else:
            out.append(rest[: cut + 1])
            rest = rest[cut + 1 :]
    if rest:
        out.append(rest)
    return out


def segment_cache_key(
    text: str, *, engine: str, model: str, voice: str, rate: str
) -> str:
    """合成缓存键：文本 + 引擎/模型/音色/语速 + 分段规则版本。"""
    payload = json.dumps(
        {
            "t": text,
            "e": engine,
            "m": model,
            "v": voice,
            "r": rate,
            "rules": RULES_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
