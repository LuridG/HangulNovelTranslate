# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from typing import Any
from ..book import (Book, Chapter, ParagraphStyle, _href_basename, book_to_txt, export_epub, load_book, metadata_from_dict, metadata_to_dict, parse_epub)
from ..glossary import Glossary
from .resources import _merge_static_resources
from .resources import _rewrite_image_markers


def merge_books(books: list[Book], *, title: str = "") -> Book:
    """按顺序拼合多卷为一部书：每卷开头插入“{书名} 第X卷”章节，章节序号重新编号；
    CSS/图片等静态资源按内容去重合并，正文里的插图标记随改名同步。"""
    chapters: list[Chapter] = []
    prefix = f"{title.strip()} " if title.strip() else ""
    css_resources, images, doc_inline_css, volume_css_list, volume_css_maps, volume_image_maps, volume_structures = (
        _merge_static_resources(books)
    )
    chapter_css: dict[str, list[str]] = {}
    document_structure: dict[str, dict] = {}
    for index, book in enumerate(books, start=1):
        volume_node_index = len(chapters)
        chapters.append(
            Chapter(
                volume_node_index,
                f"{prefix}第{index}卷",
                [],
                heading_level=1,
                is_section=True,
            )
        )
        old_to_new = {
            chapter.index: volume_node_index + 1 + i
            for i, chapter in enumerate(book.chapters)
        }
        per_volume_css = (
            volume_css_list[index - 1] if index - 1 < len(volume_css_list) else []
        )
        per_volume_css_map = (
            volume_css_maps[index - 1] if index - 1 < len(volume_css_maps) else {}
        )
        per_volume_images = (
            volume_image_maps[index - 1] if index - 1 < len(volume_image_maps) else {}
        )
        if index - 1 < len(volume_structures):
            document_structure.update(volume_structures[index - 1])
        for chapter in book.chapters:
            new_sid = f"v{index}:{chapter.source_id}" if chapter.source_id else ""
            if new_sid:
                chapter_css[new_sid] = list(per_volume_css_map.get(new_sid, per_volume_css))
            paragraphs = (
                [_rewrite_image_markers(p, per_volume_images) for p in chapter.paragraphs]
                if per_volume_images
                else list(chapter.paragraphs)
            )
            new_level = min(chapter.heading_level + 1, 6)
            source_parent = chapter.parent_index
            if source_parent is not None and source_parent in old_to_new:
                new_parent = old_to_new[source_parent]
            else:
                new_parent = volume_node_index
            chapters.append(
                Chapter(
                    len(chapters),
                    chapter.title,
                    paragraphs,
                    new_sid,
                    list(chapter.styles),
                    title_zh=chapter.title_zh,
                    heading_level=new_level,
                    parent_index=new_parent,
                    is_section=chapter.is_section,
                )
            )
    titles = [b.title for b in books if b.title]
    merged = Book(title=title.strip() or "、".join(titles), chapters=chapters)
    metadata: dict[str, Any] = {}
    if css_resources:
        metadata["css_resources"] = css_resources
    if images:
        metadata["images"] = images
    if doc_inline_css:
        metadata["doc_inline_css"] = doc_inline_css
    if chapter_css:
        metadata["chapter_css"] = chapter_css
    if document_structure:
        metadata["document_structure"] = document_structure
    merged.metadata = metadata
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
