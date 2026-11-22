# -*- coding: utf-8 -*-
"""``relaxbook.ingest`` 的测试。

所有 EPUB / PDF 都在 ``tmp_path`` 现场构造，不依赖仓库内的样书。
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from relaxbook.config import Settings
from relaxbook.ingest import IngestError, detect_format, parse_book
from relaxbook.ingest.epub import parse_epub
from relaxbook.ingest.pdf import parse_pdf

# ---------------------------------------------------------------------------
# EPUB 构造工具
# ---------------------------------------------------------------------------

_CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _write_epub(
    path: Path,
    *,
    opf_path: str,
    opf_xml: str,
    files: dict[str, str | bytes],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml", _CONTAINER_XML.format(opf_path=opf_path)
        )
        archive.writestr(opf_path, opf_xml)
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _chapter_html(title: str, paragraphs: list[str], *, heading_id: str | None = None) -> str:
    identifier = f' id="{heading_id}"' if heading_id else ""
    body = f"<h1{identifier}>{title}</h1>" + "".join(
        f"<p>{paragraph}</p>" for paragraph in paragraphs
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        f"<head><title>{title}</title></head>\n"
        f"<body>{body}</body>\n"
        "</html>"
    )


def _make_epub2_three_chapters(tmp_path: Path) -> Path:
    chapters = [
        ("ch1.xhtml", "序言", ["清晨的列车缓缓驶出站台。", "窗外是连绵的田野和薄雾。"]),
        ("ch2.xhtml", "出发", ["午后的车站人声鼎沸。", "我们拖着行李挤上了车厢。"]),
        ("ch3.xhtml", "抵达", ["傍晚的灯光照在陌生的街道上。", "旅程在这里画上句号。"]),
    ]
    manifest = "".join(
        f'<item id="{href[:-6]}" href="{href}" media-type="application/xhtml+xml"/>'
        for href, _, _ in chapters
    )
    spine = "".join(f'<itemref idref="{href[:-6]}"/>' for href, _, _ in chapters)
    nav_points = "".join(
        f'<navPoint id="np{index}" playOrder="{index}">'
        f"<navLabel><text>{title}</text></navLabel>"
        f'<content src="{href}"/></navPoint>'
        for index, (href, title, _) in enumerate(chapters, start=1)
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>测试 EPUB2</dc:title>
  <dc:creator>张三</dc:creator>
  <dc:identifier id="bookid">urn:uuid:test-epub2</dc:identifier>
  <meta name="cover" content="cover-image"/>
 </metadata>
 <manifest>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  {manifest}
  <item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"/>
 </manifest>
 <spine toc="ncx">{spine}</spine>
</package>"""
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
 <head><meta name="dtb:uid" content="urn:uuid:test-epub2"/></head>
 <docTitle><text>测试 EPUB2</text></docTitle>
 <navMap>{nav_points}</navMap>
</ncx>"""
    files: dict[str, str | bytes] = {
        f"OEBPS/{href}": _chapter_html(title, paragraphs)
        for href, title, paragraphs in chapters
    }
    files["OEBPS/toc.ncx"] = ncx
    files["OEBPS/images/cover.jpg"] = b"\xff\xd8\xff\xe0FAKEJPEG-BYTES"
    return _write_epub(
        tmp_path / "epub2.epub",
        opf_path="OEBPS/content.opf",
        opf_xml=opf,
        files=files,
    )


def _make_epub3(
    path: Path,
    *,
    book_title: str,
    author: str | None,
    spine: list[str],
    documents: dict[str, str],
    nav_links: list[tuple[str, str]] | None,
    cover_bytes: bytes | None = None,
) -> Path:
    manifest: list[str] = []
    spine_refs: list[str] = []
    for index, href in enumerate(spine, start=1):
        manifest.append(
            f'<item id="doc{index}" href="{href}" '
            'media-type="application/xhtml+xml"/>'
        )
        spine_refs.append(f'<itemref idref="doc{index}"/>')
    if nav_links is not None:
        manifest.append(
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
            'properties="nav"/>'
        )
    if cover_bytes is not None:
        manifest.append(
            '<item id="cover-image" href="images/cover.png" '
            'media-type="image/png" properties="cover-image"/>'
        )
    creators = f"<dc:creator>{author}</dc:creator>" if author else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="bookid">urn:uuid:test-epub3</dc:identifier>
  <dc:title>{book_title}</dc:title>
  {creators}
 </metadata>
 <manifest>
  {"".join(manifest)}
 </manifest>
 <spine>{"".join(spine_refs)}</spine>
</package>"""
    files: dict[str, str | bytes] = {
        f"OEBPS/{href}": document for href, document in documents.items()
    }
    if nav_links is not None:
        links = "".join(
            f'<li><a href="{href}">{title}</a></li>' for title, href in nav_links
        )
        files["OEBPS/nav.xhtml"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops">\n'
            "<head><title>目录</title></head>\n"
            "<body><nav epub:type=\"toc\" id=\"toc\"><h1>目录</h1>"
            f"<ol>{links}</ol></nav></body>\n</html>"
        )
    if cover_bytes is not None:
        files["OEBPS/images/cover.png"] = cover_bytes
    return _write_epub(path, opf_path="OEBPS/content.opf", opf_xml=opf, files=files)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(data_dir=str(tmp_path / "data"), **overrides)


# ---------------------------------------------------------------------------
# PDF 构造工具
# ---------------------------------------------------------------------------


def _make_pdf(
    path: Path,
    *,
    pages: list[list[str]],
    title: str = "",
    author: str = "",
    outline: list[tuple[str, int]] | None = None,
    header: str | None = None,
    headers: list[str | None] | None = None,
    footer: str | None = None,
) -> Path:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=15)
    if title:
        pdf.set_title(title)
    if author:
        pdf.set_author(author)
    for page_index, lines in enumerate(pages):
        pdf.add_page()
        page_header = headers[page_index] if headers is not None else header
        if page_header:
            pdf.set_font("Helvetica", size=9)
            pdf.cell(0, 7, page_header, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=11)
        for line in lines:
            pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
        if footer:
            # 关闭自动分页后再写页脚：否则 y 落在分页触发线上，fpdf 会先分页，
            # 把页脚挤成独立一页（页数翻倍会让书签页码整体错位）。
            pdf.set_auto_page_break(False)
            pdf.set_y(-15)
            pdf.set_font("Helvetica", size=9)
            pdf.cell(0, 7, footer, align="C")
            pdf.set_auto_page_break(True, margin=15)

    raw_path = path.with_suffix(".raw.pdf")
    pdf.output(str(raw_path))

    reader = PdfReader(str(raw_path))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    metadata: dict[str, str] = {}
    if title:
        metadata["/Title"] = title
    if author:
        metadata["/Author"] = author
    if metadata:
        writer.add_metadata(metadata)
    for outline_title, page_index in outline or []:
        writer.add_outline_item(title=outline_title, page_number=page_index)
    with path.open("wb") as handle:
        writer.write(handle)
    raw_path.unlink()
    return path


# ---------------------------------------------------------------------------
# detect_format / parse_book 入口
# ---------------------------------------------------------------------------


def test_detect_format_prefers_extension() -> None:
    assert detect_format("book.epub") == "epub"
    assert detect_format("BOOK.PDF") == "pdf"
    assert detect_format("a.mobi") == "mobi"
    assert detect_format("a.azw3") == "azw3"
    # 扩展名优先：即使文件头是 ZIP，.pdf 也按 PDF 处理
    assert detect_format("a.pdf", head=b"PK\x03\x04") == "pdf"


def test_detect_format_falls_back_to_magic() -> None:
    assert detect_format("unknown.bin", head=b"%PDF-1.7 ...") == "pdf"
    assert detect_format("unknown.dat", head=b"PK\x03\x04rest") == "epub"
    assert detect_format("book.azw", head=b"\x00" * 60 + b"BOOKMOBI") == "mobi"


def test_detect_format_rejects_unknown() -> None:
    with pytest.raises(IngestError) as excinfo:
        detect_format("notes.txt")
    assert "不支持的文件格式" in str(excinfo.value)
    with pytest.raises(IngestError):
        detect_format("", head=b"hello world")


def test_parse_book_epub_dispatch(tmp_path: Path) -> None:
    book = parse_book(
        _make_epub2_three_chapters(tmp_path),
        format="epub",
        settings=_settings(tmp_path),
    )
    assert book.format == "epub"
    assert len(book.chapters) == 3


def test_parse_book_mobi_converts_then_parses(tmp_path: Path, monkeypatch) -> None:
    from relaxbook.ingest import convert

    epub_path = _make_epub2_three_chapters(tmp_path)
    monkeypatch.setattr(
        convert, "to_epub", lambda path, settings, work_dir: epub_path
    )
    source = tmp_path / "book.mobi"
    source.write_bytes(b"BOOKMOBI" + b"\x00" * 32)
    book = parse_book(source, format="mobi", settings=_settings(tmp_path))
    assert book.format == "mobi"
    assert len(book.chapters) == 3
    assert any("calibre" in warning for warning in book.warnings)


def test_parse_book_unknown_format_raises(tmp_path: Path) -> None:
    source = tmp_path / "book.txt"
    source.write_text("hello", encoding="utf-8")
    with pytest.raises(IngestError) as excinfo:
        parse_book(source, format="txt", settings=_settings(tmp_path))
    assert "不支持的文件格式" in str(excinfo.value)
    with pytest.raises(IngestError):
        parse_book(tmp_path / "missing.epub", format="epub", settings=_settings(tmp_path))


# ---------------------------------------------------------------------------
# EPUB fixtures
# ---------------------------------------------------------------------------


def test_epub2_ncx_three_chapters(tmp_path: Path) -> None:
    book = parse_epub(_make_epub2_three_chapters(tmp_path))
    assert book.title == "测试 EPUB2"
    assert book.author == "张三"
    assert book.format == "epub"
    assert [chapter.title for chapter in book.chapters] == ["序言", "出发", "抵达"]
    assert "清晨的列车" in book.chapters[0].text
    assert "午后的车站" in book.chapters[1].text
    assert "傍晚的灯光" in book.chapters[2].text
    # 段落之间保留换行
    assert "窗外是连绵的田野和薄雾。" in book.chapters[0].text
    assert "清晨的列车缓缓驶出站台。" in book.chapters[0].text.split("\n")
    assert book.chapters[0].source_locator == {"href": "ch1.xhtml"}
    assert book.cover_bytes == b"\xff\xd8\xff\xe0FAKEJPEG-BYTES"
    assert book.cover_ext == "jpg"


def test_epub3_nav_three_chapters(tmp_path: Path) -> None:
    spine = ["ch1.xhtml", "ch2.xhtml", "ch3.xhtml"]
    documents = {
        "ch1.xhtml": _chapter_html("序言", ["清晨的列车缓缓驶出站台。", "薄雾散开。"]),
        "ch2.xhtml": _chapter_html("出发", ["午后的车站人声鼎沸。", "我们挤上了车厢。"]),
        "ch3.xhtml": _chapter_html("抵达", ["傍晚的灯光照着街道。", "旅程结束。"]),
    }
    epub_path = _make_epub3(
        tmp_path / "epub3.epub",
        book_title="测试 EPUB3",
        author="李四",
        spine=spine,
        documents=documents,
        nav_links=[
            ("序言", "ch1.xhtml"),
            ("出发", "ch2.xhtml"),
            ("抵达", "ch3.xhtml"),
        ],
    )
    book = parse_epub(epub_path)
    assert book.title == "测试 EPUB3"
    assert book.author == "李四"
    assert [chapter.title for chapter in book.chapters] == ["序言", "出发", "抵达"]
    assert "午后的车站" in book.chapters[1].text


def test_epub3_multiple_anchors_split_one_xhtml(tmp_path: Path) -> None:
    body = (
        '<h1 id="s1">开篇</h1><p>第一段甲。</p><p>第一段乙。</p>'
        '<h1 id="s2">中段</h1><p>第二段甲。</p>'
        '<h1 id="s3">结尾</h1><p>第三段甲。</p>'
    )
    document = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>单文件</title>'
        f"</head><body>{body}</body></html>"
    )
    epub_path = _make_epub3(
        tmp_path / "anchors.epub",
        book_title="锚点样书",
        author=None,
        spine=["book.xhtml"],
        documents={"book.xhtml": document},
        nav_links=[
            ("开篇", "book.xhtml#s1"),
            ("中段", "book.xhtml#s2"),
            ("结尾", "book.xhtml#s3"),
        ],
    )
    book = parse_epub(epub_path)
    assert [chapter.title for chapter in book.chapters] == ["开篇", "中段", "结尾"]
    assert len(book.chapters) == 3
    assert "第一段甲" in book.chapters[0].text
    assert "第二段甲" not in book.chapters[0].text
    assert "第二段甲" in book.chapters[1].text
    assert "第三段甲" not in book.chapters[1].text
    assert "第三段甲" in book.chapters[2].text
    assert book.chapters[0].source_locator == {"href": "book.xhtml", "anchor": "s1"}
    assert book.chapters[1].source_locator == {"href": "book.xhtml", "anchor": "s2"}


def test_epub3_duplicate_titles_are_kept(tmp_path: Path) -> None:
    epub_path = _make_epub3(
        tmp_path / "duplicate.epub",
        book_title="同名样书",
        author=None,
        spine=["a.xhtml", "b.xhtml"],
        documents={
            "a.xhtml": _chapter_html("出发（上）", ["第一次出发的正文。"]),
            "b.xhtml": _chapter_html("出发（下）", ["第二次出发的正文。"]),
        },
        nav_links=[
            ("第1章 出发", "a.xhtml"),
            ("第2章 出发", "b.xhtml"),
        ],
    )
    book = parse_epub(epub_path)
    assert [chapter.title for chapter in book.chapters] == ["出发", "出发"]
    assert "第一次出发" in book.chapters[0].text
    assert "第二次出发" in book.chapters[1].text


def test_epub3_uncovered_body_becomes_pending_chapter(tmp_path: Path) -> None:
    epub_path = _make_epub3(
        tmp_path / "uncovered.epub",
        book_title="未覆盖样书",
        author=None,
        spine=["ch1.xhtml", "ch2.xhtml"],
        documents={
            "ch1.xhtml": _chapter_html("序言", ["目录里的第一章正文。"]),
            "ch2.xhtml": _chapter_html("附录", ["未在目录中的附录内容。", "第二段附录内容。"]),
        },
        nav_links=[("序言", "ch1.xhtml")],
    )
    book = parse_epub(epub_path)
    assert len(book.chapters) == 2
    assert book.chapters[0].title == "序言"
    uncovered = book.chapters[1]
    assert "目录未覆盖，按正文补入" in uncovered.warnings
    assert "未在目录中的附录内容" in uncovered.text
    assert uncovered.title == "附录"


def test_epub3_invalid_anchor_falls_back_to_file_split(tmp_path: Path) -> None:
    epub_path = _make_epub3(
        tmp_path / "bad-anchor.epub",
        book_title="坏锚点样书",
        author=None,
        spine=["ch1.xhtml", "ch2.xhtml"],
        documents={
            "ch1.xhtml": _chapter_html("序言", ["坏锚点文件的第一段。", "坏锚点文件的第二段。"]),
            "ch2.xhtml": _chapter_html("出发", ["第二个文件正文。"]),
        },
        nav_links=[("序言", "ch1.xhtml#missing"), ("出发", "ch2.xhtml")],
    )
    book = parse_epub(epub_path)
    assert any("目录锚点失效" in warning for warning in book.warnings)
    assert len(book.chapters) == 2
    assert "坏锚点文件的第一段" in book.chapters[0].text
    assert "坏锚点文件的第二段" in book.chapters[0].text


def test_epub3_duplicate_target_falls_back_to_file_split(tmp_path: Path) -> None:
    document = _chapter_html("正文", ["上篇内容。", "下篇内容。"])
    epub_path = _make_epub3(
        tmp_path / "duplicate-target.epub",
        book_title="重复目标样书",
        author=None,
        spine=["book.xhtml"],
        documents={"book.xhtml": document},
        nav_links=[("上篇", "book.xhtml"), ("下篇", "book.xhtml")],
    )
    book = parse_epub(epub_path)
    assert any("多个目录项指向同一位置" in warning for warning in book.warnings)
    assert len(book.chapters) == 1
    assert "上篇内容" in book.chapters[0].text
    assert "下篇内容" in book.chapters[0].text


def test_epub3_toc_order_conflict_warns_and_uses_spine_order(tmp_path: Path) -> None:
    epub_path = _make_epub3(
        tmp_path / "order.epub",
        book_title="乱序样书",
        author=None,
        spine=["ch1.xhtml", "ch2.xhtml"],
        documents={
            "ch1.xhtml": _chapter_html("序言", ["正文先出现在这里。"]),
            "ch2.xhtml": _chapter_html("出发", ["正文后出现在这里。"]),
        },
        nav_links=[("出发", "ch2.xhtml"), ("序言", "ch1.xhtml")],
    )
    book = parse_epub(epub_path)
    assert any("目录顺序与正文顺序冲突" in warning for warning in book.warnings)
    assert [chapter.title for chapter in book.chapters] == ["序言", "出发"]
    assert "正文先出现在这里" in book.chapters[0].text


# ---------------------------------------------------------------------------
# PDF fixtures
# ---------------------------------------------------------------------------


def test_pdf_text_pdf_with_headings_splits_chapters(tmp_path: Path) -> None:
    pages = [
        [
            "1. Departure",
            "The morning train rolled slowly out of the station.",
            "Fields and thin mist stretched beyond the windows.",
        ],
        [
            "2. Arrival",
            "The afternoon station was crowded with travellers.",
            "We dragged our luggage onto the platform at last.",
        ],
        [
            "3. Home",
            "Evening lights shone over the familiar street.",
            "The journey ended quietly where it had begun.",
        ],
    ]
    pdf_path = _make_pdf(
        tmp_path / "headings.pdf",
        pages=pages,
        title="Sample PDF Book",
        author="Li Ming",
    )
    book = parse_pdf(pdf_path)
    assert book.format == "pdf"
    assert book.title == "Sample PDF Book"
    assert book.author == "Li Ming"
    assert [chapter.title for chapter in book.chapters] == [
        "Departure",
        "Arrival",
        "Home",
    ]
    assert "morning train" in book.chapters[0].text
    assert "afternoon station" in book.chapters[1].text
    assert "Evening lights" in book.chapters[2].text
    assert book.chapters[0].source_locator == {"pages": [1, 1]}
    assert book.chapters[2].source_locator == {"pages": [3, 3]}


def test_pdf_without_headings_becomes_single_chapter(tmp_path: Path) -> None:
    pages = [
        [
            "The old house stood beside the river for many years.",
            "Its windows faced the water and the morning sun.",
        ],
        [
            "Travellers often stopped there to ask for directions.",
            "The owner always welcomed them with warm tea.",
        ],
        [
            "In winter the river froze and the road grew quiet.",
            "Still the lamps burned late into the night.",
        ],
    ]
    pdf_path = _make_pdf(tmp_path / "plain.pdf", pages=pages, title="Plain Book")
    book = parse_pdf(pdf_path)
    assert len(book.chapters) == 1
    assert book.chapters[0].title == "Plain Book"
    assert "未识别到章节结构，整本作为一章" in book.chapters[0].warnings
    assert book.chapters[0].source_locator == {"pages": [1, 3]}
    assert "old house stood beside the river" in book.chapters[0].text


def test_pdf_bookmarks_define_chapter_ranges(tmp_path: Path) -> None:
    pages = [
        [
            "Chapter One",
            "The morning train rolled slowly out of the station.",
            "Fields and mist stretched beyond the windows.",
        ],
        [
            "Chapter Two",
            "The afternoon station was crowded with travellers.",
            "We dragged our luggage onto the platform.",
        ],
        [
            "Chapter Three",
            "Evening lights shone over the familiar street.",
            "The journey ended quietly where it had begun.",
        ],
    ]
    pdf_path = _make_pdf(
        tmp_path / "bookmarks.pdf",
        pages=pages,
        title="Bookmarked Book",
        outline=[("Chapter One", 0), ("Chapter Two", 1), ("Chapter Three", 2)],
        footer="RelaxBook sample footer",
    )
    book = parse_pdf(pdf_path)
    assert [chapter.title for chapter in book.chapters] == [
        "Chapter One",
        "Chapter Two",
        "Chapter Three",
    ]
    assert book.chapters[0].source_locator == {"pages": [1, 1]}
    assert book.chapters[1].source_locator == {"pages": [2, 2]}
    assert book.chapters[2].source_locator == {"pages": [3, 3]}
    assert "afternoon station" in book.chapters[1].text
    # 页脚被识别并剔除
    assert any("页眉页脚" in warning for warning in book.warnings)
    assert all("RelaxBook sample footer" not in chapter.text for chapter in book.chapters)


def test_pdf_running_header_equal_to_bookmark_title_is_quiet(tmp_path: Path) -> None:
    """跨页 running header 被剔除后，书签同名标题不应产生“未能定位”噪声告警。"""
    pages = [
        [
            "Chapter One",
            "The first chapter starts on this page with some prose.",
            "It continues with a second sentence.",
        ],
        [
            "Chapter Two",
            "The second chapter begins here on its first page.",
            "Its body continues for a while.",
        ],
        [
            "Chapter Two",
            "The second chapter spills onto a second page here.",
            "The book then ends.",
        ],
    ]
    pdf_path = _make_pdf(
        tmp_path / "running-header.pdf",
        pages=pages,
        title="Running Header Book",
        outline=[("Chapter One", 0), ("Chapter Two", 1)],
        headers=["Chapter One", "Chapter Two", "Chapter Two"],
    )
    book = parse_pdf(pdf_path)
    assert [chapter.title for chapter in book.chapters] == ["Chapter One", "Chapter Two"]
    assert book.chapters[1].source_locator == {"pages": [2, 3]}
    assert "second chapter begins" in book.chapters[1].text
    assert "spills onto a second page" in book.chapters[1].text
    # running header 被正确剔除，且没有“未能定位”噪声告警
    assert any("页眉页脚" in warning for warning in book.warnings)
    assert not any("未能按标题在页内定位" in warning for warning in book.warnings)


def test_pdf_low_text_page_warns(tmp_path: Path) -> None:
    pages = [
        [
            "The first page carries a long paragraph of readable prose.",
            "It continues with a second sentence so the page is not short.",
        ],
        ["Short."],
        [
            "The last page also carries a long paragraph of readable prose.",
            "It ends with another sentence here for the test.",
        ],
    ]
    pdf_path = _make_pdf(tmp_path / "low-text.pdf", pages=pages)
    book = parse_pdf(pdf_path)
    low_warnings = [warning for warning in book.warnings if "文本量偏少" in warning]
    assert low_warnings
    assert "第 2 页" in low_warnings[0]


def test_pdf_without_text_raises_scan_error(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_blank_page(width=300, height=400)
    blank_path = tmp_path / "blank.pdf"
    with blank_path.open("wb") as handle:
        writer.write(handle)

    with pytest.raises(IngestError) as excinfo:
        parse_pdf(blank_path)
    assert "OCR" in str(excinfo.value)
    assert "扫描" in str(excinfo.value)

    # 文本量不足 100 字符同样按扫描件处理
    tiny_path = _make_pdf(tmp_path / "tiny.pdf", pages=[["Only a few words here."]])
    with pytest.raises(IngestError):
        parse_pdf(tiny_path)


# ---------------------------------------------------------------------------
# MOBI / AZW3 → calibre
# ---------------------------------------------------------------------------


def test_mobi_without_calibre_reports_chinese_error(tmp_path: Path, monkeypatch) -> None:
    from relaxbook.ingest import convert

    monkeypatch.setattr(convert, "locate_tool", lambda name: "")
    source = tmp_path / "book.mobi"
    source.write_bytes(b"BOOKMOBI" + b"\x00" * 64)

    with pytest.raises(IngestError) as excinfo:
        parse_book(source, format="mobi", settings=_settings(tmp_path))
    message = str(excinfo.value)
    assert "calibre" in message
    assert "ebook-convert" in message
    assert "MOBI" in message
    assert "AZW3" in message


def test_convert_failure_includes_stderr_tail(tmp_path: Path, monkeypatch) -> None:
    from relaxbook.ingest import convert

    fake_tool = tmp_path / "ebook-convert.exe"
    fake_tool.write_bytes(b"stub")
    monkeypatch.setattr(convert, "locate_tool", lambda name: str(fake_tool))
    monkeypatch.setattr(
        convert.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"calibre boom " * 100
        ),
    )
    source = tmp_path / "broken.mobi"
    source.write_bytes(b"BOOKMOBI" + b"\x00" * 64)
    with pytest.raises(IngestError) as excinfo:
        convert.to_epub(source, _settings(tmp_path), tmp_path / "work")
    message = str(excinfo.value)
    assert "calibre 转换失败" in message
    assert "calibre boom" in message
    assert len(message) < 900


def test_convert_success_returns_work_copy(tmp_path: Path, monkeypatch) -> None:
    from relaxbook.ingest import convert

    fake_tool = tmp_path / "ebook-convert.exe"
    fake_tool.write_bytes(b"stub")
    monkeypatch.setattr(convert, "locate_tool", lambda name: str(fake_tool))
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[2]).write_bytes(b"PK\x03\x04fake-epub")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(convert.subprocess, "run", fake_run)
    source = tmp_path / "book.mobi"
    source.write_bytes(b"BOOKMOBI" + b"\x00" * 64)
    result = convert.to_epub(source, _settings(tmp_path), tmp_path / "work")

    assert result.exists()
    assert result.suffix == ".epub"
    command = captured["command"]
    assert command[0] == str(fake_tool)
    assert command[1] == str(source)
    assert command[3] == "--no-images=false"
    assert captured["kwargs"]["timeout"] == 600
    assert captured["kwargs"]["capture_output"] is True
