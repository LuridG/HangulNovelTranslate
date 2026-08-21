import unittest
from dataclasses import dataclass, field


@dataclass
class DummyChapter:
    index: int
    title: str
    paragraphs: list[str] = field(default_factory=list)


def select_strided_chapters(chapters: list[DummyChapter], target_count: int = 6, min_len: int = 300) -> list[DummyChapter]:
    """对应 docs/glossary_sampling_plan.md 中的分桶采样算法原型。"""
    valid = [
        (idx, ch) for idx, ch in enumerate(chapters)
        if sum(len(p) for p in ch.paragraphs) >= min_len
    ]
    if not valid:
        valid = list(enumerate(chapters))

    total = len(valid)
    if total <= target_count:
        return [ch for _, ch in valid]

    selected = []
    bucket_size = total / target_count
    for b in range(target_count):
        start = int(b * bucket_size)
        end = int((b + 1) * bucket_size) if b < target_count - 1 else total
        bucket = valid[start:end]
        if bucket:
            best_idx, best_ch = max(bucket, key=lambda it: sum(len(p) for p in it[1].paragraphs))
            selected.append((best_idx, best_ch))

    selected.sort(key=lambda it: it[0])
    return [ch for _, ch in selected]


class TestStridedSampling(unittest.TestCase):
    def test_short_book_fallback(self):
        # 章节少于目标采样数，全部返回
        chapters = [DummyChapter(i, f"第{i}章", ["正文内容" * 100]) for i in range(4)]
        sampled = select_strided_chapters(chapters, target_count=6)
        self.assertEqual(len(sampled), 4)

    def test_filter_junk_short_chapters(self):
        # 忽略封面、版权页（< 300 字）
        chapters = [
            DummyChapter(0, "封面", ["封面标题"]), # 4 字
            DummyChapter(1, "第1章", ["正文1" * 200]),
            DummyChapter(2, "第2章", ["正文2" * 200]),
            DummyChapter(3, "版权", ["版权信息"]), # 4 字
        ]
        sampled = select_strided_chapters(chapters, target_count=2, min_len=300)
        self.assertEqual(len(sampled), 2)
        self.assertEqual(sampled[0].title, "第1章")
        self.assertEqual(sampled[1].title, "第2章")

    def test_long_book_strided_coverage(self):
        # 60 章的长篇合集，均匀分 6 桶采样，覆盖前中后期
        chapters = [DummyChapter(i, f"第{i+1}章", [f"内容{i}" * 150]) for i in range(60)]
        sampled = select_strided_chapters(chapters, target_count=6)
        self.assertEqual(len(sampled), 6)
        indices = [ch.index for ch in sampled]
        # 检查是否跨越前、中、后各个阶段
        self.assertTrue(any(idx < 15 for idx in indices))  # 前期
        self.assertTrue(any(20 <= idx <= 40 for idx in indices))  # 中期
        self.assertTrue(any(idx >= 45 for idx in indices))  # 后期


if __name__ == "__main__":
    unittest.main()
