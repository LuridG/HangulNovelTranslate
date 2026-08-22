# hangul_novel_translator/book.py
from __future__ import annotations

import base64
import html
import json
import os
import posixpath
import re
import tempfile
import unicodedata
import zlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
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


def _href_basename(href: str) -> str:
    try:
        parsed = urlparse(href)
        path = unquote(parsed.path)
        return Path(path).name
    except Exception:
        return href


def _resolve_epub_path(base_path: str, href: str) -> str:
    """按 EPUB 包内路径解析相对引用，保留完整路径并去掉 query/fragment。"""
    try:
        parsed = urlparse(str(href))
        target = unquote(parsed.path).replace("\\", "/")
        if not target:
            return ""
        base = posixpath.dirname(str(base_path).replace("\\", "/"))
        resolved = posixpath.normpath(posixpath.join(base, target))
        return resolved.lstrip("/")
    except Exception:
        return str(href).replace("\\", "/")


def _relative_epub_href(document_path: str, resource_path: str) -> str:
    """把包内资源路径转换为相对于 XHTML 文档的 href。"""
    document_dir = posixpath.dirname(document_path) or "."
    return posixpath.relpath(resource_path, start=document_dir)


def _toc_href(entry) -> str:
    """取目录项的正文页文件名（无对应页返回空串）。"""
    href = getattr(entry, "href", None) or getattr(entry, "file_name", None)
    if not href:
        return ""
    return _href_basename(href)


def _toc_title(entry) -> str:
    return _strip_invisible_chars(str(getattr(entry, "title", "")))


def _collect_toc_hierarchy(toc, depth: int = 0, parent_href: str | None = None,
                           out: dict | None = None) -> dict[str, dict]:
    """递归遍历 book.toc，保留目录层级供还原树状 TOC。

    返回 {basename: {"depth", "parent_href", "is_section", "title"}}。
    纯容器节（无对应正文页的 Section）不生成章节，其子级提升到当前深度，
    避免产生悬空父级；无 href 的空链接直接忽略。
    """
    if out is None:
        out = {}
    if not toc:
        return out
    for item in toc:
        if isinstance(item, (tuple, list)):
            if not item:
                continue
            entry = item[0]
            kids = item[1] if len(item) > 1 else ()
            is_section = True
        elif hasattr(item, "href") and hasattr(item, "title"):
            entry = item
            kids = ()
            is_section = False
        else:
            continue
        href = _toc_href(entry)
        if not href:
            # 纯容器或无页链接：不新增章节，子级继承父级层级。
            _collect_toc_hierarchy(kids, depth, parent_href, out)
            continue
        out[href] = {
            "depth": depth,
            "parent_href": parent_href,
            "is_section": is_section,
            "title": _toc_title(entry),
        }
        _collect_toc_hierarchy(kids, depth + 1, href, out)
    return out


def _build_toc_tree(chapters: list[Chapter]) -> tuple:
    """把 Chapter.parent_index 关系还原为 index 级嵌套树。

    返回结构：叶子为 int（chapters 内的下标），分组为 (父下标, (子节点...))。
    若章节间不存在任何 parent_index 关系，退化为一层平铺；遇到环则断链为叶子。
    """
    index_to_pos = {ch.index: pos for pos, ch in enumerate(chapters)}
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for pos, ch in enumerate(chapters):
        parent = ch.parent_index
        if parent is not None and parent in index_to_pos:
            children.setdefault(index_to_pos[parent], []).append(pos)
        else:
            roots.append(pos)

    def build(pos: int, visiting: set[int]) -> int | tuple:
        if pos in visiting:
            return pos
        kids = children.get(pos, [])
        if kids:
            return (pos, tuple(build(k, visiting | {pos}) for k in kids))
        return pos

    return tuple(build(pos, set()) for pos in roots)


_SKIP_PAGE_MARKERS = ("목차", "판권", "copyright")


def _strip_invisible_chars(text: str) -> str:
    """去掉零宽/不可见格式字符（U+200B~U+200F、U+2060~U+2064、U+FEFF 等 Cf 类字符）。"""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _normalize_title(text: str) -> str:
    """规整章节名：只去掉零宽不可见字符并折叠空白；保留 zalgo 组合装饰符（原书故意设计的美感）。"""
    return re.sub(r"\s+", " ", _strip_invisible_chars(text)).strip()


def is_decorative_title(title: str) -> bool:
    """是否装饰性乱码标题（含组合附加符 Mn/Me，如 zalgo 效果）：保持原样、不做 LLM 翻译。"""
    return any(unicodedata.category(ch) in ("Mn", "Me") for ch in title)


def _is_weak_title(title: str) -> bool:
    """目录里的占位标题（如单字母 A/B、空标题）视为弱标题，优先用正文标题。"""
    return len(title) <= 2


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
    for key in ("id", "lang", "align", "title", "role", "epub:type"):
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
            # 无属性的语义/布局容器也要保留，否则 CSS 的后代选择器无法匹配。
            siblings = list(parent.parent.find_all(name, recursive=False)) if getattr(parent, "parent", None) is not None else []
            if len(siblings) > 1 and parent in siblings:
                style.attrs["__path"] = siblings.index(parent)
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
    css_names = [str(item.get_name()) for item in book.get_items() if item.get_type() == ITEM_STYLE]
    for css_name, css in zip(css_names, css_texts):
        for url in _css_urls(css):
            referenced.add(_resolve_epub_path(css_name, url))
    for item in book.get_items():
        if item.get_name() in added_names:
            continue
        if item.get_type() == ITEM_FONT:
            resources.append({"name": item.get_name(), "content": bytes(item.get_content())})
            added_names.add(item.get_name())
        elif item.get_type() == ITEM_IMAGE:
            # 保留 EPUB 内全部图片：嵌套 @import、未出现在正文的背景图和封面图都不能因
            # 当前章节解析不到直接引用而丢失；导出阶段会按原包内路径复用。
            resources.append({"name": item.get_name(), "content": bytes(item.get_content())})
            added_names.add(item.get_name())
    return resources



_MARKER_RE = re.compile("\u27e6(/?)([a-z]+)(?::([^\u27e7]*))?\u27e7")
_INLINE_STYLE_KEYS = ("color", "background-color", "font-style", "font-weight")
_INLINE_ATTRS = ("class", "id", "style", "title", "lang", "dir", "href", "role", "epub:type", "color", "face", "size")
_INLINE_TAGS = {"span", "font", "a", "sup", "sub", "code", "small", "mark", "ruby", "rt"}


def _safe_tag_attrs(tag, allowed: tuple[str, ...] = _INLINE_ATTRS) -> dict[str, str | list[str]]:
    attrs: dict[str, str | list[str]] = {}
    for key in allowed:
        value = tag.get(key)
        if value is None or key.lower().startswith("on"):
            continue
        if isinstance(value, list):
            value = [str(v) for v in value]
        else:
            value = str(value)
        if value:
            attrs[key] = value
    return attrs


def _attrs_to_html(attrs: dict) -> str:
    parts: list[str] = []
    for key, value in attrs.items():
        if str(key).startswith("__") or str(key).lower().startswith("on"):
            continue
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        parts.append(f' {html.escape(str(key), quote=True)}="{html.escape(str(value), quote=True)}"')
    return "".join(parts)


def _document_attrs(tag) -> dict[str, str | list[str]]:
    allowed = ("id", "class", "style", "lang", "dir", "title", "role", "xml:lang", "xmlns", "xmlns:epub", "epub:prefix")
    return _safe_tag_attrs(tag, allowed) if tag is not None else {}


def _outer_container_snapshot(body) -> list[dict]:
    """保存 body 直接外层容器，供存档审计和旧结构兼容使用。"""
    names = {"div", "section", "article", "aside", "main", "blockquote", "figure", "table", "ul", "ol"}
    result: list[dict] = []
    if body is None:
        return result
    for child in body.find_all(recursive=False):
        if child.name in names:
            result.append({"tag": child.name, "attrs": _safe_tag_attrs(child, _INLINE_ATTRS + ("align",))})
    return result


def _encode_inline_marker(tag_name: str, attrs: dict, inner: str) -> str:
    payload = json.dumps({"tag": tag_name, "attrs": attrs}, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"\u27e6x:{encoded}\u27e7{inner}\u27e6/x\u27e7"


def _decode_inline_marker(value: str) -> tuple[str, dict] | None:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw.decode("utf-8"))
        tag = str(data.get("tag", "")).lower()
        attrs = data.get("attrs") or {}
        if tag not in _INLINE_TAGS or not isinstance(attrs, dict):
            return None
        return tag, attrs
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _inline_style_css(tag) -> str:
    """从行内标签提取局部格式：只保留颜色/背景色/斜体/加粗等简单属性。"""
    parts: list[str] = []
    style = (tag.get("style") or "").strip()
    if style:
        for decl in style.rstrip(";").split(";"):
            decl = decl.strip()
            if not decl:
                continue
            key, _, value = decl.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key in _INLINE_STYLE_KEYS and value:
                parts.append(f"{key}:{value}")
    if tag.name == "font":
        color = (tag.get("color") or "").strip()
        if color and not any(p.startswith("color:") for p in parts):
            parts.append(f"color:{color}")
        face = (tag.get("face") or "").strip()
        if face and not any(p.startswith("font-family:") for p in parts):
            parts.append(f"font-family:{face}")
    return ";".join(parts)


def _resolve_href(base_href: str, src: str) -> str:
    """以章节文件为基准，把 img src 解析为 epub 包内路径。"""
    try:
        return _resolve_epub_path(base_href, src)
    except Exception:
        return src


def _collect_image_items(book) -> dict[str, bytes]:
    from ebooklib import ITEM_IMAGE

    items: dict[str, bytes] = {}
    for item in book.get_items():
        if item.get_type() == ITEM_IMAGE:
            items[str(item.get_name())] = bytes(item.get_content())
    return items


def _register_image(resolved: str, image_items: dict[str, bytes], images_out: dict) -> str:
    """把插图登记进 metadata["images"]，返回包内路径；找不到原图时仅保留标记。"""
    content = image_items.get(resolved)
    if content is None:
        for name, data in image_items.items():
            if _href_basename(name) == _href_basename(resolved):
                content = data
                resolved = name
                break
    if content is None:
        return resolved
    added = images_out["added"]
    if resolved not in added:
        added.add(resolved)
        images_out["list"].append({"name": resolved, "content": content})
    return resolved


def _inline_markup_children(node, image_items: dict, base_href: str, images_out: dict) -> str:
    parts: list[str] = []
    for child in node.children:
        parts.append(_inline_markup(child, image_items, base_href, images_out))
    return "".join(parts)


def _inline_markup(node, image_items: dict, base_href: str, images_out: dict) -> str:
    """递归提取行内内容，把局部格式编码为 ⟦b⟧/⟦i⟧/⟦u⟧/⟦s:样式⟧、插图 ⟦img:路径⟧、
    脚注引用 ⟦fn:锚点⟧、换行 ⟦br⟧ 等标记，译文回传时再还原为 HTML。"""
    if isinstance(node, str) or getattr(node, "name", None) is None:
        return str(node)
    name = node.name.lower()
    if name in ("script", "style", "noscript"):
        return ""
    if name == "img":
        src = (node.get("src") or "").strip()
        if not src or src.lower().startswith("data:"):
            return ""
        resolved = _resolve_href(base_href, src)
        return f"\u27e6img:{_register_image(resolved, image_items, images_out)}\u27e7"
    if name == "br":
        return "\u27e6br\u27e7"
    if name == "a":
        href = node.get("href") or ""
        attrs = _safe_tag_attrs(node)
        if href.startswith("#") and len(href) > 1 and not any(k in attrs for k in ("class", "id", "style", "title")):
            return f"\u27e6fn:{href[1:]}\u27e7"
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        return _encode_inline_marker(name, attrs, inner) if inner else ""
    if name in ("b", "strong"):
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        return f"\u27e6b\u27e7{inner}\u27e6/b\u27e7" if inner else ""
    if name in ("i", "em"):
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        return f"\u27e6i\u27e7{inner}\u27e6/i\u27e7" if inner else ""
    if name == "u":
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        return f"\u27e6u\u27e7{inner}\u27e6/u\u27e7" if inner else ""
    if name in ("span", "font"):
        css = _inline_style_css(node)
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        if not inner:
            return ""
        attrs = _safe_tag_attrs(node)
        if name == "span" and css and set(attrs) == {"style"}:
            return f"\u27e6s:{css}\u27e7{inner}\u27e6/s\u27e7"
        return _encode_inline_marker(name, attrs, inner) if attrs else inner
    if name in _INLINE_TAGS:
        inner = _inline_markup_children(node, image_items, base_href, images_out)
        attrs = _safe_tag_attrs(node)
        return _encode_inline_marker(name, attrs, inner) if inner else ""
    return _inline_markup_children(node, image_items, base_href, images_out)


def _block_text_markers(block, image_items: dict, base_href: str, images_out: dict) -> str:
    raw = _inline_markup(block, image_items, base_href, images_out)
    return _strip_invisible_chars(re.sub(r"\s+", " ", raw)).strip()


def _open_marker_tag(name: str, value: str) -> str:
    if name == "b":
        return "<b>"
    if name == "i":
        return "<i>"
    if name == "u":
        return "<u>"
    if name == "s":
        style_attr = f' style="{html.escape(value, quote=True)}"' if value else ""
        return f"<span{style_attr}>"
    if name == "x":
        decoded = _decode_inline_marker(value)
        if not decoded:
            return ""
        tag, attrs = decoded
        return f"<{tag}{_attrs_to_html(attrs)}>"
    return ""


def _close_marker_tag(name: str) -> str:
    if name in ("b", "i", "u"):
        return f"</{name}>"
    if name == "s":
        return "</span>"
    if name == "x":
        return "</span>"  # replaced by the decoder stack below
    return ""


def _inline_markers_to_html(text: str) -> str:
    """把 ⟦格式⟧ 标记还原为行内 HTML；自动丢弃孤立闭合、自动闭合未闭合的配对标记。"""
    escaped = html.escape(text, quote=False)
    parts: list[str] = []
    stack: list[tuple[str, str]] = []
    pos = 0
    for m in _MARKER_RE.finditer(escaped):
        parts.append(escaped[pos:m.start()])
        pos = m.end()
        closing = m.group(1) == "/"
        name = m.group(2)
        value = m.group(3) or ""
        if name == "img":
            parts.append(f'<img src="{html.escape(value, quote=True)}" alt="插图"/>')
        elif name == "br":
            parts.append("<br/>")
        elif name == "fn":
            parts.append(f'<a href="#{html.escape(value, quote=True)}"><sup>注</sup></a>')
        elif closing:
            if stack and stack[-1][0] == name:
                _, open_value = stack.pop()
                parts.append(f"</{open_value}>" if name == "x" else _close_marker_tag(name))
        elif name in ("b", "i", "u", "s"):
            stack.append((name, value))
            parts.append(_open_marker_tag(name, value))
        elif name == "x":
            decoded = _decode_inline_marker(value)
            if decoded:
                stack.append((name, decoded[0]))
                parts.append(_open_marker_tag(name, value))
        # 其余未知标记：忽略
    parts.append(escaped[pos:])
    for name, value in reversed(stack):
        parts.append(f"</{value}>" if name == "x" else _close_marker_tag(name))
    return "".join(parts)


def _rewrite_inline_image_hrefs(text: str, document_path: str) -> str:
    """把包内图片路径转换成相对于当前 XHTML 文档的路径。"""
    return re.sub(
        r"⟦img:([^⟧]*)⟧",
        lambda match: f"⟦img:{_relative_epub_href(document_path, match.group(1))}⟧",
        text,
    )


def strip_inline_markers(text: str, *, image_placeholder: str = "【插图】") -> str:
    """去掉行内格式标记，用于 TXT 输出与词表采样。"""
    def _repl(match) -> str:
        return image_placeholder if match.group(2) == "img" else ""

    return _MARKER_RE.sub(_repl, text)


def metadata_to_dict(metadata: dict) -> dict:
    """把 Book.metadata 序列化为可写入 JSON 的 dict（CSS/图片等二进制资源转 base64）。"""
    result: dict = {}
    for key in ("css_resources", "images"):
        resources = metadata.get(key)
        if not resources:
            continue
        serialized: list[dict] = []
        for res in resources:
            content = res.get("content", b"")
            if isinstance(content, str):
                content = content.encode("utf-8")
            serialized.append(
                {
                    "name": str(res.get("name", "")),
                    "content_b64": base64.b64encode(bytes(content)).decode("ascii"),
                }
            )
        result[key] = serialized
    inline = metadata.get("doc_inline_css")
    if inline:
        result["doc_inline_css"] = {str(k): list(v) for k, v in inline.items()}
    chapter_css = metadata.get("chapter_css")
    if chapter_css:
        result["chapter_css"] = {str(k): list(v) for k, v in chapter_css.items()}
    structure = metadata.get("document_structure")
    if structure:
        result["document_structure"] = structure
    return result


def metadata_from_dict(data: dict | None) -> dict:
    """metadata_to_dict 的逆操作。"""
    data = data or {}
    result: dict = {}
    for key in ("css_resources", "images"):
        resources = data.get(key)
        if not resources:
            continue
        result[key] = []
        for res in resources:
            encoded = res.get("content_b64") or res.get("content")
            if isinstance(encoded, str):
                try:
                    content = base64.b64decode(encoded)
                except Exception:
                    content = encoded.encode("utf-8")
            else:
                content = bytes(encoded or b"")
            result[key].append({"name": str(res.get("name", "")), "content": content})
    inline = data.get("doc_inline_css")
    if inline:
        result["doc_inline_css"] = {str(k): list(v) for k, v in inline.items()}
    chapter_css = data.get("chapter_css")
    if chapter_css:
        result["chapter_css"] = {str(k): list(v) for k, v in chapter_css.items()}
    structure = data.get("document_structure")
    if structure:
        result["document_structure"] = structure
    return result


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

    toc_map = _collect_toc_hierarchy(book.toc)
    items = {item.get_id(): item for item in book.get_items_of_type(ITEM_DOCUMENT)}

    chapters: list[Chapter] = []
    index_by_basename: dict[str, int] = {}
    parent_refs: dict[int, str] = {}
    spine_ids = [ref for ref in getattr(book, "spine", [])]
    if not spine_ids:
        spine_ids = list(items.keys())

    skip_filename_markers = ("cover", "copyright", "toc", "titlepage", "colophon", "frontmatter", "backmatter", "nav")

    doc_inline_css: dict[str, list[str]] = {}
    chapter_css: dict[str, list[str]] = {}
    css_resources = _collect_css_resources(book)
    image_items = _collect_image_items(book)
    metadata_images: list[dict] = []
    images_out: dict = {"added": set(), "list": metadata_images}
    document_structure: dict[str, dict] = {}

    for index, idref in enumerate(spine_ids):
        item_id = idref[0] if isinstance(idref, (tuple, list)) else idref
        item = items.get(item_id)
        if item is None:
            continue
        content = _decode_bytes(item.get_content())
        soup = BeautifulSoup(content, "html.parser")
        document_structure[item_id] = {
            "html_attrs": _document_attrs(soup.find("html")),
            "body_attrs": _document_attrs(soup.find("body")),
            "outer_containers": _outer_container_snapshot(soup.find("body")),
        }
        linked_css: list[str] = []
        for link in soup.find_all("link"):
            rel = link.get("rel") or []
            rel_values = [str(value).lower() for value in rel] if isinstance(rel, list) else [str(rel).lower()]
            href = link.get("href") or ""
            if "stylesheet" in rel_values and href:
                resolved_css = _resolve_epub_path(item.get_name(), href)
                if resolved_css and resolved_css not in linked_css:
                    linked_css.append(resolved_css)
        if linked_css:
            chapter_css[item_id] = linked_css
        inline_css = [st.get_text() for st in soup.find_all("style")]
        if inline_css:
            doc_inline_css.setdefault(item_id, []).extend(inline_css)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        basename = _href_basename(item.get_name())
        heading = soup.find(["h1", "h2", "h3"])
        heading_title = _normalize_title(heading.get_text(" ", strip=True)) if heading else ""
        toc_info = toc_map.get(basename)
        toc_title = toc_info["title"] if toc_info else ""
        body_heading_level = int(heading.name[1]) if heading and heading.name in ("h1", "h2", "h3") else 1
        heading_level = min((toc_info["depth"] + 1) if toc_info else body_heading_level, 6)
        parent_href = toc_info["parent_href"] if toc_info else None
        is_section = bool(toc_info and toc_info.get("is_section"))
        if toc_title and not _is_weak_title(toc_title):
            chapter_title = toc_title
        elif heading_title:
            chapter_title = heading_title
        else:
            chapter_title = toc_title  # 弱目录标题或空标题
        chapter_title = _normalize_title(chapter_title)  # 只做零宽/空白清理，保留 zalgo 装饰

        block_names = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dt", "dd", "pre", "figcaption"}
        blocks = []
        for node in soup.find_all(list(block_names)):
            descendants = [child for child in node.find_all() if getattr(child, "name", None) in block_names]
            if descendants and node.name not in ("blockquote", "li"):
                continue
            if any(getattr(parent, "name", None) in ("blockquote", "li") for parent in node.parents):
                continue
            blocks.append(node)
        paragraphs: list[str] = []
        styles: list[ParagraphStyle | None] = []
        seen: set[str] = set()
        for block in blocks:
            if block is heading:
                # 章节标题元素不进正文，避免标题被当正文翻译并重复输出。
                continue
            text = _block_text_markers(block, image_items, item.get_name(), images_out)
            if not text or text in seen:
                continue
            seen.add(text)
            paragraphs.append(text)
            styles.append(_paragraph_style_for(block))

        if not paragraphs:
            body = soup.body or soup
            if heading is not None:
                heading.decompose()
            text = _strip_invisible_chars(body.get_text("\n", strip=True))
            paragraphs = [x.strip() for x in text.split("\n") if x.strip()]

        if any(marker in basename.lower() for marker in skip_filename_markers):
            continue
        if _has_skip_text_marker(paragraphs):
            continue
        if not paragraphs and not chapter_title:
            continue  # 无标题也无正文的空白/装饰页

        if not chapter_title and chapters:
            # 无标题页面视为上一章的续篇正文页，并入上一章，不产生“第 X 节”式假章节名。
            chapters[-1].paragraphs.extend(paragraphs)
            chapters[-1].styles.extend(styles)
            if linked_css:
                existing_css = chapter_css.setdefault(chapters[-1].source_id, [])
                for css_name in linked_css:
                    if css_name not in existing_css:
                        existing_css.append(css_name)
            if inline_css:
                doc_inline_css.setdefault(chapters[-1].source_id, []).extend(inline_css)
            if item_id in document_structure and chapters[-1].source_id not in document_structure:
                document_structure[chapters[-1].source_id] = document_structure[item_id]
            continue
        if not chapter_title:
            chapter_title = Path(item.get_name()).stem.replace("_", " ").strip() or "未命名"

        chapters.append(
            Chapter(
                index,
                chapter_title,
                paragraphs,
                source_id=item_id,
                styles=styles,
                heading_level=heading_level,
                is_section=is_section,
            )
        )
        index_by_basename[basename] = index
        if parent_href:
            parent_refs[index] = parent_href

    for chapter in chapters:
        parent_basename = parent_refs.get(chapter.index)
        if parent_basename:
            chapter.parent_index = index_by_basename.get(parent_basename)

    result = Book(title=title, chapters=chapters, source_path=path)
    if doc_inline_css:
        result.metadata["doc_inline_css"] = doc_inline_css
    if chapter_css:
        result.metadata["chapter_css"] = chapter_css
    if css_resources:
        result.metadata["css_resources"] = css_resources
    if metadata_images:
        result.metadata["images"] = metadata_images
    if document_structure:
        result.metadata["document_structure"] = document_structure
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
        if str(key).startswith("__"):
            continue
        parts.append(f'{html.escape(key, quote=True)}="{html.escape(str(value), quote=True)}"')
    return " ".join(parts)


def _block_to_html(style: ParagraphStyle | None, text: str) -> str:
    """按段落样式还原 HTML：无样式输出普通 <p>，有样式则还原标签/class/内联 style；
    段落内的 ⟦格式⟧ 标记一并还原为行内 HTML。"""
    body = _inline_markers_to_html(text)
    if style is None or style.block is None:
        return f"<p>{body}</p>"
    attrs = _style_attrs(style.block)
    opening = f"<{style.block.tag} {attrs}>" if attrs else f"<{style.block.tag}>"
    return f"{opening}{body}</{style.block.tag}>"



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


def _restore_document_attrs(path: Path, structure: dict[str, dict]) -> None:
    """恢复 ebooklib 模板无法保留的原 XHTML/html、body 属性。"""
    if not structure or not path.exists():
        return
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return
    fd, temp_name = tempfile.mkstemp(prefix=".attrs_", suffix=".epub", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(temp_path, "w") as zout:
            for info in zin.infolist():
                content = zin.read(info.filename)
                if info.filename.lower().endswith((".xhtml", ".html")):
                    data = structure.get(info.filename)
                    if data is None:
                        stem = Path(info.filename).stem
                        candidates = [key for key in structure if Path(str(key)).stem == stem]
                        data = structure[candidates[0]] if candidates else None
                    if data is not None:
                        if not (data.get("html_attrs") or data.get("body_attrs")):
                            zout.writestr(info, content)
                            continue
                        for tag_name, key in (("html", "html_attrs"), ("body", "body_attrs")):
                            attrs = data.get(key) or {}
                            if not attrs:
                                continue
                            pattern = re.compile(rb"<" + tag_name.encode("ascii") + rb"\b[^>]*>", re.IGNORECASE)
                            def append_missing(match):
                                opening = match.group(0)
                                additions: list[str] = []
                                for attr, value in attrs.items():
                                    if str(attr).startswith("__"):
                                        continue
                                    attr_bytes = re.escape(str(attr).encode("utf-8"))
                                    opening = re.sub(
                                        rb"\s+" + attr_bytes + rb"\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
                                        b"",
                                        opening,
                                        flags=re.IGNORECASE,
                                    )
                                    if isinstance(value, list):
                                        value = " ".join(str(item) for item in value)
                                    additions.append(f' {attr}="{html.escape(str(value), quote=True)}"')
                                return opening[:-1] + "".join(additions).encode("utf-8") + b">"
                            content = pattern.sub(
                                append_missing,
                                content,
                                count=1,
                            )
                zout.writestr(info, content)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def export_epub(book: Book, path: Path, source_title: str | None = None, sanitizer: Any = None) -> None:
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
    css_resources: list[dict] = list(metadata.get("css_resources") or [])
    doc_inline_css: dict[str, list[str]] = metadata.get("doc_inline_css") or {}

    css_names = {
        str(res["name"])
        for res in css_resources
        if str(res["name"]).lower().endswith(".css")
    }
    chapter_css_map = metadata.get("chapter_css") or {}

    chapter_items: list = []
    exported_structure: dict[str, dict] = {}
    for i, chapter in enumerate(book.chapters, start=1):
        file_name = f"chap_{i:04d}.xhtml"
        item = epub.EpubHtml(title=chapter.display_title, file_name=file_name, lang="zh")
        source_structure = (metadata.get("document_structure") or {}).get(chapter.source_id)
        if source_structure:
            exported_structure[file_name] = source_structure
        chapter_css = chapter_css_map.get(chapter.source_id)
        if chapter_css:
            for name in chapter_css:
                item.add_link(href=_relative_epub_href(file_name, name), rel="stylesheet", type="text/css")
        else:
            for name in sorted(css_names):
                item.add_link(href=_relative_epub_href(file_name, name), rel="stylesheet", type="text/css")
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
        body = [
            f"<h{chapter.heading_level}>{html.escape(chapter.display_title)}</h{chapter.heading_level}>"
        ]
        prev_key = None
        prev_style: ParagraphStyle | None = None
        first = True
        for pi, paragraph in enumerate(chapter.paragraphs):
            cleaned_para = sanitizer.clean_paragraph(paragraph) if sanitizer else paragraph
            style = chapter.styles[pi] if pi < len(chapter.styles) else None
            key = _ancestors_key(style)
            if key != prev_key:
                if not first and prev_style is not None:
                    body.append(_ancestors_close(prev_style))
                if style is not None:
                    body.append(_ancestors_open(style))
                prev_key = key
                prev_style = style
            body.append(_block_to_html(style, _rewrite_inline_image_hrefs(cleaned_para, file_name)))
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
    for img in metadata.get("images") or []:
        name = str(img.get("name", ""))
        if name and not any(it.file_name == name for it in out.items):
            out.add_item(
                epub.EpubItem(
                    uid=f"img-{len(out.items)}",
                    file_name=name,
                    media_type=_mime_for_name(name),
                    content=img.get("content", b""),
                )
            )

    def toc_node(node: int | tuple):
        """把 index 级树转换为 ebooklib 的嵌套 TOC 元组。"""
        if isinstance(node, tuple):
            pos, kids = node
            item = chapter_items[pos]
            return (
                epub.Section(item.title, href=item.file_name),
                tuple(toc_node(k) for k in kids),
            )
        item = chapter_items[node]
        return epub.Link(item.file_name, item.title, item.file_name)

    tree = _build_toc_tree(book.chapters)
    out.toc = tuple(toc_node(node) for node in tree)
    out.add_item(epub.EpubNcx())
    out.add_item(epub.EpubNav())
    out.spine = ["nav"] + chapter_items

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(path), out)
    _restore_document_attrs(path, exported_structure or metadata.get("document_structure") or {})
