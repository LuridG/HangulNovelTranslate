"""EPUB 静态资源与 CSS 使用关系检查。"""
from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


@dataclass
class EpubValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unused_selectors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
_IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.IGNORECASE)
_RULE_RE = re.compile(r"([^{}]+)\{")


def _target(base: str, raw: str) -> str | None:
    raw = unquote(raw.strip())
    if not raw or raw.startswith("#") or raw.lower().startswith(("data:", "http:", "https:", "mailto:")):
        return None
    parsed = urlparse(raw)
    path = parsed.path
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path)).lstrip("/")


def _selectors(css: str) -> list[str]:
    selectors: list[str] = []
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # 容器 at-rule 的头部不是选择器；移除头部但保留其中嵌套规则供扫描。
    cleaned = re.sub(r"@(media|supports|layer|container)\b[^{}]*\{", "", cleaned, flags=re.I)
    for match in _RULE_RE.finditer(cleaned):
        header = match.group(1).strip()
        if header.startswith("@"):
            continue
        selectors.extend(part.strip() for part in header.split(",") if part.strip())
    return selectors


def validate_epub_resources(path: str | Path) -> EpubValidationResult:
    result = EpubValidationResult()
    path = Path(path)
    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        documents: dict[str, BeautifulSoup] = {}
        linked: dict[str, set[str]] = {}
        for name in names:
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            soup = BeautifulSoup(archive.read(name), "html.parser")
            documents[name] = soup
            css_names: set[str] = set()
            for link in soup.find_all("link"):
                rel = [str(x).lower() for x in (link.get("rel") or [])]
                if "stylesheet" not in rel:
                    continue
                target = _target(name, str(link.get("href") or ""))
                if target:
                    css_names.add(target)
                    if target not in names:
                        result.errors.append(f"{name}: 链接的 CSS 不存在: {target}")
            linked[name] = css_names
            for tag_name, attr in (("img", "src"), ("image", "href")):
                for tag in soup.find_all(tag_name):
                    target = _target(name, str(tag.get(attr) or ""))
                    if target and target not in names:
                        result.errors.append(f"{name}: 引用资源不存在: {target}")

        for css_name in names:
            if not css_name.lower().endswith(".css"):
                continue
            css = archive.read(css_name).decode("utf-8", errors="replace")
            for raw in _URL_RE.findall(css):
                target = _target(css_name, raw[1])
                if target and target not in names:
                    label = "字体或资源" if "@font-face" in css[max(0, css.find(raw[1]) - 120):css.find(raw[1]) + 20] else "资源"
                    result.errors.append(f"{css_name}: CSS {label}不存在: {target}")
            for _, raw in _IMPORT_RE.findall(css):
                target = _target(css_name, raw)
                if target and target not in names:
                    result.errors.append(f"{css_name}: @import 不存在: {target}")
            css_docs = [documents[doc] for doc in documents if css_name in linked.get(doc, set())]
            if not css_docs:
                continue
            for selector in _selectors(css):
                if ":" in selector:
                    continue
                try:
                    matched = any(doc.select(selector) for doc in css_docs)
                except Exception:
                    result.warnings.append(f"{css_name}: 无法检查选择器: {selector}")
                    continue
                if not matched:
                    result.unused_selectors.append(f"{css_name}: {selector}")
    return result
