import base64
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from hangul_novel_translator.book import Book, Chapter, export_epub, load_book


# 1x1 透明 PNG，仅用作封面资源内容占位。
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _find_opf(zin: zipfile.ZipFile) -> str:
    container = ET.fromstring(zin.read("META-INF/container.xml"))
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    return container.find(".//c:rootfile", ns).get("full-path")


class EpubCoverAutoTest(unittest.TestCase):
    def test_cover_image_is_declared(self):
        try:
            import ebooklib  # noqa: F401
        except ImportError:
            self.skipTest("需要 ebooklib")
        book = Book(title="测试", chapters=[Chapter(0, "第一章", ["内容"])])
        book.metadata = {
            "images": [
                {"name": "Images/cover.png", "content": _PNG_1PX},
                {"name": "Images/body.png", "content": b"fake-body"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.epub"
            export_epub(book, out, source_title="测试")
            self.assertTrue(out.exists())
            with zipfile.ZipFile(out) as zin:
                opf = ET.fromstring(zin.read(_find_opf(zin)))
                ns = {"opf": "http://www.idpf.org/2007/opf"}
                metas = opf.findall(".//opf:meta[@name='cover']", ns)
                self.assertTrue(any(m.get("content") == "cover-img" for m in metas))
                covers = opf.findall(".//opf:item[@properties='cover-image']", ns)
                self.assertEqual(len(covers), 1)
                self.assertEqual(covers[0].get("href"), "Images/cover.png")

    def test_no_cover_keeps_single_image(self):
        try:
            import ebooklib  # noqa: F401
        except ImportError:
            self.skipTest("需要 ebooklib")
        book = Book(title="测试", chapters=[Chapter(0, "第一章", ["内容"])])
        book.metadata = {
            "images": [
                {"name": "Images/body.png", "content": b"fake-body"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.epub"
            export_epub(book, out, source_title="测试")
            with zipfile.ZipFile(out) as zin:
                opf = ET.fromstring(zin.read(_find_opf(zin)))
                ns = {"opf": "http://www.idpf.org/2007/opf"}
                self.assertEqual(opf.findall(".//opf:item[@properties='cover-image']", ns), [])

    def test_standalone_cover_roundtrip_preserved(self):
        """Sigil/Txt2Epub 风格的独立封面（文件名不以 cover 开头）在 load→export 后保留。"""
        try:
            import ebooklib  # noqa: F401
        except ImportError:
            self.skipTest("需要 ebooklib")
        book = Book(title="测试", chapters=[Chapter(0, "第一章", ["内容"])])
        book.metadata = {
            "images": [{"name": "Images/xxlarge.webp", "content": _PNG_1PX}],
            "cover_image": "Images/xxlarge.webp",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = tmp / "first.epub"
            export_epub(book, first, source_title="测试")
            loaded = load_book(first)
            self.assertEqual(loaded.metadata.get("cover_image"), "Images/xxlarge.webp")
            self.assertIn(
                "Images/xxlarge.webp",
                [i.get("name") for i in loaded.metadata.get("images", [])],
            )
            second = tmp / "second.epub"
            export_epub(loaded, second, source_title="测试")
            with zipfile.ZipFile(second) as zin:
                opf = ET.fromstring(zin.read(_find_opf(zin)))
                ns = {"opf": "http://www.idpf.org/2007/opf"}
                metas = opf.findall(".//opf:meta[@name='cover']", ns)
                self.assertTrue(any(m.get("content") == "cover-img" for m in metas))
                covers = opf.findall(".//opf:item[@properties='cover-image']", ns)
                self.assertEqual(len(covers), 1)
                self.assertEqual(covers[0].get("href"), "Images/xxlarge.webp")


if __name__ == "__main__":
    unittest.main()
