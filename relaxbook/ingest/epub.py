"""EPUB 解析：OPF spine 顺序 + EPUB3 nav / EPUB2 NCX 目录与锚点切章。

契约见 ``docs/CONTRACTS.md`` 第 3 节。要点：

* ``META-INF/container.xml`` → OPF → metadata / manifest / spine；
* 目录（EPUB3 nav 优先，其次 EPUB2 NCX）给出标题与片段锚点；
* 同一 XHTML 内不同锚点切章；目录未覆盖的可读正文单独列为待核对章；
* 锚点失效、目录顺序与 spine 冲突、多个目录项指向同一位置时记 warning 并回退按文件切分；
* 用 ``lxml.html`` 提取正文，段落之间保留 ``\\n``，跳过 script/style/nav。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lxml import etree, html

from relaxbook.textnorm import normalize_title

from . import IngestError
from .types import ChapterDraft, ParsedBook

_EPUB_OPS_NS = "http://www.idpf.org/2007/ops"

_SKIP_TAGS = {
    "script",
    "style",
    "nav",
    "head",
    "title",
    "noscript",
    "template",
    "svg",
    "math",
    "form",
    "button",
    "iframe",
    "object",
    "embed",
    "input",
    "select",
    "textarea",
}

_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "pre",
    "td",
    "th",
    "figcaption",
    "dt",
    "dd",
    "caption",
    "address",
    "center",
    "div",
    "section",
    "article",
    "aside",
    "header",
    "footer",
    "figure",
    "main",
    "table",
    "ul",
    "ol",
    "dl",
    "hr",
}

_IMAGE_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
}

_SCAN_ERROR = "未提取到可读正文，疑似扫描件，需要先 OCR。"


@dataclass
class _TocEntry:
    """目录项。``title`` 保留原始文本，``path`` 是相对 OPF 的 ZIP 内路径。"""

    title: str
    href: str
    path: str
    anchor: str | None = None
    spine: int = -1


@dataclass
class _Item:
    """manifest 中的一项。"""

    id: str
    href: str
    path: str
    media_type: str
    properties: str


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_epub(path: Path) -> ParsedBook:
    """解析 EPUB 文件，返回书名、作者、章节草稿与封面。"""
    source = Path(path)
    if not source.exists():
        raise IngestError(f"EPUB 文件不存在：{source.name}")
    try:
        with zipfile.ZipFile(source) as zf:
            return _parse_zip(zf, source)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"无法打开 EPUB，文件不是有效的 ZIP 包：{source.name}") from exc
    except OSError as exc:
        raise IngestError(f"无法读取 EPUB 文件：{source.name}（{exc}）") from exc


def _parse_zip(zf: zipfile.ZipFile, source: Path) -> ParsedBook:
    warnings: list[str] = []
    lookup = _name_lookup(zf.namelist())
    opf_path = _find_opf(zf, lookup, warnings)
    opf_dir = posixpath.dirname(opf_path)
    items, metadata = _parse_opf(zf, lookup, opf_path, warnings)
    spine_items = _parse_spine(items, metadata.get("spine_ids", []), warnings)

    readable: list[tuple[int, _Item]] = []
    for index, item in enumerate(spine_items):
        media = item.media_type
        is_text = media == "" or "html" in media or media in ("application/xml", "text/xml")
        if not is_text:
            warnings.append(f"正文顺序中的非文本项目已跳过：{item.href}")
            continue
        readable.append((index, item))
    readable_index = {index for index, _ in readable}

    toc_entries = _read_toc(zf, lookup, items, metadata.get("toc_id"), warnings)
    if not toc_entries:
        warnings.append("未找到目录（EPUB3 nav 或 EPUB2 NCX），所有正文按文件切分。")
    by_spine = _map_toc_to_spine(toc_entries, spine_items, readable_index, warnings)

    chapters: list[ChapterDraft] = []
    for index, item in readable:
        doc = _load_document(zf, lookup, item.path, item.href, warnings)
        if doc is None:
            continue
        if not doc.text.strip():
            if index in by_spine:
                warnings.append(f"目录项指向的正文为空：{item.href}")
            continue
        chapters.extend(_build_chapters(doc, item, by_spine.get(index, []), warnings))

    title = _collapse(metadata.get("title") or "") or source.stem
    author = _collapse(metadata.get("author") or "") or None
    cover_bytes, cover_ext = _read_cover(zf, lookup, items, metadata, warnings)

    if not any(chapter.text.strip() for chapter in chapters):
        raise IngestError(_SCAN_ERROR)

    return ParsedBook(
        title=title,
        author=author,
        format="epub",
        chapters=chapters,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# ZIP / OPF 基础读取
# ---------------------------------------------------------------------------


def _name_lookup(names: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for name in names:
        lookup[name] = name
        lookup.setdefault(unquote(name), name)
    return lookup


def _read_bytes(zf: zipfile.ZipFile, lookup: dict[str, str], name: str) -> bytes:
    real = lookup.get(name)
    if real is None:
        real = lookup.get(unquote(name))
    if real is None:
        raise KeyError(name)
    return zf.read(real)


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        recover=True,
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )


def _find_opf(zf: zipfile.ZipFile, lookup: dict[str, str], warnings: list[str]) -> str:
    container_name = "META-INF/container.xml"
    if container_name in lookup:
        try:
            root = etree.fromstring(
                _read_bytes(zf, lookup, container_name), parser=_xml_parser()
            )
            for element in root.iter():
                if _local(element.tag) != "rootfile":
                    continue
                full_path = _attr(element, "full-path")
                if not full_path:
                    continue
                candidate = unquote(full_path).lstrip("/")
                if candidate in lookup:
                    return lookup[candidate]
                if candidate in zf.namelist():
                    return candidate
            warnings.append("container.xml 中的 OPF 路径无效，改为自动查找 OPF。")
        except (etree.XMLSyntaxError, ValueError, KeyError):
            warnings.append("META-INF/container.xml 解析失败，改为自动查找 OPF。")

    candidates = [
        name
        for name in zf.namelist()
        if name.lower().endswith(".opf") and not name.startswith("META-INF/")
    ]
    if not candidates:
        raise IngestError("EPUB 缺少 OPF 文件，无法解析。")
    candidates.sort(key=lambda name: (name.count("/"), name))
    return candidates[0]


def _parse_opf(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    opf_path: str,
    warnings: list[str],
) -> tuple[dict[str, _Item], dict]:
    try:
        root = etree.fromstring(
            _read_bytes(zf, lookup, opf_path), parser=_xml_parser()
        )
    except (etree.XMLSyntaxError, ValueError, KeyError) as exc:
        raise IngestError(f"EPUB 的 OPF 文件无法解析：{opf_path}") from exc
    opf_dir = posixpath.dirname(opf_path)

    items: dict[str, _Item] = {}
    title = ""
    author = ""
    cover_id = ""
    toc_id = ""
    spine_ids: list[str] = []

    for element in root.iter():
        tag = _local(element.tag)
        if tag == "item":
            item_id = _attr(element, "id")
            href = _attr(element, "href")
            if not item_id or not href:
                continue
            items[item_id] = _Item(
                id=item_id,
                href=href,
                path=_resolve_path(opf_dir, href),
                media_type=(_attr(element, "media-type") or "").lower(),
                properties=(_attr(element, "properties") or "").lower(),
            )
        elif tag == "title" and not title:
            text = _element_text(element)
            if text:
                title = text
        elif tag == "creator" and not author:
            text = _element_text(element)
            if text:
                author = text
        elif tag == "meta":
            if (_attr(element, "name") or "").lower() == "cover":
                cover_id = (_attr(element, "content") or "").strip()
        elif tag == "spine":
            toc_id = (_attr(element, "toc") or "").strip()
            for child in element:
                if _local(child.tag) == "itemref":
                    idref = (_attr(child, "idref") or "").strip()
                    if idref:
                        spine_ids.append(idref)
            if not spine_ids:
                for descendant in element.iter():
                    if _local(descendant.tag) == "itemref":
                        idref = (_attr(descendant, "idref") or "").strip()
                        if idref:
                            spine_ids.append(idref)

    if not items:
        warnings.append("EPUB 的 manifest 为空。")
    metadata = {
        "title": title,
        "author": author,
        "cover_id": cover_id,
        "toc_id": toc_id,
        "spine_ids": spine_ids,
    }
    return items, metadata


def _parse_spine(
    items: dict[str, _Item], spine_ids: list[str], warnings: list[str]
) -> list[_Item]:
    spine: list[_Item] = []
    for idref in spine_ids:
        item = items.get(idref)
        if item is None:
            warnings.append(f"spine 引用了不存在的 manifest 项目：{idref}")
            continue
        spine.append(item)
    return spine


# ---------------------------------------------------------------------------
# 目录：EPUB3 nav / EPUB2 NCX
# ---------------------------------------------------------------------------


def _read_toc(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    items: dict[str, _Item],
    toc_id: str,
    warnings: list[str],
) -> list[_TocEntry]:
    nav_item = next(
        (
            item
            for item in items.values()
            if "nav" in item.properties.split()
        ),
        None,
    )
    entries: list[_TocEntry] = []
    if nav_item is not None:
        entries = _parse_nav(zf, lookup, nav_item, warnings)
    if entries:
        return entries

    ncx_item = items.get(toc_id) if toc_id else None
    if ncx_item is None:
        ncx_item = next(
            (
                item
                for item in items.values()
                if item.media_type == "application/x-dtbncx+xml"
            ),
            None,
        )
    if ncx_item is not None:
        entries = _parse_ncx(zf, lookup, ncx_item, warnings)
    return entries


def _parse_nav(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    nav_item: _Item,
    warnings: list[str],
) -> list[_TocEntry]:
    try:
        root = etree.fromstring(
            _read_bytes(zf, lookup, nav_item.path), parser=_xml_parser()
        )
    except (etree.XMLSyntaxError, ValueError, KeyError):
        warnings.append("EPUB3 nav 目录解析失败，改为尝试 EPUB2 NCX 目录。")
        return []

    nav_element = None
    for element in root.iter():
        if _local(element.tag) != "nav":
            continue
        if (_attr(element, "type") or "").lower() == "toc":
            nav_element = element
            break
        if nav_element is None:
            nav_element = element
    if nav_element is None:
        return []

    base_dir = posixpath.dirname(nav_item.path)
    entries: list[_TocEntry] = []

    def walk_list(ol_element) -> None:
        for li in ol_element:
            if _local(li.tag) != "li":
                continue
            label = None
            for child in li:
                if _local(child.tag) in ("a", "span"):
                    label = child
                    break
            if label is None:
                for descendant in li.iterdescendants():
                    if _local(descendant.tag) in ("a", "span"):
                        label = descendant
                        break
            href = None
            title = ""
            if label is not None:
                title = _element_text(label)
                if _local(label.tag) == "a":
                    href = _attr(label, "href")
                if not href:
                    anchor = next(
                        (
                            descendant
                            for descendant in label.iter()
                            if _local(descendant.tag) == "a"
                        ),
                        None,
                    )
                    if anchor is not None:
                        href = _attr(anchor, "href")
            if title or href:
                target_path, target_anchor = _split_href(base_dir, href)
                if target_path:
                    entries.append(
                        _TocEntry(
                            title=title,
                            href=href or "",
                            path=target_path,
                            anchor=target_anchor,
                        )
                    )
            for child in li:
                if _local(child.tag) == "ol":
                    walk_list(child)

    direct_lists = [child for child in nav_element if _local(child.tag) == "ol"]
    if direct_lists:
        for ol_element in direct_lists:
            walk_list(ol_element)
    else:
        for descendant in nav_element.iter():
            if _local(descendant.tag) == "ol":
                walk_list(descendant)
                break
    return entries


def _parse_ncx(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    ncx_item: _Item,
    warnings: list[str],
) -> list[_TocEntry]:
    try:
        root = etree.fromstring(
            _read_bytes(zf, lookup, ncx_item.path), parser=_xml_parser()
        )
    except (etree.XMLSyntaxError, ValueError, KeyError):
        warnings.append("EPUB2 NCX 目录解析失败。")
        return []

    nav_map = None
    for element in root.iter():
        if _local(element.tag) == "navmap":
            nav_map = element
            break
    if nav_map is None:
        return []

    base_dir = posixpath.dirname(ncx_item.path)
    entries: list[_TocEntry] = []

    def walk(nav_point) -> None:
        for child in nav_point:
            if _local(child.tag) != "navpoint":
                continue
            title = ""
            content_src = None
            for sub in child:
                tag = _local(sub.tag)
                if tag == "navlabel" and not title:
                    for text_element in sub.iter():
                        if _local(text_element.tag) == "text":
                            title = _element_text(text_element)
                            break
                elif tag == "content" and content_src is None:
                    content_src = _attr(sub, "src")
            if content_src:
                target_path, target_anchor = _split_href(base_dir, content_src)
                if target_path:
                    entries.append(
                        _TocEntry(
                            title=title,
                            href=content_src,
                            path=target_path,
                            anchor=target_anchor,
                        )
                    )
            walk(child)

    walk(nav_map)
    return entries


def _map_toc_to_spine(
    toc_entries: list[_TocEntry],
    spine_items: list[_Item],
    readable_index: set[int],
    warnings: list[str],
) -> dict[int, list[_TocEntry]]:
    spine_by_path: dict[str, int] = {}
    for index, item in enumerate(spine_items):
        spine_by_path.setdefault(item.path, index)
    lower_paths = {path.lower(): index for path, index in spine_by_path.items()}

    mapped: dict[int, list[_TocEntry]] = {}
    positions: list[int] = []
    for entry in toc_entries:
        index = spine_by_path.get(entry.path)
        if index is None:
            index = lower_paths.get(entry.path.lower())
        if index is None:
            warnings.append(f"目录项未对应到正文顺序，已跳过：{entry.href or entry.path}")
            continue
        if index not in readable_index:
            warnings.append(f"目录项指向的正文不是 HTML/XHTML，已跳过：{entry.href}")
            continue
        entry.spine = index
        mapped.setdefault(index, []).append(entry)
        positions.append(index)

    seen_max = -1
    for position in positions:
        if position < seen_max:
            warnings.append("目录顺序与正文顺序冲突，已按正文顺序排列章节。")
            break
        seen_max = max(seen_max, position)
    return mapped


# ---------------------------------------------------------------------------
# 正文提取
# ---------------------------------------------------------------------------


class _Document:
    """一个 XHTML 文件的正文、段落偏移与锚点索引。"""

    __slots__ = ("text", "_elements", "_order", "_para_by_order", "_para_keys", "anchors")

    def __init__(self, root) -> None:
        self._elements = list(root.iter())
        self._order = {id(element): index for index, element in enumerate(self._elements)}

        paragraphs: list[tuple[int, str]] = []

        def walk(element) -> None:
            tag = _local(element.tag)
            if tag in _SKIP_TAGS:
                return
            if _is_leaf_block(element):
                text = _clean_paragraph(element.text_content())
                if text:
                    paragraphs.append((self._order[id(element)], text))
                return
            if element.text and element.text.strip():
                text = _clean_paragraph(element.text)
                if text:
                    paragraphs.append((self._order[id(element)], text))
            for child in element:
                walk(child)
                if child.tail and child.tail.strip():
                    text = _clean_paragraph(child.tail)
                    if text:
                        paragraphs.append((self._order[id(child)], text))

        walk(root)
        paragraphs.sort(key=lambda item: item[0])

        self.text = "\n".join(text for _, text in paragraphs)
        self._para_by_order: dict[int, int] = {}
        offset = 0
        for order_index, text in paragraphs:
            self._para_by_order[order_index] = offset
            offset += len(text) + 1
        self._para_keys = sorted(self._para_by_order)

        self.anchors: dict[str, object] = {}
        for element in self._elements:
            identifier = element.get("id")
            if identifier and identifier not in self.anchors:
                self.anchors[identifier] = element
            if _local(element.tag) == "a":
                name = element.get("name")
                if name and name not in self.anchors:
                    self.anchors[name] = element

    def anchor_offset(self, element) -> int:
        """锚点对应的正文偏移；找不到时返回文件末尾。"""
        node = element
        while node is not None:
            index = self._order.get(id(node))
            if index is not None and index in self._para_by_order:
                return self._para_by_order[index]
            node = node.getparent()
        index = self._order.get(id(element))
        if index is None:
            return len(self.text)
        for order_index in self._para_keys:
            if order_index >= index:
                return self._para_by_order[order_index]
        return len(self.text)


def _load_document(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    path: str,
    href: str,
    warnings: list[str],
) -> _Document | None:
    try:
        data = _read_bytes(zf, lookup, path)
    except KeyError:
        warnings.append(f"EPUB 内缺少正文文件，已跳过：{href}")
        return None
    try:
        root = html.document_fromstring(data)
    except (etree.ParserError, ValueError):
        warnings.append(f"正文文件无法解析，已跳过：{href}")
        return None
    if root is None:
        warnings.append(f"正文文件内容为空，已跳过：{href}")
        return None
    document = _Document(root)
    if not document.text.strip():
        return document
    return document


def _build_chapters(
    document: _Document,
    item: _Item,
    entries: list[_TocEntry],
    warnings: list[str],
) -> list[ChapterDraft]:
    text = document.text
    href = item.href
    chapters: list[ChapterDraft] = []

    if not entries:
        return [
            ChapterDraft(
                title=_guess_title(text),
                text=text,
                source_locator={"href": href},
                warnings=["目录未覆盖，按正文补入"],
            )
        ]

    resolved: list[tuple[int, _TocEntry]] = []
    fallback = False
    fallback_reason = ""
    for entry in entries:
        if entry.anchor:
            anchor_element = document.anchors.get(entry.anchor)
            if anchor_element is None:
                fallback = True
                fallback_reason = f"目录锚点失效：{href}#{entry.anchor}"
                warnings.append(f"目录锚点失效：{href}#{entry.anchor}（{entry.title or '未命名'}），已回退为按文件切分。")
                continue
            offset = document.anchor_offset(anchor_element)
            if offset >= len(text):
                fallback = True
                fallback_reason = f"目录锚点定位到文件末尾：{href}#{entry.anchor}"
                warnings.append(f"目录锚点定位到正文末尾：{href}#{entry.anchor}（{entry.title or '未命名'}），已回退为按文件切分。")
                continue
        else:
            offset = 0
        resolved.append((offset, entry))

    if not fallback:
        resolved.sort(key=lambda pair: pair[0])
        for index in range(1, len(resolved)):
            if resolved[index][0] == resolved[index - 1][0]:
                fallback = True
                fallback_reason = f"多个目录项指向同一位置：{href}"
                warnings.append(
                    f"多个目录项指向同一位置（{href}），已回退为按文件切分。"
                )
                break

    if fallback:
        title = normalize_title(entries[0].title) if entries else ""
        return [
            ChapterDraft(
                title=title,
                text=text,
                source_locator={"href": href},
                warnings=["目录锚点失效或重复，已按文件切分"],
            )
        ]

    if resolved[0][0] > 0:
        leading = text[: resolved[0][0]].strip()
        if leading:
            chapters.append(
                ChapterDraft(
                    title=_guess_title(leading),
                    text=leading,
                    source_locator={"href": href},
                    warnings=["目录未覆盖，按正文补入"],
                )
            )

    for index, (offset, entry) in enumerate(resolved):
        end = resolved[index + 1][0] if index + 1 < len(resolved) else len(text)
        chunk = text[offset:end].strip()
        if not chunk:
            warnings.append(f"目录项未覆盖到正文：{entry.title or '未命名'}（{href}）")
            continue
        locator: dict = {"href": href}
        if entry.anchor:
            locator["anchor"] = entry.anchor
        chapters.append(
            ChapterDraft(
                title=normalize_title(entry.title),
                text=chunk,
                source_locator=locator,
            )
        )
    if not chapters:
        chapters.append(
            ChapterDraft(
                title=normalize_title(entries[0].title),
                text=text,
                source_locator={"href": href},
                warnings=["目录项未覆盖到正文，按文件切分"],
            )
        )
    return chapters


def _guess_title(text: str) -> str:
    """从正文首行猜一个标题，用于目录未覆盖的待核对章节。"""
    first = (text or "").strip().split("\n", 1)[0]
    first = re.sub(r"\s+", " ", first).strip()
    if 0 < len(first) <= 40:
        return normalize_title(first)
    return normalize_title("")


# ---------------------------------------------------------------------------
# 封面
# ---------------------------------------------------------------------------


def _read_cover(
    zf: zipfile.ZipFile,
    lookup: dict[str, str],
    items: dict[str, _Item],
    metadata: dict,
    warnings: list[str],
) -> tuple[bytes | None, str]:
    cover_item = items.get(metadata.get("cover_id") or "")
    if cover_item is None:
        cover_item = next(
            (
                item
                for item in items.values()
                if "cover-image" in item.properties.split()
            ),
            None,
        )
    if cover_item is None:
        for item in items.values():
            if "cover" in item.id.lower() and item.media_type.startswith("image/"):
                cover_item = item
                break
    if cover_item is None:
        return None, "jpg"

    is_image = cover_item.media_type.startswith("image/")
    suffix = posixpath.splitext(cover_item.path)[1].lower().lstrip(".")
    if not is_image and suffix not in _IMAGE_EXT.values():
        return None, "jpg"
    try:
        data = _read_bytes(zf, lookup, cover_item.path)
    except KeyError:
        warnings.append(f"封面文件缺失：{cover_item.href}")
        return None, "jpg"
    if not data:
        return None, "jpg"
    extension = _IMAGE_EXT.get(cover_item.media_type, "").lower()
    if not extension:
        extension = "jpg" if suffix == "jpeg" else (suffix or "jpg")
        if extension not in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tiff"}:
            extension = "jpg"
    if extension == "jpeg":
        extension = "jpg"
    return data, extension


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1].lower()
    if ":" in tag:
        return tag.rsplit(":", 1)[1].lower()
    return tag.lower()


def _attr(element, name: str) -> str | None:
    value = element.get(name)
    if value is not None:
        return value
    for key, candidate in element.attrib.items():
        if _local(key) == name:
            return candidate
    return None


def _element_text(element) -> str:
    return _collapse("".join(element.itertext()))


def _collapse(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_paragraph(text: str | None) -> str:
    return _collapse(text or "")


def _is_leaf_block(element) -> bool:
    if _local(element.tag) not in _BLOCK_TAGS:
        return False
    for descendant in element.iterdescendants():
        if _local(descendant.tag) in _BLOCK_TAGS:
            return False
    return True


def _resolve_path(base_dir: str, href: str) -> str:
    href = unquote((href or "").strip()).replace("\\", "/")
    if not href:
        return ""
    joined = posixpath.join(base_dir, href) if base_dir else href
    return posixpath.normpath(joined).lstrip("/")


def _split_href(base_dir: str, href: str | None) -> tuple[str, str | None]:
    if not href:
        return "", None
    href = href.strip()
    lowered = href.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "javascript:")):
        return "", None
    path_part = href
    anchor = None
    if "#" in href:
        path_part, fragment = href.split("#", 1)
        anchor = unquote(fragment).strip() or None
    if not path_part:
        return "", anchor
    return _resolve_path(base_dir, path_part), anchor
