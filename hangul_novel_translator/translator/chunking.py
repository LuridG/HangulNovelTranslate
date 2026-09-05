# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import hashlib
from dataclasses import (dataclass, field, replace)
from typing import (Any, Callable)
from ..book import (Book, Chapter, ParagraphStyle, book_to_txt, export_epub, is_decorative_title, load_book, metadata_to_dict)
from ..config import AppConfig
from ..sampling import (collect_sample_text_strided, format_sample_chapters, sample_chapter_report)
from ..utils import (extract_json, parse_paragraphs_from_payload, split_paragraph_smart)
from .models import Chunk


def _retry_chunk_config(state: dict, config: AppConfig) -> AppConfig:
    """重试失败块时优先使用翻译时存档的分块参数，避免 chunk 错位。"""
    kwargs: dict[str, Any] = {}
    if isinstance(state.get("chunk_chars"), int) and state["chunk_chars"] > 0:
        kwargs["chunk_chars"] = state["chunk_chars"]
    if isinstance(state.get("max_paragraph_chars"), int) and state["max_paragraph_chars"] > 0:
        kwargs["max_paragraph_chars"] = state["max_paragraph_chars"]
    return replace(config, **kwargs) if kwargs else config



def _chunk_signature(chunks: list[Chunk]) -> str:
    """章节/分块结构签名：任一分块 id 变化（解析规则、分块参数、章节边界）都会改变签名，
    用于续传前检测旧完成块是否仍能按 id 安全复用。"""
    return hashlib.sha256("\n".join(c.id for c in chunks).encode("utf-8")).hexdigest()



def _failed_entry(chunk: Chunk, error: str) -> dict[str, Any]:
    """失败块入档：除错误原因外，同时持久化原文章节与段落。

    这样重试不再依赖“原书仍在原路径 + 重新解析出同样的分块”，失败块本身自包含、可排查；
    旧存档（只存错误字符串）仍由 retry_failed 走重新解析回退。
    """
    return {
        "error": error,
        "chapter_index": chunk.chapter_index,
        "chapter_title": chunk.chapter_title,
        "chunk_index": chunk.chunk_index,
        "paragraphs": list(chunk.paragraphs),
    }



def _chunk_from_failed_entry(chunk_id: str, entry: dict[str, Any]) -> Chunk | None:
    """由失败块存档还原 Chunk；当 entry 不是含 paragraphs 的字典时返回 None。

    返回 None 表示该失败块没有持久化原文，需要按原书重新分块定位（旧版存档兼容）。
    """
    paragraphs = entry.get("paragraphs")
    if not isinstance(paragraphs, list):
        return None
    return Chunk(
        id=chunk_id,
        chapter_index=int(entry.get("chapter_index", 0) or 0),
        chapter_title=str(entry.get("chapter_title", "")),
        chunk_index=int(entry.get("chunk_index", 0) or 0),
        paragraphs=[str(p) for p in paragraphs],
    )



def build_chunks(book: Book, config: AppConfig) -> list[Chunk]:
    chunks: list[Chunk] = []
    paragraph_limit = min(config.chunk_chars, config.max_paragraph_chars)
    for chapter in book.chapters:
        pieces: list[tuple[str, ParagraphStyle | None]] = []
        for index, paragraph in enumerate(chapter.paragraphs):
            style = chapter.styles[index] if index < len(chapter.styles) else None
            for piece in split_paragraph_smart(paragraph, paragraph_limit):
                pieces.append((piece, style))

        current: list[str] = []
        current_styles: list[ParagraphStyle | None] = []
        current_chars = 0
        chunk_index = 0

        def flush() -> None:
            nonlocal current, current_styles, current_chars, chunk_index
            if current:
                chunks.append(
                    Chunk(
                        id=f"ch-{chapter.index:05d}-{chunk_index:05d}",
                        chapter_index=chapter.index,
                        chapter_title=chapter.title,
                        chunk_index=chunk_index,
                        paragraphs=current,
                        styles=current_styles,
                    )
                )
                chunk_index += 1
                current = []
                current_styles = []
                current_chars = 0

        for piece, style in pieces:
            if current and current_chars + len(piece) + 1 > config.chunk_chars:
                flush()
            current.append(piece)
            current_styles.append(style)
            current_chars += len(piece) + 1
        flush()
    return chunks



def collect_sample_text(book: Book, config: AppConfig) -> str:
    """按全书跨度均匀采样样章（向后兼容别名）。"""
    return collect_sample_text_strided(book, config)
