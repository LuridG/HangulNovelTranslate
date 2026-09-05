# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
from pathlib import Path
from .models import Book
from .models import Chapter
from ._consts import _CHAPTER_PATTERNS
from .text import _decode_bytes
from .text import _strip_invisible_chars
from .markup import strip_inline_markers


_MAX_HEADING_LEN = 90

# 内置 TXT 章节标题正则：用于「章节正则选择框」的候选模板，也作为无自定义正则时的兜底之一。
# 每个元素都是可手动编辑的原始正则字符串。
DEFAULT_TXT_PATTERNS = [
    r"^\s*(?:제\s*)?(?:\d{1,4}|[一二三四五六七八九十百千零〇]+)\s*(?:장|화|부|편|권|막)\b",
    r"^\s*(?:프롤로그|에필로그|서문|발문|후기|외전|번외\s*편?|side\s*story|prologue|epilogue)\s*[:.]?",
    r"^\s*(?:chapter|ep\.?|no\.?)\s*\d+\b",
    r"^\s*(?:#|=)\s*\d+\b",
    r"^\s*[#\[]\s*\d+\s*(?:화|장|话|回)?\s*[\]\]]",
    r"^\s*第\s*\d+\s*[章话回节]",
]


def _looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > _MAX_HEADING_LEN:
        return False
    for pattern in _CHAPTER_PATTERNS:
        if pattern.match(line):
            return True
    return False


def _compile_user_patterns(patterns) -> list[re.Pattern]:
    """把用户提供的正则字符串列表编译为 pattern；跳过空串与非法正则。"""
    compiled: list[re.Pattern] = []
    for raw in patterns or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def _heading_with_patterns(line: str, patterns: list[re.Pattern]) -> bool:
    """按用户正则判断一行是否为章节标题。"""
    line = line.strip()
    if not line or len(line) > _MAX_HEADING_LEN:
        return False
    for pattern in patterns:
        if pattern.match(line):
            return True
    return False



def _read_text_auto(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except Exception:
            pass
    return _decode_bytes(data)



def _chunk_paragraphs_by_chars(paragraphs: list[str], max_chars: int = 8000) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for para in paragraphs:
        if current and size + len(para) + 1 > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(para)
        size += len(para) + 1
    if current:
        chunks.append(current)
    return chunks



def parse_txt(path: Path) -> Book:
    raw = _read_text_auto(path)
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_strip_invisible_chars(x) for x in normalized.split("\n")]
    chapters = _split_from_name(lines, _CHAPTER_PATTERNS)
    return Book(title=path.stem, chapters=chapters, source_path=path)


def _split_from_name(lines: list[str], patterns: list[re.Pattern]) -> list[Chapter]:
    """按给定的正则列表把归一化行切分为章节；无标题命中则回退按字数切块。"""
    heading_indices: list[int] = []
    for i, line in enumerate(lines):
        if _heading_with_patterns(line, patterns):
            heading_indices.append(i)

    chapters: list[Chapter] = []
    if not heading_indices:
        paragraphs = [x.strip() for x in lines if x.strip()]
        for i, part in enumerate(_chunk_paragraphs_by_chars(paragraphs, 8000)):
            chapters.append(Chapter(i, f"第 {i + 1} 节", part))
        return chapters

    pre_lines = [x for x in lines[: heading_indices[0]] if x.strip()]
    if pre_lines:
        chapters.append(Chapter(0, "正文", pre_lines))
    for pos, start in enumerate(heading_indices):
        end = heading_indices[pos + 1] if pos + 1 < len(heading_indices) else len(lines)
        body = lines[start:end]
        title = body[0].strip() if body else f"第 {len(chapters) + 1} 节"
        paragraphs = [x.strip() for x in body[1:] if x.strip()]
        chapters.append(Chapter(len(chapters), title, paragraphs))
    return chapters


def parse_txt_with_patterns(path: Path, patterns=None) -> Book:
    """用用户提供的正则列表切分 TXT 章节；无有效正则时回退到默认解析。"""
    path = Path(path)
    compiled = _compile_user_patterns(patterns)
    raw = _read_text_auto(path)
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_strip_invisible_chars(x) for x in normalized.split("\n")]

    if compiled:
        chapters = _split_from_name(lines, compiled)
    else:
        return parse_txt(path)
    return Book(title=path.stem, chapters=chapters, source_path=path)


def preview_txt_chapters(path: Path, patterns=None) -> list[str]:
    """按用户正则提取 TXT 章节标题，用于「章节检测」预览；无命中回退默认标题。"""
    path = Path(path)
    compiled = _compile_user_patterns(patterns)
    raw = _read_text_auto(path)
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_strip_invisible_chars(x) for x in normalized.split("\n")]

    if compiled:
        titled: list[str] = [
            line.strip() for line in lines if _heading_with_patterns(line, compiled)
        ]
        if titled:
            return titled
    return [ch.display_title for ch in _split_from_name(lines, _CHAPTER_PATTERNS)]


def drop_zero_chapters(book: Book, *, merge_into_prev: bool = True) -> Book:
    """剔除正文为 0 字的空章节，并把其正文（通常为空）并入上一正常章。

    用于「忽视 0 字章节」：空章不单独成章，避免把分割产生的空标题当成正式章节。
    返回新的 Book，保留源路径与元数据；章节 index 重新连续编号。
    """
    kept: list[Chapter] = []
    for ch in book.chapters:
        chars = sum(len(p) for p in ch.paragraphs)
        if chars == 0:
            if merge_into_prev and kept:
                kept[-1].paragraphs.extend(ch.paragraphs)
            continue
        kept.append(ch)
    for i, ch in enumerate(kept):
        ch.index = i
    return Book(title=book.title, chapters=kept, source_path=book.source_path, metadata=book.metadata)



def book_to_txt(book: Book, path: Path, encoding: str = "utf-8", sanitizer: Any = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [book.title, ""]
    for chapter in book.chapters:
        lines.append(chapter.display_title)
        lines.append("")
        for para in chapter.paragraphs:
            cleaned_para = sanitizer.clean_paragraph(para) if sanitizer else para
            lines.append(strip_inline_markers(cleaned_para))
            lines.append("")
        lines.append("")
    path.write_text("\n".join(lines), encoding=encoding)
