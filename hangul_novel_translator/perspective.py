"""中文小说第一人称改第三人称。

视角转换是独立于韩语翻译和多卷合并的流程。输入 EPUB 作为 ZIP 读取，只有正文
XHTML/HTML 中选定的可见文本节点会被改写；CSS、图片、字体、OPF、目录以及其它
包内资源按原条目复制。每个块的原文和分段译文都写入独立状态存档，便于恢复和手动
补写失败块。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .glossary import Glossary
from .llm import LLMCancelled
from .utils import extract_json


PERSPECTIVE_PROMPT_VERSION = "perspective-v1"
_HTML_SUFFIXES = (".xhtml", ".html", ".htm")
_BLOCK_TAGS = {
    "p", "li", "blockquote", "pre", "td", "th", "dt", "dd", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP_FILE_MARKERS = ("nav", "toc", "contents", "titlepage", "cover", "copyright")
_QUOTE_PAIR_RE = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]+\"|'[^']+'")
_INNER_RE = re.compile(r"(?:inner|monologue|thought|soliloquy|心理|内心|独白|心声)", re.IGNORECASE)
_LETTER_RE = re.compile(r"(?:letter|diary|journal|chat|message|mail|书信|日记|聊天|消息|邮件|引用|quote)", re.IGNORECASE)
_MARKUP_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


class PerspectiveError(RuntimeError):
    """视角转换输入、模型返回或状态存档不满足安全条件。"""


class PerspectiveCancelled(PerspectiveError):
    """用户停止视角转换时抛出，当前批次保持待处理。"""


@dataclass
class PerspectiveOptions:
    narrator_name: str
    short_name: str = ""
    pronoun: str = "他"
    style: str = "pronoun"
    rewrite_dialogue: bool = False
    rewrite_letters: bool = False
    rewrite_inner_monologue: bool = False
    strategy: str = "conservative"
    chunk_chars: int = 1800
    prompt_version: str = PERSPECTIVE_PROMPT_VERSION

    def normalized(self) -> "PerspectiveOptions":
        name = self.narrator_name.strip()
        pronoun = self.pronoun.strip() or "他"
        style = self.style if self.style in {"full_name", "short_name", "pronoun"} else "pronoun"
        strategy = self.strategy if self.strategy in {"conservative", "coverage"} else "conservative"
        if not name:
            raise ValueError("叙述者/主角名称不能为空")
        if self.chunk_chars <= 0:
            raise ValueError("每批字符数必须大于 0")
        return PerspectiveOptions(
            narrator_name=name,
            short_name=self.short_name.strip(),
            pronoun=pronoun,
            style=style,
            rewrite_dialogue=bool(self.rewrite_dialogue),
            rewrite_letters=bool(self.rewrite_letters),
            rewrite_inner_monologue=bool(self.rewrite_inner_monologue),
            strategy=strategy,
            chunk_chars=int(self.chunk_chars),
            prompt_version=self.prompt_version or PERSPECTIVE_PROMPT_VERSION,
        )


@dataclass
class PerspectiveBlock:
    id: str
    file: str
    index: int
    source_segments: list[str]
    source_text: str
    classification: str = "narration"

    @property
    def char_count(self) -> int:
        return len(self.source_text)


@dataclass
class PerspectiveFailedBlock:
    state_path: Path
    block_id: str
    file: str
    source_segments: list[str]
    translated_segments: list[str]
    error: str
    classification: str = "narration"


def _is_html(name: str) -> bool:
    return name.lower().endswith(_HTML_SUFFIXES)


def _safe_state_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff\uac00-\ud7af]+", "_", value or "book")
    safe = re.sub(r"_+", "_", safe).strip("._")
    return safe or "book"


def _source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            part = handle.read(1024 * 1024)
            if not part:
                break
            digest.update(part)
    return digest.hexdigest()


def perspective_state_path(input_path: str | Path, output_path: str | Path) -> Path:
    """状态文件与输出文件同目录，且不会覆盖翻译主流程的 state。"""
    output = Path(output_path)
    return output.parent / f".{_safe_state_name(output.stem)}.perspective_state.json"


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


def _preserve_edge_whitespace(source: str, value: str) -> str:
    prefix = re.match(r"^\s*", source).group(0)
    suffix = re.search(r"\s*$", source).group(0)
    core = value.strip()
    if not core:
        return source
    return prefix + core + suffix


def _parse_rewrite_payload(payload: Any, blocks: list[PerspectiveBlock]) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        raise PerspectiveError("模型返回不是 JSON 对象")
    raw_blocks = payload.get("blocks")
    if not isinstance(raw_blocks, list):
        raise PerspectiveError("模型返回缺少 blocks 数组")
    expected = {block.id: block for block in blocks}
    result: dict[str, list[str]] = {}
    for item in raw_blocks:
        if not isinstance(item, dict):
            raise PerspectiveError("模型返回的 block 不是对象")
        block_id = str(item.get("id", "")).strip()
        segments = item.get("segments")
        block = expected.get(block_id)
        if block is None or block_id in result:
            raise PerspectiveError(f"模型返回了未知或重复 block：{block_id}")
        if not isinstance(segments, list) or len(segments) != len(block.source_segments):
            actual = len(segments) if isinstance(segments, list) else "无"
            raise PerspectiveError(
                f"{block_id} 的 segment 数量不匹配：期望 {len(block.source_segments)}，实际 {actual}"
            )
        clean: list[str] = []
        for source, segment in zip(block.source_segments, segments):
            if not isinstance(segment, str):
                raise PerspectiveError(f"{block_id} 含有非文本 segment")
            if _MARKUP_RE.search(segment):
                raise PerspectiveError(f"{block_id} 的模型返回包含 HTML 标签，已拒绝写回")
            if not segment.strip():
                raise PerspectiveError(f"{block_id} 含有空 segment，已拒绝写回")
            clean.append(_preserve_edge_whitespace(source, segment))
        result[block_id] = clean
    missing = set(expected) - set(result)
    if missing:
        raise PerspectiveError(f"模型遗漏 block：{', '.join(sorted(missing))}")
    return result


def rewrite_blocks(
    llm: Any,
    options: PerspectiveOptions,
    glossary: Glossary | None,
    blocks: list[PerspectiveBlock],
    *,
    cancel_event: Any = None,
) -> dict[str, list[str]]:
    if not blocks:
        return {}
    messages = build_perspective_prompt(options, glossary, blocks)
    chat_kwargs = {"temperature": 0.2, "json_mode": True}
    if cancel_event is not None:
        chat_kwargs["cancel_event"] = cancel_event
    raw = llm.chat(messages, **chat_kwargs)
    try:
        payload = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        raise PerspectiveError(f"模型没有返回合法 JSON：{str(raw)[:300]}") from exc
    result = _parse_rewrite_payload(payload, blocks)
    if glossary:
        result = {
            block_id: [glossary.apply_replacements(segment) for segment in segments]
            for block_id, segments in result.items()
        }
    return result


def _batch_blocks(blocks: list[PerspectiveBlock], limit: int) -> list[list[PerspectiveBlock]]:
    batches: list[list[PerspectiveBlock]] = []
    current: list[PerspectiveBlock] = []
    size = 0
    for block in blocks:
        block_size = max(block.char_count, 1)
        if current and size + block_size > limit:
            batches.append(current)
            current = []
            size = 0
        current.append(block)
        size += block_size
    if current:
        batches.append(current)
    return batches


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


_RAW_TOKEN_RE = re.compile(r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<[^>]*>|[^<]+")
_RAW_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9:_-]*)[^>]*?>\s*$", re.S)
_RAW_SKIP_TAGS = {"head", "script", "style", "noscript", "title"}


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


def _state_options(options: PerspectiveOptions) -> dict[str, Any]:
    return asdict(options.normalized())


def _block_state(
    block: PerspectiveBlock,
    *,
    status: str = "pending",
    error: str = "",
    translated: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "file": block.file,
        "index": block.index,
        "classification": block.classification,
        "source": block.source_text,
        "source_segments": list(block.source_segments),
        "translated_segments": list(translated or []),
        "status": status,
        "error": error,
    }


def load_failed_perspective_blocks(state_path: str | Path) -> list[PerspectiveFailedBlock]:
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    result: list[PerspectiveFailedBlock] = []
    for block_id, item in (data.get("blocks") or {}).items():
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        source_segments = item.get("source_segments") or [str(item.get("source", ""))]
        translated = item.get("translated_segments") or []
        result.append(
            PerspectiveFailedBlock(
                state_path=path,
                block_id=str(block_id),
                file=str(item.get("file", "")),
                source_segments=[str(x) for x in source_segments],
                translated_segments=[str(x) for x in translated],
                error=str(item.get("error", "")),
                classification=str(item.get("classification", "narration")),
            )
        )
    return result


def load_perspective_state(state_path: str | Path) -> dict[str, Any]:
    """读取并校验视角转换存档，供 GUI 直接恢复一次作业。"""
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("视角转换存档必须是 JSON 对象")
    if data.get("mode") != "first_to_third":
        raise ValueError("这不是第一人称改第三人称存档")
    if not str(data.get("source_path", "")).strip():
        raise ValueError("视角转换存档缺少输入 EPUB 路径")
    if not str(data.get("output_path", "")).strip():
        raise ValueError("视角转换存档缺少输出 EPUB 路径")
    options = data.get("options")
    if not isinstance(options, dict):
        raise ValueError("视角转换存档缺少 options")
    normalized_options = PerspectiveOptions(**options).normalized()
    data["options"] = _state_options(normalized_options)
    blocks = data.get("blocks")
    if not isinstance(blocks, dict):
        raise ValueError("视角转换存档缺少 blocks")
    return data


def save_manual_perspective_translation(
    state_path: str | Path,
    block_id: str,
    translated_segments: list[str],
) -> dict[str, int]:
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    item = (data.get("blocks") or {}).get(block_id)
    if not isinstance(item, dict):
        raise ValueError(f"找不到视角转换块：{block_id}")
    source_segments = item.get("source_segments") or []
    if len(source_segments) != len(translated_segments):
        raise ValueError(f"译文分段数量不匹配：需要 {len(source_segments)} 段")
    if not all(isinstance(value, str) and value.strip() for value in translated_segments):
        raise ValueError("译文不能为空")
    item["translated_segments"] = [
        _preserve_edge_whitespace(str(source), str(value))
        for source, value in zip(source_segments, translated_segments)
    ]
    item["status"] = "completed"
    item["error"] = ""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    statuses = [
        value.get("status")
        for value in (data.get("blocks") or {}).values()
        if isinstance(value, dict)
    ]
    return {
        "completed": sum(status == "completed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
    }


def save_perspective_failure(
    state_path: str | Path,
    block_id: str,
    error: str,
) -> dict[str, int]:
    """更新一次失败重试的错误原因，并保留该块的 failed 状态。"""
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    item = (data.get("blocks") or {}).get(block_id)
    if not isinstance(item, dict):
        raise ValueError(f"找不到视角转换块：{block_id}")
    item["status"] = "failed"
    item["error"] = str(error)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    statuses = [
        value.get("status")
        for value in (data.get("blocks") or {}).values()
        if isinstance(value, dict)
    ]
    return {
        "completed": sum(status == "completed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
    }


def inspect_perspective_epub(
    input_path: str | Path,
    options: PerspectiveOptions,
) -> dict[str, Any]:
    options = options.normalized()
    total_blocks = 0
    eligible_blocks = 0
    protected_blocks = 0
    total_chars = 0
    files: list[str] = []
    samples: list[str] = []
    with zipfile.ZipFile(input_path, "r") as archive:
        for info in archive.infolist():
            if not _is_html(info.filename):
                continue
            soup = BeautifulSoup(archive.read(info.filename), "html.parser", from_encoding="utf-8")
            if _skip_document(info.filename, soup):
                continue
            file_has_eligible = False
            for node, classification in _iter_content_blocks(soup):
                block = _block_from_node(info.filename, total_blocks, node, classification)
                if block is None:
                    continue
                total_blocks += 1
                total_chars += block.char_count
                if _eligible(classification, options):
                    eligible_blocks += 1
                    file_has_eligible = True
                    if len(samples) < 5:
                        samples.append(f"[{info.filename}] {block.source_text[:180]}")
                else:
                    protected_blocks += 1
            if file_has_eligible:
                files.append(info.filename)
    return {
        "total_blocks": total_blocks,
        "eligible_blocks": eligible_blocks,
        "protected_blocks": protected_blocks,
        "total_chars": total_chars,
        "files": files,
        "samples": samples,
    }


class PerspectiveConverter:
    """在源 EPUB 上执行可恢复的第一人称改第三人称转换。"""

    def __init__(
        self,
        llm: Any,
        options: PerspectiveOptions,
        glossary: Glossary | None = None,
        *,
        cancel_event: Any = None,
        progress_callback: Callable[[str, int, int, str], None] | None = None,
    ):
        self.llm = llm
        self.options = options.normalized()
        self.glossary = glossary
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback or (lambda *_: None)

    def _cancelled(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    def _save_state(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".perspective-state-", suffix=".json", dir=str(path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _scan_archive(self, archive: zipfile.ZipFile, options: PerspectiveOptions) -> dict[str, PerspectiveBlock]:
        blocks: dict[str, PerspectiveBlock] = {}
        for info in archive.infolist():
            if not _is_html(info.filename):
                continue
            soup = BeautifulSoup(archive.read(info.filename), "html.parser", from_encoding="utf-8")
            if _skip_document(info.filename, soup):
                continue
            block_number = 0
            for node, classification in _iter_content_blocks(soup):
                block = _block_from_node(info.filename, block_number, node, classification)
                block_number += 1
                if block is not None and _eligible(classification, options):
                    blocks[block.id] = block
        return blocks

    def _new_state(
        self,
        input_path: Path,
        output_path: Path,
        blocks: dict[str, PerspectiveBlock],
    ) -> dict[str, Any]:
        return {
            "mode": "first_to_third",
            "prompt_version": self.options.prompt_version,
            "source_path": str(input_path),
            "source_fingerprint": _source_fingerprint(input_path),
            "output_path": str(output_path),
            "options": _state_options(self.options),
            "blocks": {block_id: _block_state(block) for block_id, block in blocks.items()},
        }

    def _load_or_create_state(
        self,
        path: Path,
        input_path: Path,
        output_path: Path,
        blocks: dict[str, PerspectiveBlock],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        if not resume or not path.exists():
            state = self._new_state(input_path, output_path, blocks)
            self._save_state(path, state)
            return state
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("source_path") != str(input_path):
            raise ValueError("视角转换存档对应的输入 EPUB 已变化，请重置该存档后再开始。")
        if data.get("source_fingerprint") != _source_fingerprint(input_path):
            raise ValueError("输入 EPUB 已变化，为避免错写请重置视角转换存档。")
        saved_options = data.get("options")
        if not isinstance(saved_options, dict):
            raise ValueError("视角转换存档缺少 options")
        normalized_saved_options = _state_options(PerspectiveOptions(**saved_options).normalized())
        if normalized_saved_options != _state_options(self.options):
            raise ValueError("视角转换参数已变化，请先重置存档再开始。")
        data["options"] = normalized_saved_options
        old_blocks = data.get("blocks") or {}
        for block_id, block in blocks.items():
            old = old_blocks.get(block_id)
            if not isinstance(old, dict) or old.get("source_segments") != block.source_segments:
                old_blocks[block_id] = _block_state(block)
        for block_id in list(old_blocks):
            if block_id not in blocks:
                del old_blocks[block_id]
        data["blocks"] = old_blocks
        self._save_state(path, data)
        return data

    def _translate_pending(
        self,
        state_path: Path,
        state: dict[str, Any],
        blocks: dict[str, PerspectiveBlock],
    ) -> None:
        pending = []
        for block_id, block in blocks.items():
            item = state["blocks"].get(block_id) or {}
            if item.get("status") != "completed":
                pending.append(block)
        batches = _batch_blocks(pending, self.options.chunk_chars)
        total = len(blocks)
        completed = sum(
            1
            for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "completed"
        )
        attempted_failed: set[str] = set()
        self.progress_callback(
            "视角转换",
            completed,
            total,
            f"待处理 {total - completed} 块",
        )
        for batch in batches:
            if self._cancelled():
                raise PerspectiveCancelled("视角转换已停止，已完成块已保存")
            try:
                translated = rewrite_blocks(
                    self.llm,
                    self.options,
                    self.glossary,
                    batch,
                    cancel_event=self.cancel_event,
                )
                if self._cancelled():
                    raise PerspectiveCancelled("视角转换已停止，已完成块已保存")
                for block in batch:
                    item = state["blocks"][block.id]
                    item["translated_segments"] = translated[block.id]
                    item["status"] = "completed"
                    item["error"] = ""
            except LLMCancelled as exc:
                self._save_state(state_path, state)
                raise PerspectiveCancelled("视角转换已停止，已完成块已保存") from exc
            except PerspectiveCancelled:
                self._save_state(state_path, state)
                raise
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                for block in batch:
                    item = state["blocks"][block.id]
                    item["status"] = "failed"
                    item["error"] = error
                    attempted_failed.add(block.id)
            completed = sum(
                1
                for item in state["blocks"].values()
                if isinstance(item, dict) and item.get("status") == "completed"
            )
            failed = sum(
                1
                for item in state["blocks"].values()
                if isinstance(item, dict) and item.get("status") == "failed"
            )
            processed = completed + len(attempted_failed)
            self._save_state(state_path, state)
            self.progress_callback(
                "视角转换",
                processed,
                total,
                f"成功 {completed} 块，失败 {failed} 块，待处理 {max(total - processed, 0)} 块",
            )

    def _render(self, input_path: Path, output_path: Path, state: dict[str, Any]) -> dict[str, int]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if input_path.resolve() == output_path.resolve():
            raise ValueError("输出 EPUB 不能覆盖输入 EPUB")
        stats = {"modified_files": 0, "completed_blocks": 0, "failed_blocks": 0}
        fd, temp_name = tempfile.mkstemp(prefix=".perspective-", suffix=".epub", dir=str(output_path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(temp_path, "w") as target:
                for info in source.infolist():
                    content = source.read(info.filename)
                    file_changed = False
                    if _is_html(info.filename):
                        relevant = {
                            block_id: item
                            for block_id, item in (state.get("blocks") or {}).items()
                            if isinstance(item, dict) and item.get("file") == info.filename
                        }
                        if relevant:
                            soup = BeautifulSoup(content, "html.parser", from_encoding="utf-8")
                            ordered_blocks: list[PerspectiveBlock] = []
                            block_number = 0
                            for node, classification in _iter_content_blocks(soup):
                                block = _block_from_node(info.filename, block_number, node, classification)
                                block_number += 1
                                if block is not None:
                                    ordered_blocks.append(block)
                            replaced, _, mismatched = _replace_raw_blocks(
                                content,
                                ordered_blocks,
                                relevant,
                            )
                            for block_id in mismatched:
                                item = relevant[block_id]
                                item["status"] = "failed"
                                item["error"] = "输出时正文节点结构发生变化，未写入该块"
                            completed_ids = {
                                block_id
                                for block_id, item in relevant.items()
                                if item.get("status") == "completed"
                            }
                            stats["completed_blocks"] += len(completed_ids - mismatched)
                            stats["failed_blocks"] += sum(
                                item.get("status") == "failed"
                                for item in relevant.values()
                            )
                            if replaced != content:
                                content = replaced
                                file_changed = True
                            if file_changed:
                                stats["modified_files"] += 1
                    target.writestr(info, content)
            os.replace(temp_path, output_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return stats

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        resume: bool = True,
        state_path: str | Path | None = None,
    ) -> dict[str, Any]:
        input_path = Path(input_path)
        output_path = Path(output_path)
        if not input_path.exists():
            raise FileNotFoundError(f"输入 EPUB 不存在：{input_path}")
        if input_path.resolve() == output_path.resolve():
            raise ValueError("输出 EPUB 不能覆盖输入 EPUB")
        state_file = Path(state_path) if state_path is not None else perspective_state_path(input_path, output_path)
        with zipfile.ZipFile(input_path, "r") as archive:
            blocks = self._scan_archive(archive, self.options)
        state = self._load_or_create_state(state_file, input_path, output_path, blocks, resume=resume)
        cancelled = False
        try:
            self._translate_pending(state_file, state, blocks)
        except PerspectiveCancelled:
            cancelled = True
        stats = self._render(input_path, output_path, state)
        self._save_state(state_file, state)
        failed = sum(
            1 for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "failed"
        )
        completed = sum(
            1 for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "completed"
        )
        return {
            **stats,
            "cancelled": cancelled,
            "state_path": state_file,
            "output_path": output_path,
            "total_blocks": len(blocks),
            "completed_blocks": completed,
            "failed_blocks": failed,
        }

    def render_state(self, input_path: str | Path, output_path: str | Path, state_path: str | Path) -> dict[str, int]:
        state_file = Path(state_path)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        source = Path(input_path)
        if data.get("source_path") != str(source):
            raise ValueError("视角转换存档与输入 EPUB 不匹配")
        if data.get("source_fingerprint") != _source_fingerprint(source):
            raise ValueError("输入 EPUB 已变化，为避免错写请重置视角转换存档")
        stats = self._render(source, Path(output_path), data)
        self._save_state(state_file, data)
        return stats


def render_perspective_state(
    input_path: str | Path,
    output_path: str | Path,
    state_path: str | Path,
) -> dict[str, int]:
    """不调用模型，按现有状态重新生成 EPUB（供手动补写后使用）。"""
    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    options = PerspectiveOptions(**(data.get("options") or {}))
    converter = PerspectiveConverter(None, options)
    return converter.render_state(input_path, output_path, state_path)


def reset_perspective_state(
    input_path: str | Path,
    output_path: str | Path,
    state_path: str | Path | None = None,
) -> Path:
    path = Path(state_path) if state_path is not None else perspective_state_path(input_path, output_path)
    if path.exists():
        path.unlink()
    return path
