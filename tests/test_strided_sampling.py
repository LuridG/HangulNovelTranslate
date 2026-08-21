import unittest
from types import SimpleNamespace

from hangul_novel_translator.book import Book, Chapter
from hangul_novel_translator.sampling import (
    collect_sample_text_strided,
    format_sample_chapters,
    sample_chapter_report,
    select_strided_chapters,
)


def make_chapter(index: int, title: str, body: str, repeats: int = 1) -> Chapter:
    return Chapter(index, title, [body * repeats], source_id=f"ch{index}")


def make_book(chapters: list[Chapter]) -> Book:
    return Book(title="测试书", chapters=chapters)


class TestStridedSampling(unittest.TestCase):
    def test_short_book_fallback(self):
        # 章节数少于目标采样数，全部返回。
        chapters = [make_chapter(i, f"第{i}章", "正文", 100) for i in range(4)]
        sampled = select_strided_chapters(chapters, target_count=6, min_chapter_len=300)
        self.assertEqual(len(sampled), 4)
        self.assertEqual([ch.index for ch in sampled], [0, 1, 2, 3])

    def test_filter_junk_short_chapters(self):
        # 忽略封面、版权页（< 300 字）。
        chapters = [
            make_chapter(0, "封面", "封面标题"),  # 4 字
            make_chapter(1, "第1章", "正文1", 200),
            make_chapter(2, "第2章", "正文2", 200),
            make_chapter(3, "版权", "版权信息"),  # 4 字
        ]
        sampled = select_strided_chapters(chapters, target_count=2, min_chapter_len=300)
        self.assertEqual(len(sampled), 2)
        self.assertEqual(sampled[0].title, "第1章")
        self.assertEqual(sampled[1].title, "第2章")

    def test_long_book_strided_coverage(self):
        # 60 章长篇合集，均匀分 6 桶，覆盖前中后期。
        chapters = [
            make_chapter(i, f"第{i+1}章", f"内容{i}", 150) for i in range(60)
        ]
        sampled = select_strided_chapters(chapters, target_count=6, min_chapter_len=300)
        self.assertEqual(len(sampled), 6)
        indices = [ch.index for ch in sampled]
        # 检查跨前、中、后各个阶段。
        self.assertTrue(any(idx < 15 for idx in indices), "应覆盖前期")
        self.assertTrue(any(20 <= idx <= 40 for idx in indices), "应覆盖中期")
        self.assertTrue(any(idx >= 45 for idx in indices), "应覆盖后期")
        # 结果按原书出现顺序排序。
        self.assertEqual(indices, sorted(indices))

    def test_collect_sample_text_respects_budget(self):
        # 每章正文 500 字，extract_sample_chars=1200，只截取前两桶内容。
        chapters = [
            make_chapter(i, f"第{i}章", "春江水暖鸭先知。", 100) for i in range(6)
        ]
        config = SimpleNamespace(extract_sample_chars=1200, extract_sample_chapters=3)
        sample = collect_sample_text_strided(
            make_book(chapters), config, min_chapter_len=100
        )
        # 预算按正文字符数统计，去掉拼接用的换行符后不超 1200。
        self.assertLessEqual(len(sample.replace("\n", "")), 1200)
        self.assertIn("春江水暖鸭先知。", sample)

    def test_collect_sample_text_all_junk_short(self):
        # 全部章节都过短时退化为全量，仍能产出样章。
        chapters = [make_chapter(0, "封面", "封面"), make_chapter(1, "版权", "版权")]
        config = SimpleNamespace(extract_sample_chars=30000, extract_sample_chapters=6)
        sample = collect_sample_text_strided(make_book(chapters), config, min_chapter_len=300)
        self.assertIn("封面", sample)
        self.assertIn("版权", sample)

    def test_sample_chapter_report_spans_whole_book(self):
        chapters = [
            make_chapter(i, f"第{i+1}章", f"内容{i}", 150) for i in range(60)
        ]
        config = SimpleNamespace(extract_sample_chars=30000, extract_sample_chapters=6)
        report = sample_chapter_report(make_book(chapters), config, min_chapter_len=300)
        self.assertEqual(report["total_chapters"], 60)
        self.assertEqual(report["sample_chapters"], 6)
        self.assertLessEqual(report["first_progress"], 0.34)
        self.assertGreaterEqual(report["last_progress"], 0.66)
        self.assertTrue(report["middle_covered"])
        self.assertTrue(report["spans_whole"])
        # 选中章按阅读顺序递增，且位置确实覆盖前中后。
        self.assertEqual(report["positions"], sorted(report["positions"]))
        self.assertTrue(any(0.25 <= item["progress"] <= 0.75 for item in report["selected"]))

    def test_sample_chapter_report_short_book_all(self):
        chapters = [make_chapter(i, f"第{i}章", "正文", 100) for i in range(3)]
        config = SimpleNamespace(extract_sample_chars=30000, extract_sample_chapters=6)
        report = sample_chapter_report(make_book(chapters), config, min_chapter_len=300)
        self.assertTrue(report["all_chapters"])
        self.assertEqual(report["sample_chapters"], 3)
        self.assertTrue(report["spans_whole"])

    def test_format_sample_chapters_readable(self):
        chapters = [
            make_chapter(i, f"第{i+1}章", f"内容{i}", 150) for i in range(30)
        ]
        config = SimpleNamespace(extract_sample_chars=30000, extract_sample_chapters=6)
        text = format_sample_chapters(
            sample_chapter_report(make_book(chapters), config, min_chapter_len=300)
        )
        self.assertIn("章，位置[", text)
        self.assertIn("前中后覆盖达标", text)


if __name__ == "__main__":
    unittest.main()
