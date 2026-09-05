# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
from typing import (Any, Callable)
from ..glossary import Glossary
from ..utils import extract_json
from .models import PerspectiveBlock
from .models import PerspectiveError
from .models import PerspectiveOptions
from ._consts import _MARKUP_RE
from .analysis import build_perspective_prompt


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
