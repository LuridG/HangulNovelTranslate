import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hangul_novel_translator.book import Book, Chapter, load_book
from hangul_novel_translator.config import AppConfig
from hangul_novel_translator.glossary import Glossary, GlossaryEntry
from hangul_novel_translator.merge import (
    archive_filename_title,
    book_from_state,
    detect_merge_title,
    export_merged,
    fix_book,
    inspect_state,
    merge_books,
    preview_fix,
    hangul_char_count,
    audit_translation_state,
    review_translation_state,
)
from hangul_novel_translator.translator import build_chunks


class MergeLogicTest(unittest.TestCase):
    def test_audit_translation_state_moves_matching_pattern_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "audit.txt"
            source.write_text("제1장 시작\n번역 대상 본문입니다.", encoding="utf-8")
            book = load_book(source)
            chunks = build_chunks(book, AppConfig(chunk_chars=1800, max_paragraph_chars=2600))
            state = {
                "source": str(source),
                "chunk_chars": 1800,
                "max_paragraph_chars": 2600,
                "completed": {
                    chunks[0].id: ["翻译失败了，请重试。"],
                },
                "failed": {},
            }
            state_path = tmp / "audit.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            result = audit_translation_state(state_path, AppConfig(), pattern="翻译失败")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result["flagged"], 1)
            self.assertNotIn(chunks[0].id, saved["completed"])
            self.assertEqual(saved["failed"][chunks[0].id]["paragraphs"], ["번역 대상 본문입니다."])
            self.assertIn("翻译失败", saved["failed"][chunks[0].id]["error"])

    def test_audit_ignores_non_matching_blocks_and_validates_pattern(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "audit2.txt"
            source.write_text("제1장 시작\n본문입니다.", encoding="utf-8")
            book = load_book(source)
            chunks = build_chunks(book, AppConfig(chunk_chars=1800, max_paragraph_chars=2600))
            state = {
                "source": str(source),
                "chunk_chars": 1800,
                "max_paragraph_chars": 2600,
                "completed": {chunks[0].id: ["这是正常翻译结果。"]},
                "failed": {},
            }
            state_path = tmp / "audit2.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            result = audit_translation_state(state_path, AppConfig(), pattern="翻译失败")
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result["flagged"], 0)
            self.assertIn(chunks[0].id, saved["completed"])
            self.assertEqual(saved["failed"], {})
            with self.assertRaises(ValueError):
                audit_translation_state(state_path, AppConfig(), pattern="[")

    def test_review_translation_state_moves_long_korean_completion_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "review.txt"
            source.write_text("제1장 시작\n번역 대상 본문입니다.", encoding="utf-8")
            book = load_book(source)
            chunks = build_chunks(book, AppConfig(chunk_chars=1800, max_paragraph_chars=2600))
            state = {
                "source": str(source),
                "chunk_chars": 1800,
                "max_paragraph_chars": 2600,
                "completed": {chunks[0].id: ["한국어가 아주 길게 이어지는 문장입니다. 이것은 번역되지 않았습니다."]},
                "failed": {},
            }
            state_path = tmp / "review.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            result = review_translation_state(state_path, AppConfig(), threshold=10)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result["flagged"], 1)
            self.assertNotIn(chunks[0].id, saved["completed"])
            self.assertEqual(saved["failed"][chunks[0].id]["paragraphs"], ["번역 대상 본문입니다."])

    def test_review_ignores_short_korean_and_counts_across_spaces(self):
        self.assertEqual(hangul_char_count("中文 佑胜 OK 한국어"), 3)
        self.assertEqual(hangul_char_count("문자를 확인한 佑胜은 휴대폰을 넣었다."), 14)
        self.assertEqual(hangul_char_count("中文 ⟦s:font-style:italic⟧‘아’⟦/s⟧"), 1)

    def _make_volume(self, tmp: Path, name: str) -> tuple[Path, Path]:
        """构造一本含 2 段的临时原书 + 一份翻译存档。"""
        txt = tmp / f"{name}.txt"
        txt.write_text("\n".join(["제1장 시작", "본문 1입니다.", "본문 2입니다."]), encoding="utf-8")
        book = load_book(txt)
        config = AppConfig(chunk_chars=1800, max_paragraph_chars=2600)
        chunks = build_chunks(book, config)
        state = {
            "source": str(txt),
            "total_chunks": len(chunks),
            "completed": {chunks[0].id: [f"译{name}一", f"译{name}二"]},
            "failed": {},
            "chunk_chars": 1800,
            "max_paragraph_chars": 2600,
        }
        state_path = tmp / f"{name}.translation_state.json"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return txt, state_path

    def test_inspect_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, state = self._make_volume(tmp, "vol1")
            info = inspect_state(state)
            self.assertEqual(info["title"], "vol1")
            self.assertEqual(info["completed"], 1)
            self.assertEqual(info["failed"], 0)

    def test_book_from_state_reassembles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _, state = self._make_volume(tmp, "vol1")
            book = book_from_state(state, AppConfig())
            self.assertEqual(len(book.chapters), 1)
            self.assertEqual(book.chapters[0].paragraphs, ["译vol1一", "译vol1二"])

    def test_book_from_state_requires_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "bad.json"
            state.write_text(
                json.dumps({"source": str(Path(tmp) / "missing.txt"), "completed": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                book_from_state(state, AppConfig())

    def test_merge_books_inserts_volume_headings(self):
        b1 = Book(title="卷一", chapters=[Chapter(0, "第1章", ["a"])])
        b2 = Book(title="卷二", chapters=[Chapter(0, "第2章", ["b"]), Chapter(1, "第3章", ["c"])])
        merged = merge_books([b1, b2], title="测试合集")
        self.assertEqual(
            [ch.title for ch in merged.chapters],
            ["测试合集 第1卷", "第1章", "测试合集 第2卷", "第2章", "第3章"],
        )
        self.assertEqual([ch.index for ch in merged.chapters], [0, 1, 2, 3, 4])
        self.assertEqual(merged.title, "测试合集")

    def test_merge_books_volume_heading_without_prefix(self):
        b1 = Book(title="卷一", chapters=[Chapter(0, "第1章", ["a"])])
        merged = merge_books([b1])
        self.assertEqual([ch.title for ch in merged.chapters], ["第1卷", "第1章"])

    def test_preview_fix_does_not_modify(self):
        book = Book(title="测试", chapters=[Chapter(0, "第1章", ["崔范镇来了。"])])
        glossary = Glossary(
            entries=[GlossaryEntry(ko="최범진", zh="崔凡镇", confirmed=True, zh_history=["崔范镇"])]
        )
        stats = preview_fix(book, glossary)
        self.assertEqual(stats["hit_paragraphs"], 1)
        self.assertEqual(book.chapters[0].paragraphs, ["崔范镇来了。"])

    def test_fix_book_applies_glossary(self):
        book = Book(title="测试", chapters=[Chapter(0, "第1章", ["崔范镇来了。", "没有任何变化。"])])
        glossary = Glossary(
            entries=[GlossaryEntry(ko="최범진", zh="崔凡镇", confirmed=True, zh_history=["崔范镇"])]
        )
        stats = fix_book(book, glossary)
        self.assertEqual(stats["hit_paragraphs"], 1)
        self.assertEqual(stats["hit_sources"], 1)
        self.assertEqual(book.chapters[0].paragraphs[0], "崔凡镇来了。")
        self.assertEqual(book.chapters[0].paragraphs[1], "没有任何变化。")

    def test_export_merged_writes_txt(self):
        books = [
            Book(title="卷一", chapters=[Chapter(0, "第1章", ["崔范镇来了。"])]),
            Book(title="卷二", chapters=[Chapter(0, "第2章", ["他走了。"])]),
        ]
        glossary = Glossary(
            entries=[GlossaryEntry(ko="최범진", zh="崔凡镇", confirmed=True, zh_history=["崔范镇"])]
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = export_merged(
                books,
                glossary,
                AppConfig(),
                out,
                title="合集",
                output_txt=True,
                output_epub=False,
            )
            self.assertTrue(result["paths"])
            txt = result["paths"][0]
            self.assertTrue(txt.exists())
            content = txt.read_text(encoding="utf-8-sig")
            self.assertIn("崔凡镇来了。", content)
            self.assertIn("第1章", content)
            self.assertIn("第2章", content)
            self.assertIn("合集 第1卷", content)
            self.assertIn("合集 第2卷", content)

    def test_export_merged_requires_books(self):
        with self.assertRaises(ValueError):
            export_merged([], Glossary(), AppConfig(), Path("."))



class MergeStyleResourceTest(unittest.TestCase):
    def _volume(self, title: str, css: bytes, img: bytes) -> Book:
        return Book(
            title=title,
            chapters=[
                Chapter(
                    0,
                    "第1章",
                    ["正文 \u27e6img:Images/p1.png\u27e7 结尾"],
                    source_id="chap.xhtml",
                )
            ],
            metadata={
                "css_resources": [{"name": "Styles/main.css", "content": css}],
                "images": [{"name": "Images/p1.png", "content": img}],
                "doc_inline_css": {"chap.xhtml": ["p { color: red; }"]},
            },
        )

    def test_merge_books_dedupes_identical_css_and_images(self):
        css = b".center { text-align: center; }"
        b1 = self._volume("卷一", css, b"AAA")
        b2 = self._volume("卷二", css, b"AAA")
        merged = merge_books([b1, b2], title="合集")
        self.assertEqual(len(merged.metadata["css_resources"]), 1)
        self.assertEqual(len(merged.metadata["images"]), 1)
        self.assertEqual(
            merged.metadata["chapter_css"],
            {
                "v1:chap.xhtml": ["Styles/main.css"],
                "v2:chap.xhtml": ["Styles/main.css"],
            },
        )
        self.assertIn("v1:chap.xhtml", merged.metadata["doc_inline_css"])
        self.assertIn("v2:chap.xhtml", merged.metadata["doc_inline_css"])

    def test_merge_books_renames_conflicting_css_and_images_per_volume(self):
        b1 = self._volume("卷一", b".center { color: red; }", b"AAA")
        b2 = self._volume("卷二", b".center { color: blue; }", b"BBB")
        merged = merge_books([b1, b2], title="合集")
        css_names = [r["name"] for r in merged.metadata["css_resources"]]
        self.assertEqual(css_names, ["Styles/main.css", "Styles/main_v2.css"])
        self.assertEqual(
            merged.metadata["chapter_css"],
            {
                "v1:chap.xhtml": ["Styles/main.css"],
                "v2:chap.xhtml": ["Styles/main_v2.css"],
            },
        )
        img_names = [r["name"] for r in merged.metadata["images"]]
        self.assertIn("Images/p1.png", img_names)
        self.assertIn("Images/p1_v2.png", img_names)
        paras = [ch.paragraphs[0] for ch in merged.chapters if ch.title == "第1章"]
        self.assertEqual(len(paras), 2)
        self.assertIn("\u27e6img:Images/p1.png\u27e7", paras[0])
        self.assertIn("\u27e6img:Images/p1_v2.png\u27e7", paras[1])

    def test_merge_normalizes_parent_dir_image_names(self):
        """带 ../ 的图片应落到 Images/，正文与 CSS 引用同步改写。"""
        book = Book(
            title="卷一",
            chapters=[
                Chapter(
                    0,
                    "第1章",
                    ["正文 \u27e6img:../dnjakrdlTek123.png\u27e7 结尾"],
                    source_id="Text/chap.xhtml",
                )
            ],
            metadata={
                "css_resources": [
                    {"name": "Styles/sy.css", "content": b"body { background: url('../../dnjakrdlTek123.png'); }"},
                ],
                "images": [{"name": "../dnjakrdlTek123.png", "content": b"\x89PNG"}],
                "chapter_css": {"Text/chap.xhtml": ["Styles/sy.css"]},
            },
        )
        merged = merge_books([book])
        img_names = [r["name"] for r in merged.metadata["images"]]
        self.assertIn("Images/dnjakrdlTek123.png", img_names)
        self.assertNotIn("..", "".join(img_names))
        css = {r["name"]: r["content"] for r in merged.metadata["css_resources"]}
        self.assertIn(b"Images/dnjakrdlTek123.png", css["Styles/sy.css"])
        paras = [ch.paragraphs[0] for ch in merged.chapters if ch.title != "第1卷"]
        self.assertIn("\u27e6img:Images/dnjakrdlTek123.png\u27e7", paras[0])

    def test_merge_rewrites_css_relative_resources_with_full_paths(self):
        b1 = Book(
            title="卷一",
            chapters=[Chapter(0, "第1章", ["正文"], source_id="Text/chap.xhtml")],
            metadata={
                "css_resources": [
                    {"name": "Styles/main.css", "content": b"@import 'nested/extra.css'; body { background: url('../Images/bg.png'); }"},
                    {"name": "Styles/nested/extra.css", "content": b"@font-face { src: url('../../Fonts/main.woff2'); }"},
                    {"name": "Images/bg.png", "content": b"BG1"},
                    {"name": "Fonts/main.woff2", "content": b"FONT1"},
                ],
                "chapter_css": {"Text/chap.xhtml": ["Styles/main.css"]},
            },
        )
        b2 = Book(
            title="卷二",
            chapters=[Chapter(0, "第1章", ["正文"], source_id="Text/chap.xhtml")],
            metadata={
                "css_resources": [
                    {"name": "Styles/main.css", "content": b"@import 'nested/extra.css'; body { background: url('../Images/bg.png'); }"},
                    {"name": "Styles/nested/extra.css", "content": b"@font-face { src: url('../../Fonts/main.woff2'); }"},
                    {"name": "Images/bg.png", "content": b"BG2"},
                    {"name": "Fonts/main.woff2", "content": b"FONT2"},
                ],
                "chapter_css": {"Text/chap.xhtml": ["Styles/main.css"]},
            },
        )
        merged = merge_books([b1, b2], title="合集")
        css = {item["name"]: item["content"] for item in merged.metadata["css_resources"]}
        self.assertIn("Styles/main_v2.css", css)
        self.assertIn("Styles/nested/extra_v2.css", css)
        self.assertIn(b"main_v2.woff2", css["Styles/nested/extra_v2.css"])
        self.assertIn(b"bg_v2.png", css["Styles/main_v2.css"])

    def test_book_from_state_restores_metadata(self):
        import base64

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            txt = tmp / "vol1.txt"
            txt.write_text("제1장 시작\n본문입니다.", encoding="utf-8")
            book = load_book(txt)
            config = AppConfig(chunk_chars=1800, max_paragraph_chars=2600)
            chunks = build_chunks(book, config)
            state = {
                "source": str(txt),
                "total_chunks": len(chunks),
                "completed": {chunks[0].id: ["译文一", "译文二"]},
                "failed": {},
                "chunk_chars": 1800,
                "max_paragraph_chars": 2600,
                "metadata": {
                    "css_resources": [
                        {
                            "name": "Styles/main.css",
                            "content_b64": base64.b64encode(b".center{}").decode("ascii"),
                        }
                    ],
                    "images": [
                        {
                            "name": "Images/p1.png",
                            "content_b64": base64.b64encode(b"\x89PNG").decode("ascii"),
                        }
                    ],
                },
            }
            state_path = tmp / "vol1.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            restored = book_from_state(state_path, AppConfig())
        self.assertEqual(
            restored.metadata["css_resources"][0]["content"], b".center{}"
        )
        self.assertEqual(restored.metadata["images"][0]["content"], b"\x89PNG")



class ChapterTitleMergeTest(unittest.TestCase):
    def test_merge_books_heading_levels(self):
        b1 = Book(title="卷一", chapters=[Chapter(0, "제1장", ["a"])])
        b2 = Book(title="卷二", chapters=[Chapter(0, "제2장", ["b"])])
        merged = merge_books([b1, b2], title="合集")
        self.assertEqual([ch.title for ch in merged.chapters], ["合集 第1卷", "제1장", "合集 第2卷", "제2장"])
        self.assertEqual([ch.heading_level for ch in merged.chapters], [1, 2, 1, 2])

    def test_merge_books_carries_title_zh(self):
        ch = Chapter(0, "제1장", ["a"], title_zh="第一章")
        merged = merge_books([Book(title="卷一", chapters=[ch])], title="合集")
        self.assertEqual(merged.chapters[1].title_zh, "第一章")
        self.assertEqual(merged.chapters[1].display_title, "第一章")

    def test_book_from_state_restores_title_zh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            txt = tmp / "vol1.txt"
            txt.write_text("제1장 시작\n본문입니다.", encoding="utf-8")
            book = load_book(txt)
            config = AppConfig(chunk_chars=1800, max_paragraph_chars=2600)
            chunks = build_chunks(book, config)
            state = {
                "source": str(txt),
                "total_chunks": len(chunks),
                "completed": {chunks[0].id: ["译文一", "译文二"]},
                "failed": {},
                "chunk_chars": 1800,
                "max_paragraph_chars": 2600,
                "chapter_titles": {"0": {"ko": "제1장 시작", "zh": "第一章 开始"}},
            }
            state_path = tmp / "vol1.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            restored = book_from_state(state_path, AppConfig())
        self.assertEqual(restored.chapters[0].title_zh, "第一章 开始")
        self.assertEqual(restored.chapters[0].display_title, "第一章 开始")

    def test_book_from_state_raises_on_structure_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            txt = tmp / "vol1.txt"
            txt.write_text("본문입니다.", encoding="utf-8")
            state = {
                "source": str(txt),
                "total_chunks": 5,
                "completed": {"ch-00000-00000": ["旧译文"]},
                "failed": {},
            }
            state_path = tmp / "vol1.translation_state.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                book_from_state(state_path, AppConfig())


class MergeHierarchyTest(unittest.TestCase):
    def test_merge_books_builds_volume_tree(self):
        b1 = Book(title="卷一", chapters=[Chapter(0, "제1장", ["a"]), Chapter(1, "제2장", ["b"])])
        b2 = Book(title="卷二", chapters=[Chapter(0, "제3장", ["c"])])
        merged = merge_books([b1, b2], title="合集")
        self.assertEqual(
            [ch.title for ch in merged.chapters],
            ["合集 第1卷", "제1장", "제2장", "合集 第2卷", "제3장"],
        )
        self.assertEqual([ch.heading_level for ch in merged.chapters], [1, 2, 2, 1, 2])
        self.assertEqual([ch.parent_index for ch in merged.chapters], [None, 0, 0, None, 3])
        self.assertEqual([ch.is_section for ch in merged.chapters], [True, False, False, True, False])

    def test_merge_books_shifts_nested_levels(self):
        src = Book(
            title="源",
            chapters=[
                Chapter(0, "第1篇", [], heading_level=1, is_section=True),
                Chapter(1, "제1장", ["x"], heading_level=2, parent_index=0),
                Chapter(2, "제2장", ["y"], heading_level=2, parent_index=0),
            ],
        )
        merged = merge_books([src], title="合集")
        self.assertEqual([ch.heading_level for ch in merged.chapters], [1, 2, 3, 3])
        self.assertEqual([ch.parent_index for ch in merged.chapters], [None, 0, 1, 1])
        self.assertEqual([ch.is_section for ch in merged.chapters], [True, True, False, False])


class DetectMergeTitleTest(unittest.TestCase):
    def test_single_archive_uses_filename(self):
        self.assertEqual(detect_merge_title(["魂火"]), "魂火")

    def test_identical_titles_kept(self):
        self.assertEqual(detect_merge_title(["魂火", "魂火"]), "魂火")

    def test_multi_volume_common_prefix(self):
        self.assertEqual(detect_merge_title(["魂火1", "魂火2"]), "魂火")
        self.assertEqual(detect_merge_title(["魂火 上", "魂火 下"]), "魂火")

    def test_separator_is_stripped(self):
        self.assertEqual(detect_merge_title(["魂火·上", "魂火·下"]), "魂火")

    def test_empty_and_blank(self):
        self.assertEqual(detect_merge_title([]), "")
        self.assertEqual(detect_merge_title(["", "  "]), "")

    def test_unrelated_titles_return_empty(self):
        self.assertEqual(detect_merge_title(["甲书", "乙书"]), "")


class ArchiveFilenameTitleTest(unittest.TestCase):
    def test_strips_state_suffix_and_volume_marker(self):
        self.assertEqual(archive_filename_title(Path("烟灰_第1卷.translation_state.json")), "烟灰")
        self.assertEqual(archive_filename_title(Path("烟灰_第4卷.json")), "烟灰")

    def test_strips_leading_hidden_dot(self):
        self.assertEqual(archive_filename_title(Path(".Kiss_me_Liar_第1卷.translation_state.json")), "Kiss_me_Liar")
        self.assertEqual(archive_filename_title(Path(".烟灰_第2卷.translation_state.json")), "烟灰")

    def test_handles_separators_and_cn_korean_volume_words(self):
        self.assertEqual(archive_filename_title(Path("烟灰 上卷.translation_state.json")), "烟灰")
        self.assertEqual(archive_filename_title(Path("烟灰·外传.translation_state.json")), "烟灰")
        self.assertEqual(archive_filename_title(Path("烟灰 1권.translation_state.json")), "烟灰")

    def test_plain_name_preserved(self):
        self.assertEqual(archive_filename_title(Path("烟灰.translation_state.json")), "烟灰")


class MergeCommonGlossaryTest(unittest.TestCase):
    def _books(self, text: str) -> list[Book]:
        return [Book(title="卷一", chapters=[Chapter(0, "第1章", [text])])]

    def test_preview_fix_reports_common_and_dedicated(self):
        book = self._books("俊希 范振 番外")[0]
        dedicated = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=True, alternatives="俊希"),
                GlossaryEntry(ko="범진", zh="范镇", confirmed=True, alternatives="范振"),
            ]
        )
        common = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙（通用）", confirmed=True, alternatives="俊希"),
                GlossaryEntry(ko="", zh="外传", confirmed=True, alternatives="番外"),
            ]
        )
        stats = preview_fix(book, dedicated, common)
        self.assertEqual(stats["hit_paragraphs"], 1)
        self.assertGreaterEqual(stats["common_sources"], 1)
        self.assertGreaterEqual(stats["dedicated_sources"], 1)
        # 预检不应修改正文
        self.assertEqual(book.chapters[0].paragraphs[0], "俊希 范振 番外")

    def test_fix_book_applies_common_and_overrides(self):
        book = self._books("俊希 范振 番外")[0]
        dedicated = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=True, alternatives="俊希"),
                GlossaryEntry(ko="범진", zh="范镇", confirmed=True, alternatives="范振"),
            ]
        )
        common = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙（通用）", confirmed=True, alternatives="俊希"),
                GlossaryEntry(ko="", zh="外传", confirmed=True, alternatives="番外"),
            ]
        )
        stats = fix_book(book, dedicated, common)
        self.assertEqual(stats["common_override_count"], 1)
        self.assertEqual(book.chapters[0].paragraphs[0], "俊熙（通用） 范镇 外传")
        by_ko = {e.ko: e for e in dedicated.entries}
        self.assertEqual(by_ko["준희"].zh, "俊熙（通用）")


if __name__ == "__main__":
    unittest.main()
