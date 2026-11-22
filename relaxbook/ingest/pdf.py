"""PDF 解析：pypdf 读书签与页数，pdfplumber 逐页取文本。

契约见 ``docs/CONTRACTS.md`` 第 4 节。要点：

* 有书签：按书签页码（并尝试在页内定位标题）映射章页码范围；
* 无书签：按标题模式（``第X章`` / ``^数字[.、）)]`` / 全大写短行 / 短行接正文）提候选章，
  找不到就整本一章并记 warning；
* 清理跨页高频的页眉页脚；低文本量页记 warning；整本可见字符 < 100 视为扫描件抛 ``IngestError``。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

from relaxbook.textnorm import normalize_title

from . import IngestError
from .types import ChapterDraft, ParsedBook

SCAN_ERROR = "未提取到可读正文，疑似扫描件，需要先 OCR。"
MIN_VISIBLE_CHARS = 100
LOW_TEXT_CHARS = 40

_CHAPTER_HEADING_RE = re.compile(
    r"^\s*第\s*[一二三四五六七八九十百千零〇两\d]+\s*[章节回卷篇部集]"
)
_NUMBER_HEADING_RE = re.compile(r"^\s*\d{1,3}\s*[\.、）)]\s*\S")

_PUNCT_ENDINGS = ("。", "，", ",", "：", ":", "!", "！", "?", "？", "；", ";")


def parse_pdf(path: Path) -> ParsedBook:
    """解析文本型 PDF；纯扫描件（无文本）抛 ``IngestError``。"""
    source = Path(path)
    if not source.exists():
        raise IngestError(f"PDF 文件不存在：{source.name}")

    try:
        reader = PdfReader(str(source))
    except Exception as exc:  # noqa: BLE001 - pypdf 的异常类型较杂
        raise IngestError(f"无法打开 PDF 文件：{source.name}（{exc}）") from exc

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise IngestError("PDF 已加密，无法读取。请先解除密码保护。") from exc
        if reader.is_encrypted:
            raise IngestError("PDF 已加密，无法读取。请先解除密码保护。")

    page_count = len(reader.pages)
    if page_count <= 0:
        raise IngestError("PDF 没有页面，无法解析。")

    warnings: list[str] = []
    book_title, author = _read_metadata(reader, source)
    bookmarks = _read_outline(reader, warnings)

    raw_pages, failed_pages = _extract_pages(source)
    if failed_pages:
        warnings.append(
            "以下页面文本提取失败，请核对：第 "
            + "、".join(str(page) for page in failed_pages)
            + " 页"
        )

    page_lines = [
        [line.strip() for line in text.splitlines() if line.strip()]
        for text in raw_pages
    ]
    page_lines, removed_markers = _strip_headers_footers(page_lines)
    if removed_markers:
        preview = "、".join(removed_markers[:3])
        suffix = " 等" if len(removed_markers) > 3 else ""
        warnings.append(f"已剔除疑似页眉页脚重复文本：{preview}{suffix}")

    total_visible = sum(_visible_length(lines) for lines in page_lines)
    if total_visible < MIN_VISIBLE_CHARS:
        raise IngestError(SCAN_ERROR)

    low_pages = [
        index + 1
        for index, lines in enumerate(page_lines)
        if _visible_length(lines) < LOW_TEXT_CHARS
    ]
    if low_pages:
        displayed = "、".join(str(page) for page in low_pages[:30])
        suffix = " 等" if len(low_pages) > 30 else ""
        warnings.append(
            f"以下页面文本量偏少（少于 {LOW_TEXT_CHARS} 字符），可能是扫描页或图片页，请核对："
            f"第 {displayed}{suffix} 页"
        )

    # 记录被剔除的页眉页脚文本键：书签名恰好是被剔除的 running header 时不应告警。
    removed_marker_keys = {
        _match_key(marker) for marker in removed_markers if marker and _match_key(marker)
    }
    chapters: list[ChapterDraft] = []
    if bookmarks:
        chapters = _chapters_from_bookmarks(
            bookmarks, page_lines, warnings, removed_marker_keys
        )
        if not chapters:
            warnings.append("PDF 书签未能定位到可读正文，改为按标题模式识别章节。")
    if not chapters:
        chapters = _chapters_from_headings(page_lines, book_title, warnings)

    if not chapters:
        raise IngestError(SCAN_ERROR)

    return ParsedBook(
        title=book_title,
        author=author,
        format="pdf",
        chapters=chapters,
        cover_bytes=None,
        cover_ext="jpg",
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _read_metadata(reader: PdfReader, source: Path) -> tuple[str, str | None]:
    metadata = reader.metadata
    title = ""
    author = ""
    if metadata is not None:
        try:
            title = str(metadata.title or "")
        except Exception:  # noqa: BLE001
            title = ""
        try:
            author = str(metadata.author or "")
        except Exception:  # noqa: BLE001
            author = ""
        if not title:
            try:
                title = str(metadata.get("/Title") or "")
            except Exception:  # noqa: BLE001
                title = ""
        if not author:
            try:
                author = str(metadata.get("/Author") or "")
            except Exception:  # noqa: BLE001
                author = ""
    book_title = _collapse(title) or source.stem
    return book_title, _collapse(author) or None


def _read_outline(reader: PdfReader, warnings: list[str]) -> list[tuple[str, int]]:
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001
        warnings.append("PDF 书签读取失败，改为按标题模式识别章节。")
        return []
    if not outline:
        return []

    entries: list[tuple[str, int]] = []
    try:
        _flatten_outline(reader, list(outline), entries, warnings)
    except Exception:  # noqa: BLE001
        warnings.append("PDF 书签结构损坏，改为按标题模式识别章节。")
        return entries
    return entries


def _flatten_outline(
    reader: PdfReader,
    items: list,
    entries: list[tuple[str, int]],
    warnings: list[str],
) -> None:
    for item in items:
        if isinstance(item, list):
            _flatten_outline(reader, item, entries, warnings)
            continue
        title = _collapse(str(getattr(item, "title", "") or ""))
        try:
            page_number = reader.get_destination_page_number(item)
        except Exception:  # noqa: BLE001
            page_number = None
        if page_number is None or page_number < 0:
            warnings.append(f"PDF 书签“{title or '未命名'}”缺少有效页码，已跳过。")
            continue
        entries.append((title, int(page_number)))


def _extract_pages(source: Path) -> tuple[list[str], list[int]]:
    pages: list[str] = []
    failed: list[int] = []
    try:
        with pdfplumber.open(str(source)) as pdf:
            for index, page in enumerate(pdf.pages):
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001
                    pages.append("")
                    failed.append(index + 1)
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"无法读取 PDF 页面文本：{source.name}（{exc}）") from exc
    return pages, failed


# ---------------------------------------------------------------------------
# 页眉 / 页脚
# ---------------------------------------------------------------------------


def _line_key(line: str) -> str:
    key = re.sub(r"\d+", "#", line.strip())
    key = re.sub(r"\s+", " ", key)
    return key if len(key) <= 80 else ""


def _strip_headers_footers(
    page_lines: list[list[str]],
) -> tuple[list[list[str]], list[str]]:
    page_count = len(page_lines)
    if page_count < 2:
        return page_lines, []

    first_counter: Counter[str] = Counter()
    last_counter: Counter[str] = Counter()
    for lines in page_lines:
        if not lines:
            continue
        first_counter[_line_key(lines[0])] += 1
        if len(lines) > 1:
            last_counter[_line_key(lines[-1])] += 1

    threshold = max(2, math.ceil(page_count * 0.4))
    removed: list[str] = []
    cleaned_pages: list[list[str]] = []
    for lines in page_lines:
        remaining = list(lines)
        if remaining:
            key = _line_key(remaining[0])
            if key and first_counter[key] >= threshold:
                removed.append(remaining.pop(0))
        if remaining:
            key = _line_key(remaining[-1])
            if key and last_counter[key] >= threshold:
                removed.append(remaining.pop())
        cleaned_pages.append(remaining)

    unique_removed: list[str] = []
    for marker in removed:
        if marker and marker not in unique_removed:
            unique_removed.append(marker)
    return cleaned_pages, unique_removed


# ---------------------------------------------------------------------------
# 书签分章
# ---------------------------------------------------------------------------


def _chapters_from_bookmarks(
    bookmarks: list[tuple[str, int]],
    page_lines: list[list[str]],
    warnings: list[str],
    removed_marker_keys: set[str] | None = None,
) -> list[ChapterDraft]:
    page_count = len(page_lines)
    valid: list[tuple[str, int]] = []
    for title, page in bookmarks:
        if page >= page_count:
            warnings.append(f"PDF 书签“{title or '未命名'}”页码超出范围，已跳过。")
            continue
        valid.append((title, page))

    page_sequence = [page for _, page in valid]
    if any(page_sequence[i] < page_sequence[i - 1] for i in range(1, len(page_sequence))):
        warnings.append("PDF 书签顺序与页码顺序冲突，已按页码顺序排列章节。")
        valid.sort(key=lambda item: item[1])

    positioned: list[tuple[int, int, str]] = []
    for title, page in valid:
        line_index = _find_title_line(page_lines[page], title)
        if line_index is None:
            line_index = 0
            # 书签名恰好是被剔除的跨页页眉页脚（running header）时，标题行是有意
            # 移除的，直接从页首正文开始即可，不需要产生噪声告警。
            title_key = _match_key(title)
            is_removed_marker = bool(
                removed_marker_keys and title_key and title_key in removed_marker_keys
            )
            if not is_removed_marker:
                warnings.append(
                    f"PDF 书签“{title or '未命名'}”未能按标题在页内定位，按页首开始。"
                )
        positioned.append((page, line_index, title))
    positioned.sort(key=lambda item: (item[0], item[1]))

    unique_positions: list[tuple[int, int, str]] = []
    for position in positioned:
        if unique_positions and position[:2] == unique_positions[-1][:2]:
            warnings.append(
                f"同一位置有多个 PDF 书签，已合并："
                f"{unique_positions[-1][2] or '未命名'} / {position[2] or '未命名'}"
            )
            continue
        unique_positions.append(position)

    chapters: list[ChapterDraft] = []
    start_page, start_line, _ = unique_positions[0]
    leading = _slice_lines(page_lines, (0, 0), (start_page, start_line))
    if leading.strip():
        chapters.append(
            ChapterDraft(
                title=_guess_title(leading),
                text=leading,
                source_locator={"pages": [1, start_page + 1]},
                warnings=["书签未覆盖前面的正文，按页补入"],
            )
        )

    for index, (page, line, title) in enumerate(unique_positions):
        end = (
            (unique_positions[index + 1][0], unique_positions[index + 1][1])
            if index + 1 < len(unique_positions)
            else None
        )
        text = _slice_lines(page_lines, (page, line), end)
        if not text.strip():
            warnings.append(f"书签章节没有可读正文：{title or '未命名'}")
            continue
        end_page = _end_page_for((page, line), end, page_lines) + 1
        chapters.append(
            ChapterDraft(
                title=normalize_title(title),
                text=text,
                source_locator={"pages": [page + 1, end_page]},
            )
        )
    return chapters


def _find_title_line(lines: list[str], title: str) -> int | None:
    target = _match_key(title)
    if not target:
        return None
    for index, line in enumerate(lines):
        if target and target in _match_key(line):
            return index
    return None


def _match_key(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[\s\-—–:：.。·•·\[\]（）()]+", "", text)
    return text


# ---------------------------------------------------------------------------
# 标题模式分章
# ---------------------------------------------------------------------------


def _chapters_from_headings(
    page_lines: list[list[str]],
    book_title: str,
    warnings: list[str],
) -> list[ChapterDraft]:
    candidates: list[tuple[int, int, str]] = []
    for page_index, lines in enumerate(page_lines):
        for line_index, line in enumerate(lines):
            next_line = lines[line_index + 1] if line_index + 1 < len(lines) else None
            if _is_heading(line, next_line):
                candidates.append((page_index, line_index, normalize_title(line)))

    if not candidates:
        text = "\n".join(
            line for lines in page_lines for line in lines
        ).strip()
        if not text:
            return []
        last_page = _last_content_page(page_lines) + 1
        return [
            ChapterDraft(
                title=book_title or normalize_title(""),
                text=text,
                source_locator={"pages": [1, last_page]},
                warnings=["未识别到章节结构，整本作为一章"],
            )
        ]

    chapters: list[ChapterDraft] = []
    first_page, first_line, _ = candidates[0]
    leading = _slice_lines(page_lines, (0, 0), (first_page, first_line))
    if leading.strip():
        chapters.append(
            ChapterDraft(
                title=_guess_title(leading),
                text=leading,
                source_locator={"pages": [1, first_page + 1]},
                warnings=["标题模式未覆盖前面的正文，按页补入"],
            )
        )

    for index, (page, line, title) in enumerate(candidates):
        end = (
            (candidates[index + 1][0], candidates[index + 1][1])
            if index + 1 < len(candidates)
            else None
        )
        text = _slice_lines(page_lines, (page, line), end)
        if not text.strip():
            continue
        end_page = _end_page_for((page, line), end, page_lines) + 1
        chapters.append(
            ChapterDraft(
                title=title,
                text=text,
                source_locator={"pages": [page + 1, end_page]},
            )
        )
    return chapters


def _is_heading(line: str, next_line: str | None) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False
    if _CHAPTER_HEADING_RE.match(stripped):
        return True
    if _NUMBER_HEADING_RE.match(stripped):
        return True
    letters = re.sub(r"[^A-Za-z]", "", stripped)
    if (
        len(letters) >= 3
        and letters == letters.upper()
        and len(stripped) <= 50
        and not stripped.endswith(_PUNCT_ENDINGS)
    ):
        return True
    if (
        len(stripped) <= 20
        and next_line is not None
        and len(next_line.strip()) > 40
        and not stripped.endswith(_PUNCT_ENDINGS)
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# 行切片与计数
# ---------------------------------------------------------------------------


def _slice_lines(
    page_lines: list[list[str]],
    start: tuple[int, int],
    end: tuple[int, int] | None,
) -> str:
    if not page_lines:
        return ""
    start_page, start_line = start
    if end is None:
        end_page = len(page_lines) - 1
        end_line = len(page_lines[-1])
    else:
        end_page, end_line = end

    parts: list[str] = []
    for page in range(start_page, min(end_page, len(page_lines) - 1) + 1):
        lines = page_lines[page]
        low = start_line if page == start_page else 0
        high = end_line if page == end_page else len(lines)
        if high > low:
            parts.extend(lines[low:high])
    return "\n".join(parts).strip()


def _end_page_for(
    start: tuple[int, int],
    end: tuple[int, int] | None,
    page_lines: list[list[str]],
) -> int:
    if end is None:
        return _last_content_page(page_lines)
    start_page, _ = start
    end_page, end_line = end
    if end_page <= start_page:
        return start_page
    if end_line == 0:
        index = end_page - 1
        while index > start_page and not page_lines[index]:
            index -= 1
        return max(index, start_page)
    return end_page


def _last_content_page(page_lines: list[list[str]]) -> int:
    for index in range(len(page_lines) - 1, -1, -1):
        if page_lines[index]:
            return index
    return 0


def _visible_length(lines: list[str]) -> int:
    if not lines:
        return 0
    return len(re.sub(r"\s+", "", "".join(lines)))


def _guess_title(text: str) -> str:
    first = (text or "").strip().split("\n", 1)[0]
    first = re.sub(r"\s+", " ", first).strip()
    if 0 < len(first) <= 40:
        return normalize_title(first)
    return normalize_title("")


def _collapse(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()
