# hangul_novel_translator/merge.py
"""多卷修正与合并：读取翻译存档 JSON，按当前词表修正并拼合输出 TXT/EPUB。"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .book import Book, Chapter, ParagraphStyle, book_to_txt, export_epub, load_book
from .config import AppConfig
from .glossary import Glossary
from .translator import build_chunks


def inspect_state(path: Path) -> dict[str, Any]:
    """校验并读取一个翻译存档的基本信息。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    source = str(data.get("source", ""))
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        raise ValueError("存档缺少 completed 字段")
    if not source:
        raise ValueError("存档缺少 source 字段")
    return {
        "title": Path(source).stem,
        "source": source,
        "total_chunks": int(data.get("total_chunks", 0)),
        "completed": len(completed),
        "failed": len(data.get("failed") or {}),
    }


def _chunk_config(state: dict[str, Any], config: AppConfig) -> AppConfig:
    """重组时优先使用翻译时存档的分块参数，避免后来修改参数导致 chunk 错位。"""
    kwargs: dict[str, Any] = {}
    if isinstance(state.get("chunk_chars"), int) and state["chunk_chars"] > 0:
        kwargs["chunk_chars"] = state["chunk_chars"]
    if isinstance(state.get("max_paragraph_chars"), int) and state["max_paragraph_chars"] > 0:
        kwargs["max_paragraph_chars"] = state["max_paragraph_chars"]
    return replace(config, **kwargs) if kwargs else config


def book_from_state(state_path: Path, config: AppConfig) -> Book:
    """读断点续传存档 + 原书，重组为译文 Book（失败/未完成块保留原文）。"""
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    source = Path(str(data.get("source", "")))
    if not source.exists():
        raise ValueError(f"存档对应的原书不存在：{source}")
    book = load_book(source)
    cfg = _chunk_config(data, config)
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        completed = {}
    chunks = build_chunks(book, cfg)
    by_chapter: dict[int, list[Any]] = {}
    for chunk in chunks:
        by_chapter.setdefault(chunk.chapter_index, []).append(chunk)

    chapters: list[Chapter] = []
    for chapter in book.chapters:
        paragraphs: list[str] = []
        styles: list[ParagraphStyle | None] = []
        for chunk in sorted(by_chapter.get(chapter.index, []), key=lambda c: c.chunk_index):
            paras = completed.get(chunk.id)
            if isinstance(paras, list):
                paragraphs.extend(str(p) for p in paras)
                styles.extend(chunk.styles[: len(paras)])
            else:
                paragraphs.extend(chunk.paragraphs)
                styles.extend(chunk.styles[: len(chunk.paragraphs)])
        if not paragraphs:
            paragraphs = chapter.paragraphs
            styles = list(chapter.styles)
        chapters.append(Chapter(chapter.index, chapter.title, paragraphs, chapter.source_id, styles))
    return Book(title=book.title, chapters=chapters, source_path=book.source_path)


def merge_books(books: list[Book], *, title: str = "") -> Book:
    """按顺序拼合多卷为一部书：每卷开头插入“{书名} 第X卷”章节，章节序号重新编号。"""
    chapters: list[Chapter] = []
    prefix = f"{title.strip()} " if title.strip() else ""
    for index, book in enumerate(books, start=1):
        chapters.append(Chapter(len(chapters), f"{prefix}第{index}卷", []))
        for chapter in book.chapters:
            chapters.append(
                Chapter(
                    len(chapters),
                    chapter.title,
                    chapter.paragraphs,
                    chapter.source_id,
                    list(chapter.styles),
                )
            )
    titles = [b.title for b in books if b.title]
    merged = Book(title=title.strip() or "、".join(titles), chapters=chapters)
    if books:
        merged.metadata = dict(books[0].metadata or {})
    return merged


def preview_fix(book: Book, glossary: Glossary) -> dict[str, int]:
    """只统计不修改：返回预计命中的段落数与写法数。"""
    pairs = glossary.replacement_pairs()
    hit_paragraphs = 0
    hit_sources: set[str] = set()
    for chapter in book.chapters:
        for paragraph in chapter.paragraphs:
            matched = [src for src, _ in pairs if src and src in paragraph]
            if matched:
                hit_paragraphs += 1
                hit_sources.update(matched)
    return {"hit_paragraphs": hit_paragraphs, "hit_sources": len(hit_sources)}


def fix_book(book: Book, glossary: Glossary) -> dict[str, int]:
    """按当前词表对译文做机器修正，返回实际替换统计。"""
    preview = preview_fix(book, glossary)
    for chapter in book.chapters:
        for i, paragraph in enumerate(chapter.paragraphs):
            chapter.paragraphs[i] = glossary.apply_replacements(paragraph)
    return {"hit_paragraphs": preview["hit_paragraphs"], "hit_sources": preview["hit_sources"]}


def export_merged(
    books: list[Book],
    glossary: Glossary,
    config: AppConfig,
    output_dir: Path,
    *,
    title: str = "合集",
    output_txt: bool = True,
    output_epub: bool = True,
) -> dict[str, Any]:
    """合并多卷 → 按词表修正 → 输出 TXT/EPUB，返回统计与输出路径。"""
    if not books:
        raise ValueError("没有可合并的存档")
    merged = merge_books(books, title=title)
    fix_stats = fix_book(merged, glossary)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title.strip() or "合集")
    paths: list[Path] = []
    if output_txt:
        txt_path = output_dir / f"{safe_title}.zh.txt"
        book_to_txt(merged, txt_path, config.output_encoding)
        paths.append(txt_path)
    if output_epub:
        epub_path = output_dir / f"{safe_title}.zh.epub"
        export_epub(merged, epub_path, source_title=title.strip() or merged.title)
        paths.append(epub_path)
    return {
        "paths": paths,
        "chapters": len(merged.chapters),
        "paragraphs": sum(len(ch.paragraphs) for ch in merged.chapters),
        **fix_stats,
    }