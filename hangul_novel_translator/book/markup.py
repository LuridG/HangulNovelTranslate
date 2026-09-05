# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import base64
import html
import json
from ._consts import _INLINE_ATTRS
from ._consts import _INLINE_STYLE_KEYS
from ._consts import _INLINE_TAGS
from ._consts import _MARKER_RE


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



def strip_inline_markers(text: str, *, image_placeholder: str = "【插图】") -> str:
    """去掉行内格式标记，用于 TXT 输出与词表采样。"""
    def _repl(match) -> str:
        return image_placeholder if match.group(2) == "img" else ""

    return _MARKER_RE.sub(_repl, text)
