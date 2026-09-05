# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from ..book import (Book, Chapter, ParagraphStyle, _href_basename, book_to_txt, export_epub, load_book, metadata_from_dict, metadata_to_dict, parse_epub)
from ..config import AppConfig
from ..translator import (_chunk_signature, build_chunks, normalize_completed_paragraphs)
from ._consts import _HANGUL_RUN_RE
from ._consts import _STATE_FILE_SUFFIX
from ._consts import _VOLUME_MARK_RE


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



def audit_translation_state(
    state_path: Path,
    config: AppConfig,
    *,
    pattern: str,
) -> dict[str, Any]:
    """按用户自定义正则复查已完成块，命中即转入失败块。

    与韩文复查共用同一套失败块持久化结构（含原文章节/段落），方便直接复用
    现有“失败块查看 / 校验并重试 / 手动编辑”。只检测 completed，不改动未命中块。
    """
    state_path = Path(state_path)
    pattern_text = str(pattern).strip()
    if not pattern_text:
        raise ValueError("请先填写失败定义正则")
    try:
        regex = re.compile(pattern_text)
    except re.error as exc:  # noqa: BLE001
        raise ValueError(f"正则表达式无效：{exc}") from exc

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
        joined = "\n".join(str(value) for value in values)
        if not regex.search(joined):
            continue
        chunk = chunks.get(str(chunk_id))
        if chunk is None:
            flagged.append({"chunk_id": str(chunk_id), "missing": True})
            continue
        failed[str(chunk_id)] = {
            "error": f"自检发现译文命中规则“{pattern_text}”，疑似失败被写入正文",
            "chapter_index": chunk.chapter_index,
            "chapter_title": chunk.chapter_title,
            "chunk_index": chunk.chunk_index,
            "paragraphs": list(chunk.paragraphs),
        }
        completed.pop(chunk_id, None)
        flagged.append({"chunk_id": str(chunk_id), "missing": False})

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



def detect_merge_title(titles: list[str]) -> str:
    """从各卷原书文件名提取合并书名。
    单本直接用文件名；多卷取公共前缀（如 魂火1/魂火2 → 魂火），
    避免出现用顿号拼接卷名的劣质标题。"""
    cleaned = [str(t).strip() for t in titles if t and str(t).strip()]
    if not cleaned:
        return ""
    if len(set(cleaned)) == 1:
        return cleaned[0]
    prefix = os.path.commonprefix(cleaned)
    return prefix.rstrip(" \t·-—_/")



def archive_filename_title(path: Path) -> str:
    """从翻译存档文件名提取用户自定义书名，并去掉末尾的分卷标记。
    例如：烟灰_第1卷.translation_state.json → 烟灰。"""
    name = _STATE_FILE_SUFFIX.sub("", str(path.name)).strip()
    name = _VOLUME_MARK_RE.sub("", name)
    # 生成的文件名会带一个隐藏文件前导点（如 .书名_第1卷.translation_state.json）。
    return name.strip().lstrip(".")



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
                cleaned, _status = normalize_completed_paragraphs(
                    paras,
                    expected_count=len(chunk.paragraphs),
                    source_paragraphs=list(chunk.paragraphs),
                    fill_from_source=True,
                )
                paragraphs.extend(cleaned)
                styles.extend(chunk.styles[: len(cleaned)])
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
