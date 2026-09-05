"""成品 EPUB 标题修正 / 格式整理服务。

在「成品矫正」tab 中提供三类操作：
1. 标题重分：按用户正则清除旧标题并重新断章（针对 txt 自动切分的遗留问题）。
2. 标题重组：从正文里提取真正的短标题，替换占位章节名（如 EP.0）。
3. 格式整理：检测/清理旧制作说明与每章字数统计，并可重新生成中文版格式。

所有改写都走「重新解析 → 修改章节 → 重新导出」的流水线，复用仓库自己验证过的
``load_book`` / ``export_epub``，保证输出仍是合法 EPUB，排版/CSS/图片尽量保留。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .book import Book, Chapter, export_epub, load_book


# ---------------- 格式标记正则 ----------------
_WORD_COUNT_RE = re.compile(r"本章字数\s*[:：]\s*\d+")
_PROD_TITLE = "制作说明"
_PROD_BODY_MARKERS = ("【制作说明】", "【书籍统计】", "制作工具", "Txt2Epub", "预计阅读时长")
_TITLE_SKIP_LINES = ("收起封面", "关闭封面", "封面", "目录", "作者的话", "公告", "致谢", "后记", "番外")
_TITLE_SKIP_SUBSTR = ("收起封面", "关闭封面")
_QUOTE_ENDINGS = "”’" + "」』】》>)]）)"
_SENTENCE_ENDINGS = ("。", "！", "？", "…", "!", "?", "……")

# GUI 展示用默认正则
DEFAULT_PREFIX_PATTERN = r"^EP\.\d+$"
DEFAULT_BODY_TITLE_PATTERN = r"^(.{1,60}?)\s*[（(]\s*\d+\s*[）)]$"


def _is_word_count_line(text: str) -> bool:
    return bool(_WORD_COUNT_RE.search(text or ""))


def _is_production_chapter(ch: Chapter) -> bool:
    if _PROD_TITLE in (ch.title or ""):
        return True
    joined = "".join(ch.paragraphs or [])
    return any(marker in joined for marker in _PROD_BODY_MARKERS)


def _filter_paragraphs(
    paragraphs: list[str],
    styles: list[Any],
    keep: Callable[[str], bool],
) -> tuple[list[str], list[Any]]:
    keep_p: list[str] = []
    keep_s: list[Any] = []
    for i, para in enumerate(paragraphs or []):
        if keep(para):
            keep_p.append(para)
            keep_s.append(styles[i] if i < len(styles) else None)
    return keep_p, keep_s


def _renumber_chapters(chapters: list[Chapter]) -> list[Chapter]:
    """删除/新增章节后重排 index 并修正 parent_index，保持 TOC 层级。"""
    old_to_new = {ch.index: i + 1 for i, ch in enumerate(chapters)}
    for i, ch in enumerate(chapters):
        ch.index = i + 1
        if ch.parent_index is not None and ch.parent_index in old_to_new:
            ch.parent_index = old_to_new[ch.parent_index]
        else:
            ch.parent_index = None
    return chapters


def _looks_like_body_title(text: str) -> bool:
    """正文行是否像是真正的短标题（排除字数行、UI 行与句子）。"""
    text = (text or "").strip()
    if not text or len(text) > 60:
        return False
    if _is_word_count_line(text):
        return False
    if text in _TITLE_SKIP_LINES:
        return False
    if any(marker in text for marker in _TITLE_SKIP_SUBSTR):
        return False
    # 去掉引号/括号后再判断是否以句子标点收尾，避免““呃啊……咳……！””
    # 这类正文对话被误判成标题。
    core = text.rstrip(_QUOTE_ENDINGS)
    if core.endswith(_SENTENCE_ENDINGS) or core.endswith(("，", "、", "；", "：", ":")):
        return False
    if re.fullmatch(r"[\d\s（）()、，,.\-—]+", text):
        return False
    return True


def _extract_body_title_from(
    paragraphs: list[str],
    title_re: re.Pattern[str] | None,
    max_scan: int = 5,
) -> tuple[str | None, int | None]:
    """从段落里找正文短标题，返回 (标题文本, 在列表中的下标)。

    优先使用用户给定的标题正则（整串匹配即标题），否则退回“短行且非句子”启发式。
    只扫描正文开头少数非字数段，避免把后文里的句子误当标题。
    """
    candidate: str | None = None
    candidate_idx: int | None = None
    scanned = 0
    for i, para in enumerate(paragraphs or []):
        if _is_word_count_line(para):
            continue
        if scanned >= max_scan:
            break
        scanned += 1
        if title_re:
            match = title_re.search(para)
            if match:
                return match.group(0).strip(), i
        if candidate is None and _looks_like_body_title(para):
            candidate = para.strip()
            candidate_idx = i
    if candidate is not None:
        return candidate, candidate_idx
    return None, None


def _extract_resplit_title(regex: re.Pattern[str], match: re.Match, use_group: bool) -> str:
    if use_group and match.lastindex and match.group(1) is not None:
        return match.group(1).strip()
    return match.group(0).strip()


def _collect_stream(book: Book, skip_format: bool) -> list[tuple[str, Any]]:
    """把全书段落按顺序拍平，可选跳过制作说明与每章字数行。"""
    stream: list[tuple[str, Any]] = []
    for ch in book.chapters:
        if skip_format and _is_production_chapter(ch):
            continue
        for i, para in enumerate(ch.paragraphs or []):
            if skip_format and _is_word_count_line(para):
                continue
            if not para.strip():
                continue
            style = ch.styles[i] if i < len(ch.styles) else None
            stream.append((para, style))
    return stream


def _split_by_regex(
    stream: list[tuple[str, Any]],
    regex: re.Pattern[str],
    use_group: bool,
) -> list[tuple[str, list[str], list[Any]]]:
    result: list[tuple[str, list[str], list[Any]]] = []
    pending_title: str | None = None
    pending_paras: list[str] = []
    pending_styles: list[Any] = []
    for para, style in stream:
        match = regex.search(para)
        if match:
            if pending_title is None:
                if pending_paras:
                    result.append(("正文", pending_paras, pending_styles))
            else:
                result.append((pending_title, pending_paras, pending_styles))
            pending_title = _extract_resplit_title(regex, match, use_group)
            pending_paras = []
            pending_styles = []
        else:
            pending_paras.append(para)
            pending_styles.append(style)
    if pending_paras:
        if pending_title is None:
            result.append(("正文", pending_paras, pending_styles))
        else:
            result.append((pending_title, pending_paras, pending_styles))
    return result


# ---------------- 格式检测 ----------------
def detect_format(epub_path: str | Path) -> dict[str, Any]:
    book = load_book(epub_path)
    production: list[dict[str, Any]] = []
    word_count_chapters: list[dict[str, Any]] = []
    for ch in book.chapters:
        if _is_production_chapter(ch):
            production.append({"index": ch.index, "title": ch.title})
        word_lines = [p for p in ch.paragraphs if _is_word_count_line(p)]
        if word_lines:
            word_count_chapters.append(
                {"index": ch.index, "title": ch.title, "lines": len(word_lines)}
            )
    return {
        "chapter_count": len(book.chapters),
        "total_chars": sum(len(ch.text) for ch in book.chapters),
        "has_production_note": bool(production),
        "production": production,
        "word_count_count": len(word_count_chapters),
        "word_count_chapters": word_count_chapters,
    }


# ---------------- 清理旧格式 ----------------
def preview_clear_format(
    epub_path: str | Path,
    *,
    remove_production: bool = True,
    remove_word_count: bool = True,
) -> dict[str, Any]:
    book = load_book(epub_path)
    production: list[str] = []
    removed_word_lines = 0
    for ch in book.chapters:
        if remove_production and _is_production_chapter(ch):
            production.append(ch.title)
        if remove_word_count:
            removed_word_lines += sum(
                1 for p in ch.paragraphs if _is_word_count_line(p)
            )
    return {
        "would_remove_production": production,
        "would_remove_word_count_lines": removed_word_lines,
        "chapter_count": len(book.chapters),
        "remaining_chapters": (
            len(book.chapters) - len(production)
            if remove_production
            else len(book.chapters)
        ),
    }


def apply_clear_format(
    epub_path: str | Path,
    output_path: str | Path,
    *,
    remove_production: bool = True,
    remove_word_count: bool = True,
    source_title: str | None = None,
) -> dict[str, Any]:
    book = load_book(epub_path)
    removed_production: list[str] = []
    removed_word_lines = 0
    new_chapters: list[Chapter] = []
    for ch in book.chapters:
        if remove_production and _is_production_chapter(ch):
            removed_production.append(ch.title)
            continue
        if remove_word_count:
            keep_p, keep_s = _filter_paragraphs(
                ch.paragraphs, ch.styles, lambda p: not _is_word_count_line(p)
            )
            removed_word_lines += len(ch.paragraphs) - len(keep_p)
            ch.paragraphs, ch.styles = keep_p, keep_s
        new_chapters.append(ch)
    book.chapters = new_chapters
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "removed_production": removed_production,
        "removed_word_count_lines": removed_word_lines,
        "chapter_count": len(book.chapters),
        "output": str(output_path),
    }


# ---------------- 新增中文版格式 ----------------
def preview_add_format(
    epub_path: str | Path,
    *,
    cover_font: str = "source.ttf（自用字体）",
) -> dict[str, Any]:
    book = load_book(epub_path)
    book.chapters = [ch for ch in book.chapters if not _is_production_chapter(ch)]
    total_chars = 0
    per_chapter: list[dict[str, Any]] = []
    for ch in book.chapters:
        n = sum(len(p) for p in ch.paragraphs)
        total_chars += n
        per_chapter.append({"title": ch.title, "chars": n})
    minutes = max(0, total_chars // 400)
    note = _build_production_note(total_chars, minutes, len(book.chapters), cover_font)
    return {
        "total_chars": total_chars,
        "chapter_count": len(book.chapters),
        "predicted_minutes": minutes,
        "per_chapter_count": len(per_chapter),
        "note_body": note,
    }


def apply_add_format(
    epub_path: str | Path,
    output_path: str | Path,
    *,
    cover_font: str = "source.ttf（自用字体）",
    source_title: str | None = None,
) -> dict[str, Any]:
    book = load_book(epub_path)
    # 先去掉旧制作说明与旧字数行，避免重复叠加。
    book.chapters = [ch for ch in book.chapters if not _is_production_chapter(ch)]
    for ch in book.chapters:
        keep_p, keep_s = _filter_paragraphs(
            ch.paragraphs, ch.styles, lambda p: not _is_word_count_line(p)
        )
        ch.paragraphs, ch.styles = keep_p, keep_s
    # 在写入字数行之前判断制作说明应插在封面（无正文、层级 1）之后。
    insert_after_cover = bool(
        book.chapters
        and not book.chapters[0].paragraphs
        and book.chapters[0].heading_level == 1
    )

    total_chars = 0
    per_chapter: list[dict[str, Any]] = []
    for ch in book.chapters:
        n = sum(len(p) for p in ch.paragraphs)
        total_chars += n
        per_chapter.append({"title": ch.title, "chars": n})
        if n > 0:
            ch.paragraphs.insert(0, f"(本章字数: {n})")
            ch.styles.insert(0, None)

    minutes = max(0, total_chars // 400)
    note = _build_production_note(total_chars, minutes, len(book.chapters), cover_font)
    note_chars = sum(len(p) for p in note)
    production_chapter = Chapter(
        index=0,
        title="制作说明",
        paragraphs=[f"(本章字数: {note_chars})"] + note,
        styles=[None] * (len(note) + 1),
        heading_level=2,
    )
    # 第一卷封面页（无正文、标题层级 1）之后插入制作说明；否则放在最前。
    if insert_after_cover:
        production_chapter.parent_index = book.chapters[0].index
        book.chapters.insert(1, production_chapter)
    else:
        book.chapters.insert(0, production_chapter)
    book.chapters = _renumber_chapters(book.chapters)
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "total_chars": total_chars,
        "chapter_count": len(book.chapters),
        "per_chapter_count": len(per_chapter),
        "output": str(output_path),
    }


def _build_production_note(
    total_chars: int,
    minutes: int,
    chapter_count: int,
    cover_font: str,
) -> list[str]:
    return [
        "【制作说明】",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "制作工具：韩语小说批量翻译工具",
        "---------------------------",
        "【书籍统计】",
        f"• 总字数：{total_chars:,} 字",
        f"• 预计阅读时长：约 {minutes} 分钟",
        f"• 总章节：{chapter_count} 章",
        f"• 封面字体：{cover_font}（自用字体）",
        "• 正文字体：系统默认字体",
        "• 标题字体：同正文字体",
        "本电子书由程序自动生成，仅供个人学习及预览使用，请支持正版。",
    ]


# ---------------- 标题重分（正则重断章） ----------------
def preview_regex_resplit(
    epub_path: str | Path,
    pattern: str,
    *,
    skip_format: bool = True,
    use_group: bool = False,
) -> dict[str, Any]:
    regex = re.compile(pattern)
    book = load_book(epub_path)
    stream = _collect_stream(book, skip_format)
    chunks = _split_by_regex(stream, regex, use_group)
    return {
        "title_count": len(chunks),
        "titles": [title for title, _, _ in chunks],
        "chapters": [
            {"title": title, "paragraphs": len(paras)}
            for title, paras, _ in chunks
        ],
    }


def apply_regex_resplit(
    epub_path: str | Path,
    output_path: str | Path,
    pattern: str,
    *,
    skip_format: bool = True,
    use_group: bool = False,
    source_title: str | None = None,
) -> dict[str, Any]:
    regex = re.compile(pattern)
    book = load_book(epub_path)
    stream = _collect_stream(book, skip_format)
    chunks = _split_by_regex(stream, regex, use_group)
    new_chapters: list[Chapter] = []
    for i, (title, paras, styles) in enumerate(chunks):
        new_chapters.append(
            Chapter(
                index=i + 1,
                title=title,
                paragraphs=paras,
                styles=styles,
                heading_level=1,
            )
        )
    book.chapters = new_chapters
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "chapter_count": len(new_chapters),
        "titles": [ch.title for ch in new_chapters],
        "output": str(output_path),
    }


# ---------------- 标题重组（提取正文短标题） ----------------
def preview_reassemble(
    epub_path: str | Path,
    *,
    prefix_pattern: str = DEFAULT_PREFIX_PATTERN,
    body_title_pattern: str = DEFAULT_BODY_TITLE_PATTERN,
    remove_word_count: bool = True,
) -> dict[str, Any]:
    prefix_re = re.compile(prefix_pattern) if prefix_pattern else None
    title_re = re.compile(body_title_pattern) if body_title_pattern else None
    book = load_book(epub_path)
    updates: list[dict[str, Any]] = []
    has_word_count: list[dict[str, Any]] = []
    for ch in book.chapters:
        if prefix_re and not prefix_re.search(ch.title):
            continue
        word_lines = [p for p in ch.paragraphs if _is_word_count_line(p)]
        if word_lines:
            has_word_count.append({"index": ch.index, "title": ch.title})
        sample = (
            [p for p in ch.paragraphs if not _is_word_count_line(p)]
            if remove_word_count
            else ch.paragraphs
        )
        new_title, _ = _extract_body_title_from(sample, title_re)
        if new_title:
            updates.append({"index": ch.index, "old": ch.title, "new": new_title})
    return {
        "updates": updates,
        "has_word_count": has_word_count,
        "would_remove_word_count_chapters": len(has_word_count),
    }


def apply_reassemble(
    epub_path: str | Path,
    output_path: str | Path,
    *,
    prefix_pattern: str = DEFAULT_PREFIX_PATTERN,
    body_title_pattern: str = DEFAULT_BODY_TITLE_PATTERN,
    keep_prefix: bool = False,
    remove_word_count: bool = True,
    remove_body_title: bool = True,
    source_title: str | None = None,
) -> dict[str, Any]:
    prefix_re = re.compile(prefix_pattern) if prefix_pattern else None
    title_re = re.compile(body_title_pattern) if body_title_pattern else None
    book = load_book(epub_path)
    updates: list[dict[str, Any]] = []
    removed_word_lines = 0
    for ch in book.chapters:
        if prefix_re and not prefix_re.search(ch.title):
            continue
        old_title = ch.title
        if remove_word_count:
            keep_p, keep_s = _filter_paragraphs(
                ch.paragraphs, ch.styles, lambda p: not _is_word_count_line(p)
            )
            removed_word_lines += len(ch.paragraphs) - len(keep_p)
            ch.paragraphs, ch.styles = keep_p, keep_s
        new_title, idx = _extract_body_title_from(ch.paragraphs, title_re)
        if new_title:
            ch.title = f"{old_title} {new_title}" if keep_prefix else new_title
            if remove_body_title and idx is not None and idx < len(ch.paragraphs):
                ch.paragraphs.pop(idx)
                if idx < len(ch.styles):
                    ch.styles.pop(idx)
            updates.append({"index": ch.index, "old": old_title, "new": ch.title})
    export_epub(book, output_path, source_title=source_title or book.title)
    return {
        "updated": updates,
        "removed_word_count_lines": removed_word_lines,
        "output": str(output_path),
    }


# ---------------- LLM 辅助推断标题筛选 ----------------
def infer_placeholder_pattern(
    llm: Any,
    epub_path: str | Path,
    *,
    title_limit: int = 80,
) -> dict[str, Any]:
    """把全书章节标题送给 LLM，推断能匹配占位标题（如 EP.0）的正则。"""
    book = load_book(epub_path)
    titles: list[str] = []
    seen: set[str] = set()
    for chapter in book.chapters:
        title = (chapter.title or "").strip()
        if title and len(title) <= 60 and title not in seen:
            seen.add(title)
            titles.append(title)
        if len(titles) >= title_limit:
            break
    result: dict[str, Any] = {"pattern": "", "titles": titles, "count": len(titles)}
    if not titles:
        return result
    titles_text = "\n".join(f"- {t}" for t in titles)
    system = (
        "你是 EPUB 章节标题分析助手。下面是一个程序生成的 EPUB 里出现的全部章节标题。"
        "请找出其中由程序自动生成、与真实正文标题无关的“占位/编号类标题”（例如 EP.0、第1节、chapter 1 等）。"
        "请总结这类占位标题的规律，并给出一个可以匹配它们的正则表达式。"
        "只输出一个 JSON 对象，形如 {\"pattern\": \"...\"}。"
        "如果找不到任何占位标题规律，pattern 返回空字符串\"\"。"
    )
    user = f"章节标题列表：\n{titles_text}\n\n请输出 {{\"pattern\": \"...\"}}。"
    payload = llm.chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )
    if isinstance(payload, dict):
        result["pattern"] = str(payload.get("pattern", "") or "").strip()
    return result


def infer_body_title_match(
    llm: Any,
    epub_path: str | Path,
    *,
    prefix_pattern: str,
    per_chapter_lines: int = 5,
    sample_chapters: int = 10,
) -> dict[str, Any]:
    """按跨度采样匹配占位标题的章节，取每章开头几行送给 LLM，推断正文标题正则。"""
    book = load_book(epub_path)
    prefix_re = re.compile(prefix_pattern) if prefix_pattern else None
    candidates = [
        ch
        for ch in book.chapters
        if (not prefix_re or prefix_re.search((ch.title or "").strip()))
    ]
    result: dict[str, Any] = {
        "pattern": "",
        "found": False,
        "chapters": len(candidates),
        "sample": [],
    }
    if not candidates:
        return result
    count = min(sample_chapters, len(candidates))
    if count <= 0:
        return result
    indices = (
        [round(i * (len(candidates) - 1) / (count - 1)) for i in range(count)]
        if count > 1
        else [0]
    )
    selected: list[dict[str, Any]] = []
    for idx in indices:
        chapter = candidates[idx]
        head = [
            para
            for para in chapter.paragraphs
            if not _is_word_count_line(para)
        ][:per_chapter_lines]
        selected.append({"index": chapter.index, "title": chapter.title, "head": head})
    result["sample"] = selected

    blocks: list[str] = []
    for item in selected:
        lines = "\n".join(f"  {line}" for line in item["head"]) if item["head"] else "  （无正文行）"
        blocks.append(f"[章节：{item['title']}]\n{lines}")
    sample_text = "\n\n".join(blocks)
    system = (
        "你是 EPUB 章节正文标题分析助手。下面列出若干章节开头几行内容（已去掉每章字数统计行）。"
        "请判断每章开头是否有一行是真正的章节标题（区别于 EP.0 这类占位标题，"
        "通常较短、不以句号/问号/感叹号收尾）。"
        "请给出一个可以匹配这些正文标题行的正则表达式。"
        "只输出一个 JSON 对象，形如 {\"pattern\": \"...\", \"found\": true|false}。"
        "如果所有章节开头都没有标题行，found 为 false，pattern 返回空字符串\"\"。"
    )
    user = f"章节开头内容：\n\n{sample_text}\n\n请输出 {{\"pattern\": \"...\", \"found\": ...}}。"
    payload = llm.chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
    )
    if isinstance(payload, dict):
        result["pattern"] = str(payload.get("pattern", "") or "").strip()
        result["found"] = bool(payload.get("found", bool(result["pattern"])))
    return result
