"""成品标题补采 / 章节梳理服务。

在「成品矫正」的「标题补采」tab 提供两类操作：
1. 非正常章节登记：逐章分析字数，用随机采样估算基准字数，标志出偏离基准过多
   （默认上下 50%）的疑似非正常章节；用户可对这些章节按正则重新切分，把里面
   夹带的多个子章节拆出来并按顺序追加到原章节之后。
2. 章节梳理：检测章节标题里的数字与前后缀，按用户前缀模板与「使用原数字 /
   弃用原数字」两种方式重新整理标题；对分卷（h1 = 卷、h2 = 标题）保持树状层级。

所有改写都走「重新解析 → 修改章节 → 重新导出」流水线，复用仓库已验证的
``load_book`` / ``export_epub``，保证输出仍是合法 EPUB。
"""
from __future__ import annotations

import random
import re
import statistics
from pathlib import Path
from typing import Any

from .book import Book, Chapter, export_epub, load_book


_WORD_COUNT_RE = re.compile(r"本章字数\s*[:：]\s*\d+")
_FIRST_NUMBER_RE = re.compile(r"(\d+)")


def _is_word_count_line(text: str) -> bool:
    return bool(_WORD_COUNT_RE.search(text or ""))


def _chapter_chars(ch: Chapter) -> int:
    return sum(len(p) for p in ch.paragraphs)


def _renumber(chapters: list[Chapter]) -> list[Chapter]:
    """删除/新增章节后重排 index 并修正 parent_index，保持 TOC 层级。"""
    old_to_new = {ch.index: i + 1 for i, ch in enumerate(chapters)}
    for i, ch in enumerate(chapters):
        ch.index = i + 1
        if ch.parent_index is not None and ch.parent_index in old_to_new:
            ch.parent_index = old_to_new[ch.parent_index]
        else:
            ch.parent_index = None
    return chapters


def _estimate_center(values: list[int], sample_size: int, seed: int) -> float:
    """估算“正常章节基准字数”。

    按用户要求用随机采样求平均；为避免极端离群值把均值带偏，采用裁剪均值
    （去掉样本两端 10%），并在均值与中位数偏离过大时兜底回退到中位数。
    """
    if not values:
        return 0.0
    vals = list(values)
    if sample_size and sample_size < len(vals):
        rng = random.Random(seed)
        sample = rng.sample(vals, sample_size)
    else:
        sample = vals
    trimmed = sorted(sample)
    k = int(len(trimmed) * 0.1)
    if k > 0 and len(trimmed) - 2 * k >= 1:
        trimmed = trimmed[k:-k]
    mean = sum(trimmed) / len(trimmed)
    median = statistics.median(sample)
    if median > 0 and not (median * 0.5 <= mean <= median * 2):
        return float(median)
    return float(mean)


# ---------------- 非正常章节登记 ----------------
def analyze_abnormal_chapters(
    epub_path: str | Path,
    *,
    threshold_pct: float = 50,
    sample_size: int = 200,
    seed: int = 42,
) -> dict[str, Any]:
    """逐章统计字数，估算基准并标出超出 ±threshold_pct% 的疑似非正常章节。"""
    book = load_book(epub_path)
    all_chapters: list[dict[str, Any]] = []
    for ch in book.chapters:
        all_chapters.append(
            {
                "index": ch.index,
                "title": ch.title,
                "heading_level": ch.heading_level,
                "parent_index": ch.parent_index,
                "chars": _chapter_chars(ch),
            }
        )
    content = [c for c in all_chapters if c["chars"] > 0]
    if not content:
        return {
            "chapter_count": len(all_chapters),
            "content_count": 0,
            "average": 0.0,
            "threshold_pct": threshold_pct,
            "lower": 0.0,
            "upper": 0.0,
            "abnormal_count": 0,
            "normal_count": 0,
            "chapters": all_chapters,
            "abnormal": [],
        }
    center = _estimate_center([c["chars"] for c in content], sample_size, seed)
    lower = center * (1 - threshold_pct / 100)
    upper = center * (1 + threshold_pct / 100)
    for c in content:
        normal = lower <= c["chars"] <= upper
        c["normal"] = normal
        c["abnormal"] = not normal
    abnormal = [c for c in content if c["abnormal"]]
    return {
        "chapter_count": len(all_chapters),
        "content_count": len(content),
        "average": center,
        "threshold_pct": threshold_pct,
        "lower": lower,
        "upper": upper,
        "abnormal_count": len(abnormal),
        "normal_count": len(content) - len(abnormal),
        "chapters": all_chapters,
        "abnormal": abnormal,
    }


def _split_chapter_layout(
    ch: Chapter,
    regex: re.Pattern[str],
    remove_word_count: bool,
) -> list[Chapter] | None:
    """把一个含多个子章节的章节拆成若干连续章节；无命中返回 None。"""
    paras = list(ch.paragraphs or [])
    styles = list(ch.styles or [])
    if remove_word_count:
        keep_p: list[str] = []
        keep_s: list[Any] = []
        for i, para in enumerate(paras):
            if not _is_word_count_line(para):
                keep_p.append(para)
                keep_s.append(styles[i] if i < len(styles) else None)
        paras, styles = keep_p, keep_s
    matches = [i for i, para in enumerate(paras) if regex.search(para)]
    if not matches:
        return None

    # 首个命中之前的段落保留给原章节作为“开篇”，其余每个命中段成为独立子章节。
    lead_paras = paras[: matches[0]]
    lead_styles = styles[: matches[0]]
    subs: list[Chapter] = []
    for k, m in enumerate(matches):
        # 命中正则的整行即为提取出的章节标题，保留完整标题内容。
        title = (paras[m] or "").strip()
        start = m + 1
        end = matches[k + 1] if k + 1 < len(matches) else len(paras)
        subs.append(
            Chapter(
                index=ch.index,
                title=title,
                paragraphs=paras[start:end],
                styles=styles[start:end],
                source_id=ch.source_id,
                heading_level=ch.heading_level,
                parent_index=ch.parent_index,
                is_section=ch.is_section,
            )
        )
    if lead_paras:
        wrapper = Chapter(
            index=ch.index,
            title=ch.title,
            paragraphs=lead_paras,
            styles=lead_styles,
            source_id=ch.source_id,
            title_zh=ch.title_zh,
            heading_level=ch.heading_level,
            parent_index=ch.parent_index,
            is_section=ch.is_section,
        )
        return [wrapper] + subs
    return subs


def preview_split_chapters(
    epub_path: str | Path,
    pattern: str,
    chapter_indices: list[int],
    *,
    remove_word_count: bool = True,
) -> dict[str, Any]:
    """预览对选定章节按正则拆分的子章节标题与段落数。"""
    regex = re.compile(pattern)
    book = load_book(epub_path)
    selected = [ch for ch in book.chapters if ch.index in set(chapter_indices)]
    details: list[dict[str, Any]] = []
    for ch in selected:
        layout = _split_chapter_layout(ch, regex, remove_word_count)
        if layout is None:
            details.append(
                {
                    "index": ch.index,
                    "title": ch.title,
                    "chars": _chapter_chars(ch),
                    "status": "no_match",
                    "sub_chapters": [],
                }
            )
            continue
        details.append(
            {
                "index": ch.index,
                "title": ch.title,
                "chars": _chapter_chars(ch),
                "status": "split",
                "sub_chapters": [
                    {"title": sc.title, "paragraphs": len(sc.paragraphs)}
                    for sc in layout
                ],
            }
        )
    total_sub = sum(len(d["sub_chapters"]) for d in details)
    return {"details": details, "selected": len(selected), "sub_chapter_count": total_sub}


def apply_split_chapters(
    epub_path: str | Path,
    output_path: str | Path,
    pattern: str,
    chapter_indices: list[int],
    *,
    remove_word_count: bool = True,
    source_title: str | None = None,
) -> dict[str, Any]:
    """对选定章节按正则切分，把子章节插入到原章节之后并输出新 EPUB。"""
    regex = re.compile(pattern)
    book = load_book(epub_path)
    target_set = set(chapter_indices)
    split_count = 0
    sub_count = 0
    new_chapters: list[Chapter] = []
    for ch in book.chapters:
        if ch.index in target_set:
            layout = _split_chapter_layout(ch, regex, remove_word_count)
            if layout is not None:
                split_count += 1
                sub_count += len(layout)
                new_chapters.extend(layout)
                continue
        new_chapters.append(ch)
    book.chapters = _renumber(new_chapters)
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "chapter_count": len(book.chapters),
        "split_count": split_count,
        "sub_chapter_count": sub_count,
        "added_count": sub_count - split_count,
        "output": str(output_path),
    }


# ---------------- 章节梳理 ----------------
def _records_for(book: Book) -> list[dict[str, Any]]:
    """为每章提取标题里的首个数字、前缀与剩余内容，并保留中文标题。"""
    records: list[dict[str, Any]] = []
    for pos, ch in enumerate(book.chapters):
        title = (ch.title or "").strip()
        match = _FIRST_NUMBER_RE.search(title)
        if match:
            number = int(match.group(1))
            prefix = title[: match.start()]
            content = title[match.end() :]
        else:
            number = None
            prefix = title
            content = ""
        records.append(
            {
                "pos": pos,
                "index": ch.index,
                "chapter": ch,
                "title": title,
                "title_zh": ch.title_zh,
                "heading_level": ch.heading_level,
                "parent_index": ch.parent_index,
                "chars": _chapter_chars(ch),
                "number": number,
                "prefix": prefix,
                "content": content,
                "seq": 0,
            }
        )
    return records


def _sort_group(group: list[dict[str, Any]], use_original_number: bool) -> list[dict[str, Any]]:
    """同层级组内排序：使用原数字时按数字升序，否则保留阅读顺序。"""
    if use_original_number and group and all(r["number"] is not None for r in group):
        return sorted(group, key=lambda r: (r["number"], r["pos"]))
    return list(group)


def _sort_and_assign(
    records: list[dict[str, Any]], use_original_number: bool
) -> list[dict[str, Any]]:
    """按 parent_index 组树，组内排序后深度优先展开，赋予整组序号。"""
    by_index = {r["index"]: r for r in records}
    roots: list[dict[str, Any]] = []
    children: dict[int, list[dict[str, Any]]] = {}
    for r in records:
        parent = r["parent_index"]
        if parent is not None and parent in by_index:
            children.setdefault(parent, []).append(r)
        else:
            roots.append(r)
    ordered: list[dict[str, Any]] = []

    def visit(group: list[dict[str, Any]]) -> None:
        for i, r in enumerate(_sort_group(group, use_original_number), start=1):
            r["seq"] = i
            ordered.append(r)
            if r["index"] in children:
                visit(children[r["index"]])

    visit(roots)
    return ordered


def _format_prefix(template: str, number: int) -> str:
    """把用户前缀模板（{n} / {num} / {} / 第 x 章 的 x）替换成数字。"""
    text = (template or "").strip()
    s = str(number)
    if not text:
        return s + " "
    replaced = False
    for placeholder in ("{n}", "{num}", "{}"):
        if placeholder in text:
            text = text.replace(placeholder, s)
            replaced = True
    if not replaced:
        m = re.search(r"(?<![A-Za-z0-9])x(?![A-Za-z0-9])", text)
        if m:
            text = text[: m.start()] + s + text[m.end() :]
            replaced = True
    if not replaced:
        text = text + " " + s
    return text.strip() + " "


def _rebuild_title(
    record: dict[str, Any],
    prefix_template: str,
    use_original_number: bool,
) -> str:
    # 卷/封面页（h1）保持原样，仅整理正文（h2+）标题，从而保留树状层级。
    if record["heading_level"] == 1:
        return (record["title"] or "").strip()
    if use_original_number and record["number"] is not None:
        content = (record["content"] or "").strip()
        num = record["number"]
    else:
        content = (record["title"] or "").strip()
        num = record["seq"]
    return (_format_prefix(prefix_template, num) + content).strip()


def analyze_title_numbering(epub_path: str | Path) -> dict[str, Any]:
    """检测每章标题里的数字、前缀、剩余内容，供「章节梳理」预览。"""
    book = load_book(epub_path)
    records = _records_for(book)
    for r in records:
        r.pop("chapter", None)
    counts = sum(1 for r in records if r["number"] is not None)
    return {
        "count": len(records),
        "with_number": counts,
        "records": records,
    }


def preview_organize_titles(
    epub_path: str | Path,
    *,
    prefix_template: str,
    use_original_number: bool,
) -> dict[str, Any]:
    """预览章节梳理后的新旧标题映射。"""
    book = load_book(epub_path)
    records = _records_for(book)
    ordered = _sort_and_assign(records, use_original_number)
    mapping: list[dict[str, Any]] = []
    for r in ordered:
        mapping.append(
            {
                "pos": r["pos"],
                "index": r["index"],
                "heading_level": r["heading_level"],
                "parent_index": r["parent_index"],
                "chars": r["chars"],
                "number": r["number"],
                "seq": r["seq"],
                "title": r["title"],
                "new_title": _rebuild_title(r, prefix_template, use_original_number),
            }
        )
    return {
        "count": len(mapping),
        "mapping": mapping,
        "prefix_template": prefix_template,
        "use_original_number": use_original_number,
    }


def apply_organize_titles(
    epub_path: str | Path,
    output_path: str | Path,
    *,
    prefix_template: str,
    use_original_number: bool,
    source_title: str | None = None,
) -> dict[str, Any]:
    """按前缀模板与数字策略整理章节标题、重排顺序并输出新 EPUB。"""
    book = load_book(epub_path)
    records = _records_for(book)
    ordered = _sort_and_assign(records, use_original_number)
    new_chapters: list[Chapter] = []
    updated = 0
    for r in ordered:
        ch = r["chapter"]
        new_title = _rebuild_title(r, prefix_template, use_original_number)
        if new_title != ch.title:
            updated += 1
        ch.title = new_title
        new_chapters.append(ch)
    book.chapters = _renumber(new_chapters)
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "chapter_count": len(book.chapters),
        "updated": updated,
        "output": str(output_path),
    }
