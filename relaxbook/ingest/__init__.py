"""电子书解析入口：格式识别与统一 ``parse_book``。

契约见 ``docs/CONTRACTS.md`` 第 2 节。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .types import ChapterDraft, ParsedBook

SCAN_ERROR = "未提取到可读正文，疑似扫描件，需要先 OCR。"

SUPPORTED_FORMATS: set[str] = {"epub", "pdf", "mobi", "azw3"}

_FORMAT_NAMES = {
    "epub": "EPUB",
    "pdf": "PDF",
    "mobi": "MOBI",
    "azw3": "AZW3",
}


class IngestError(Exception):
    """解析失败。``str(exc)`` 即用户可见的中文原因。"""


def detect_format(filename: str, head: bytes | None = None) -> str:
    """按扩展名优先、其次文件头 magic 判断格式。"""
    name = str(filename or "")
    extension = Path(name).suffix.lower().lstrip(".")
    if extension in SUPPORTED_FORMATS:
        return extension

    if head:
        try:
            magic = bytes(head)[:2048]
        except (TypeError, ValueError):
            magic = b""
        if magic.startswith(b"%PDF") or b"%PDF-" in magic[:1024]:
            return "pdf"
        if magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            # ZIP 容器：EPUB 也是 ZIP，无扩展名时按 EPUB 尝试。
            return "epub"
        if b"BOOKMOBI" in magic[:128] or magic.startswith(b"TEXtREAd"):
            return "mobi"

    shown = extension or "未知"
    raise IngestError(
        f"不支持的文件格式：{shown}。目前支持 EPUB、PDF、MOBI、AZW3。"
    )


def parse_book(path: Path, *, format: str, settings) -> ParsedBook:
    """解析一本书。MOBI/AZW3 先经 calibre 转成工作副本 EPUB 再解析。"""
    source = Path(path)
    if not source.exists():
        raise IngestError(f"文件不存在：{source.name}")

    file_format = str(format or "").lower().lstrip(".")
    if file_format not in SUPPORTED_FORMATS:
        shown = file_format or "未知"
        raise IngestError(
            f"不支持的文件格式：{shown}。目前支持 EPUB、PDF、MOBI、AZW3。"
        )

    if file_format == "epub":
        from . import epub as epub_parser

        book = epub_parser.parse_epub(source)
    elif file_format == "pdf":
        from . import pdf as pdf_parser

        book = pdf_parser.parse_pdf(source)
    else:
        from . import convert
        from . import epub as epub_parser

        with tempfile.TemporaryDirectory(prefix="relaxbook-ingest-") as temporary:
            converted = convert.to_epub(source, settings, Path(temporary))
            book = epub_parser.parse_epub(converted)
        book.format = file_format
        book.warnings.insert(
            0,
            f"{_FORMAT_NAMES[file_format]} 已通过 calibre 的 ebook-convert "
            "转换为工作副本 EPUB 后解析。",
        )

    if not book.chapters or not any(
        (chapter.text or "").strip() for chapter in book.chapters
    ):
        raise IngestError(SCAN_ERROR)

    if not book.title:
        book.title = source.stem
    return book


__all__ = [
    "ChapterDraft",
    "IngestError",
    "ParsedBook",
    "SUPPORTED_FORMATS",
    "detect_format",
    "parse_book",
]
