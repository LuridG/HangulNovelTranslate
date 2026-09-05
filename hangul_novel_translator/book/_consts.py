# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re


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



_SKIP_PAGE_MARKERS = ("목차", "판권", "copyright")




_MARKER_RE = re.compile("\u27e6(/?)([a-z]+)(?::([^\u27e7]*))?\u27e7")

_INLINE_STYLE_KEYS = ("color", "background-color", "font-style", "font-weight")

_INLINE_ATTRS = ("class", "id", "style", "title", "lang", "dir", "href", "role", "epub:type", "color", "face", "size")

_INLINE_TAGS = {"span", "font", "a", "sup", "sub", "code", "small", "mark", "ruby", "rt"}



_DOC_PAGE_SUFFIXES = (".html", ".xhtml", ".htm")

_HREF_ATTR_RE = re.compile(r"(href\s*=\s*[\"'])([^\"']+)([\"'])", re.IGNORECASE)
