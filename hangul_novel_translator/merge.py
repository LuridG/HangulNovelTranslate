# hangul_novel_translator/merge.py
"""多卷修正与合并：读取翻译存档 JSON，按当前词表修正并拼合输出 TXT/EPUB。"""
from __future__ import annotations

from hangul_novel_translator.sanitizer import ExportSanitizer

import json
import posixpath
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from .book import (
    Book,
    Chapter,
    ParagraphStyle,
    _href_basename,
    book_to_txt,
    export_epub,
    load_book,
    metadata_from_dict,
)
from .config import AppConfig
from .glossary import Glossary
from .translator import _chunk_signature, build_chunks


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
    restored = metadata_from_dict(data.get("metadata"))
    if restored:
        book.metadata = restored
    cfg = _chunk_config(data, config)
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        completed = {}
    chunks = build_chunks(book, cfg)
    completed_ids = {str(k) for k in (completed or {}).keys()}
    saved_total = data.get("total_chunks")
    saved_signature = data.get("chunk_signature")
    if isinstance(saved_signature, str) and saved_signature:
        structure_changed = saved_signature != _chunk_signature(chunks)
    else:
        structure_changed = (
            (isinstance(saved_total, int) and saved_total > 0 and saved_total != len(chunks))
            or (completed_ids and not (completed_ids & {c.id for c in chunks}))
        )
    if completed_ids and structure_changed:
        raise ValueError(
            "存档的章节结构与当前解析不一致（章节解析规则已更新），请重新翻译后再合并输出。"
        )
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
        chapters.append(
            Chapter(
                chapter.index,
                chapter.title,
                paragraphs,
                chapter.source_id,
                styles,
                title_zh=chapter.title_zh,
                heading_level=chapter.heading_level,
            )
        )
    saved_titles = data.get("chapter_titles") or {}
    for chapter in chapters:
        saved = saved_titles.get(str(chapter.index)) or {}
        if saved.get("zh") and saved.get("ko") == chapter.title:
            chapter.title_zh = saved["zh"]
    return Book(
            title=book.title,
            chapters=chapters,
            source_path=book.source_path,
            metadata=book.metadata,
        )



def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return str(content).encode("utf-8")


def _rewrite_css_urls(css_text: str, new_css_name: str, renamed: dict[str, str]) -> str:
    """CSS 文件被改名时，把文件内 url() 相对引用改写到新路径（保持与改名前一致的相对位置）。"""
    css_dir = posixpath.dirname(new_css_name)

    def repl(match) -> str:
        raw = match.group(1).strip()
        url = raw.strip("'\"")
        if url.lower().startswith(("data:", "http://", "https://")):
            return match.group(0)
        target = renamed.get(_href_basename(url))
        if not target:
            return match.group(0)
        rel = posixpath.relpath(target, start=css_dir)
        return f"url('{rel}')"

    return re.sub(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", repl, css_text)


_IMAGE_MARKER_RE = re.compile("\u27e6img:([^\u27e7]*)\u27e7")


def _rewrite_image_markers(text: str, image_map: dict[str, str]) -> str:
    return _IMAGE_MARKER_RE.sub(
        lambda m: f"\u27e6img:{image_map.get(m.group(1), m.group(1))}\u27e7", text
    )


def _merge_static_resources(books: list[Book]):
    """把多卷的 CSS/图片资源合并进同一包：
    - 同名且内容相同 → 去重共享（同一本书各卷通常同套资源）；
    - 同名但内容不同 → 同目录改名（CSS 内 url() 同步改写；正文里的 ⟦img:旧名⟧
      由调用方按 image_map 改写）。
    返回 (css_resources, images, doc_inline_css, volume_css, volume_image_maps)，
    其中 volume_image_maps 是每卷各自的图片改名映射（只用于改写该卷正文标记）。"""
    resources: list[dict] = []
    by_name: dict[str, bytes] = {}
    by_base: dict[str, list[tuple[str, bytes]]] = {}
    doc_inline_css: dict[str, list[str]] = {}
    volume_image_maps: list[dict[str, str]] = []
    volume_css_list: list[list[str]] = []

    for volume_index, book in enumerate(books, start=1):
        image_map: dict[str, str] = {}
        metadata = book.metadata or {}
        raw = list(metadata.get("css_resources") or []) + list(metadata.get("images") or [])
        target: dict[str, str] = {}
        renamed: dict[str, str] = {}
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            base = _href_basename(name)
            reused = next(
                (full for full, data in by_base.get(base, []) if data == content), None
            )
            if reused:
                target[name] = reused
                continue
            candidate = name
            if candidate in by_name:
                parent = posixpath.dirname(name)
                stem = posixpath.splitext(posixpath.basename(name))[0]
                suffix = posixpath.splitext(name)[1]
                candidate = posixpath.join(parent, f"{stem}_v{volume_index}{suffix}")
                guard = 2
                while candidate in by_name:
                    candidate = posixpath.join(
                        parent, f"{stem}_v{volume_index}_{guard}{suffix}"
                    )
                    guard += 1
            target[name] = candidate
            by_name[candidate] = content
            by_base.setdefault(base, []).append((candidate, content))
            if candidate != name:
                renamed[base] = candidate

        volume_css: list[str] = []
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            final = target.get(name, name)
            if name.lower().endswith(".css") and final != name:
                css_text = content.decode("utf-8", errors="ignore")
                css_text = _rewrite_css_urls(css_text, final, renamed)
                content = css_text.encode("utf-8")
            if not any(r["name"] == final for r in resources):
                resources.append({"name": final, "content": content})
            if name.lower().endswith(".css") and final not in volume_css:
                volume_css.append(final)

        for sid, styles in (metadata.get("doc_inline_css") or {}).items():
            doc_inline_css[f"v{volume_index}:{sid}"] = list(styles)
        for res in metadata.get("images") or []:
            name = str(res.get("name", ""))
            final = target.get(name)
            if final and final != name:
                image_map[name] = final
        volume_image_maps.append(image_map)
        volume_css_list.append(volume_css)

    css_resources = [r for r in resources if r["name"].lower().endswith(".css")]
    images = [r for r in resources if not r["name"].lower().endswith(".css")]
    return css_resources, images, doc_inline_css, volume_css_list, volume_image_maps


def merge_books(books: list[Book], *, title: str = "") -> Book:
    """按顺序拼合多卷为一部书：每卷开头插入“{书名} 第X卷”章节，章节序号重新编号；
    CSS/图片等静态资源按内容去重合并，正文里的插图标记随改名同步。"""
    chapters: list[Chapter] = []
    prefix = f"{title.strip()} " if title.strip() else ""
    css_resources, images, doc_inline_css, volume_css_list, volume_image_maps = (
        _merge_static_resources(books)
    )
    chapter_css: dict[str, list[str]] = {}
    for index, book in enumerate(books, start=1):
        chapters.append(Chapter(len(chapters), f"{prefix}第{index}卷", []))
        per_volume_css = (
            volume_css_list[index - 1] if index - 1 < len(volume_css_list) else []
        )
        per_volume_images = (
            volume_image_maps[index - 1] if index - 1 < len(volume_image_maps) else {}
        )
        for chapter in book.chapters:
            new_sid = f"v{index}:{chapter.source_id}" if chapter.source_id else ""
            if new_sid and per_volume_css:
                chapter_css[new_sid] = list(per_volume_css)
            paragraphs = (
                [_rewrite_image_markers(p, per_volume_images) for p in chapter.paragraphs]
                if per_volume_images
                else list(chapter.paragraphs)
            )
            chapters.append(
                Chapter(
                    len(chapters),
                    chapter.title,
                    paragraphs,
                    new_sid,
                    list(chapter.styles),
                    title_zh=chapter.title_zh,
                    heading_level=2,
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
    sanitizer = ExportSanitizer(config.sanitizer_config)
    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title.strip() or "合集")
    paths: list[Path] = []
    if output_txt:
        txt_path = output_dir / f"{safe_title}.zh.txt"
        book_to_txt(merged, txt_path, config.output_encoding, sanitizer=sanitizer)
        paths.append(txt_path)
    if output_epub:
        epub_path = output_dir / f"{safe_title}.zh.epub"
        export_epub(merged, epub_path, source_title=title.strip() or merged.title, sanitizer=sanitizer)
        paths.append(epub_path)
    return {
        "paths": paths,
        "chapters": len(merged.chapters),
        "paragraphs": sum(len(ch.paragraphs) for ch in merged.chapters),
        **fix_stats,
    }