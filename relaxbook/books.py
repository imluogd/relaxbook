import posixpath
import re
import shutil
import subprocess
import zipfile
from collections import Counter, defaultdict
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from defusedxml.ElementTree import fromstring


def chapter(title, text, source):
    return {"title": title.strip(), "text": text.strip(), "source": source, "skip": False}


def epub(path, converted=False):
    with zipfile.ZipFile(path) as archive:
        if sum(item.file_size for item in archive.infolist()) > 512 * 1024**2:
            raise ValueError("EPUB 解压后的体积超过 512 MB。")
        container = fromstring(archive.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").attrib["full-path"]
        opf = fromstring(archive.read(opf_path))

        def target(base, href):
            url = urlsplit(href)
            if url.scheme or url.netloc:
                return "", ""
            name = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(url.path)))
            return (name if url.path else base), unquote(url.fragment)

        manifest = {
            item.attrib["id"]: (target(opf_path, item.attrib["href"])[0], item.attrib)
            for item in opf.findall(".//{*}manifest/{*}item")
        }
        nav = next((name for name, attrs in manifest.values()
                    if "nav" in attrs.get("properties", "").split()), None)
        toc = defaultdict(list)
        if nav:
            soup = BeautifulSoup(archive.read(nav), "html.parser")
            menu = next((tag for tag in soup.find_all("nav")
                         if "toc" in tag.get("epub:type", "").split()
                         or tag.get("role") == "doc-toc"), None)
            if menu:
                for link in menu.find_all("a", href=True):
                    name, anchor = target(nav, link["href"])
                    toc[name].append((anchor, link.get_text(" ", strip=True)))
        spine = opf.find("{*}spine")
        ncx = manifest.get(spine.get("toc"), (None, {}))[0]
        if not toc and ncx:
            for point in fromstring(archive.read(ncx)).findall(".//{*}navPoint"):
                src = point.find("{*}content")
                if src is not None:
                    name, anchor = target(ncx, src.get("src", ""))
                    toc[name].append((anchor, point.findtext("{*}navLabel/{*}text", "")))

        chapters, warnings = [], []
        for item in spine.findall("{*}itemref"):
            name, _ = manifest[item.attrib["idref"]]
            if name == nav:
                continue
            soup = BeautifulSoup(archive.read(name), "html.parser")
            body = soup.body or soup
            for tag in body.find_all(["script", "style", "svg", "nav"]):
                tag.decompose()
            if body.get("id") == "calibre_generated_inline_toc" or (
                converted and is_inline_toc(body, name, target, set(x[0] for x in manifest.values()))
            ):
                item = chapter("目录", body.get_text("\n", strip=True), name)
                item["skip"] = True
                chapters.append(item)
                warnings.append(f"已跳过书内目录页，可在章节编辑器恢复朗读：{name}")
                continue
            events = list(body.descendants)
            positions = {}
            for i, node in enumerate(events):
                if isinstance(node, Tag):
                    anchor = node.get("id") or (node.get("name") if node.name == "a" else None)
                    if anchor:
                        positions.setdefault(anchor, i)
            boundaries = {}
            source_order = []
            for anchor, title in toc[name]:
                index = positions.get(anchor) if anchor else 0
                if index is None or index in boundaries:
                    warnings.append(f"请核对目录项「{title}」的位置：{name}" + (f"#{anchor}" if anchor else ""))
                    continue
                boundaries[index] = (title, anchor)
                source_order.append(index)
            if source_order != sorted(source_order):
                warnings.append(f"目录顺序与正文不同，已采用正文顺序：{name}")
            heading = body.find(re.compile(r"^h[1-6]$"))
            fallback = heading.get_text(" ", strip=True) if heading else f"待核对第 {len(chapters)+1} 段"
            boundaries.setdefault(0, (fallback, ""))
            indexes = sorted(boundaries)
            for start, end in zip(indexes, indexes[1:] + [len(events)]):
                title, anchor = boundaries[start]
                tokens = []
                for node in events[start:end]:
                    if isinstance(node, NavigableString) and node.name is None:
                        if node.__class__ is NavigableString:
                            tokens.append(re.sub(r"\s+", " ", str(node)))
                    elif isinstance(node, Tag) and node.name in {
                        "p", "div", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
                    }:
                        tokens.append("\n")
                content = "\n".join(s.strip() for s in "".join(tokens).splitlines() if s.strip())
                if content:
                    chapters.append(chapter(title, content, name + (f"#{anchor}" if anchor else "")))
                    if start == 0 and not anchor and (0 not in source_order):
                        warnings.append(f"请核对未被目录覆盖的正文：{name}")
                elif start in source_order:
                    warnings.append(f"目录项「{title}」没有可朗读正文：{name}")
        author = opf.findtext(".//{*}metadata/{*}creator", "")
        title = opf.findtext(".//{*}metadata/{*}title", "")
        cover_id = next((item.get("content") for item in opf.findall(".//{*}meta")
                         if item.get("name") == "cover"), None)
        cover = next((name for name, attrs in manifest.values()
                      if "cover-image" in attrs.get("properties", "").split()), None)
        cover = cover or manifest.get(cover_id, (None, {}))[0]
        cover_bytes = None
        if cover and cover in archive.namelist() and archive.getinfo(cover).file_size < 8 * 1024**2:
            raw = archive.read(cover)
            if raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                cover_bytes = raw
        return title, author, chapters, warnings, cover_bytes


def is_inline_toc(body, name, target, manifest_paths):
    links = body.find_all("a", href=True)
    if len(links) < 2 or any(target(name, link["href"])[0] not in manifest_paths for link in links):
        return False
    # Compare against a copy so the review draft can retain the original text.
    copy = BeautifulSoup(str(body), "html.parser")
    for link in copy.find_all("a", href=True):
        link.decompose()
    remainder = re.sub(r"\s+", " ", copy.get_text(" ", strip=True)).casefold()
    return remainder in {"table of contents", "contents", "目录", "目 录"}


def pdf(path):
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("文件受密码或权限保护，请导入可读取的 PDF。")
    warnings, pages = [], []
    with pdfplumber.open(path) as book:
        for i, page in enumerate(book.pages, 1):
            try:
                lines = (page.extract_text() or "").strip().splitlines()
                left, right = page.width * 0.48, page.width * 0.52
                words = page.extract_words()
                left_words = [w for w in words if w["x1"] < left]
                right_words = [w for w in words if w["x0"] > right]
                middle_words = [w for w in words if w["x0"] <= right and w["x1"] >= left]
                if min(len(left_words), len(right_words)) > 25 and len(middle_words) < 3:
                    warnings.append(f"第 {i} 页疑似多栏，请核对朗读顺序。")
                if len("".join(lines)) < 20:
                    warnings.append(f"第 {i} 页文本较少，可能为扫描页，请核对。")
                pages.append(lines)
            finally:
                page.close()
    if not any(pages):
        raise ValueError("未找到文本。扫描 PDF 需要先完成 OCR。")
    headers = Counter(lines[0] for lines in pages if len(lines) >= 4)
    footers = Counter(lines[-1] for lines in pages if len(lines) >= 4)
    for i, lines in enumerate(pages):
        if len(lines) >= 4:
            if headers[lines[0]] >= max(3, len(pages)*0.6):
                lines = lines[1:]
            if footers[lines[-1]] >= max(3, len(pages)*0.6) or re.fullmatch(r"\d{1,4}", lines[-1]):
                lines = lines[:-1]
            pages[i] = lines

    def bookmarks(nodes):
        for node in nodes:
            if isinstance(node, list):
                yield from bookmarks(node)
            else:
                index = reader.get_destination_page_number(node)
                if index is not None and 0 <= index < len(pages):
                    yield index, node.title.strip()

    chapter_starts = {}
    for index, title in bookmarks(reader.outline):
        if index < 0:
            continue
        # Whitespace varies between extracted text and PDF bookmarks.
        key = re.sub(r"\s+", "", title)
        matches = [i for i, line in enumerate(pages[index]) if re.sub(r"\s+", "", line) == key]
        offset = matches[0] if len(matches) == 1 else 0
        if (index, offset) in chapter_starts:
            warnings.append(f"第 {index+1} 页的书签「{title}」无法独立定位，请手动拆分。")
            continue
        chapter_starts[index, offset] = title
    if not chapter_starts:
        heading = re.compile(r"^(第[一二三四五六七八九十百零〇\d]+[章节回卷篇]|Chapter\s+\d+).{0,55}$", re.I)
        chapter_starts = {(i, j): line for i, lines in enumerate(pages) for j, line in enumerate(lines)
                          if heading.fullmatch(line)}
        if not chapter_starts:
            chapter_starts = {(i, 0): f"第 {i+1} 页" for i, lines in enumerate(pages) if lines}
            warnings.append("未找到可定位的章节，已按页分段，可在网页合并或拆分。")
    chapter_starts.setdefault((0, 0), "待核对正文")
    result = []
    title, source, lines = "", "", []
    for i, page in enumerate(pages):
        for j, line in enumerate(page):
            if (i, j) in chapter_starts or not title:
                if lines:
                    result.append(chapter(title, "\n".join(lines), source))
                title = chapter_starts.get((i, j), "待核对正文")
                source, lines = f"第 {i+1} 页，第 {j+1} 行", []
            lines.append(line)
        if lines:
            lines.append("")
    if lines:
        result.append(chapter(title, "\n".join(lines), source))
    metadata = reader.metadata
    return str(metadata.title or "") if metadata else "", str(metadata.author or "") if metadata else "", result, warnings, None


def parse(path, calibre="ebook-convert"):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf(path)
    if suffix in {".mobi", ".azw3"}:
        converter = shutil.which(calibre)
        if not converter:
            raise ValueError("需要 calibre 的 ebook-convert 才能导入此格式。")
        output = path.parent / "converted.epub"
        result = subprocess.run([converter, str(path), str(output)], capture_output=True, timeout=900)
        if result.returncode:
            raise ValueError("calibre 转换失败，请检查源文件是否损坏或受保护。")
        path = output
    return epub(path, converted=suffix in {".mobi", ".azw3"})
