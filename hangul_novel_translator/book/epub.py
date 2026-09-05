# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import html
import os
import posixpath
import re
import tempfile
import unicodedata
import zlib
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import (unquote, urljoin, urlparse)
from .models import BlockStyle
from .models import Book
from .models import Chapter
from .models import ParagraphStyle
from ._consts import _DOC_PAGE_SUFFIXES
from ._consts import _HREF_ATTR_RE
from ._consts import _INLINE_TAGS
from ._consts import _SKIP_PAGE_MARKERS
from .text import _decode_bytes
from .markup import _document_attrs
from .markup import _encode_inline_marker
from .markup import _inline_markers_to_html
from .markup import _inline_style_css
from .metadata import _looks_like_cover_image
from .metadata import _mime_for_name
from .metadata import _needs_cjk_font_fallback
from .markup import _outer_container_snapshot
from .markup import _safe_tag_attrs
from .text import _strip_invisible_chars
from .txt import parse_txt


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



def _epub_package_path(base: str, href: str) -> str:
    """把 OPF 中的 href 解析为规范化的包内路径。"""
    value = html.unescape(unquote(str(href or ""))).replace("\\", "/")
    value = value.split("#", 1)[0].split("?", 1)[0]
    return posixpath.normpath(posixpath.join(base, value)).lstrip("/")



def _prepare_epub_for_reader(path: Path) -> tuple[Path, Path | None]:
    """为 EPUB 阅读器创建临时副本，移除 OPF 中指向不存在文件的条目。"""
    path = Path(path)
    with zipfile.ZipFile(path, "r") as zin:
        names = set(zin.namelist())
        try:
            container_root = ET.fromstring(zin.read("META-INF/container.xml"))
        except (KeyError, ET.ParseError):
            return path, None

        opf_paths = [
            node.attrib.get("full-path", "")
            for node in container_root.iter()
            if node.tag.rsplit("}", 1)[-1] == "rootfile"
        ]
        replacements: dict[str, bytes] = {}
        for opf_path in opf_paths:
            if not opf_path or opf_path not in names:
                continue
            try:
                root = ET.fromstring(zin.read(opf_path))
            except ET.ParseError:
                continue
            base = posixpath.dirname(opf_path)
            manifest = next(
                (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "manifest"),
                None,
            )
            if manifest is None:
                continue
            removed_ids: set[str] = set()
            for item in list(manifest):
                if item.tag.rsplit("}", 1)[-1] != "item":
                    continue
                package_path = _epub_package_path(base, item.attrib.get("href", ""))
                if package_path in names:
                    continue
                item_id = item.attrib.get("id")
                if item_id:
                    removed_ids.add(item_id)
                manifest.remove(item)
            if not removed_ids:
                continue
            for spine in root.iter():
                if spine.tag.rsplit("}", 1)[-1] != "spine":
                    continue
                for itemref in list(spine):
                    if (itemref.tag.rsplit("}", 1)[-1] == "itemref" and
                            itemref.attrib.get("idref") in removed_ids):
                        spine.remove(itemref)
            replacements[opf_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        if not replacements:
            return path, None
        temp_handle = tempfile.NamedTemporaryFile(
            prefix=f".{path.stem}.epub-", suffix=".epub", delete=False
        )
        temp_path = Path(temp_handle.name)
        temp_handle.close()
        try:
            with zipfile.ZipFile(temp_path, "w") as zout:
                for info in zin.infolist():
                    content = replacements.get(info.filename, zin.read(info.filename))
                    zout.writestr(info, content)
        except Exception:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise
        return temp_path, temp_path



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



def _rewrite_inline_image_hrefs(text: str, document_path: str) -> str:
    """把包内图片路径转换成相对于当前 XHTML 文档的路径。"""
    return re.sub(
        r"⟦img:([^⟧]*)⟧",
        lambda match: f"⟦img:{_relative_epub_href(document_path, match.group(1))}⟧",
        text,
    )



def _chapter_volume(source_id: str | None) -> str:
    """从合并后的 source_id 提取卷前缀（如 ``v1:Section0011.html`` → ``v1``）。

    未合并的单卷存档 source_id 没有前缀，统一返回空串，作为同一个命名空间。
    """
    sid = str(source_id or "")
    if ":" in sid:
        prefix, _ = sid.split(":", 1)
        if re.fullmatch(r"v\d+", prefix):
            return prefix
    return ""



def _rewrite_internal_doc_links(
    html: str,
    volume: str,
    chapter_href_map: dict[tuple[str, str], str],
    anchor_map: dict[tuple[str, str], str] | None = None,
) -> str:
    """把指向原书其他章节（HTML）的 href 改写为合并后的章节文件名。

    原书脚注常用跨文件引用，例如 ``../Text/Section0011.html#cmm01``；正文中的
    引用指向独立注释页，注释页又反向指回正文章节。导出时每个章节被重命名为
    ``chap_NNNN.xhtml``，不改写这些 href 则所有脚注链接全部失效。

    - 优先按「(卷, 源文件名)」找到目标章节（适用于独立注释页）。
    - 目标源文件若在解析时并入其他章节（无独立章节），回退按 ``#锚点`` 在
      全部输出章节中查找其实际所在文件，保证反向链接也能落地。
    """
    anchor_map = anchor_map or {}

    def repl(match) -> str:
        quote = match.group(1)
        href = match.group(2)
        close = match.group(3)
        lowered = href.lower()
        if lowered.startswith(("#", "http:", "https:", "data:", "mailto:", "javascript:")):
            return match.group(0)
        parsed = urlparse(href)
        path = unquote(parsed.path)
        if not path:
            return match.group(0)
        target_base = _href_basename(path).lower()
        if not target_base.endswith(_DOC_PAGE_SUFFIXES):
            return match.group(0)
        fragment = parsed.fragment
        new_file = chapter_href_map.get((volume, target_base))
        if not new_file and fragment:
            new_file = anchor_map.get((volume, fragment))
        if not new_file:
            return match.group(0)
        new_href = f"{new_file}#{fragment}" if fragment else new_file
        return f"{quote}{new_href}{close}"

    return _HREF_ATTR_RE.sub(repl, html)



def _is_standalone_image_container(node) -> bool:
    """判断容器是否是未被块级标签覆盖的图片叶节点。"""
    if getattr(node, "name", None) not in ("span", "div", "figure", "section", "aside"):
        return False
    if not node.find("img"):
        return False
    block_names = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dt", "dd", "pre", "figcaption"}
    return not any(getattr(child, "name", None) in block_names for child in node.find_all())



def _standalone_image_blocks(soup, existing_blocks: list) -> list:
    """找出未被块级标签覆盖的独立图片容器，并按原文顺序返回。"""
    existing = {id(node) for node in existing_blocks}
    candidates = []
    for image in soup.find_all("img"):
        parent = image.parent
        while parent is not None and getattr(parent, "name", None) not in ("body", "html", "[document]"):
            if getattr(parent, "name", None) in ("p", "li", "blockquote", "td", "th", "figcaption"):
                break
            if _is_standalone_image_container(parent):
                if id(parent) not in existing:
                    candidates.append(parent)
                break
            parent = getattr(parent, "parent", None)
    unique = {id(node): node for node in candidates}
    order = {id(node): index for index, node in enumerate(soup.find_all())}
    return sorted(unique.values(), key=lambda node: order.get(id(node), 0))



def parse_epub(path: Path, *, include_standalone_images: bool = True) -> Book:
    try:
        from bs4 import BeautifulSoup
        from ebooklib import epub
    except ImportError as exc:
        raise RuntimeError("解析 EPUB 需要安装 ebooklib 和 beautifulsoup4") from exc

    try:
        from ebooklib import ITEM_DOCUMENT
    except ImportError:
        ITEM_DOCUMENT = getattr(epub, "ITEM_DOCUMENT", 9)

    reader_path, temp_path = _prepare_epub_for_reader(path)
    try:
        book = epub.read_epub(str(reader_path), options={"ignore_ncx": True})
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass
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
        if include_standalone_images:
            blocks.extend(_standalone_image_blocks(soup, blocks))
            order = {id(node): position for position, node in enumerate(soup.find_all())}
            blocks.sort(key=lambda node: order.get(id(node), 0))
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
                                    # 正文已经翻译为中文，语言属性必须保留 EpubHtml(lang="zh")
                                    # 生成的值。恢复源书的 ko/en 会让阅读器按错误语言选择
                                    # CJK 回退字体；class/style/id 等排版属性仍完整恢复。
                                    if tag_name == "html" and str(attr).lower() in ("lang", "xml:lang"):
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
    needs_cjk_fallback = _needs_cjk_font_fallback(metadata, book.chapters)
    fallback_css_name = "Styles/zh_font_fallback.css"
    if needs_cjk_fallback:
        # 韩文小说常把 p/h1/h2 直接声明为韩文字体，只覆盖 body 无法生效。
        # 这里追加一份最后加载的同优先级规则，把常见正文/标题容器统一改成
        # 中文字体栈；原 CSS 字节不动，字号、行距、缩进、对齐、class 与行内
        # 样式完全保留。fallback 是该章最后引入的样式表，靠加载顺序覆盖。
        css_resources.append({
            "name": fallback_css_name,
            "content": (
                'body, p, h1, h2, h3, h4, h5, h6, div, span, li, blockquote, '
                'td, th, a { font-family: "Microsoft YaHei", "Noto Sans CJK SC", '
                '"Noto Sans SC", sans-serif; }'
            ).encode("utf-8"),
        })

    css_names = {
        str(res["name"])
        for res in css_resources
        if str(res["name"]).lower().endswith(".css")
    }
    # 标题 CSS 排版：写入独立样式表并由每章 <link> 关联；给章节标题加统一 class。
    title_css_cfg = metadata.get("title_css") or {}
    title_class = re.sub(
        r"[^A-Za-z0-9_-]", "", str(title_css_cfg.get("class") or "")
    ) or "chapter-title"
    title_css_text = str(title_css_cfg.get("css") or "").strip()
    has_title_css = bool(title_css_text)
    title_css_name = "Styles/title.css"
    if has_title_css:
        existing_names = {str(res.get("name")) for res in css_resources}
        suffix = 2
        while title_css_name in existing_names:
            title_css_name = f"Styles/title_{suffix}.css"
            suffix += 1
        css_resources.append(
            {
                "name": title_css_name,
                "content": title_css_text.encode("utf-8"),
            }
        )
    chapter_css_map = metadata.get("chapter_css") or {}

    chapter_items: list = []
    exported_structure: dict[str, dict] = {}
    rendered: list[tuple] = []
    chapter_href_map: dict[tuple[str, str], str] = {}
    for i, chapter in enumerate(book.chapters, start=1):
        base = _href_basename(chapter.source_id or "").lower()
        if base:
            chapter_href_map[(_chapter_volume(chapter.source_id), base)] = f"chap_{i:04d}.xhtml"

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
        if has_title_css:
            item.add_link(href=_relative_epub_href(file_name, title_css_name), rel="stylesheet", type="text/css")
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
        if needs_cjk_fallback:
            item.add_link(href=_relative_epub_href(file_name, fallback_css_name), rel="stylesheet", type="text/css")
        if has_title_css:
            title_html = (
                f"<h{chapter.heading_level} class=\"{title_class}\">"
                f"{html.escape(chapter.display_title)}</h{chapter.heading_level}>"
            )
        else:
            title_html = (
                f"<h{chapter.heading_level}>{html.escape(chapter.display_title)}"
                f"</h{chapter.heading_level}>"
            )
        body = [title_html]
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
        rendered.append((item, _chapter_volume(chapter.source_id), "".join(body)))

    anchor_map: dict[tuple[str, str], str] = {}
    for item, volume, body_html in rendered:
        for anchor in re.findall(r'id="([^"]+)"', body_html):
            anchor_map.setdefault((volume, anchor), item.file_name)
    for item, volume, body_html in rendered:
        item.content = _rewrite_internal_doc_links(body_html, volume, chapter_href_map, anchor_map)
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
    images = list(metadata.get("images") or [])
    cover_img = next(
        (
            img
            for img in images
            if _looks_like_cover_image(str(img.get("name", ""))) and img.get("content")
        ),
        None,
    )
    for img in images:
        name = str(img.get("name", ""))
        if not name or any(it.file_name == name for it in out.items):
            continue
        if img is cover_img:
            # 封面稍后单独作为 EpubCover 声明，避免重复加入同类项。
            continue
        out.add_item(
            epub.EpubItem(
                uid=f"img-{len(out.items)}",
                file_name=name,
                media_type=_mime_for_name(name),
                content=img.get("content", b""),
            )
        )
    if cover_img is not None:
        cover_name = str(cover_img.get("name", ""))
        cover_item = epub.EpubCover(uid="cover-img", file_name=cover_name)
        cover_item.media_type = _mime_for_name(cover_name)
        cover_item.content = cover_img.get("content", b"")
        out.add_item(cover_item)
        out.add_metadata(None, "meta", "", {"name": "cover", "content": "cover-img"})

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
