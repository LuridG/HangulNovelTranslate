"""成品 EPUB 词表无损矫正（In-Place Glossary Fixer）。

对应 docs/epub_glossary_fixer_plan.md：对已完成排版的成品 EPUB 做纯文本节点替换，
只修改正文文本，图片 / CSS / 字体 / 元数据 / 目录结构全部原样保留。
"""
from __future__ import annotations

import os
import tempfile
import zipfile
import copy
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString
from .glossary import apply_replacement_pairs, merge_replacement_pairs


def _is_html(name: str) -> bool:
    return name.lower().endswith((".xhtml", ".html", ".htm"))


def _apply_pairs(text: str, pairs: list[tuple[str, str]]) -> str:
    """按 source 长度降序做全量替换（与 glossary.apply_replacements 等价）。"""
    return apply_replacement_pairs(text, pairs)


def _pair_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return dict(pairs)


def preview_finished_epub(
    epub_path: str | Path,
    glossary: Any,
    common_glossary: Any | None = None,
) -> dict[str, Any]:
    """扫描成品 EPUB，统计每个正文文件将发生的替换。

    返回 {"total_hits": int, "files": [{"file", "hits": {src: count}}], "lines": [str]}。
    """
    epub_path = Path(epub_path)
    if common_glossary is None:
        pairs, origin = glossary.replacement_pairs(), {}
    else:
        dedi = copy.deepcopy(glossary)
        if hasattr(dedi, "apply_common_override"):
            dedi.apply_common_override(common_glossary)
        pairs, origin = merge_replacement_pairs(dedi, common_glossary)
    result: dict[str, Any] = {
        "total_hits": 0,
        "files": [],
        "lines": [],
        "common_hits": 0,
        "dedicated_hits": 0,
    }
    if not pairs:
        return result
    dst_map = _pair_map(pairs)

    with zipfile.ZipFile(epub_path, "r") as zin:
        for item in zin.infolist():
            if not _is_html(item.filename):
                continue
            soup = BeautifulSoup(
                zin.read(item.filename), "html.parser", from_encoding="utf-8"
            )
            hits: dict[str, int] = {}
            for node in soup.find_all(string=True):
                if not isinstance(node, NavigableString):
                    continue
                if node.parent.name in ("script", "style"):
                    continue
                text = str(node)
                for src, _ in pairs:
                    if src and src in text:
                        hits[src] = hits.get(src, 0) + text.count(src)
            if hits:
                result["files"].append({"file": item.filename, "hits": hits})
                for src, count in hits.items():
                    result["total_hits"] += count
                    is_common = origin.get(src) == "common"
                    tag = "通用" if is_common else "专用"
                    if is_common:
                        result["common_hits"] += count
                    else:
                        result["dedicated_hits"] += count
                    result["lines"].append(
                        f'[{item.filename}] {tag} "{src}" -> "{dst_map.get(src, "")}" '
                        f"(出现 {count} 次)"
                    )
    return result


def fix_finished_epub_in_place(
    epub_path: str | Path,
    glossary: Any,
    output_path: str | Path,
    common_glossary: Any | None = None,
) -> dict[str, Any]:
    """对成品 EPUB 执行原地词表无损修正，返回统计。

    只替换正文 XHTML/HTML 中的纯文本节点；图片 / CSS / 字体 / 元数据按原字节复制。
    """
    epub_path = Path(epub_path)
    output_path = Path(output_path)
    override_count = 0
    if common_glossary is not None and hasattr(glossary, "apply_common_override"):
        override_count = glossary.apply_common_override(common_glossary)
    pairs, _ = merge_replacement_pairs(glossary, common_glossary)
    stats: dict[str, Any] = {
        "hit_count": 0,
        "modified_files": 0,
        "files": [],
        "common_override_count": override_count,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 先写到同目录临时文件，成功后再原子替换，避免原地覆盖时读写同一路径。
    fd, tmp_name = tempfile.mkstemp(
        prefix=".fix_", suffix=".epub", dir=str(output_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(epub_path, "r") as zin, zipfile.ZipFile(
            tmp_path, "w"
        ) as zout:
            for item in zin.infolist():
                content = zin.read(item.filename)
                if _is_html(item.filename):
                    soup = BeautifulSoup(content, "html.parser", from_encoding="utf-8")
                    file_hits = 0
                    modified = False
                    for node in soup.find_all(string=True):
                        if not isinstance(node, NavigableString):
                            continue
                        if node.parent.name in ("script", "style"):
                            continue
                        original = str(node)
                        fixed = _apply_pairs(original, pairs)
                        if fixed != original:
                            node.replace_with(fixed)
                            modified = True
                            file_hits += sum(original.count(src) for src, _ in pairs)
                    if modified:
                        stats["modified_files"] += 1
                        stats["hit_count"] += file_hits
                        stats["files"].append(
                            {"file": item.filename, "hit_count": file_hits}
                        )
                        # formatter="minimal" 尽量少改写原 HTML，避免引入额外转义。
                        content = soup.encode(formatter="minimal")
                zout.writestr(item, content)
        os.replace(tmp_path, output_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return stats
