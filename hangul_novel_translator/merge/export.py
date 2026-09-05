# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from hangul_novel_translator.sanitizer import ExportSanitizer
import re
from pathlib import Path
from typing import Any
from ..book import (Book, Chapter, ParagraphStyle, _href_basename, book_to_txt, export_epub, load_book, metadata_from_dict, metadata_to_dict, parse_epub)
from ..config import AppConfig
from ..glossary import Glossary
from .books import fix_book
from .books import merge_books


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
