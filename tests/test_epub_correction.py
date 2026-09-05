import tempfile
import unittest
import zipfile
import json
import re
from pathlib import Path

from hangul_novel_translator.epub_correction import (
    _extract_body_title_from,
    _looks_like_body_title,
    FormatTemplate,
    apply_add_format,
    apply_clear_format,
    apply_reassemble,
    apply_regex_resplit,
    detect_format,
    infer_body_title_match,
    infer_placeholder_pattern,
    load_format_template,
    preview_add_format,
    preview_clear_format,
    preview_reassemble,
    preview_regex_resplit,
)

try:
    from ebooklib import epub as epub_lib

    HAS_EPUB = True
except ImportError:  # pragma: no cover
    HAS_EPUB = False


def _html(title: str, body: str, level: int = 2) -> str:
    return (
        '<html lang="zh" xml:lang="zh" xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>{title}</title></head><body>"
        f"<h{level}>{title}</h{level}>{body}</body></html>"
    )


@unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
class EpubCorrectionTest(unittest.TestCase):
    def _build(self, tmp: Path) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("correction-fixture")
        src.set_title("测试书")
        src.set_language("zh")

        chapters = [
            ("cover.xhtml", "测试书 第1卷", "", 1),
            (
                "prod.xhtml",
                "制作说明",
                '<p style="text-align:center;font-size:0.8em">(本章字数: 10)</p>'
                "<p>【制作说明】</p><p>制作工具：Txt2Epub</p><p>【书籍统计】</p>",
                2,
            ),
            (
                "intro.xhtml",
                "简介",
                '<p style="text-align:center;font-size:0.8em">(本章字数: 8)</p>'
                "<p>这是一个简介。</p>",
                2,
            ),
            (
                "ep0.xhtml",
                "EP.0",
                '<p style="text-align:center;font-size:0.8em">(本章字数: 20)</p>'
                "<p>我正在掐着主人家少爷的脖子。</p><p>收起封面</p><p>正文。</p>",
                2,
            ),
            (
                "ep1.xhtml",
                "EP.1",
                '<p style="text-align:center;font-size:0.8em">(本章字数: 30)</p>'
                "<p>起因 (1)</p><p>这是第一章的正文第一段。</p>",
                2,
            ),
            (
                "ep2.xhtml",
                "EP.2",
                '<p style="text-align:center;font-size:0.8em">(本章字数: 40)</p>'
                "<p>发端 (2)</p><p>这是第二章的正文第一段。</p>",
                2,
            ),
        ]
        filenames = []
        toc_links = []
        for file_name, title, body, level in chapters:
            item = epub_lib.EpubHtml(
                uid=file_name, title=title, file_name=file_name, lang="zh"
            )
            item.content = _html(title, body, level)
            src.add_item(item)
            filenames.append(file_name)
            toc_links.append(epub_lib.Link(file_name, title, file_name))

        src.toc = tuple(toc_links)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav"] + filenames

        path = tmp / "book.epub"
        epub_lib.write_epub(str(path), src)
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.src = self._build(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_detect_format_finds_production_and_word_counts(self):
        result = detect_format(self.src)
        self.assertTrue(result["has_production_note"])
        self.assertTrue(any(p["title"] == "制作说明" for p in result["production"]))
        self.assertGreaterEqual(result["word_count_count"], 4)

    def test_preview_reassemble_skips_prologue_and_extracts_real_titles(self):
        result = preview_reassemble(self.src)
        titles = {u["old"]: u["new"] for u in result["updates"]}
        self.assertEqual(titles.get("EP.1"), "起因 (1)")
        self.assertEqual(titles.get("EP.2"), "发端 (2)")
        # EP.0 是序幕，正文里没有真正标题，不应被改写。
        self.assertNotIn("EP.0", titles)

    def test_apply_reassemble_writes_titles_and_removes_word_count(self):
        out = self.tmp / "reassembled.epub"
        result = apply_reassemble(self.src, out)
        self.assertEqual(result["updated"][0]["old"], "EP.1")
        self.assertEqual(result["updated"][0]["new"], "起因 (1)")
        self.assertGreaterEqual(result["removed_word_count_lines"], 3)
        with zipfile.ZipFile(out, "r") as z:
            html = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html"))]
            ep1 = next(
                z.read(n).decode("utf-8")
                for n in html
                if "起因 (1)" in z.read(n).decode("utf-8")
            )
        self.assertRegex(ep1, r"<h[1-6][^>]*>起因 \(1\)</h[1-6]>")
        self.assertIn("这是第一章的正文第一段。", ep1)
        self.assertNotIn("本章字数", ep1)
        self.assertNotIn("起因 (1)</p>", ep1)

    def test_body_title_with_epilogue_word_not_skipped(self):
        # EP.670 这类标题含“后记/番外”，不能再被当作 UI 行跳过。
        title = "PLUS 后记 - 曾是起始的故事之终结 -完-"
        self.assertTrue(_looks_like_body_title(title))
        extracted, idx = _extract_body_title_from(
            [title, "万事万物皆有其始，亦有其终。"], None
        )
        self.assertEqual(extracted, title)
        self.assertEqual(idx, 0)

    def test_apply_clear_format_removes_production_and_word_counts(self):
        out = self.tmp / "cleared.epub"
        result = apply_clear_format(self.src, out)
        self.assertEqual(result["removed_production"], ["制作说明"])
        self.assertGreaterEqual(result["removed_word_count_lines"], 4)
        after = detect_format(out)
        self.assertFalse(after["has_production_note"])
        self.assertEqual(after["word_count_count"], 0)

    def test_apply_add_format_adds_production_and_word_counts(self):
        out = self.tmp / "styled.epub"
        result = apply_add_format(self.src, out)
        self.assertGreaterEqual(result["total_chars"], 0)
        after = detect_format(out)
        self.assertTrue(after["has_production_note"])
        self.assertEqual(after["production"][0]["title"], "制作说明")
        self.assertGreaterEqual(after["word_count_count"], 5)
        with zipfile.ZipFile(out, "r") as z:
            nav = z.read("EPUB/nav.xhtml").decode("utf-8")
            self.assertIn('>制作说明<', nav)

    def test_preview_add_format_reports_stats(self):
        result = preview_add_format(self.src)
        self.assertGreaterEqual(result["total_chars"], 0)
        self.assertTrue(any("【制作说明】" in line for line in result["note_body"]))
        self.assertTrue(any("总字数" in line for line in result["note_body"]))

    def test_preview_regex_resplit_finds_titles_in_body(self):
        # 仿真“txt 自动切分、标题落在正文里”的场景：用正则匹配“起因 (1)”行。
        result = preview_regex_resplit(self.src, r"起因\s*\(1\)")
        self.assertGreaterEqual(result["title_count"], 1)
        self.assertIn("起因 (1)", result["titles"])

    def test_apply_regex_resplit_rebuilds_chapters(self):
        out = self.tmp / "resplit.epub"
        result = apply_regex_resplit(self.src, out, r"起因\s*\(1\)")
        self.assertGreaterEqual(result["chapter_count"], 1)
        self.assertIn("起因 (1)", result["titles"])


class FakeLLM:
    """最小 LLM 桩：按调用返回预设 JSON。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, *, temperature: float | None = None):
        self.calls.append(messages)
        return self.payload


class LLMTitleInferenceTest(unittest.TestCase):
    def _build(self, tmp: Path) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("title-llm")
        src.set_title("测试书")
        src.set_language("zh")
        chapters = [
            ("cover.xhtml", "测试书 第1卷", "", 1),
            ("ep0.xhtml", "EP.0", "<p>序幕正文。</p>", 2),
            (
                "ep1.xhtml",
                "EP.1",
                '<p style="text-align:center">(本章字数: 30)</p>'
                "<p>起因 (1)</p><p>正文。</p>",
                2,
            ),
        ]
        links = []
        for file_name, title, body, level in chapters:
            item = epub_lib.EpubHtml(
                uid=file_name, title=title, file_name=file_name, lang="zh"
            )
            item.content = _html(title, body, level)
            src.add_item(item)
            links.append(epub_lib.Link(file_name, title, file_name))
        src.toc = tuple(links)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav"] + [name for name, _, _, _ in chapters]
        path = tmp / "book.epub"
        epub_lib.write_epub(str(path), src)
        return path

    @unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
    def test_infer_placeholder_pattern_returns_llm_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._build(Path(td))
            llm = FakeLLM({"pattern": r"^EP\.\d+$"})
            result = infer_placeholder_pattern(llm, src)
            self.assertEqual(result["pattern"], r"^EP\.\d+$")
            self.assertTrue(any("EP.0" in t for t in result["titles"]))

    @unittest.skipUnless(HAS_EPUB, "需要 ebooklib")
    def test_infer_body_title_match_samples_and_returns_pattern(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._build(Path(td))
            llm = FakeLLM({"pattern": r"^起因 \(1\)$", "found": True})
            result = infer_body_title_match(llm, src, prefix_pattern=r"^EP\.\d+$")
            self.assertEqual(result["pattern"], r"^起因 \(1\)$")
            self.assertTrue(result["found"])
            self.assertGreaterEqual(len(result["sample"]), 1)
            self.assertTrue(
                any("起因 (1)" in item["head"] for item in result["sample"])
            )


class FormatTemplateTest(unittest.TestCase):
    def test_default_template_renders_note_and_word_count(self):
        tpl = FormatTemplate()
        note = tpl.render_note(
            total_chars=2544524,
            minutes=6361,
            chapter_count=673,
            cover_font="source.ttf（自用字体）",
            generated_at="2026-09-05 12:00:00",
        )
        self.assertIn("【制作说明】", note)
        self.assertIn("生成时间：2026-09-05 12:00:00", note)
        self.assertIn("• 总字数：2,544,524 字", note)
        self.assertIn("• 预计阅读时长：约 6361 分钟", note)
        self.assertIn("• 总章节：673 章", note)
        self.assertEqual(tpl.render_word_count(4277), "(本章字数: 4277)")

    def test_custom_template_placeholders_substitute(self):
        tpl = FormatTemplate(
            production_title="自定义说明",
            production_lines=["总字数 ${total_chars}", "章节 {chapter_count}"],
            word_count_template="本章字数：${chars}",
            word_count_style={"align": "center", "color": "gray"},
        )
        note = tpl.render_note(
            total_chars=12345, minutes=30, chapter_count=7, cover_font="sys"
        )
        self.assertEqual(note, ["总字数 12,345", "章节 7"])
        self.assertEqual(tpl.render_word_count(100), "本章字数：100")

    def test_placeholder_aliases_work(self):
        tpl = FormatTemplate(
            production_lines=["章节 ${chapteramount}", "字数 ${wordcount}"],
            word_count_template="《${word_count}》",
        )
        note = tpl.render_note(
            total_chars=10, minutes=1, chapter_count=7, cover_font="x"
        )
        self.assertEqual(note[0], "章节 7")
        self.assertEqual(tpl.render_word_count(42), "《42》")

    def test_load_format_template_reads_json_and_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "custom.json"
            path.write_text(
                json.dumps(
                    {
                        "production_title": "测试说明",
                        "production_lines": ["${total_chars}"],
                        "word_count_template": "${chars} 字",
                    }
                ),
                encoding="utf-8",
            )
            tpl = load_format_template(path)
            self.assertEqual(tpl.production_title, "测试说明")
            self.assertEqual(tpl.render_word_count(42), "42 字")
            # 不存在 / 损坏时回落默认
            missing = load_format_template(Path(td) / "nope.json")
            self.assertEqual(missing.production_title, "制作说明")

    def test_apply_add_format_uses_custom_template(self):
        with tempfile.TemporaryDirectory() as td:
            tpl = FormatTemplate(
                production_title="制作说明",
                production_lines=["自定义制作说明", "总字数 ${total_chars}"],
                word_count_template="本章字数：${chars}",
                word_count_style={"align": "center", "color": "gray"},
            )
            src = self._build(Path(td))
            out = Path(td) / "custom_styled.epub"
            apply_add_format(src, out, template=tpl)
            result = detect_format(out)
            self.assertTrue(result["has_production_note"])
            self.assertEqual(result["production"][0]["title"], "制作说明")
            self.assertGreaterEqual(result["word_count_count"], 2)
            with zipfile.ZipFile(out, "r") as z:
                html = [
                    n
                    for n in z.namelist()
                    if n.lower().endswith((".xhtml", ".html"))
                ]
                styled = any(
                    "text-align:center" in z.read(n).decode("utf-8")
                    and "color:gray" in z.read(n).decode("utf-8")
                    for n in html
                )
                self.assertTrue(styled)

    def test_apply_add_format_injects_title_css(self):
        with tempfile.TemporaryDirectory() as td:
            tpl = FormatTemplate(
                title_enabled=True,
                title_style={
                    "align": "center",
                    "color": "#FF8800",
                    "font_size": "1.4em",
                    "bold": True,
                },
            )
            src = self._build(Path(td))
            out = Path(td) / "title_css.epub"
            result = apply_add_format(src, out, template=tpl)
            self.assertTrue(result["title_css"])
            self.assertIn(".chapter-title", result["title_css"])
            with zipfile.ZipFile(out, "r") as z:
                css_names = [
                    n for n in z.namelist() if n.lower().endswith(".css")
                ]
                css_content = "\n".join(
                    z.read(n).decode("utf-8") for n in css_names
                )
                self.assertIn(".chapter-title", css_content)
                self.assertIn("text-align:center", css_content)
                self.assertIn("color:#FF8800", css_content)
                html_names = [
                    n
                    for n in z.namelist()
                    if n.lower().endswith((".xhtml", ".html"))
                ]
                self.assertTrue(
                    any(
                        'class="chapter-title"' in z.read(n).decode("utf-8")
                        for n in html_names
                    )
                )

    def _build(self, tmp: Path) -> Path:
        src = epub_lib.EpubBook()
        src.set_identifier("template-fixture")
        src.set_title("测试书")
        src.set_language("zh")
        chapters = [
            ("cover.xhtml", "测试书 第1卷", "", 1),
            (
                "ep0.xhtml",
                "EP.0",
                '<p>(本章字数: 20)</p><p>正文内容。</p>',
                2,
            ),
            (
                "ep1.xhtml",
                "EP.1",
                '<p>(本章字数: 30)</p><p>正文内容。</p>',
                2,
            ),
        ]
        links = []
        for file_name, title, body, level in chapters:
            item = epub_lib.EpubHtml(
                uid=file_name, title=title, file_name=file_name, lang="zh"
            )
            item.content = _html(title, body, level)
            src.add_item(item)
            links.append(epub_lib.Link(file_name, title, file_name))
        src.toc = tuple(links)
        src.add_item(epub_lib.EpubNcx())
        src.add_item(epub_lib.EpubNav())
        src.spine = ["nav"] + [name for name, _, _, _ in chapters]
        path = tmp / "book.epub"
        epub_lib.write_epub(str(path), src)
        return path


if __name__ == "__main__":
    unittest.main()
