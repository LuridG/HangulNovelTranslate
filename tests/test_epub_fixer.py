import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from hangul_novel_translator.epub_fixer import (
    fix_finished_epub_in_place,
    preview_finished_epub,
)


class FakeGlossary:
    """最小词表桩：只提供 replacement_pairs，避免测试依赖 openai。"""

    def __init__(self, pairs):
        self._pairs = pairs

    def replacement_pairs(self):
        return list(self._pairs)


def build_epub() -> bytes:
    """构造一个真实 zip 容器：mimetype + 样式 + 图片 + 正文 xhtml。"""
    fake_html = (
        '<html><head><link rel="stylesheet" href="style.css"/></head>'
        '<body><h1>Title</h1>'
        '<p class="dialogue">“俊希，今天天气不错。”</p>'
        '<p>这是俊希的房间。</p></body></html>'
    ).encode("utf-8")
    fake_css = b"body { font-size: 1em; }"
    fake_img = b"\x89PNG\r\n\x1a\nfake-image"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # mimetype 必须是第一条且不压缩（EPUB 规范）。
        mimetype_info = zipfile.ZipInfo("mimetype")
        mimetype_info.compress_type = zipfile.ZIP_STORED
        zf.writestr(mimetype_info, b"application/epub+zip")
        zf.writestr("OEBPS/style.css", fake_css)
        zf.writestr("OEBPS/img.png", fake_img)
        zf.writestr("OEBPS/chapter01.xhtml", fake_html)
    buf.seek(0)
    return buf.getvalue()


class TestEpubFixer(unittest.TestCase):
    def test_inplace_html_text_replacement(self):
        glossary = FakeGlossary([("俊希", "俊熙")])
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.epub"
            src.write_bytes(build_epub())
            out = Path(tmp) / "book_fixed.epub"
            stats = fix_finished_epub_in_place(src, glossary, out)
            self.assertGreaterEqual(stats["modified_files"], 1)
            self.assertGreaterEqual(stats["hit_count"], 2)
            with zipfile.ZipFile(out, "r") as zcheck:
                chap = zcheck.read("OEBPS/chapter01.xhtml").decode("utf-8")
                self.assertIn("“俊熙，今天天气不错。”", chap)
                self.assertIn("这是俊熙的房间。", chap)
                self.assertNotIn("俊希", chap)
                self.assertIn('class="dialogue"', chap)
                self.assertIn("style.css", chap)
                self.assertEqual(zcheck.read("OEBPS/style.css"), b"body { font-size: 1em; }")
                self.assertEqual(zcheck.read("OEBPS/img.png"), b"\x89PNG\r\n\x1a\nfake-image")
                # mimetype 保持第一条且仍为 STORED。
                mimetype_info = zcheck.getinfo("mimetype")
                self.assertEqual(mimetype_info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zcheck.infolist()[0].filename, "mimetype")

    def test_no_pairs_no_changes(self):
        glossary = FakeGlossary([])
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.epub"
            src.write_bytes(build_epub())
            out = Path(tmp) / "book_fixed.epub"
            stats = fix_finished_epub_in_place(src, glossary, out)
            self.assertEqual(stats["hit_count"], 0)
            self.assertEqual(stats["modified_files"], 0)
            with zipfile.ZipFile(out, "r") as zcheck:
                chap = zcheck.read("OEBPS/chapter01.xhtml").decode("utf-8")
                self.assertIn("俊希", chap)

    def test_preview_reports_hits(self):
        glossary = FakeGlossary([("俊希", "俊熙")])
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.epub"
            src.write_bytes(build_epub())
            result = preview_finished_epub(src, glossary)
            self.assertGreaterEqual(result["total_hits"], 2)
            self.assertTrue(result["lines"])
            joined = "\n".join(result["lines"])
            self.assertIn("俊希", joined)
            self.assertIn("俊熙", joined)

    def test_overwrite_in_place_is_atomic(self):
        # 输出路径与输入相同（原地覆盖）时，仍能安全写入并保留其它资源。
        glossary = FakeGlossary([("俊希", "俊熙")])
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.epub"
            src.write_bytes(build_epub())
            stats = fix_finished_epub_in_place(src, glossary, src)
            self.assertGreaterEqual(stats["modified_files"], 1)
            with zipfile.ZipFile(src, "r") as zcheck:
                chap = zcheck.read("OEBPS/chapter01.xhtml").decode("utf-8")
                self.assertNotIn("俊希", chap)
                self.assertIn("俊熙", chap)
                self.assertEqual(zcheck.read("OEBPS/img.png"), b"\x89PNG\r\n\x1a\nfake-image")

    def test_script_style_nodes_untouched(self):
        glossary = FakeGlossary([("俊希", "俊熙")])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "OEBPS/page.xhtml",
                (
                    "<html><body><p>俊希</p><style>.a{content:'俊希'}</style>"
                    "<script>var x='俊希';</script></body></html>"
                ).encode("utf-8"),
            )
        buf.seek(0)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.epub"
            src.write_bytes(buf.getvalue())
            out = Path(tmp) / "book_fixed.epub"
            fix_finished_epub_in_place(src, glossary, out)
            with zipfile.ZipFile(out, "r") as zcheck:
                content = zcheck.read("OEBPS/page.xhtml").decode("utf-8")
                self.assertIn("<p>俊熙</p>", content)
                self.assertIn("content:'俊希'", content)
                self.assertIn("var x='俊希';", content)


if __name__ == "__main__":
    unittest.main()
