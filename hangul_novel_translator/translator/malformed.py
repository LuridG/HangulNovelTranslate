# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import json
from pathlib import Path
from ..book import (Book, Chapter, ParagraphStyle, book_to_txt, export_epub, is_decorative_title, load_book, metadata_to_dict)
from ..config import AppConfig
from .models import MalformedBlock
from .response import _classify_malformed
from .chunking import _retry_chunk_config
from .chunking import build_chunks
from .response import normalize_completed_paragraphs


def detect_malformed_blocks(state_path: str | Path, config: AppConfig) -> list[MalformedBlock]:
    """扫描翻译存档的 completed，挑出畸形块并附上机器修复建议。

    repairable=True：可用机器直接修复（不耗 token）；
    repairable=False：需用户手动补翻或回传 LLM 重翻（如译文彻底丢失）。
    """
    state_path = Path(state_path)
    data = json.loads(state_path.read_text(encoding="utf-8"))
    source = Path(str(data.get("source", "")))
    completed = data.get("completed") or {}
    if not isinstance(completed, dict):
        completed = {}
    if not source.exists():
        raise ValueError(f"存档对应的原书不存在：{source}")
    cfg = _retry_chunk_config(data, config)

    book = load_book(source)
    chunks = {c.id: c for c in build_chunks(book, cfg)}
    blocks: list[MalformedBlock] = []
    for chunk_id, values in completed.items():
        if not isinstance(values, list):
            continue
        chunk = chunks.get(str(chunk_id))
        source_paras = list(chunk.paragraphs) if chunk is not None else []
        expected = len(source_paras) if chunk is not None else None
        parsed, status = normalize_completed_paragraphs(
            values, expected, source_paras, fill_from_source=False
        )
        if status in ("recovered", "cleaned", "fallback_source", "unresolved"):
            block = _classify_malformed(
                str(chunk_id), values, source_paras, parsed, status, expected
            )
            if chunk is not None:
                block.archive = state_path
                block.chapter_index = chunk.chapter_index
                block.chapter_title = chunk.chapter_title
            blocks.append(block)
    return blocks



def repair_malformed_blocks(blocks: list[MalformedBlock]) -> dict[str, int]:
    """把机器可修复的畸形块写回 completed，返回 {fixed, skipped, unresolved}。"""
    fixed = 0
    skipped = 0
    unresolved = 0
    # 按存档分组，避免同一文件反复读写。
    by_archive: dict[Path, list[MalformedBlock]] = {}
    for block in blocks:
        if not block.repairable or not block.repaired:
            unresolved += 1
            continue
        if not block.archive:
            skipped += 1
            continue
        by_archive.setdefault(block.archive, []).append(block)

    for archive, items in by_archive.items():
        data = json.loads(archive.read_text(encoding="utf-8"))
        completed = data.setdefault("completed", {})
        changed = False
        for block in items:
            paras = list(block.repaired)
            if not any(p.strip() for p in paras):
                continue
            completed[str(block.chunk_id)] = paras
            changed = True
            fixed += 1
        if changed:
            archive.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"fixed": fixed, "skipped": skipped, "unresolved": unresolved}
