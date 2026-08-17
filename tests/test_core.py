import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hangul_novel_translator.book import (
    export_epub,
    parse_epub,
    parse_txt,
    _has_skip_text_marker,
    _strip_invisible_chars,
)
from hangul_novel_translator.utils import extract_json, split_paragraph_smart

try:
    from ebooklib import epub as epub_lib

    HAS_EPUB = True
except ImportError:
    HAS_EPUB = False


class TxtParsingTest(unittest.TestCase):
    def test_chapter_headings(self):
        raw = "제1장 시작\n\n첫 번째 문장.\n두 번째 문장.\n\n제2장 전개\n\n세 번째 문장.\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_text(raw, encoding="utf-8")
            book = parse_txt(path)
        self.assertEqual(len(book.chapters), 2)
        self.assertEqual(book.chapters[0].title, "제1장 시작")
        self.assertEqual(len(book.chapters[0].paragraphs), 2)
        self.assertEqual(book.chapters[1].title, "제2장 전개")

    def test_no_headings_fallback(self):
        raw = "문장 하나.\n문장 둘.\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.txt"
            path.write_text(raw, encoding="utf-8")
            book = parse_txt(path)
        self.assertGreaterEqual(len(book.chapters), 1)
        self.assertGreater(book.total_chars, 0)

    def test_heading_with_invisible_chars(self):
        raw = "\u2060제1장 시작\u2060\n\n첫 번째 문장.\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invisible.txt"
            path.write_text(raw, encoding="utf-8")
            book = parse_txt(path)
        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].title, "제1장 시작")
        self.assertEqual(book.chapters[0].paragraphs, ["첫 번째 문장."])


class SkipMarkerTest(unittest.TestCase):
    def test_body_common_word_not_treated_as_toc(self):
        # “한 차례”是普通正文词汇，不应再触发目录/版权页跳过。
        self.assertFalse(_has_skip_text_marker(["한 차례 막았다.", "준희는 자리에서 일어났다."]))
        # “차례”已从跳过集移除，整行出现也不算目录标记。
        self.assertFalse(_has_skip_text_marker(["차례"]))

    def test_standalone_marker_line_still_skips(self):
        self.assertTrue(_has_skip_text_marker(["목차"]))
        self.assertTrue(_has_skip_text_marker(["  \u2060\u2063목차\u2060  "]))
        self.assertTrue(_has_skip_text_marker(["판권"]))
        self.assertTrue(_has_skip_text_marker(["Copyright"]))

    def test_marker_inside_sentence_not_skipped(self):
        self.assertFalse(_has_skip_text_marker(["목차가 필요하다.", "본문 내용"]))


class InvisibleCharsTest(unittest.TestCase):
    def test_strip_invisible_chars(self):
        self.assertEqual(_strip_invisible_chars("a\u2060b\u2063c\ufeffd"), "abcd")
        self.assertEqual(_strip_invisible_chars("\u200b\u200c목차\u200d"), "목차")


@unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
class EpubParsingRegressionTest(unittest.TestCase):
    def test_toc_page_skipped_but_body_word_kept(self):
        src = epub_lib.EpubBook()
        src.set_identifier("regression-0001")
        src.set_title("들개 출몰")
        src.set_language("ko")

        toc_page = epub_lib.EpubHtml(uid="toc.xhtml", title="목차", file_name="toc.xhtml", lang="ko")
        toc_page.content = "<html><body><p>목차</p><p>4부</p></body></html>"
        chapter = epub_lib.EpubHtml(uid="chap.xhtml", title="4부", file_name="chap.xhtml", lang="ko")
        chapter.content = (
            "<html><body><h2>4부</h2>"
            "<p>한 차례 막았다.</p>"
            "<p>본문 내용.</p>"
            "</body></html>"
        )
        src.add_item(toc_page)
        src.add_item(chapter)
        src.toc = (epub_lib.Link("chap.xhtml", "4부", "chap.xhtml"),)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["toc.xhtml", "chap.xhtml"]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vol3.epub"
            epub_lib.write_epub(str(path), src)
            book = parse_epub(path)

        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].title, "4부")
        self.assertIn("한 차례 막았다.", book.chapters[0].paragraphs)


@unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
class EpubStylePreservationTest(unittest.TestCase):
    def _make_styled_book(self, tmp: Path) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("style-0001")
        src.set_title("스타일 테스트")
        src.set_language("ko")
        src.add_item(
            epub_lib.EpubItem(
                uid="css1",
                file_name="Styles/main.css",
                media_type="text/css",
                content=b".center { text-align: center; }",
            )
        )
        body = (
            "<body>"
            "<h1>제1장</h1>"
            '<p class="center">중앙 정렬</p>'
            "<p>일반 문단</p>"
            "<blockquote><p>인용문</p></blockquote>"
            '<div class="letter"><p>편지</p></div>'
            "</body>"
        )
        chapter = epub_lib.EpubHtml(uid="chap.xhtml", title="", file_name="Text/chap.xhtml", lang="ko")
        chapter.content = (
            "<html><head><link rel='stylesheet' type='text/css' href='../Styles/main.css'/></head>"
            + body
            + "</html>"
        )
        src.add_item(chapter)
        src.toc = (epub_lib.Link("Text/chap.xhtml", "제1장", "chap.xhtml"),)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav", chapter]
        path = tmp / "styled.epub"
        epub_lib.write_epub(str(path), src)
        return path

    def test_parse_keeps_block_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_styled_book(Path(tmp))
            book = parse_epub(path)
        chapter = book.chapters[-1]  # 排除 nav 页
        styles = dict(zip(chapter.paragraphs, chapter.styles))
        self.assertEqual(styles["중앙 정렬"].block.klass, "center")
        self.assertEqual(styles["인용문"].block.tag, "blockquote")
        self.assertEqual([a.klass for a in styles["편지"].ancestors], ["letter"])
        self.assertEqual(book.metadata["css_resources"][0]["name"], "Styles/main.css")

    def test_export_restores_styles_and_css(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = self._make_styled_book(tmp)
            book = parse_epub(path)
            out = tmp / "out.epub"
            export_epub(book, out)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertTrue(any(n.endswith("Styles/main.css") for n in names))
                chapter_html = ""
                for n in names:
                    if n.endswith(".xhtml") and "nav" not in n:
                        chapter_html += zf.read(n).decode("utf-8")
                self.assertIn('class="center"', chapter_html)
                self.assertIn("<blockquote>", chapter_html)
                self.assertIn('class="letter"', chapter_html)
                self.assertIn('rel="stylesheet"', chapter_html)



@unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
class EpubInlineFormatTest(unittest.TestCase):
    def _make_book(self, tmp: Path) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("inline-0001")
        src.set_title("인라인 테스트")
        src.set_language("ko")
        src.add_item(
            epub_lib.EpubItem(
                uid="pic1",
                file_name="Images/p1.png",
                media_type="image/png",
                content=b"\x89PNG-fake",
            )
        )
        body = (
            "<body>"
            "<h1>제1장</h1>"
            '<p>안녕 <b>세상</b> <span style="color:red">빨강</span>'
            ' <a href="#fn1">[1]</a></p>'
            '<p><img src="../Images/p1.png" alt="그림"/></p>'
            '<aside id="fn1"><p>주석 내용.</p></aside>'
            "</body>"
        )
        chapter = epub_lib.EpubHtml(
            uid="chap.xhtml", title="", file_name="Text/chap.xhtml", lang="ko"
        )
        chapter.content = "<html><head></head>" + body + "</html>"
        src.add_item(chapter)
        src.toc = (epub_lib.Link("Text/chap.xhtml", "제1장", "chap.xhtml"),)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav", chapter]
        path = tmp / "inline.epub"
        epub_lib.write_epub(str(path), src)
        return path

    def test_parse_keeps_inline_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = parse_epub(self._make_book(Path(tmp)))
        chapter = book.chapters[-1]
        joined = "\n".join(chapter.paragraphs)
        self.assertIn("\u27e6b\u27e7세상\u27e6/b\u27e7", joined)
        self.assertIn("\u27e6s:color:red\u27e7빨강\u27e6/s\u27e7", joined)
        self.assertIn("\u27e6fn:fn1\u27e7", joined)
        self.assertIn("\u27e6img:Images/p1.png\u27e7", joined)
        names = [r["name"] for r in book.metadata.get("images", [])]
        self.assertIn("Images/p1.png", names)

    def test_export_restores_inline_markers(self):
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            book = parse_epub(self._make_book(tmp))
            out = tmp / "out.epub"
            export_epub(book, out)
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
                self.assertTrue(any(n.endswith("Images/p1.png") for n in names))
                html = ""
                for n in names:
                    if n.endswith(".xhtml") and "nav" not in n:
                        html += zf.read(n).decode("utf-8")
                self.assertIn("<b>세상</b>", html)
                self.assertIn('<span style="color:red">빨강</span>', html)
                self.assertIn('<a href="#fn1">', html)
                self.assertIn('<img src="Images/p1.png"', html)

    def test_strip_inline_markers(self):
        from hangul_novel_translator.book import strip_inline_markers

        text = "안녕 \u27e6b\u27e7세상\u27e6/b\u27e7 \u27e6img:Images/p1.png\u27e7"
        self.assertEqual(strip_inline_markers(text), "안녕 세상 【插图】")

    def test_metadata_json_round_trip(self):
        import json as jsonlib

        from hangul_novel_translator.book import metadata_from_dict, metadata_to_dict

        meta = {
            "css_resources": [{"name": "Styles/main.css", "content": b".center{}"}],
            "images": [{"name": "Images/p1.png", "content": b"\x89PNG"}],
            "doc_inline_css": {"chap.xhtml": ["p { color: red; }"]},
        }
        dumped = jsonlib.dumps(metadata_to_dict(meta), ensure_ascii=False)
        restored = metadata_from_dict(jsonlib.loads(dumped))
        self.assertEqual(restored["css_resources"][0]["content"], b".center{}")
        self.assertEqual(restored["images"][0]["content"], b"\x89PNG")
        self.assertEqual(
            restored["doc_inline_css"], {"chap.xhtml": ["p { color: red; }"]}
        )



@unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
class ChapterTitleTest(unittest.TestCase):
    def _make_two_page_book(self, tmp: Path, page2_body: str) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("title-test")
        src.set_title("타이틀")
        src.set_language("ko")
        page1 = epub_lib.EpubHtml(uid="p1", title="", file_name="Text/p1.xhtml", lang="ko")
        page1.content = "<html><body><h1>제1장 시작</h1><p>첫 문단.</p></body></html>"
        page2 = epub_lib.EpubHtml(uid="p2", title="", file_name="Text/p2.xhtml", lang="ko")
        page2.content = f"<html><body>{page2_body}</body></html>"
        src.add_item(page1)
        src.add_item(page2)
        src.toc = (epub_lib.Link("Text/p1.xhtml", "제1장 시작", "p1"),)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav", page1, page2]
        path = tmp / "titles.epub"
        epub_lib.write_epub(str(path), src)
        return path

    def test_heading_not_duplicated_and_untitled_merges(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = parse_epub(self._make_two_page_book(Path(tmp), "<p>둘째 문단.</p>"))
        # nav 页被跳过；p1 标题不进正文；p2 无标题并入 p1，不再产生“第 X 节”
        self.assertEqual(len(book.chapters), 1)
        ch = book.chapters[0]
        self.assertEqual(ch.title, "제1장 시작")
        self.assertEqual(ch.paragraphs, ["첫 문단.", "둘째 문단."])
        self.assertNotIn("第", ch.title)

    def test_normalize_title_keeps_zalgo(self):
        from hangul_novel_translator.book import _normalize_title, is_decorative_title

        zalgo = "S\u0337\u0308\u0311tr\u0301\u0337ange dream"
        self.assertEqual(_normalize_title(zalgo), zalgo)  # 保留原书故意设计的 zalgo 装饰
        self.assertTrue(is_decorative_title(zalgo))
        self.assertFalse(is_decorative_title("후기"))
        self.assertEqual(_normalize_title("  후기  "), "후기")


class UtilsTest(unittest.TestCase):
    def test_extract_json_code_block(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_split_long_paragraph(self):
        para = "첫 문장. " * 400
        parts = split_paragraph_smart(para, 120)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 130 for p in parts))


if __name__ == "__main__":
    unittest.main()