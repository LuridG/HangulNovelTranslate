# hangul_novel_translator/book.py
from __future__ import annotations

import html
import re
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse
from typing import Iterable


@dataclass
class Chapter:
    index: int
    title: str
    paragraphs: list[str] = field(default_factory=list)
    source_id: str = ""

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


_CHAPTER_PATTERNS = [
    re.compile(
        r"^\s*(?:제\s*)?(?:\d{1,4}|[一二三四五六七八九十百千零〇]+)\s*(?:장|화|부|편|권|막)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:프롤로그|에필로그|서문|발문|후기|외전|번외\s*편?|side\s*story|chapter\s*\d+|prologue|epilogue)\s*[:.]?",
        re.IGNORECASE,
    ),
]


def _looks_like_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90:
        return False
    for pattern in _CHAPTER_PATTERNS:
        if pattern.match(line):
            return True
    return False


def _decode_bytes(data: bytes) -> str:
    last_error: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "utf-16"):
        try:
            return data.decode(encoding)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    return data.decode("utf-8", errors="replace")


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

    heading_indices: list[int] = []
    for i, line in enumerate(lines):
        if _looks_like_heading(line):
            heading_indices.append(i)

    chapters: list[Chapter] = []
    if heading_indices:
        # 标题之前的零散内容作为引子。
        pre_lines = [x for x in lines[: heading_indices[0]] if x.strip()]
        if pre_lines:
            chapters.append(Chapter(0, "正文", pre_lines))
        for pos, start in enumerate(heading_indices):
            end = heading_indices[pos + 1] if pos + 1 < len(heading_indices) else len(lines)
            body = lines[start:end]
            title = body[0].strip() if body else f"第 {len(chapters) + 1} 节"
            paragraphs = [x.strip() for x in body[1:] if x.strip()]
            chapters.append(Chapter(len(chapters), title, paragraphs))
    else:
        paragraphs = [x.strip() for x in lines if x.strip()]
        for i, part in enumerate(_chunk_paragraphs_by_chars(paragraphs, 8000)):
            chapters.append(Chapter(i, f"第 {i + 1} 节", part))

    return Book(title=path.stem, chapters=chapters, source_path=path)


def _flatten_toc(toc, prefix: str = "") -> list[tuple[str, str]]:
    """返回扁平化的 (href, title)。"""
    result: list[tuple[str, str]] = []
    if not toc:
        return result
    for item in toc:
        if isinstance(item, (tuple, list)):
            result.extend(_flatten_toc(item, prefix))
        elif hasattr(item, "href") and hasattr(item, "title"):
            result.append((str(item.href), str(item.title)))
    return result


def _href_basename(href: str) -> str:
    try:
        parsed = urlparse(href)
        path = unquote(parsed.path)
        return Path(path).name
    except Exception:
        return href


_SKIP_PAGE_MARKERS = ("목차", "판권", "copyright")


def _strip_invisible_chars(text: str) -> str:
    """去掉零宽/不可见格式字符（U+200B~U+200F、U+2060~U+2064、U+FEFF 等 Cf 类字符）。"""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _is_skip_marker_line(line: str) -> bool:
    """整行是否为目录/版权标记（允许前后空白与少量尾部标点）。"""
    cleaned = _strip_invisible_chars(line).strip().rstrip(":：.。·")
    return cleaned.lower() in _SKIP_PAGE_MARKERS


def _has_skip_text_marker(paragraphs: list[str]) -> bool:
    """目录/版权页识别：只按“整行”判断，避免正文普通词（如“차례”）误杀整章。"""
    return any(_is_skip_marker_line(p) for p in paragraphs)


def parse_epub(path: Path) -> Book:
    try:
        from bs4 import BeautifulSoup
        from ebooklib import epub
    except ImportError as exc:
        raise RuntimeError("解析 EPUB 需要安装 ebooklib 和 beautifulsoup4") from exc

    try:
        from ebooklib import ITEM_DOCUMENT
    except ImportError:
        ITEM_DOCUMENT = getattr(epub, "ITEM_DOCUMENT", 9)

    book = epub.read_epub(str(path), options={"ignore_ncx": True})
    title_values = book.get_metadata("DC", "title")
    title = str(title_values[0][0]) if title_values else path.stem

    toc_titles = {
        _href_basename(href): _strip_invisible_chars(t) for href, t in _flatten_toc(book.toc)
    }
    items = {item.get_id(): item for item in book.get_items_of_type(ITEM_DOCUMENT)}

    chapters: list[Chapter] = []
    spine_ids = [ref for ref in getattr(book, "spine", [])]
    if not spine_ids:
        spine_ids = list(items.keys())

    skip_filename_markers = ("cover", "copyright", "toc", "titlepage", "colophon", "frontmatter", "backmatter")

    for index, idref in enumerate(spine_ids):
        item_id = idref[0] if isinstance(idref, (tuple, list)) else idref
        item = items.get(item_id)
        if item is None:
            continue
        content = _decode_bytes(item.get_content())
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        basename = _href_basename(item.get_name())
        chapter_title = toc_titles.get(basename) or ""
        if not chapter_title:
            heading = soup.find(["h1", "h2", "h3"])
            chapter_title = (
                _strip_invisible_chars(heading.get_text(" ", strip=True)) if heading else ""
            )

        if not chapter_title:
            chapter_title = f"第 {index + 1} 节"

        blocks = soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"])
        paragraphs: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            text = _strip_invisible_chars(block.get_text(" ", strip=True))
            if not text or text in seen:
                continue
            seen.add(text)
            paragraphs.append(text)

        if not paragraphs:
            body = soup.body or soup
            text = _strip_invisible_chars(body.get_text("\n", strip=True))
            paragraphs = [x.strip() for x in text.split("\n") if x.strip()]

        if any(marker in basename.lower() for marker in skip_filename_markers):
            continue
        if _has_skip_text_marker(paragraphs):
            continue

        if paragraphs:
            chapters.append(Chapter(index, chapter_title, paragraphs, source_id=item_id))

    return Book(title=title, chapters=chapters, source_path=path)


def load_book(path: Path) -> Book:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return parse_txt(path)
    if suffix == ".epub":
        return parse_epub(path)
    raise ValueError("目前只支持 .txt 和 .epub 文件")


def book_to_txt(book: Book, path: Path, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [book.title, ""]
    for chapter in book.chapters:
        lines.append(chapter.title)
        lines.append("")
        for para in chapter.paragraphs:
            lines.append(para)
            lines.append("")
        lines.append("")
    path.write_text("\n".join(lines), encoding=encoding)


def export_epub(book: Book, path: Path, source_title: str | None = None) -> None:
    try:
        from ebooklib import epub
    except ImportError as exc:
        raise RuntimeError("导出 EPUB 需要安装 ebooklib") from exc

    out = epub.EpubBook()
    stable_hash = zlib.crc32((source_title or book.title).encode("utf-8")) % 10_000_000
    out.set_identifier(f"hangul-translated-{stable_hash}")
    out.set_title(source_title or book.title)
    out.set_language("zh")

    chapter_items = []
    for i, chapter in enumerate(book.chapters, start=1):
        file_name = f"chap_{i:04d}.xhtml"
        item = epub.EpubHtml(title=chapter.title, file_name=file_name, lang="zh")
        body = [f"<h1>{html.escape(chapter.title)}</h1>"]
        body.extend(f"<p>{html.escape(p)}</p>" for p in chapter.paragraphs)
        item.content = "".join(body)
        out.add_item(item)
        chapter_items.append(item)

    out.toc = tuple(
        epub.Link(item.file_name, item.title, item.file_name) for item in chapter_items
    )
    out.add_item(epub.EpubNcx())
    out.add_item(epub.EpubNav())
    out.spine = ["nav"] + chapter_items

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(path), out)
