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
    book_from_state,
    export_merged,
    fix_book,
    inspect_state,
    merge_books,
    preview_fix,
)
from hangul_novel_translator.translator import build_chunks


class MergeLogicTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()