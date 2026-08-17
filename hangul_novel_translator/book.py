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


def _collect_block_style(tag) -> BlockStyle:
    klass = " ".join(tag.get("class") or [])
    style = (tag.get("style") or "").strip()
    attrs: dict = {}
    for key in ("id", "lang", "align", "title"):
        value = tag.get(key)
        if value:
            attrs[key] = str(value)
    return BlockStyle(tag=tag.name, klass=klass, style=style, attrs=attrs)


def _ancestor_styles(node) -> list[BlockStyle]:
    """收集块元素到 body 之间的容器样式链（div/section/ul 等带 class/style 的祖先）。"""
    chain: list[BlockStyle] = []
    parent = getattr(node, "parent", None)
    while parent is not None and getattr(parent, "name", None) not in (None, "body", "html", "[document]"):
        name = getattr(parent, "name", "") or ""
        if name in (
            "div",
            "section",
            "article",
            "aside",
            "main",
            "blockquote",
            "ul",
            "ol",
            "table",
            "thead",
            "tbody",
            "tr",
            "td",
            "th",
        ):
            style = _collect_block_style(parent)
            if style.klass or style.style or style.attrs:
                chain.append(style)
        parent = getattr(parent, "parent", None)
    chain.reverse()
    return chain


def _paragraph_style_for(block) -> ParagraphStyle | None:
    block_style = _collect_block_style(block)
    ancestors = _ancestor_styles(block)
    if (
        block_style.tag == "p"
        and not block_style.klass
        and not block_style.style
        and not block_style.attrs
        and not ancestors
    ):
        return None
    return ParagraphStyle(block=block_style, ancestors=ancestors)


def _css_urls(css: str) -> list[str]:
    return re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css)


def _collect_css_resources(book) -> list[dict]:
    """收集原书 CSS 文件、字体与 CSS 引用的图片，供导出时原样复用。"""
    from ebooklib import ITEM_FONT, ITEM_IMAGE, ITEM_STYLE

    resources: list[dict] = []
    added_names: set[str] = set()
    css_texts: list[str] = []
    for item in book.get_items():
        if item.get_type() == ITEM_STYLE:
            content = bytes(item.get_content())
            resources.append({"name": item.get_name(), "content": content})
            added_names.add(item.get_name())
            try:
                css_texts.append(content.decode("utf-8", errors="ignore"))
            except Exception:
                pass
    referenced: set[str] = set()
    for css in css_texts:
        for url in _css_urls(css):
            referenced.add(_href_basename(url))
    for item in book.get_items():
        if item.get_name() in added_names:
            continue
        if item.get_type() == ITEM_FONT:
            resources.append({"name": item.get_name(), "content": bytes(item.get_content())})
            added_names.add(item.get_name())
        elif item.get_type() == ITEM_IMAGE and _href_basename(item.get_name()) in referenced:
            resources.append({"name": item.get_name(), "content": bytes(item.get_content())})
            added_names.add(item.get_name())
    return resources


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

    doc_inline_css: dict[str, list[str]] = {}
    css_resources = _collect_css_resources(book)

    for index, idref in enumerate(spine_ids):
        item_id = idref[0] if isinstance(idref, (tuple, list)) else idref
        item = items.get(item_id)
        if item is None:
            continue
        content = _decode_bytes(item.get_content())
        soup = BeautifulSoup(content, "html.parser")
        inline_css = [st.get_text() for st in soup.find_all("style")]
        if inline_css:
            doc_inline_css.setdefault(item_id, []).extend(inline_css)
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
        styles: list[ParagraphStyle | None] = []
        seen: set[str] = set()
        for block in blocks:
            text = _strip_invisible_chars(block.get_text(" ", strip=True))
            if not text or text in seen:
                continue
            seen.add(text)
            paragraphs.append(text)
            styles.append(_paragraph_style_for(block))

        if not paragraphs:
            body = soup.body or soup
            text = _strip_invisible_chars(body.get_text("\n", strip=True))
            paragraphs = [x.strip() for x in text.split("\n") if x.strip()]

        if any(marker in basename.lower() for marker in skip_filename_markers):
            continue
        if _has_skip_text_marker(paragraphs):
            continue

        if paragraphs:
            chapters.append(
                Chapter(index, chapter_title, paragraphs, source_id=item_id, styles=styles)
            )

    result = Book(title=title, chapters=chapters, source_path=path)
    if doc_inline_css:
        result.metadata["doc_inline_css"] = doc_inline_css
    if css_resources:
        result.metadata["css_resources"] = css_resources
    return result


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


def _mime_for_name(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {
        ".css": "text/css",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".eot": "application/vnd.ms-fontobject",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")


def _style_attrs(style: BlockStyle) -> str:
    parts: list[str] = []
    if style.klass:
        parts.append(f'class="{html.escape(style.klass, quote=True)}"')
    if style.style:
        parts.append(f'style="{html.escape(style.style, quote=True)}"')
    for key, value in style.attrs.items():
        parts.append(f'{html.escape(key, quote=True)}="{html.escape(str(value), quote=True)}"')
    return " ".join(parts)


def _block_to_html(style: ParagraphStyle | None, text: str) -> str:
    """按段落样式还原 HTML：无样式输出普通 <p>，有样式则还原标签/class/内联 style。"""
    if style is None or style.block is None:
        return f"<p>{html.escape(text)}</p>"
    attrs = _style_attrs(style.block)
    opening = f"<{style.block.tag} {attrs}>" if attrs else f"<{style.block.tag}>"
    return f"{opening}{html.escape(text)}</{style.block.tag}>"


def _ancestors_key(style: ParagraphStyle | None):
    """父容器链的唯一键：连续相同键的段落合并到同一容器里，避免每个段落重复开 div。"""
    if style is None or not style.ancestors:
        return None
    return tuple(
        (a.tag, a.klass, a.style, tuple(sorted(a.attrs.items()))) for a in style.ancestors
    )


def _ancestors_open(style: ParagraphStyle | None) -> str:
    if style is None:
        return ""
    parts: list[str] = []
    for a in style.ancestors:
        attrs = _style_attrs(a)
        parts.append(f"<{a.tag} {attrs}>" if attrs else f"<{a.tag}>")
    return "".join(parts)


def _ancestors_close(style: ParagraphStyle | None) -> str:
    if style is None:
        return ""
    return "".join(f"</{a.tag}>" for a in reversed(style.ancestors))


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

    metadata = book.metadata or {}
    css_resources: list[dict] = metadata.get("css_resources") or []
    doc_inline_css: dict[str, list[str]] = metadata.get("doc_inline_css") or {}

    css_names = {
        str(res["name"])
        for res in css_resources
        if str(res["name"]).lower().endswith(".css")
    }

    chapter_items: list = []
    for i, chapter in enumerate(book.chapters, start=1):
        file_name = f"chap_{i:04d}.xhtml"
        item = epub.EpubHtml(title=chapter.title, file_name=file_name, lang="zh")
        for name in sorted(css_names):
            item.add_link(href=name, rel="stylesheet", type="text/css")
        inline_styles = doc_inline_css.get(chapter.source_id) or []
        if inline_styles:
            # 原文档的内联 <style> 转成独立 CSS 项，只挂到对应章节。
            inline_name = f"Styles/inline_{i:04d}.css"
            item.add_link(href=inline_name, rel="stylesheet", type="text/css")
            css_resources.append(
                {
                    "name": inline_name,
                    "content": ("\n".join(inline_styles)).encode("utf-8"),
                }
            )
        body = [f"<h1>{html.escape(chapter.title)}</h1>"]
        prev_key = None
        prev_style: ParagraphStyle | None = None
        first = True
        for pi, paragraph in enumerate(chapter.paragraphs):
            style = chapter.styles[pi] if pi < len(chapter.styles) else None
            key = _ancestors_key(style)
            if key != prev_key:
                if not first and prev_style is not None:
                    body.append(_ancestors_close(prev_style))
                if style is not None:
                    body.append(_ancestors_open(style))
                prev_key = key
                prev_style = style
            body.append(_block_to_html(style, paragraph))
            first = False
        if not first and prev_style is not None:
            body.append(_ancestors_close(prev_style))

        item.content = "".join(body)
        out.add_item(item)
        chapter_items.append(item)

    for res in css_resources:
        name = str(res["name"])
        if not any(it.file_name == name for it in out.items):
            out.add_item(
                epub.EpubItem(
                    uid=f"res-{len(out.items)}",
                    file_name=name,
                    media_type=_mime_for_name(name),
                    content=res["content"],
                )
            )

    out.toc = tuple(
        epub.Link(item.file_name, item.title, item.file_name) for item in chapter_items
    )
    out.add_item(epub.EpubNcx())
    out.add_item(epub.EpubNav())
    out.spine = ["nav"] + chapter_items

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(path), out)
