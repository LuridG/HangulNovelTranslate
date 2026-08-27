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
    metadata_to_dict,
    parse_epub,
)
from .config import AppConfig
from .glossary import Glossary
from .translator import _chunk_signature, build_chunks


_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\ud7b0-\ud7ff]+")


def hangul_char_count(text: str) -> int:
    """统计单段中的韩文字符；空格、标点及夹杂的中文专名不打断统计。"""
    return sum(len(match.group(0)) for match in _HANGUL_RUN_RE.finditer(str(text)))


def review_translation_state(
    state_path: Path,
    config: AppConfig,
    *,
    threshold: int = 30,
) -> dict[str, Any]:
    """复查已完成块，把疑似大段漏译的块迁移到 failed，供现有失败块流程处理。"""
    if threshold < 1:
        raise ValueError("单段韩文字符阈值必须大于 0")
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    source = Path(str(data.get("source", "")))
    if not source.exists():
        raise ValueError(f"存档对应的原书不存在：{source}")
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        completed = {}
    reviewed_count = len(completed)
    failed = data.setdefault("failed", {})
    if not isinstance(failed, dict):
        failed = {}
        data["failed"] = failed

    cfg = _chunk_config(data, config)
    book = load_book(source)
    chunks = {chunk.id: chunk for chunk in build_chunks(book, cfg)}
    flagged: list[dict[str, Any]] = []
    for chunk_id, values in list(completed.items()):
        if not isinstance(values, list):
            continue
        hangul_count = max((hangul_char_count(value) for value in values), default=0)
        if hangul_count < threshold:
            continue
        chunk = chunks.get(str(chunk_id))
        if chunk is None:
            flagged.append({"chunk_id": str(chunk_id), "hangul_count": hangul_count, "missing": True})
            continue
        failed[str(chunk_id)] = {
            "error": f"复查发现单段译文含韩文 {hangul_count} 字，疑似未翻译（阈值 {threshold} 字）",
            "chapter_index": chunk.chapter_index,
            "chapter_title": chunk.chapter_title,
            "chunk_index": chunk.chunk_index,
            "paragraphs": list(chunk.paragraphs),
        }
        completed.pop(chunk_id, None)
        flagged.append({"chunk_id": str(chunk_id), "hangul_count": hangul_count, "missing": False})

    data["completed"] = completed
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "reviewed": reviewed_count,
        "flagged": len(flagged),
        "missing": sum(1 for item in flagged if item.get("missing")),
        "items": flagged,
    }


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
                parent_index=chapter.parent_index,
                is_section=chapter.is_section,
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


def repair_image_state(state_path: Path, config: AppConfig) -> dict[str, Any]:
    """为旧存档补回解析阶段遗漏的独立图片，不调用模型。

    旧解析结果用于保持原有译文与 chunk 映射，新解析结果只提供新增图片段落；
    CSS、字体及图片二进制资源仍来自原书和存档 metadata。
    """
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    source = Path(str(data.get("source", "")))
    if not source.exists():
        raise ValueError(f"存档对应的原书不存在：{source}")
    old_book = parse_epub(source, include_standalone_images=False)
    new_book = parse_epub(source, include_standalone_images=True)
    old_cfg = _chunk_config(data, config)
    old_chunks = build_chunks(old_book, old_cfg)
    new_chunks = build_chunks(new_book, old_cfg)
    legacy_state = not bool(data.get("chunk_signature"))
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        completed = {}
    old_by_chapter: dict[int, list[Any]] = {}
    for chunk in old_chunks:
        old_by_chapter.setdefault(chunk.chapter_index, []).append(chunk)
    translated_by_chapter: dict[int, list[str]] = {}
    completed_by_chapter: dict[int, list[bool]] = {}
    error_by_chapter: dict[int, list[str]] = {}
    source_by_chapter: dict[int, list[str]] = {}
    for chapter_index, chunks in old_by_chapter.items():
        source_values: list[str] = []
        translated_values: list[str] = []
        completed_values: list[bool] = []
        error_values: list[str] = []
        for chunk in sorted(chunks, key=lambda item: item.chunk_index):
            values = completed.get(chunk.id)
            values = values if isinstance(values, list) and len(values) == len(chunk.paragraphs) else chunk.paragraphs
            source_values.extend(chunk.paragraphs)
            translated_values.extend(str(value) for value in values)
            completed_values.extend([isinstance(completed.get(chunk.id), list) and len(completed[chunk.id]) == len(chunk.paragraphs)] * len(chunk.paragraphs))
            failed_entry = (data.get("failed") or {}).get(chunk.id, {})
            error = failed_entry.get("error", "原翻译块未完成") if isinstance(failed_entry, dict) else str(failed_entry)
            error_values.extend([error] * len(chunk.paragraphs))
        source_by_chapter[chapter_index] = source_values
        translated_by_chapter[chapter_index] = translated_values
        completed_by_chapter[chapter_index] = completed_values
        error_by_chapter[chapter_index] = error_values
    rebuilt: dict[str, list[str]] = {}
    rebuilt_failed: dict[str, dict[str, Any]] = {}
    old_positions = {index: 0 for index in source_by_chapter}
    new_count = 0

    def migrate_legacy_without_images() -> dict[str, Any]:
        """旧版无法和新图片解析结果对齐时，先升级旧解析版本的存档。

        这样多卷合并仍可使用原有译文；图片补集不伪造引用，并把结果明确返回给 GUI。
        """
        if not legacy_state:
            raise ValueError(f"无法安全对齐存档中的原文，未修改存档")
        old_signature = _chunk_signature(old_chunks)
        data["total_chunks"] = len(old_chunks)
        data["chunk_signature"] = old_signature
        data["chunk_chars"] = old_cfg.chunk_chars
        data["max_paragraph_chars"] = old_cfg.max_paragraph_chars
        data["metadata"] = metadata_to_dict(old_book.metadata)
        state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "path": state_path,
            "added": 0,
            "chunks": len(old_chunks),
            "changed": True,
            "migrated": True,
            "images_skipped": True,
        }

    def paragraph_key(value: str) -> str:
        # BeautifulSoup/旧版解析器对 XHTML 空白的折叠方式可能不同，不能把这种
        # 无语义差异误判为章节错位；图片标记仍保持原样参与匹配。
        return re.sub(r"\s+", " ", str(value or "")).strip()

    for chunk in new_chunks:
        source_values = source_by_chapter.get(chunk.chapter_index, [])
        translated_values = translated_by_chapter.get(chunk.chapter_index, [])
        position = old_positions.get(chunk.chapter_index, 0)
        values: list[str] = []
        statuses: list[bool] = []
        errors: list[str] = []
        for paragraph in chunk.paragraphs:
            if paragraph.startswith("⟦img:") and paragraph.endswith("⟧"):
                values.append(paragraph)
                statuses.append(True)
                errors.append("")
                new_count += 1
                continue
            current_matches = (
                position < len(source_values)
                and paragraph_key(source_values[position]) == paragraph_key(paragraph)
            )
            if not current_matches:
                # 新解析器只允许在旧段落序列中插入独立图片；若存在少量空白
                # 节点差异，可安全跳过；任何有意义的旧正文都必须精确对齐。
                candidate = next(
                    (
                        index
                        for index in range(position + 1, min(len(source_values), position + 4))
                        if paragraph_key(source_values[index]) == paragraph_key(paragraph)
                    ),
                    None,
                )
                skipped = source_values[position:candidate] if candidate is not None else []
                if candidate is None or any(
                    item and not (item.startswith("⟦img:") and item.endswith("⟧"))
                    for item in skipped
                ):
                    if legacy_state:
                        return migrate_legacy_without_images()
                    raise ValueError(f"无法安全对齐第 {chunk.chapter_index + 1} 章的原文段落，未修改存档")
                position = candidate
            if position >= len(source_values):
                if legacy_state:
                    return migrate_legacy_without_images()
                raise ValueError(f"无法安全对齐第 {chunk.chapter_index + 1} 章的原文段落，未修改存档")
            values.append(translated_values[position])
            statuses.append(completed_by_chapter.get(chunk.chapter_index, [])[position])
            errors.append(error_by_chapter.get(chunk.chapter_index, ["原翻译块未完成"])[position])
            position += 1
        old_positions[chunk.chapter_index] = position
        if all(statuses):
            rebuilt[chunk.id] = values
        else:
            rebuilt.pop(chunk.id, None)
            failed_entry = {"error": next((error for error, status in zip(errors, statuses) if not status), "原翻译块未完成"),
                            "chapter_index": chunk.chapter_index,
                            "chapter_title": chunk.chapter_title,
                            "chunk_index": chunk.chunk_index,
                            "paragraphs": list(chunk.paragraphs)}
            rebuilt_failed[chunk.id] = failed_entry
    # 旧版存档可能只有 source/completed/failed；只要对齐成功，就升级为当前
    # 格式，使后续多卷合并、校验重试和图片补集都能复用同一套定位信息。
    migrated = (
        data.get("chunk_chars") != old_cfg.chunk_chars
        or data.get("max_paragraph_chars") != old_cfg.max_paragraph_chars
        or data.get("total_chunks") != len(new_chunks)
        or data.get("chunk_signature") != _chunk_signature(new_chunks)
        or not data.get("metadata")
    )
    if not new_count and not migrated:
        return {"path": state_path, "added": 0, "chunks": len(new_chunks), "changed": False}
    data["completed"] = rebuilt
    data["failed"] = rebuilt_failed
    data["total_chunks"] = len(new_chunks)
    data["chunk_signature"] = _chunk_signature(new_chunks)
    data["chunk_chars"] = old_cfg.chunk_chars
    data["max_paragraph_chars"] = old_cfg.max_paragraph_chars
    data["metadata"] = metadata_to_dict(new_book.metadata)
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": state_path, "added": new_count, "chunks": len(new_chunks), "changed": True, "migrated": migrated}



def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return str(content).encode("utf-8")


def _rewrite_css_urls(
    css_text: str,
    old_css_name: str,
    new_css_name: str,
    resource_map: dict[str, str],
) -> str:
    """按 CSS 原路径解析 url()，再改写为最终资源的相对路径。"""
    new_css_dir = posixpath.dirname(new_css_name) or "."

    def repl(match) -> str:
        raw = match.group(1).strip()
        url = raw.strip("'\"")
        if url.lower().startswith(("data:", "http://", "https://")):
            return match.group(0)
        source_target = posixpath.normpath(
            posixpath.join(posixpath.dirname(old_css_name) or ".", url)
        ).lstrip("/")
        target = resource_map.get(source_target)
        if not target:
            return match.group(0)
        rel = posixpath.relpath(target, start=new_css_dir)
        return f"url('{rel}')"

    return re.sub(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", repl, css_text)


def _rewrite_css_imports(
    css_text: str,
    old_css_name: str,
    new_css_name: str,
    resource_map: dict[str, str],
) -> str:
    """改写 @import 的字符串路径，兼容 url() 形式由 _rewrite_css_urls 处理。"""
    new_css_dir = posixpath.dirname(new_css_name) or "."

    def repl(match) -> str:
        quote = match.group(1)
        url = match.group(2).strip()
        source_target = posixpath.normpath(
            posixpath.join(posixpath.dirname(old_css_name) or ".", url)
        ).lstrip("/")
        target = resource_map.get(source_target)
        if not target:
            return match.group(0)
        rel = posixpath.relpath(target, start=new_css_dir)
        return f"@import {quote}{rel}{quote}"

    return re.sub(r"@import\s+(['\"])([^'\"]+)\1", repl, css_text, flags=re.IGNORECASE)


def _css_dependencies_match(
    css_name: str,
    css_text: str,
    current_resources: dict[str, bytes],
    existing_resources: dict[str, bytes],
) -> bool:
    """判断同字节 CSS 是否仍可共享：其相对引用的资源也必须逐一相同。"""
    urls = list(_CSS_URL_RE.findall(css_text))
    urls.extend(_CSS_IMPORT_RE.findall(css_text))
    for raw in urls:
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(css_name) or ".", raw)
        ).lstrip("/")
        if target in current_resources and current_resources[target] != existing_resources.get(target):
            return False
    return True


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
    返回 (css_resources, images, doc_inline_css, volume_css, volume_css_maps, volume_image_maps)，
    其中 CSS 和图片映射均按卷保存，供正文和章节资源链接分别重写。"""
    resources: list[dict] = []
    by_name: dict[str, bytes] = {}
    by_base: dict[str, list[tuple[str, bytes]]] = {}
    doc_inline_css: dict[str, list[str]] = {}
    volume_image_maps: list[dict[str, str]] = []
    volume_css_list: list[list[str]] = []
    volume_css_maps: list[dict[str, list[str]]] = []
    volume_structures: list[dict[str, dict]] = []

    for volume_index, book in enumerate(books, start=1):
        image_map: dict[str, str] = {}
        metadata = book.metadata or {}
        volume_structures.append({
            f"v{volume_index}:{sid}": dict(value)
            for sid, value in (metadata.get("document_structure") or {}).items()
        })
        raw = list(metadata.get("css_resources") or []) + list(metadata.get("images") or [])
        current_resource_bytes = {
            str(res.get("name", "")): _as_bytes(res.get("content", b""))
            for res in raw
            if res.get("name")
        }
        target: dict[str, str] = {}
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            base = _href_basename(name)
            reused = next(
                (
                    full
                    for full, data in by_base.get(base, [])
                    if data == content
                    and (
                        not name.lower().endswith(".css")
                        or _css_dependencies_match(
                            name,
                            content.decode("utf-8", errors="ignore"),
                            current_resource_bytes,
                            {r["name"]: r["content"] for r in resources},
                        )
                    )
                ),
                None,
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

        volume_css: list[str] = []
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            final = target.get(name, name)
            if name.lower().endswith(".css"):
                css_text = content.decode("utf-8", errors="ignore")
                css_text = _rewrite_css_urls(css_text, name, final, target)
                css_text = _rewrite_css_imports(css_text, name, final, target)
                content = css_text.encode("utf-8")
            if not any(r["name"] == final for r in resources):
                resources.append({"name": final, "content": content})
            if name.lower().endswith(".css") and final not in volume_css:
                volume_css.append(final)

        for sid, styles in (metadata.get("doc_inline_css") or {}).items():
            doc_inline_css[f"v{volume_index}:{sid}"] = list(styles)
        css_map: dict[str, list[str]] = {}
        for sid, names in (metadata.get("chapter_css") or {}).items():
            css_map[f"v{volume_index}:{sid}"] = [target.get(str(name), str(name)) for name in names]
        for res in metadata.get("images") or []:
            name = str(res.get("name", ""))
            final = target.get(name)
            if final and final != name:
                image_map[name] = final
        volume_image_maps.append(image_map)
        volume_css_list.append(volume_css)
        volume_css_maps.append(css_map)

    css_resources = [r for r in resources if r["name"].lower().endswith(".css")]
    images = [r for r in resources if not r["name"].lower().endswith(".css")]
    return css_resources, images, doc_inline_css, volume_css_list, volume_css_maps, volume_image_maps, volume_structures


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
