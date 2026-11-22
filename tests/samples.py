from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from xml.sax.saxutils import escape
from io import BytesIO

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


CHAPTERS = [
    ("序言", "清晨的列车驶出站台。薄雾慢慢散去，露出远方的村庄。我们带着地图出发。"),
    ("出发", "午后的车站格外安静。小林说，下一站还要等十五分钟。大家坐下来休息。"),
    ("出发", "傍晚的灯光照亮了小路。远处的山渐渐隐入夜色。我们在桥边结束了这一天的旅程。"),
]


def sample_epub(path, chapters=CHAPTERS):
    title = "三章试读"
    nav = "".join(f'<li><a href="book.xhtml#ch{i}">{escape(heading)}</a></li>'
                  for i, (heading, _) in enumerate(chapters))
    text = "".join(f'<section id="ch{i}"><h1>{escape(heading)}</h1><p>{escape(body)}</p></section>'
                   for i, (heading, body) in enumerate(chapters))
    cover = BytesIO()
    Image.new("RGB", (160, 220), "#255e50").save(cover, format="PNG")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", ZIP_STORED)
        archive.writestr("META-INF/container.xml", '''
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="book.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''')
        archive.writestr("book.opf", f'''
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">test:relaxbook</dc:identifier>
<dc:title>{title}</dc:title><dc:language>zh</dc:language><dc:creator>RelaxBook</dc:creator>
<meta property="dcterms:modified">2026-11-22T00:00:00Z</meta></metadata>
<manifest><item id="chapter" href="book.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/></manifest>
<spine><itemref idref="chapter"/></spine></package>''')
        archive.writestr("book.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head><body>{text}</body></html>')
        archive.writestr("nav.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>目录</title></head><body><nav epub:type="toc"><ol>{nav}</ol></nav></body></html>')
        archive.writestr("cover.png", cover.getvalue())
    return path


def sample_pdf(path):
    writer = PdfWriter()
    writer.add_metadata({"/Title": "Text PDF"})
    for i in range(1, 4):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): DictionaryObject({
                NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 14 Tf 72 720 Td (Chapter {i}) Tj 0 -24 Td (This is the body of chapter {i}.) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.add_outline_item(f"Chapter {i}", i-1)
    writer.write(path)
    return path


if __name__ == "__main__":
    import sys
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    sample_epub(folder / "三章试读.epub")
    sample_pdf(folder / "three-chapters.pdf")
    Image.new("RGB", (300, 400), "white").save(folder / "scan.pdf")
