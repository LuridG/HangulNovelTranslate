"""全书跨度采样（Strided/Distributed Sampling）。

对应 docs/glossary_sampling_plan.md：在多卷/超长合集里，单纯取“最长几章”会遗漏中后期
登场的人物与术语。本模块改为按全书阅读进度均匀分桶，每个桶内挑最长代表章节，再按配额拼接。
"""
from __future__ import annotations

from .book import strip_inline_markers


def _chapter_len(chapter) -> int:
    """章节正文字符数：按段落累计，避免 join 额外开销。"""
    return sum(len(p) for p in chapter.paragraphs)


def select_strided_chapters(
    chapters: list,
    *,
    target_count: int = 6,
    min_chapter_len: int = 300,
) -> list:
    """按全书跨度均匀分桶选出代表性章节，返回按原书出现顺序排序的章节列表。

    过滤封面/版权/目录等过短章节（< min_chapter_len 字）；若全部过短则退化为全量。
    章节数不超过 target_count 时，直接返回全部有效章节（单卷/短篇回退）。
    """
    valid = [
        (idx, ch)
        for idx, ch in enumerate(chapters)
        if _chapter_len(ch) >= min_chapter_len
    ]
    if not valid:
        valid = list(enumerate(chapters))

    total = len(valid)
    if total <= target_count:
        return [ch for _, ch in valid]

    selected: list[tuple[int, object]] = []
    bucket_size = total / target_count
    for b in range(target_count):
        start = int(b * bucket_size)
        end = int((b + 1) * bucket_size) if b < target_count - 1 else total
        bucket = valid[start:end]
        if not bucket:
            continue
        best = max(bucket, key=lambda item: _chapter_len(item[1]))
        selected.append(best)

    selected.sort(key=lambda item: item[0])
    return [ch for _, ch in selected]


def collect_sample_text_strided(
    book,
    config,
    *,
    min_chapter_len: int = 300,
) -> str:
    """按全书跨度均匀采样章节文本，确保多卷合集从头到尾的专有名词都能被捕捉。

    参数：
        book：Book 对象（含 chapters 列表，章节含 paragraphs 列表）。
        config：AppConfig，使用 extract_sample_chars / extract_sample_chapters。
        min_chapter_len：过滤封面/版权等短文档的字数阈值。

    返回：
        按阅读顺序拼接的样章文本，总长不超过 extract_sample_chars。
    """
    selected = select_strided_chapters(
        book.chapters,
        target_count=config.extract_sample_chapters,
        min_chapter_len=min_chapter_len,
    )
    if not selected:
        return ""

    sample: list[str] = []
    chars = 0
    per_chapter_quota = max(3000, config.extract_sample_chars // len(selected))
    for chapter in selected:
        chapter_chars = 0
        for paragraph in chapter.paragraphs:
            text = strip_inline_markers(paragraph).strip()
            if not text:
                continue
            # 严格不超预算：超过 extract_sample_chars 或单章配额时截断到剩余额度。
            allowed = min(
                len(text),
                config.extract_sample_chars - chars,
                per_chapter_quota - chapter_chars,
            )
            if allowed <= 0:
                break
            piece = text[:allowed]
            sample.append(piece)
            chars += len(piece)
            chapter_chars += len(piece)
            if chars >= config.extract_sample_chars or chapter_chars >= per_chapter_quota:
                break
        if chars >= config.extract_sample_chars:
            break
    return "\n".join(sample)
