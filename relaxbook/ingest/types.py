"""解析结果的数据结构。

契约见 ``docs/CONTRACTS.md`` 第 1 节。``ChapterDraft.text`` 是已清理的朗读稿正文，
``source_locator`` 记录来源位置（EPUB: ``{"href","anchor"}``，PDF: ``{"pages":[1,2]}``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChapterDraft:
    """一章的解析草稿，尚未落库。"""

    title: str
    text: str
    read: bool = True
    source_locator: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedBook:
    """整本解析结果。"""

    title: str
    author: str | None
    format: str
    chapters: list[ChapterDraft]
    cover_bytes: bytes | None = None
    cover_ext: str = "jpg"
    warnings: list[str] = field(default_factory=list)
