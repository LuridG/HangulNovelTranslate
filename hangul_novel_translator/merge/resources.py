# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import posixpath
import re
from typing import Any
from ..book import (Book, Chapter, ParagraphStyle, _href_basename, book_to_txt, export_epub, load_book, metadata_from_dict, metadata_to_dict, parse_epub)
from ._consts import _CSS_IMPORT_RE
from ._consts import _CSS_URL_RE
from ._consts import _IMAGE_MARKER_RE


def _is_path_escape(name: str) -> bool:
    """判断资源名是否包含 .. 目录跳转，这类路径会被 EPUB 工具判为包外引用。"""
    value = str(name).replace("\\", "/")
    return any(part in ("..",) for part in value.split("/"))



def _safe_resource_name(name: str) -> str:
    """把带 .. 的包内资源名规范化为根目录下的安全路径。

    图片统一落到 Images/，样式落到 Styles/，其余落到 Misc/。
    只调整最终打包路径，原引用（正文 ⟦img:...⟧、CSS url()）由调用方
    通过 target map 一并改写，因此不改动原名字符串本身的匹配关系。
    """
    value = str(name).replace("\\", "/").lstrip("/")
    if not _is_path_escape(value):
        return value
    basename = posixpath.basename(value)
    if not basename:
        return value
    lower = basename.lower()
    if lower.endswith(".css"):
        return posixpath.join("Styles", basename)
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return posixpath.join("Images", basename)
    return posixpath.join("Misc", basename)




def _as_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    return str(content).encode("utf-8")



def _rewrite_css_urls(
    css_text: str,
    old_css_name: str,
    new_css_name: str,
    resource_map: dict[str, str],
) -> str:
    """按 CSS 原路径解析 url()，再改写为最终资源的相对路径。"""
    new_css_dir = posixpath.dirname(new_css_name) or "."

    def repl(match) -> str:
        raw = match.group(1).strip()
        url = raw.strip("'\"")
        if url.lower().startswith(("data:", "http://", "https://")):
            return match.group(0)
        source_target = posixpath.normpath(
            posixpath.join(posixpath.dirname(old_css_name) or ".", url)
        ).lstrip("/")
        target = resource_map.get(source_target)
        if not target:
            return match.group(0)
        rel = posixpath.relpath(target, start=new_css_dir)
        return f"url('{rel}')"

    return re.sub(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", repl, css_text)



def _rewrite_css_imports(
    css_text: str,
    old_css_name: str,
    new_css_name: str,
    resource_map: dict[str, str],
) -> str:
    """改写 @import 的字符串路径，兼容 url() 形式由 _rewrite_css_urls 处理。"""
    new_css_dir = posixpath.dirname(new_css_name) or "."

    def repl(match) -> str:
        quote = match.group(1)
        url = match.group(2).strip()
        source_target = posixpath.normpath(
            posixpath.join(posixpath.dirname(old_css_name) or ".", url)
        ).lstrip("/")
        target = resource_map.get(source_target)
        if not target:
            return match.group(0)
        rel = posixpath.relpath(target, start=new_css_dir)
        return f"@import {quote}{rel}{quote}"

    return re.sub(r"@import\s+(['\"])([^'\"]+)\1", repl, css_text, flags=re.IGNORECASE)



def _css_dependencies_match(
    css_name: str,
    css_text: str,
    current_resources: dict[str, bytes],
    existing_resources: dict[str, bytes],
) -> bool:
    """判断同字节 CSS 是否仍可共享：其相对引用的资源也必须逐一相同。"""
    urls = list(_CSS_URL_RE.findall(css_text))
    urls.extend(_CSS_IMPORT_RE.findall(css_text))
    for raw in urls:
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(css_name) or ".", raw)
        ).lstrip("/")
        if target in current_resources and current_resources[target] != existing_resources.get(target):
            return False
    return True



def _rewrite_image_markers(text: str, image_map: dict[str, str]) -> str:
    return _IMAGE_MARKER_RE.sub(
        lambda m: f"\u27e6img:{image_map.get(m.group(1), m.group(1))}\u27e7", text
    )



def _merge_static_resources(books: list[Book]):
    """把多卷的 CSS/图片资源合并进同一包：
    - 同名且内容相同 → 去重共享（同一本书各卷通常同套资源）；
    - 同名但内容不同 → 同目录改名（CSS 内 url() 同步改写；正文里的 ⟦img:旧名⟧
      由调用方按 image_map 改写）。
    返回 (css_resources, images, doc_inline_css, volume_css, volume_css_maps, volume_image_maps)，
    其中 CSS 和图片映射均按卷保存，供正文和章节资源链接分别重写。"""
    resources: list[dict] = []
    by_name: dict[str, bytes] = {}
    by_base: dict[str, list[tuple[str, bytes]]] = {}
    doc_inline_css: dict[str, list[str]] = {}
    volume_image_maps: list[dict[str, str]] = []
    volume_css_list: list[list[str]] = []
    volume_css_maps: list[dict[str, list[str]]] = []
    volume_structures: list[dict[str, dict]] = []

    for volume_index, book in enumerate(books, start=1):
        image_map: dict[str, str] = {}
        metadata = book.metadata or {}
        volume_structures.append({
            f"v{volume_index}:{sid}": dict(value)
            for sid, value in (metadata.get("document_structure") or {}).items()
        })
        raw = list(metadata.get("css_resources") or []) + list(metadata.get("images") or [])
        current_resource_bytes = {
            str(res.get("name", "")): _as_bytes(res.get("content", b""))
            for res in raw
            if res.get("name")
        }
        target: dict[str, str] = {}
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            safe_name = _safe_resource_name(name)
            base = _href_basename(safe_name)
            reused = next(
                (
                    full
                    for full, data in by_base.get(base, [])
                    if data == content
                    and (
                        not safe_name.lower().endswith(".css")
                        or _css_dependencies_match(
                            name,
                            content.decode("utf-8", errors="ignore"),
                            current_resource_bytes,
                            {r["name"]: r["content"] for r in resources},
                        )
                    )
                ),
                None,
            )
            if reused:
                target[name] = reused
                continue
            candidate = safe_name
            if candidate in by_name:
                parent = posixpath.dirname(safe_name) or "."
                stem = posixpath.splitext(posixpath.basename(safe_name))[0]
                suffix = posixpath.splitext(safe_name)[1]
                candidate = posixpath.join(parent, f"{stem}_v{volume_index}{suffix}")
                guard = 2
                while candidate in by_name:
                    candidate = posixpath.join(
                        parent, f"{stem}_v{volume_index}_{guard}{suffix}"
                    )
                    guard += 1
            target[name] = candidate
            by_name[candidate] = content
            by_base.setdefault(base, []).append((candidate, content))

        volume_css: list[str] = []
        for res in raw:
            name = str(res.get("name", ""))
            if not name:
                continue
            content = _as_bytes(res.get("content", b""))
            final = target.get(name, name)
            if name.lower().endswith(".css"):
                css_text = content.decode("utf-8", errors="ignore")
                css_text = _rewrite_css_urls(css_text, name, final, target)
                css_text = _rewrite_css_imports(css_text, name, final, target)
                content = css_text.encode("utf-8")
            if not any(r["name"] == final for r in resources):
                resources.append({"name": final, "content": content})
            if name.lower().endswith(".css") and final not in volume_css:
                volume_css.append(final)

        for sid, styles in (metadata.get("doc_inline_css") or {}).items():
            doc_inline_css[f"v{volume_index}:{sid}"] = list(styles)
        css_map: dict[str, list[str]] = {}
        for sid, names in (metadata.get("chapter_css") or {}).items():
            css_map[f"v{volume_index}:{sid}"] = [target.get(str(name), str(name)) for name in names]
        for res in metadata.get("images") or []:
            name = str(res.get("name", ""))
            final = target.get(name)
            if final and final != name:
                image_map[name] = final
        volume_image_maps.append(image_map)
        volume_css_list.append(volume_css)
        volume_css_maps.append(css_map)

    css_resources = [r for r in resources if r["name"].lower().endswith(".css")]
    images = [r for r in resources if not r["name"].lower().endswith(".css")]
    return css_resources, images, doc_inline_css, volume_css_list, volume_css_maps, volume_image_maps, volume_structures
