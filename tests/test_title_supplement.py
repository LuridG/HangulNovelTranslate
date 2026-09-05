import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hangul_novel_translator.book import Book, Chapter, export_epub
from hangul_novel_translator.epub_title_supplement import (
    analyze_abnormal_chapters,
    apply_organize_titles,
    apply_split_chapters,
    preview_organize_titles,
    preview_split_chapters,
)


def _chapter(title, paras, index=1, level=2, parent=None):
    return Chapter(
        index=index,
        title=title,
        paragraphs=paras,
        heading_level=level,
        parent_index=parent,
    )


def _make_epub(tmp, chapters, title="测试书"):
    book = Book(title=title, chapters=chapters)
    out = Path(tmp) / "novel.epub"
    export_epub(book, out, source_title=title)
    return out


class AbnormalChapterTest(unittest.TestCase):
    def test_flags_huge_chapter_as_abnormal(self):
        with tempfile.TemporaryDirectory() as tmp:
            chapters = []
            for i in range(1, 11):
                chapters.append(_chapter(f"Chapter {i} - 正常", ["正文。" * 40], index=i))
            # 一个异常超大章节，夹杂多个子章节。
            sub_paras = []
            for j in range(1, 6):
                sub_paras.append(f"EP.{j} 子标题 j")
                sub_paras.append("子章节正文。" * 200)
            chapters.append(_chapter("Chapter 11 - 异常超大", sub_paras, index=11))
            epub = _make_epub(tmp, chapters)
            result = analyze_abnormal_chapters(epub)
        abnormal = {c["index"] for c in result["abnormal"]}
        self.assertIn(11, abnormal)
        self.assertNotIn(1, abnormal)
        self.assertEqual(result["abnormal_count"], 1)

    def test_threshold_controls_sensitivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            chapters = [
                _chapter("Chapter 1 - 正常", ["正文。" * 50], index=1),
                _chapter("Chapter 2 - 略长", ["正文。" * 80], index=2),
                _chapter("Chapter 3 - 正常", ["正文。" * 50], index=3),
            ]
            epub = _make_epub(tmp, chapters)
            loose = analyze_abnormal_chapters(epub, threshold_pct=50)
            tight = analyze_abnormal_chapters(epub, threshold_pct=5)
        self.assertEqual(loose["abnormal_count"], 0)
        self.assertGreaterEqual(tight["abnormal_count"], 1)


class SplitChaptersTest(unittest.TestCase):
    def _synthetic(self, tmp):
        big_paras = []
        big_paras.append("开篇正文。")
        big_paras.append("EP.343 IF : 如果她是勇者的话。(17)")
        big_paras.append("第一段正文。")
        big_paras.append("EP.344 IF : 如果她是勇者的话。(完)")
        big_paras.append("第二段正文。")
        big_paras.append("EP.345 IF : 如果她是勇者。 - 后日谈。(1)")
        big_paras.append("第三段正文。")
        chapters = [_chapter("Chapter 342 - IF", big_paras, index=1)]
        return _make_epub(tmp, chapters)

    def test_preview_split_counts_sub_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = self._synthetic(tmp)
            result = preview_split_chapters(epub, r"^EP\.\d+", [1])
        detail = result["details"][0]
        self.assertEqual(detail["status"], "split")
        # 开篇（原章节）+ 3 个子章节。
        self.assertEqual(len(detail["sub_chapters"]), 4)
        self.assertEqual(result["sub_chapter_count"], 4)

    def test_apply_split_appends_after_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = self._synthetic(tmp)
            out = Path(tmp) / "split.epub"
            result = apply_split_chapters(epub, out, r"^EP\.\d+", [1])
            from hangul_novel_translator.book import load_book
            book = load_book(out)
            titles = [c.title for c in book.chapters]
        self.assertEqual(result["chapter_count"], 4)
        self.assertEqual(titles[0], "Chapter 342 - IF")
        self.assertEqual(titles[1], "EP.343 IF : 如果她是勇者的话。(17)")
        self.assertEqual(titles[3], "EP.345 IF : 如果她是勇者。 - 后日谈。(1)")


class OrganizeTitlesTest(unittest.TestCase):
    def _synthetic(self, tmp):
        chapters = [
            _chapter("书 第1卷", [], index=1, level=1, parent=None),
            _chapter("Chapter 2 - 幸福结局.(2)", ["正文。"], index=2, level=2, parent=1),
            _chapter("Chapter 1 - 幸福结局.(1)", ["正文。"], index=3, level=2, parent=1),
        ]
        return _make_epub(tmp, chapters)

    def test_use_original_number_keeps_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = self._synthetic(tmp)
            result = preview_organize_titles(
                epub, prefix_template="第 {n} 章", use_original_number=True
            )
            mapping = {m["title"]: m for m in result["mapping"]}
        self.assertEqual(mapping["Chapter 1 - 幸福结局.(1)"]["new_title"], "第 1 章 - 幸福结局.(1)")
        self.assertEqual(mapping["Chapter 2 - 幸福结局.(2)"]["new_title"], "第 2 章 - 幸福结局.(2)")

    def test_discard_number_uses_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = self._synthetic(tmp)
            result = preview_organize_titles(
                epub, prefix_template="chapter ", use_original_number=False
            )
            mapping = {m["index"]: m for m in result["mapping"]}
        # 组内按阅读顺序：index2 在前（seq1），index3 在后（seq2）。
        self.assertEqual(mapping[2]["new_title"], "chapter 1 Chapter 2 - 幸福结局.(2)")
        self.assertEqual(mapping[3]["new_title"], "chapter 2 Chapter 1 - 幸福结局.(1)")

    def test_apply_organize_preserves_volume_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            epub = self._synthetic(tmp)
            out = Path(tmp) / "organized.epub"
            result = apply_organize_titles(
                epub, out, prefix_template="第 {n} 章", use_original_number=True
            )
            from hangul_novel_translator.book import load_book
            book = load_book(out)
            parsed = [(c.heading_level, c.parent_index) for c in book.chapters]
            titles = [c.title for c in book.chapters]
        self.assertEqual(result["chapter_count"], 3)
        # 卷（h1）保持 parent=None，章节（h2）保持 parent=1。
        self.assertEqual(parsed[0], (1, None))
        self.assertEqual(parsed[1], (2, 1))
        self.assertEqual(parsed[2], (2, 1))
        # 卷页标题默认保持原样，不套用内容前缀。
        self.assertEqual(titles[0], "书 第1卷")


if __name__ == "__main__":
    unittest.main()
