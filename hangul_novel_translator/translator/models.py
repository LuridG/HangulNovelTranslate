# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from dataclasses import (dataclass, field, replace)
from pathlib import Path
from ..book import (Book, Chapter, ParagraphStyle, book_to_txt, export_epub, is_decorative_title, load_book, metadata_to_dict)


class TranslationCancelled(RuntimeError):
    pass



@dataclass
class Chunk:
    id: str
    chapter_index: int
    chapter_title: str
    chunk_index: int
    paragraphs: list[str]
    styles: list[ParagraphStyle | None] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs)



@dataclass
class TranslationResult:
    input_path: Path
    output_dir: Path
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    output_paths: list[Path]



@dataclass
class FailedChunk:
    """一处失败块的可视化信息：所属存档 + 原文章节/段落 + 错误原因。"""

    archive: Path
    chunk_id: str
    chapter_index: int
    chapter_title: str
    error: str
    paragraphs: list[str] = field(default_factory=list)
    missing: bool = False



@dataclass
class MalformedBlock:
    """一处畸形块的可视化信息：所属存档 + 当前损坏值 + 机器修复建议。"""

    archive: Path
    chunk_id: str
    chapter_index: int
    chapter_title: str
    issue: str
    source_paragraphs: list[str] = field(default_factory=list)
    current_values: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    repairable: bool = False
