# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from dataclasses import (dataclass, field)
from pathlib import Path


@dataclass
class BlockStyle:
    """一个块级元素（段落/标题/列表项等）的样式信息。"""
    tag: str
    klass: str = ""
    style: str = ""
    attrs: dict = field(default_factory=dict)



@dataclass
class ParagraphStyle:
    """段落样式：自身块样式 + 父容器链（用于还原如 div.quote p 这类上下文样式）。"""
    block: BlockStyle | None = None
    ancestors: list[BlockStyle] = field(default_factory=list)



@dataclass
class Chapter:
    index: int
    title: str
    paragraphs: list[str] = field(default_factory=list)
    source_id: str = ""
    styles: list[ParagraphStyle | None] = field(default_factory=list)
    title_zh: str = ""
    heading_level: int = 1
    parent_index: int | None = None
    is_section: bool = False

    @property
    def display_title(self) -> str:
        """输出用章节名：优先已翻译的译文标题，否则用原文章节名。"""
        return self.title_zh or self.title

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)

    def __len__(self) -> int:
        return len(self.text)



@dataclass
class Book:
    title: str
    chapters: list[Chapter] = field(default_factory=list)
    source_path: Path | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def total_chars(self) -> int:
        return sum(len(ch.text) for ch in self.chapters)
