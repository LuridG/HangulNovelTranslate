# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import json
import re
from pathlib import Path
from ..book import (Book, Chapter, ParagraphStyle, book_to_txt, export_epub, is_decorative_title, load_book, metadata_to_dict)
from ..config import AppConfig
from .models import Chunk
from .models import FailedChunk
from .chunking import _retry_chunk_config
from .chunking import build_chunks
from .response import sanitize_committed_paragraphs


def load_failed_chunks(state_path: str | Path, config: AppConfig) -> list[FailedChunk]:
    """读取翻译存档中的失败块，还原其原文章节与段落。

    新版存档（failed[id] 为含 paragraphs 的字典）直接用持久化原文；旧版字符串存档按原书重新分块定位；
    仍然定位不到时 missing=True（原文为空）。用于“失败块查看 / 手动编辑”入口。
    """
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    source = Path(str(data.get("source", "")))
    failed = data.get("failed") or {}
    if not isinstance(failed, dict):
        failed = {}
    cfg = _retry_chunk_config(data, config)

    book = None
    chunks_by_id: dict[str, Chunk] | None = None
    items: list[FailedChunk] = []
    for chunk_id in list(failed.keys()):
        entry = failed[chunk_id]
        paragraphs = None
        chapter_index = 0
        chapter_title = ""
        if isinstance(entry, dict):
            paras = entry.get("paragraphs")
            if isinstance(paras, list):
                paragraphs = [str(p) for p in paras]
                chapter_index = int(entry.get("chapter_index", 0) or 0)
                chapter_title = str(entry.get("chapter_title", ""))
        if paragraphs is None and source and source.exists():
            if chunks_by_id is None:
                book = load_book(source)
                chunks_by_id = {c.id: c for c in build_chunks(book, cfg)}
            chunk = chunks_by_id.get(chunk_id)
            if chunk is not None:
                paragraphs = list(chunk.paragraphs)
                chapter_index = chunk.chapter_index
                chapter_title = chunk.chapter_title
        error = entry.get("error") if isinstance(entry, dict) else str(entry)
        items.append(
            FailedChunk(
                archive=state_path,
                chunk_id=chunk_id,
                chapter_index=chapter_index,
                chapter_title=chapter_title,
                error=error,
                paragraphs=paragraphs or [],
                missing=paragraphs is None,
            )
        )
    return items



def save_manual_translation(
    state_path: str | Path,
    chunk_id: str,
    translated_paragraphs: list[str],
) -> dict[str, int]:
    """把手动补翻的译文写回存档：写入 completed、从 failed 移除，返回 {completed, failed} 计数。"""
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    completed = data.setdefault("completed", {})
    failed = data.setdefault("failed", {})
    if chunk_id in failed:
        failed.pop(chunk_id, None)
    completed[chunk_id] = sanitize_committed_paragraphs(translated_paragraphs)
    state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"completed": len(completed), "failed": len(failed)}



def _sanitize_state_name(stem: str, *, legacy: bool = False) -> str:
    """存档文件名清洗：保留拉丁/数字/中日韩/韩文，其余换下划线；空名兜底为 book。"""
    pattern = (
        r"[^A-Za-z0-9_\-\u4e00-\u9fff]+"
        if legacy
        else r"[^A-Za-z0-9_\-\u4e00-\u9fff\uac00-\ud7af]+"
    )
    safe = re.sub(pattern, "_", stem or "")
    safe = re.sub(r"_+", "_", safe)
    safe = safe.strip("._") or "book"
    return safe
