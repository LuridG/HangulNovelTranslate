# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import base64
import re
from pathlib import Path
from .models import Chapter


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



def _looks_like_cover_image(name: str) -> bool:
    """按文件名判断是否为封面：去掉扩展名与非字母数字后，名称以 cover 开头。"""
    base = Path(str(name)).name.lower()
    stem = "".join(ch for ch in base if ch.isalnum())
    return stem.startswith("cover")



def _contains_cjk_text(value: str) -> bool:
    """检测中日韩统一表意文字；用于只对中文翻译启用字体兼容层。"""
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value or ""))



def _needs_cjk_font_fallback(metadata: dict, chapters: list[Chapter]) -> bool:
    """命中原书把正文继承到韩文字体、而译文包含中文的 EPUB。"""
    css_text = "\n".join(
        _as_text(res.get("content", b""))
        for res in metadata.get("css_resources") or []
        if str(res.get("name", "")).lower().endswith(".css")
    )
    return bool(re.search(r"굴림|gulim|바탕|batang|돋움|dotum|궁서|gungsuh|맑은 고딕|malgun", css_text, re.IGNORECASE)) and any(
        _contains_cjk_text(paragraph)
        for chapter in chapters
        for paragraph in chapter.paragraphs
    )



def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")
