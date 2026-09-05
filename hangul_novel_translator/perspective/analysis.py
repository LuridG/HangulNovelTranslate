# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import html
import re
from pathlib import Path
from typing import (Any, Callable)
from bs4 import (BeautifulSoup, Comment, NavigableString, Tag)
from ..glossary import Glossary
from .models import PerspectiveBlock
from .models import PerspectiveError
from .models import PerspectiveOptions
from ._consts import _BLOCK_TAGS
from ._consts import _HTML_SUFFIXES
from ._consts import _INNER_RE
from ._consts import _LETTER_RE
from ._consts import _QUOTE_PAIR_RE
from ._consts import _RAW_SKIP_TAGS
from ._consts import _RAW_TAG_RE
from ._consts import _RAW_TOKEN_RE
from ._consts import _SKIP_FILE_MARKERS


def _is_html(name: str) -> bool:
    return name.lower().endswith(_HTML_SUFFIXES)



def _node_text(node: Tag) -> str:
    return node.get_text("", strip=False)



def _marker_value(node: Tag) -> str:
    values: list[str] = []
    for key in ("class", "id", "role", "epub:type", "data-type"):
        value = node.get(key)
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        elif value:
            values.append(str(value))
    return " ".join(values)



def _has_marker(node: Tag, pattern: re.Pattern[str]) -> bool:
    current: Tag | None = node
    while current is not None:
        if pattern.search(_marker_value(current)):
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False



def _has_dialogue(text: str) -> bool:
    return bool(_QUOTE_PAIR_RE.search(text))



def _is_inner_monologue(node: Tag, text: str) -> bool:
    if _has_marker(node, _INNER_RE):
        return True
    compact = text.strip()
    return (
        len(compact) >= 2
        and ((compact[0], compact[-1]) in {("（", "）"), ("(", ")")})
        and not _has_dialogue(compact)
    )



def _is_letter_or_quote(node: Tag) -> bool:
    if _has_marker(node, _LETTER_RE):
        return True
    current: Tag | None = node
    while current is not None:
        if current.name in {"blockquote", "q", "pre", "code"}:
            return True
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return False



def _text_nodes(node: Tag) -> list[NavigableString]:
    result: list[NavigableString] = []
    for item in node.find_all(string=True):
        if not isinstance(item, NavigableString):
            continue
        if isinstance(item, Comment):
            continue
        parent = item.parent
        if not isinstance(parent, Tag) or parent.name in {"script", "style", "noscript"}:
            continue
        # 空白节点仍留在原 DOM 中，不送给模型，也不替换它。
        if str(item).strip():
            result.append(item)
    return result



def _contains_block_ancestor(node: Tag) -> bool:
    parent = node.parent
    while isinstance(parent, Tag) and parent.name not in {"body", "html"}:
        if parent.name in _BLOCK_TAGS:
            return True
        parent = parent.parent
    return False



def _iter_content_blocks(soup: BeautifulSoup) -> list[tuple[Tag, str]]:
    """只取叶级正文块，避免同一段被 div/p 重复送入模型。"""
    candidates: list[Tag] = []
    for node in soup.find_all(list(_BLOCK_TAGS)):
        if not _contains_block_ancestor(node):
            candidates.append(node)
    if not candidates:
        for node in soup.find_all(["div", "section", "article"]):
            if not node.find(list(_BLOCK_TAGS)) and node.get_text(strip=True):
                candidates.append(node)
    if not candidates and soup.body and soup.body.get_text(strip=True):
        candidates = [soup.body]

    result: list[tuple[Tag, str]] = []
    for node in candidates:
        text = _node_text(node)
        if not text.strip():
            continue
        if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            classification = "heading"
        elif node.name in {"pre", "code"}:
            classification = "code"
        elif _is_letter_or_quote(node):
            classification = "letter_or_quote"
        elif _has_dialogue(text):
            classification = "dialogue"
        elif _is_inner_monologue(node, text):
            classification = "inner_monologue"
        else:
            classification = "narration"
        result.append((node, classification))
    return result



def _eligible(classification: str, options: PerspectiveOptions) -> bool:
    if options.strategy == "coverage":
        return classification not in {"heading", "code"}
    if classification == "narration":
        return True
    if classification == "dialogue":
        return options.rewrite_dialogue
    if classification == "letter_or_quote":
        return options.rewrite_letters
    if classification == "inner_monologue":
        return options.rewrite_inner_monologue
    return False



def _block_from_node(file_name: str, index: int, node: Tag, classification: str) -> PerspectiveBlock | None:
    segments = _text_nodes(node)
    if not segments:
        return None
    source_segments = [str(segment) for segment in segments]
    return PerspectiveBlock(
        id=f"{file_name}::b{index:05d}",
        file=file_name,
        index=index,
        source_segments=source_segments,
        source_text="".join(source_segments),
        classification=classification,
    )



def _style_instruction(options: PerspectiveOptions) -> str:
    return {
        "full_name": f"需要时优先使用叙述者全名“{options.narrator_name}”",
        "short_name": f"需要时优先使用叙述者简称“{options.short_name or options.narrator_name}”",
        "pronoun": f"叙述中优先使用主角代词“{options.pronoun}”，避免每句重复姓名",
    }[options.style]



def build_perspective_prompt(
    options: PerspectiveOptions,
    glossary: Glossary | None,
    blocks: list[PerspectiveBlock],
) -> list[dict[str, str]]:
    options = options.normalized()
    proper_names = "（无）"
    if glossary:
        lines: list[str] = []
        seen: set[str] = set()
        for entry in glossary.valid_entries():
            value = entry.zh.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            suffix = f"（{entry.kind}）" if entry.kind else ""
            lines.append(f"- {entry.ko} -> {value}{suffix}")
        if lines:
            proper_names = "\n".join(lines)

    strategy_instruction = (
        "采用策略 2（正文全覆盖）：所有非标题、非代码正文块都已送入。逐段判断哪些文字属于叙述性旁白，"
        "只把旁白中的第一人称改成第三人称；对白、人物原话、书信、聊天、日记和引用中的说话者第一人称应保持原样。"
        "classification 只是粗略提示，混合段落必须分别处理旁白和引号内原话。"
        if options.strategy == "coverage"
        else "采用策略 1（保守筛选）：输入主要是已由本地规则筛选出的叙述性旁白。"
    )
    system = (
        "你是一名中文小说编辑，负责把第一人称叙述改写成自然、连贯的第三人称叙述。\n"
        f"叙述者/主角：{options.narrator_name}\n"
        f"主角代词：{options.pronoun}\n"
        f"称谓风格：{_style_instruction(options)}\n"
        f"处理策略：{strategy_instruction}\n"
        "规则：\n"
        "1. 只改写叙述性旁白中的第一人称指代；保持事实、时态、语气、情节和信息量。\n"
        "2. 输入中的每个 block 与 segment 必须原样保留编号和顺序，返回数量完全一致。\n"
        "3. 不要把第三人称改回第一人称；不要把“我”机械替换成姓名，按上下文调整“我/我的/我看见”等指代。\n"
        "4. 对话、书信、聊天、日记、引用和内心独白若被送入，按原文语境处理，不擅自改变说话者身份。\n"
        "5. 不改变专有名词、人物关系、地点、组织、作品名和格式；不要输出 HTML 标签、解释或 Markdown。\n"
        f"6. 专有名词表如下，中文译名必须保持完全一致：\n{proper_names}\n"
        '只输出 JSON：{"blocks":[{"id":"原 block id","segments":["改写后的 segment"]}]}。'
    )
    payload_lines: list[str] = []
    for block in blocks:
        payload_lines.append(f"BLOCK {block.id}")
        payload_lines.append(f"CLASSIFICATION: {block.classification}")
        for index, segment in enumerate(block.source_segments):
            payload_lines.append(f"SEGMENT {index}: {segment}")
        payload_lines.append("END BLOCK")
    user = (
        "请改写下面这些正文块。只处理每个 segment 的文字，不要合并、拆分、排序或遗漏 segment。\n\n"
        + "\n".join(payload_lines)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]



def _document_has_nav(soup: BeautifulSoup) -> bool:
    return soup.find("nav") is not None



def _skip_document(name: str, soup: BeautifulSoup) -> bool:
    path_parts = {part.lower() for part in Path(name).parts}
    stem = Path(name).stem.lower()
    return _document_has_nav(soup) or stem in _SKIP_FILE_MARKERS or bool(path_parts & set(_SKIP_FILE_MARKERS))



def _apply_segments(node: Tag, segments: list[str]) -> None:
    nodes = _text_nodes(node)
    if len(nodes) != len(segments):
        raise PerspectiveError("正文节点结构已变化，无法安全应用译文")
    for node_text, value in zip(nodes, segments):
        node_text.replace_with(value)



def _decode_xhtml(data: bytes) -> tuple[str, str]:
    """解码 XHTML，随后只替换文本片段并按相同编码写回。"""
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    match = re.search(br"encoding\s*=\s*['\"]([^'\"]+)['\"]", data[:512], re.I)
    encoding = match.group(1).decode("ascii", errors="ignore") if match else "utf-8"
    try:
        return data.decode(encoding), encoding
    except (LookupError, UnicodeDecodeError) as exc:
        raise PerspectiveError(f"无法安全解码 XHTML：{exc}") from exc



def _raw_text_positions(document: str) -> list[tuple[int, int, str]]:
    """返回原 XHTML 中可见文本 token 的位置，标签和 script/style 内容不参与匹配。"""
    positions: list[tuple[int, int, str]] = []
    skip_stack: list[str] = []
    for match in _RAW_TOKEN_RE.finditer(document):
        token = match.group(0)
        if token.startswith("<"):
            tag_match = _RAW_TAG_RE.match(token)
            if not tag_match:
                continue
            closing = bool(tag_match.group(1))
            name = tag_match.group(2).lower()
            self_closing = token.rstrip().endswith("/>")
            if closing:
                if name in _RAW_SKIP_TAGS:
                    for index in range(len(skip_stack) - 1, -1, -1):
                        if skip_stack[index] == name:
                            del skip_stack[index]
                            break
            elif not self_closing and name in _RAW_SKIP_TAGS:
                skip_stack.append(name)
            continue
        if not skip_stack and token.strip():
            positions.append((match.start(), match.end(), html.unescape(token)))
    return positions



def _replace_raw_blocks(
    content: bytes,
    ordered_blocks: list[PerspectiveBlock],
    state_blocks: dict[str, dict[str, Any]],
) -> tuple[bytes, set[str], set[str]]:
    """只替换原 XHTML 的文本 token，保留标签、属性、空白和 XML 声明字节。"""
    document, encoding = _decode_xhtml(content)
    positions = _raw_text_positions(document)
    cursor = 0
    replacements: list[tuple[int, int, str]] = []
    applied: set[str] = set()
    mismatched: set[str] = set()

    for block in ordered_blocks:
        item = state_blocks.get(block.id)
        source_segments = block.source_segments
        segment_positions: list[tuple[int, int]] = []
        local_cursor = cursor
        matched = True
        for source in source_segments:
            found_index = None
            for index in range(local_cursor, len(positions)):
                position = positions[index]
                start, end, value = position
                if value == source:
                    found_index = index
                    break
            if found_index is None:
                # 受保护块找不到时不阻断后续目标块；目标块则标记为结构不匹配。
                if item and item.get("status") == "completed":
                    mismatched.add(block.id)
                matched = False
                break
            start, end, _ = positions[found_index]
            segment_positions.append((start, end))
            local_cursor = found_index + 1
        if not matched:
            continue
        cursor = local_cursor
        if not item or item.get("status") != "completed" or block.id in mismatched:
            continue
        translated = [str(value) for value in (item.get("translated_segments") or [])]
        if len(translated) != len(segment_positions):
            mismatched.add(block.id)
            continue
        for (start, end), value in zip(segment_positions, translated):
            replacements.append((start, end, html.escape(value, quote=False)))
        applied.add(block.id)

    for start, end, value in reversed(replacements):
        document = document[:start] + value + document[end:]
    try:
        return document.encode(encoding), applied, mismatched
    except UnicodeEncodeError as exc:
        raise PerspectiveError(f"视角转换译文无法按原 XHTML 编码写回：{exc}") from exc
