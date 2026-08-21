"""全书跨度采样（Strided/Distributed Sampling）。

对应 docs/glossary_sampling_plan.md：在多卷/超长合集里，单纯取“最长几章”会遗漏中后期
登场的人物与术语。本模块改为按全书阅读进度均匀分桶，每个桶内挑最长代表章节，再按配额拼接。
"""
from __future__ import annotations

import math

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


def select_region_chapters(
    chapters: list,
    *,
    regions: int = 3,
    per_region: int = 2,
    min_chapter_len: int = 300,
    skip: int = 0,
) -> list:
    """按前/中/后区域各取最长代表章，保证整本书从头到尾都有采样覆盖。

    与 select_strided_chapters 的区别：先按阅读顺序切成 regions 个连续区间，
    再从每个区间内取 per_region 个最长章节，避免桶边界挤压导致某个区域被漏掉。
    skip 用于“提取更多”的章节偏移：每轮在区域内按字数降序向后跳过 skip 个，
    使连续几轮抽到不同章节，而不是重复同一批。
    """
    valid = [
        (idx, ch)
        for idx, ch in enumerate(chapters)
        if _chapter_len(ch) >= min_chapter_len
    ]
    if not valid:
        valid = list(enumerate(chapters))

    total = len(valid)
    target = min(regions * per_region, total)
    if total <= target:
        return [ch for _, ch in valid]

    region_count = min(regions, total)
    selected: list[tuple[int, object]] = []
    for r in range(region_count):
        start = int(r * total / region_count)
        end = int((r + 1) * total / region_count) if r < region_count - 1 else total
        bucket = valid[start:end]
        count = min(per_region, len(bucket))
        sorted_bucket = sorted(bucket, key=lambda item: _chapter_len(item[1]), reverse=True)
        best = sorted_bucket[skip : skip + count]
        selected.extend(best)

    selected.sort(key=lambda item: item[0])
    return [ch for _, ch in selected]


def _book_total_chars(book) -> int:
    return sum(_chapter_len(ch) for ch in book.chapters)


def _sample_budget(total_chars: int, config) -> int:
    """采样字符预算：以 extract_sample_chars 为下限，随全书字数放大并封顶。"""
    base = int(getattr(config, "extract_sample_chars", 30000))
    per_100k = int(getattr(config, "extract_sample_chars_per_100k", 0))
    cap = int(getattr(config, "extract_sample_chars_cap", base))
    scaled = base + per_100k * (total_chars // 100000)
    return min(cap, max(base, scaled))


def _region_plan(config, budget: int) -> tuple[int, int]:
    """按预算推算前/中/后区域与每区章节数，让长书抽更多章。"""
    regions = int(getattr(config, "extract_sample_regions", 3))
    base_per_region = int(getattr(config, "extract_sample_per_region", 2))
    base_chapters = int(getattr(config, "extract_sample_chapters", 6))
    base = int(getattr(config, "extract_sample_chars", 30000))
    base_per_chapter = max(3000, base // max(1, base_chapters))
    target_chapters = max(base_chapters, budget // base_per_chapter)
    per_region = max(base_per_region, math.ceil(target_chapters / max(1, regions)))
    return regions, per_region


def _select_sampled_chapters(chapters: list, config, total_chars: int, sample_round: int = 0) -> list:
    """按配置挑选采样章节：优先区域采样，否则退化为跨度分桶。"""
    regions = getattr(config, "extract_sample_regions", None)
    per_region = getattr(config, "extract_sample_per_region", None)
    if regions is not None and per_region is not None:
        budget = _sample_budget(total_chars, config)
        regions, per_region = _region_plan(config, budget)
        return select_region_chapters(
            chapters,
            regions=regions,
            per_region=per_region,
            skip=sample_round * per_region,
        )
    return select_strided_chapters(
        chapters,
        target_count=int(getattr(config, "extract_sample_chapters", 6)),
    )


def collect_sample_text_strided(
    book,
    config,
    *,
    min_chapter_len: int = 300,
    sample_round: int = 0,
) -> str:
    """按全书跨度均匀采样章节文本，确保多卷合集从头到尾的专有名词都能被捕捉。

    参数：
        book：Book 对象（含 chapters 列表，章节含 paragraphs 列表）。
        config：AppConfig，使用 extract_sample_chars / extract_sample_chapters。
        min_chapter_len：过滤封面/版权等短文档的字数阈值。

    返回：
        按阅读顺序拼接的样章文本，总长不超过 extract_sample_chars。
    """
    total_chars = _book_total_chars(book)
    selected = _select_sampled_chapters(book.chapters, config, total_chars, sample_round)
    if not selected:
        return ""

    budget = _sample_budget(total_chars, config)
    sample: list[str] = []
    chars = 0
    per_chapter_quota = max(3000, budget // len(selected))
    for chapter in selected:
        chapter_chars = 0
        for paragraph in chapter.paragraphs:
            text = strip_inline_markers(paragraph).strip()
            if not text:
                continue
            # 严格不超预算：超过 extract_sample_chars 或单章配额时截断到剩余额度。
            allowed = min(
                len(text),
                budget - chars,
                per_chapter_quota - chapter_chars,
            )
            if allowed <= 0:
                break
            piece = text[:allowed]
            sample.append(piece)
            chars += len(piece)
            chapter_chars += len(piece)
            if chars >= budget or chapter_chars >= per_chapter_quota:
                break
        if chars >= budget:
            break
    return "\n".join(sample)


def sample_chapter_report(book, config, *, min_chapter_len: int = 300, sample_round: int = 0) -> dict:
    """对全书跨度采样做“分章校验”，返回选中章节与前后中覆盖信息。

    返回：
        total_chapters / valid_chapters / sample_chapters：全书章数、有效章数、抽样章数。
        selected：每章 {index,title,chars,position,progress} 列表，position 为阅读顺序下标。
        positions：选中章按阅读顺序排列的下标。
        first_progress / last_progress：首尾选中章的阅读进度（0.0~1.0）。
        middle_covered：是否覆盖了中段（25%~75%）章节。
        spans_whole：是否达到“前/后/中”整体覆盖，或全量抽样。
        all_chapters：是否等于全量（章节数少时自然全量）。
    """
    chapters = book.chapters
    total = len(chapters)
    valid = [ch for ch in chapters if _chapter_len(ch) >= min_chapter_len] or list(chapters)
    total_chars = _book_total_chars(book)
    selected = _select_sampled_chapters(chapters, config, total_chars, sample_round)

    pos_by_id = {id(ch): pos for pos, ch in enumerate(chapters)}
    selected_positions = sorted(pos_by_id[id(ch)] for ch in selected)

    def progress_of(pos: int) -> float:
        if total <= 1:
            return 1.0
        return round(pos / (total - 1), 3)

    selected_info: list[dict] = []
    for ch in selected:
        pos = pos_by_id[id(ch)]
        selected_info.append(
            {
                "index": ch.index,
                "title": ch.title,
                "chars": _chapter_len(ch),
                "position": pos,
                "progress": progress_of(pos),
            }
        )

    middle_covered = any(0.25 <= item["progress"] <= 0.75 for item in selected_info)
    first_progress = progress_of(selected_positions[0]) if selected_positions else None
    last_progress = progress_of(selected_positions[-1]) if selected_positions else None
    all_chapters = bool(selected) and len(selected) == total
    spans_whole = all_chapters or (
        first_progress is not None
        and first_progress <= 0.34
        and last_progress is not None
        and last_progress >= 0.66
        and middle_covered
    )

    return {
        "total_chapters": total,
        "valid_chapters": len(valid),
        "sample_chapters": len(selected),
        "total_chars": total_chars,
        "budget_chars": _sample_budget(total_chars, config),
        "selected": selected_info,
        "positions": selected_positions,
        "first_progress": first_progress,
        "last_progress": last_progress,
        "middle_covered": middle_covered,
        "spans_whole": spans_whole,
        "all_chapters": all_chapters,
    }


def format_sample_chapters(report: dict) -> str:
    """把分章校验结果压缩为一行可读日志。"""
    if not report.get("selected"):
        return "无有效章节，无法校验"
    positions = report.get("positions") or []
    pos_str = ",".join(str(p) for p in positions)
    first = int((report.get("first_progress") or 0) * 100)
    last = int((report.get("last_progress") or 0) * 100)
    total_chars = report.get("total_chars")
    budget_chars = report.get("budget_chars")
    size_str = ""
    if budget_chars is not None:
        if total_chars is not None and total_chars >= 10000:
            actual = min(budget_chars, total_chars)
            size_str = f"，样本 {actual}/{total_chars} 字"
        else:
            size_str = f"，样本 {budget_chars} 字"
    if report.get("all_chapters"):
        status = "全章覆盖"
    elif report.get("spans_whole"):
        status = "前中后覆盖达标"
    else:
        status = "覆盖不足，建议提高抽样章数"
    return (
        f"{report.get('sample_chapters')}/{report.get('valid_chapters')} 章"
        f"{size_str}，位置[{pos_str}]，进度 {first}%~{last}%，{status}"
    )
