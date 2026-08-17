# hangul_novel_translator/utils.py
from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """从模型返回文本中尽量稳定地解析 JSON。"""
    if text is None:
        raise ValueError("空响应")
    text = text.strip()
    if not text:
        raise ValueError("空响应")

    # 直接解析。
    try:
        return json.loads(text)
    except Exception:
        pass

    # 去掉 markdown 代码块。
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 截取第一个 { 到最后一个 }。
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 截取第一个 [ 到最后一个 ]。
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raise ValueError("无法从模型响应中解析 JSON")


def parse_paragraphs_from_payload(payload: Any) -> list[str]:
    """兼容多种返回结构，返回非空段落列表。"""
    if isinstance(payload, str):
        candidates = [payload]
    elif isinstance(payload, dict):
        for key in ("paragraphs", "translation", "text", "content"):
            value = payload.get(key)
            if value is not None:
                return parse_paragraphs_from_payload(value)
        # 形如 {"p1": "...", "p2": "..."}。
        items = []
        for value in payload.values():
            items.extend(parse_paragraphs_from_payload(value))
        return _clean_paragraphs(items)
    elif isinstance(payload, list):
        candidates = payload
    else:
        candidates = [str(payload)]

    result: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            result.extend(parse_paragraphs_from_payload(item))
        elif item is not None:
            result.append(str(item).strip())
    return _clean_paragraphs(result)


def _clean_paragraphs(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            item = str(item)
        item = item.strip()
        if item:
            out.append(item)
    return out


def split_paragraph_smart(text: str, max_chars: int) -> list[str]:
    """先按行/段，再按句子标点拆分超长段落。"""
    raw_parts = re.split(r"\n+", text.strip())
    chunks: list[str] = []
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")

    def push(value: str) -> None:
        value = value.strip()
        if not value:
            return
        if len(value) <= max_chars:
            chunks.append(value)
            return
        for i in range(0, len(value), max_chars):
            chunks.append(value[i : i + max_chars].strip())

    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            chunks.append(part)
            continue

        # 韩语与中文通用句子分隔符。保留分隔符。
        sentences = re.split(r"(?<=[.!?…。！？])", part)
        buffer = ""
        for sentence in sentences:
            candidate = (buffer + " " + sentence).strip()
            if buffer and len(candidate) > max_chars:
                push(buffer)
                buffer = sentence.strip()
            else:
                buffer = candidate
        if buffer:
            push(buffer)
    return [c for c in chunks if c]
