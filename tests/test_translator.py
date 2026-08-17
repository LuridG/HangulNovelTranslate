import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 未安装 openai 时，仅用替身完成纯逻辑测试。
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

from hangul_novel_translator.book import Book, Chapter
from hangul_novel_translator.config import AppConfig
from hangul_novel_translator.translator import build_chunks, collect_sample_text, reconcile_paragraphs


class TranslatorLogicTest(unittest.TestCase):
    def test_build_chunks_keeps_chapter_groups(self):
        book = Book(
            title="测试",
            chapters=[
                Chapter(0, "제1장", ["가나다" * 300, "라마바" * 300]),
                Chapter(1, "제2장", ["사아자" * 300]),
            ],
        )
        config = AppConfig(chunk_chars=700, max_paragraph_chars=1000)
        chunks = build_chunks(book, config)
        self.assertTrue(all(c.id.startswith("ch-") for c in chunks))
        self.assertLessEqual(max(c.char_count for c in chunks), 750)
        self.assertEqual(len({c.chapter_index for c in chunks}), 2)

    def test_reconcile_pads_or_truncates(self):
        self.assertEqual(reconcile_paragraphs(["가", "나"], 2), ["가", "나"])
        self.assertEqual(reconcile_paragraphs(["가"], 3), ["가", "", ""])
        self.assertEqual(reconcile_paragraphs(["가", "나", "다"], 2), ["가", "나"])

    def test_collect_sample_text(self):
        book = Book(
            title="测试",
            chapters=[Chapter(0, "제1장", ["가" * 500, "나" * 500]), Chapter(1, "제2장", ["다" * 500])],
        )
        config = AppConfig(extract_sample_chars=800, extract_sample_chapters=2)
        sample = collect_sample_text(book, config)
        self.assertIn("가", sample)
        self.assertGreater(len(sample), 0)

    def test_collect_sample_prefers_longest_chapter(self):
        book = Book(
            title="测试",
            chapters=[
                Chapter(0, "목차", ["목차"]),
                Chapter(1, "제1장", ["가나다라마바사" * 1000]),
            ],
        )
        config = AppConfig(extract_sample_chars=200, extract_sample_chapters=2)
        sample = collect_sample_text(book, config)
        self.assertNotIn("목차", sample)
        self.assertIn("가나다라마바사", sample)


if __name__ == "__main__":
    unittest.main()
