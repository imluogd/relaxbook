"""Generate redistributable sample books used by RelaxBook acceptance checks.

Everything here is originally written for this project (no third-party text), so
the generated files can be committed to the repository.

Outputs (written to ``sample/`` next to the repository root):

* ``示例书.epub``      three chapters titled 序言 / 出发 / 出发 (EPUB3 nav + EPUB2 NCX)
* ``预期清单.md``      titles, first/last/middle sentences to check the produced audio against
* ``示例书.pdf``       text PDF **with** bookmarks
* ``扫描示例.pdf``     image-only PDF (must be reported as needing OCR)
* ``sample-en.epub``   small English EPUB

Usage::

    python tools/make_samples.py [--out sample]
"""

from __future__ import annotations

import argparse
import io
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "sample"

BOOK_TITLE = "示例书"
BOOK_AUTHOR = "示例作者"
BOOK_LANG = "zh-CN"

# (title, paragraphs) - first sentence / last sentence / middle sentence are
# pinned by docs/ACCEPTANCE.md and must not change without updating the checklist.
CHAPTERS: list[tuple[str, list[str]]] = [
    (
        "序言",
        [
            "清晨的列车缓缓驶出站台，车厢里弥漫着咖啡与旧报纸的气味。他靠窗坐下，把行李放在脚边。",
            "窗外的田野一片接一片地退后，远处的山脊被薄雾切成几段。这是他第一次独自离开家乡，去一座从未去过的城市。",
            "他在笔记本上写下第一行字：把路上的每一件事都记下来。",
        ],
    ),
    (
        "出发",
        [
            "午后的车站比想象中安静，只有广播反复念着晚点的车次。他坐在长椅上，把车票翻来覆去地看，确认日期和座位号都没有错。",
            "月台上有人拖着行李箱快步走过，轮子在地面上发出细碎的声响。他想起母亲临行前塞给他的那袋橘子，还放在背包最上面。",
            "检票口开了，人群像潮水一样往前涌。他随着人流走进车厢，找到自己的座位，把背包抱在怀里，闭上眼睛听车轮开始转动。",
        ],
    ),
    (
        "出发",
        [
            "傍晚的灯光从车窗斜斜地照进来，把整节车厢染成橘色。他睁开眼，发现邻座的老人正在削苹果，果皮一圈一圈地落进纸袋里。",
            "老人问他去哪里，他说了一个地名，老人笑起来，说自己也曾在那个地方住过三年。两个人聊起那条河、那座桥，还有冬天里总也化不掉的雪。",
            "天色彻底暗下来的时候，老人把削好的苹果递给他一半。他咬了一口，甜得有些意外。列车在黑暗里继续往前，只有车窗上映出他们模糊的影子。",
            "到站前，老人先下车了，临别时拍了拍他的肩膀，说路上小心。他点点头，看着那道背影消失在站台尽头，忽然觉得这趟旅程比自己以为的要长，"
            "也没有想象中那么孤单。",
        ],
    ),
]

EN_CHAPTERS: list[tuple[str, list[str]]] = [
    (
        "The Quiet Station",
        [
            "The train left the platform at dawn, and the carriage smelled of coffee and old newspapers.",
            "He watched the fields slide past the window and wrote his first note in a small notebook.",
        ],
    ),
    (
        "A Long Way Home",
        [
            "By evening the light had turned orange, and an old man beside him was peeling an apple.",
            "They talked about a river, a bridge, and the snow that never seemed to melt.",
        ],
    ),
]


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------
def _xhtml(title: str, paragraphs: list[str], lang: str = BOOK_LANG) -> str:
    body = "\n".join(f"    <p>{p}</p>" for p in paragraphs)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
  <head>
    <meta charset="utf-8" />
    <title>{title}</title>
  </head>
  <body>
    <h1>{title}</h1>
{body}
  </body>
</html>
"""


def build_epub(out: Path, *, title: str, author: str, lang: str,
               chapters: list[tuple[str, list[str]]], book_id: str) -> None:
    files: dict[str, bytes] = {}
    manifest_items: list[str] = []
    spine_items: list[str] = []
    nav_entries: list[str] = []
    ncx_points: list[str] = []

    for i, (ch_title, paragraphs) in enumerate(chapters, start=1):
        href = f"ch{i:03d}.xhtml"
        files[f"OEBPS/{href}"] = _xhtml(ch_title, paragraphs, lang).encode("utf-8")
        manifest_items.append(
            f'    <item id="ch{i:03d}" href="{href}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'    <itemref idref="ch{i:03d}"/>')
        nav_entries.append(f'      <li><a href="{href}">{ch_title}</a></li>')
        ncx_points.append(
            f"""    <navPoint id="np{i:03d}" playOrder="{i}">
      <navLabel><text>{ch_title}</text></navLabel>
      <content src="{href}"/>
    </navPoint>"""
        )

    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"
      xml:lang="{lang}" lang="{lang}">
  <head><meta charset="utf-8"/><title>目录</title></head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>目录</h1>
      <ol>
{chr(10).join(nav_entries)}
      </ol>
    </nav>
  </body>
</html>
"""
    files["OEBPS/nav.xhtml"] = nav.encode("utf-8")
    manifest_items.append(
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    )

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{book_id}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
{chr(10).join(ncx_points)}
  </navMap>
</ncx>
"""
    files["OEBPS/toc.ncx"] = ncx.encode("utf-8")
    manifest_items.append(
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    )

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{lang}</dc:language>
    <meta property="dcterms:modified">2026-11-22T00:00:00Z</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine_items)}
  </spine>
</package>
"""
    files["OEBPS/content.opf"] = opf.encode("utf-8")
    files["META-INF/container.xml"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    ).encode("utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as zf:
        # mimetype must be the first entry and stored uncompressed
        zf.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", zipfile.ZIP_STORED)
        for name, data in files.items():
            zf.writestr(name, data, zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _find_cjk_font() -> Path | None:
    candidates = [
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
        Path(r"C:\Windows\Fonts\Deng.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_text_pdf(out: Path) -> None:
    from fpdf import FPDF

    font = _find_cjk_font()
    if font is None:
        raise SystemExit("未找到中文字体，无法生成示例 PDF")

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("cjk", "", str(font))
    pdf.set_font("cjk", size=12)

    # title page
    pdf.add_page()
    pdf.set_font("cjk", size=24)
    pdf.cell(0, 20, BOOK_TITLE, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("cjk", size=14)
    pdf.cell(0, 12, BOOK_AUTHOR, new_x="LMARGIN", new_y="NEXT")

    chapter_pages: list[tuple[str, int]] = []
    for ch_title, paragraphs in CHAPTERS:
        pdf.add_page()
        chapter_pages.append((ch_title, pdf.page_no() - 1))
        pdf.set_font("cjk", size=20)
        pdf.cell(0, 16, ch_title, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font("cjk", size=12)
        for p in paragraphs:
            pdf.multi_cell(0, 8, p)
            pdf.ln(3)

    pdf.output(str(out))

    # add bookmarks (PDF outline) with pypdf
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(out))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_outline_item(title=BOOK_TITLE, page_number=0)
    for ch_title, page_index in chapter_pages:
        writer.add_outline_item(title=ch_title, page_number=page_index)
    with open(out, "wb") as fh:
        writer.write(fh)


def build_scanned_pdf(out: Path, pages: int = 3) -> None:
    from PIL import Image, ImageDraw

    images = []
    for page in range(pages):
        img = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(img)
        y = 150
        for line in range(24):
            width = 800 if (line + page) % 5 else 520
            draw.rectangle([140, y, 140 + width, y + 18], fill=(190, 190, 190))
            y += 60
        images.append(img)
    first, rest = images[0], images[1:]
    first.save(out, "PDF", resolution=150.0, save_all=True, append_images=rest)


# ---------------------------------------------------------------------------
# expected-checklist markdown
# ---------------------------------------------------------------------------
def build_expected_md(out: Path) -> None:
    lines = [
        "# 示例书预期清单",
        "",
        "本文件由 `tools/make_samples.py` 生成，正文为本项目自写内容，可随仓库分发。",
        "生成音频后逐章核对下列句子是否被完整、按序朗读。",
        "",
        f"- 书名：{BOOK_TITLE}",
        f"- 作者：{BOOK_AUTHOR}",
        f"- 章节数：{len(CHAPTERS)}（第 2、3 章同名「出发」）",
        "",
        "| 序号 | 章名 | 首句 | 中段句 | 末句 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for i, (title, paragraphs) in enumerate(CHAPTERS, start=1):
        first = paragraphs[0].split("。")[0] + "。"
        middle = paragraphs[len(paragraphs) // 2].split("。")[0] + "。"
        last = paragraphs[-1].split("。")[-2] + "。" if paragraphs[-1].endswith("。") else paragraphs[-1]
        lines.append(f"| {i} | {title} | {first} | {middle} | {last} |")
    lines += [
        "",
        "## 文件名预期",
        "",
        "```",
        "001 - 序言.mp3",
        "002 - 出发.mp3",
        "003 - 出发.mp3",
        "```",
        "",
        "宽度取 3 与总章数位数的较大值；同名章由序号区分。",
        "",
        "## 时长预期",
        "",
        "成品时长在生成后测量，写入验收记录，不在此处预设。三段正文长度不同，",
        "可用于发现「串章」（音频内容与章节对不上）和时长明显异常。",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    build_epub(
        out / "示例书.epub",
        title=BOOK_TITLE,
        author=BOOK_AUTHOR,
        lang=BOOK_LANG,
        chapters=CHAPTERS,
        book_id=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'relaxbook/sample/zh')}",
    )
    build_expected_md(out / "预期清单.md")
    build_text_pdf(out / "示例书.pdf")
    build_scanned_pdf(out / "扫描示例.pdf")
    build_epub(
        out / "sample-en.epub",
        title="The Quiet Train",
        author="RelaxBook Samples",
        lang="en-US",
        chapters=EN_CHAPTERS,
        book_id=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'relaxbook/sample/en')}",
    )

    for path in sorted(out.iterdir()):
        print(f"{path.name:20s} {path.stat().st_size:>10,d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
