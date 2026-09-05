# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from typing import Any
from .models import Glossary
from .models import GlossaryEntry
from .models import _clean_alternatives

def payload_to_glossary(payload: Any, source_text: str = "") -> Glossary:
    glossary = Glossary(source_text=source_text)
    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, dict):
        # 已知的分组键，按顺序合并。
        grouped_keys = (
            "entries",
            "terms",
            "people",
            "persons",
            "characters",
            "places",
            "locations",
            "organizations",
            "orgs",
            "titles",
        )
        collected: list[Any] = []
        for key in grouped_keys:
            value = payload.get(key)
            if isinstance(value, list):
                collected.extend(value)
            elif isinstance(value, dict):
                collected.append(value)
        if collected:
            raw_entries = collected
        else:
            # 兼容 {"韩文": "中文"} 简单映射。
            for ko, zh in payload.items():
                if isinstance(zh, (str, int, float)) and isinstance(ko, str):
                    glossary.entries.append(GlossaryEntry(ko=ko.strip(), zh=str(zh).strip(), kind="term"))
            return glossary
    else:
        raw_entries = []

    for item in raw_entries:
        entry = _entry_from_item(item)
        if entry:
            glossary.entries.append(entry)
    return glossary



def _entry_from_item(item: Any) -> GlossaryEntry | None:
    if isinstance(item, dict):
        ko = (
            item.get("ko")
            or item.get("kr")
            or item.get("korean")
            or item.get("source")
            or item.get("원문")
            or item.get("原文")
        )
        zh = (
            item.get("zh")
            or item.get("cn")
            or item.get("chinese")
            or item.get("target")
            or item.get("中文")
            or item.get("译名")
        )
        if not ko or not zh:
            return None
        return GlossaryEntry(
            ko=str(ko).strip(),
            zh=str(zh).strip(),
            kind=str(item.get("kind", "term")).strip() or "term",
            note=str(item.get("note", "")).strip(),
            confirmed=bool(item.get("confirmed", False)),
            alternatives=_clean_alternatives(
                item.get("alts") or item.get("alternatives") or "",
                ko=str(ko).strip(),
                zh=str(zh).strip(),
            ),
        )

    if isinstance(item, str):
        parts = [x.strip() for x in item.split("->", 1)]
        if len(parts) == 2:
            return GlossaryEntry(ko=parts[0], zh=parts[1])
    return None
