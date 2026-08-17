# hangul_novel_translator/glossary.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .llm import LLMClient
from .utils import extract_json, parse_paragraphs_from_payload

# 可能译法参与替换的最短长度：单字变体几乎必是常用字，跳过以避免误替换。
_MIN_ALTERNATIVE_LEN = 2


@dataclass
class GlossaryEntry:
    ko: str
    zh: str
    kind: str = "term"
    note: str = ""
    confirmed: bool = False
    alternatives: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GlossaryEntry":
        return cls(
            ko=str(data.get("ko", "")).strip(),
            zh=str(data.get("zh", "")).strip(),
            kind=str(data.get("kind", "term")).strip() or "term",
            note=str(data.get("note", "")).strip(),
            confirmed=bool(data.get("confirmed", False)),
            alternatives=str(data.get("alternatives", "")).strip(),
        )

    def alternative_list(self) -> list[str]:
        """把半角逗号分隔的可能译法解析为列表（去掉空项）。"""
        return [x.strip() for x in self.alternatives.split(",") if x.strip()]


@dataclass
class Glossary:
    entries: list[GlossaryEntry] = field(default_factory=list)
    source_text: str = ""
    raw_response: str = ""
    sample_chars: int = 0

    def valid_entries(self) -> list[GlossaryEntry]:
        return [e for e in self.entries if e.ko and e.zh]

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_text": self.source_text,
            "raw_response": self.raw_response,
            "sample_chars": self.sample_chars,
            "entries": [e.to_dict() for e in self.entries],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        path = Path(path)
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        glossary = cls(
            source_text=str(payload.get("source_text", "")),
            raw_response=str(payload.get("raw_response", "")),
            sample_chars=int(payload.get("sample_chars", 0)),
        )
        for item in payload.get("entries", []):
            if isinstance(item, dict):
                glossary.entries.append(GlossaryEntry.from_dict(item))
        return glossary

    def upsert(self, entry: GlossaryEntry) -> None:
        for old in self.entries:
            if old.ko == entry.ko:
                old.zh = entry.zh
                old.kind = entry.kind
                old.note = entry.note
                old.confirmed = entry.confirmed
                return
        self.entries.append(entry)

    def remove(self, ko: str) -> None:
        self.entries = [e for e in self.entries if e.ko != ko]

    def append_unique(self, entries: list[GlossaryEntry]) -> tuple[int, int]:
        """追加新词条并自动去重（以韩文原文为准，已存在的保持原样）；返回 (新增数, 跳过数)。"""
        existing_kos = {e.ko for e in self.entries}
        added = 0
        skipped = 0
        for entry in entries:
            if not entry.ko or not entry.zh:
                continue
            if entry.ko in existing_kos:
                skipped += 1
                continue
            self.entries.append(entry)
            existing_kos.add(entry.ko)
            added += 1
        return added, skipped

    def dedupe(self) -> int:
        """移除韩文原文重复的词条（保留第一条）；返回移除条数。"""
        seen: set[str] = set()
        kept: list[GlossaryEntry] = []
        removed = 0
        for entry in self.entries:
            if not entry.ko or entry.ko in seen:
                removed += 1
                continue
            seen.add(entry.ko)
            kept.append(entry)
        self.entries = kept
        return removed

    def to_prompt_lines(self) -> list[str]:
        lines: list[str] = []
        for e in self.valid_entries():
            note = f"（{e.note}）" if e.note else ""
            lines.append(f"- {e.ko} -> {e.zh}{note}")
        return lines

    def prompt_text(self) -> str:
        lines = self.to_prompt_lines()
        return "\n".join(lines) if lines else "（无）"

    def apply_replacements(self, text: str) -> str:
        """翻译后再做一层保底替换：韩文专名替换为人工译名；
        已确认词条的可能误译（alternatives）也会替换为人工译名，作为对 LLM 的机器矫正。"""
        # 收集 (源词, 目标译名) 并去重。
        pairs: list[tuple[str, str]] = []
        for entry in self.valid_entries():
            pairs.append((entry.ko, entry.zh))
            if entry.confirmed:
                for alt in entry.alternative_list():
                    if len(alt) >= _MIN_ALTERNATIVE_LEN:
                        pairs.append((alt, entry.zh))
        # 按字符串长度降序，避免短词先替换破坏长词。
        for src, dst in sorted(set(pairs), key=lambda pair: len(pair[0]), reverse=True):
            if src and src in text:
                text = text.replace(src, dst)
        return text


EXTRACTION_SYSTEM = """你是一名资深的韩语小说中文译者与编辑，负责从小说样章中提取需要在全书中保持译名一致的专有名词词表。

【提取范围】
只提取真正的专有名词：人名、地名、组织/势力、种族或物种、特殊称谓、作品内设定术语、固定物品名。
不要提取：普通名词、动词、形容词、副词、数词、时间词、常见场所词（如学校、医院、便利店）、完整句子、口语短句。
只出现一次且不重要的词可以不提取。

【词形规范】
- 必须提取原形，去掉助词与词尾：正文中的“준희가”“준희는”“제원을”应统一提取为“준희”“제원”。
- 不要包含称呼后缀：不要带“씨/님/군/양/선생님”等。
- 同一实体只保留一条：正文中同一人、地、物有多种写法时，取最常见、最完整的形式。

【译名规范】
- 中文译名一律使用简体中文，并在全书中保持一致。
- 优先使用常见汉字，避免生僻音译字；人名用字要自然、常见。
- 拿不准时也要给出最合理的译名，并在 note 中注明“待确认”。

【输出格式】
只输出一个 JSON 对象，不要输出解释或 Markdown 代码块。
字段：ko=韩文原词，zh=中文译名，kind=person|place|org|term|title，note=简短备注。
示例：{"entries":[{"ko":"준희","zh":"俊熙","kind":"person","note":""}]}"""


def extract_glossary_with_llm(llm: LLMClient, sample_text: str, limit: int) -> Glossary:
    sample = sample_text.strip()
    if not sample:
        raise ValueError("没有读取到可用样章，无法提取词表。请确认 EPUB/TXT 已成功解析。")

    user = (
        "请从下面这段小说样章中提取专有名词词表。"
        f"最多提取 {limit} 条，按出现频率和重要性从高到低排序；"
        "只出现一次且不重要的词可以不提取。原文如下：\n\n"
        f"{sample[:200000]}"
    )
    messages = [
        {"role": "system", "content": EXTRACTION_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw_response = llm.chat(messages, temperature=0.1, json_mode=True)
    try:
        payload = extract_json(raw_response)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"模型没有返回合法 JSON：{raw_response[:300]}") from exc

    glossary = payload_to_glossary(payload, source_text=sample_text)
    glossary.raw_response = raw_response
    glossary.sample_chars = len(sample)
    glossary.entries = glossary.entries[:limit]
    return glossary


EXTRACTION_MORE_SYSTEM = """你是一名资深的韩语小说中文译者与编辑。用户已经维护了一份专有名词词表，现在需要你根据新的样章，只补充词表中还没有的专有名词。

【核心要求】
1. 只提取现有词表中还没有的新专有名词。
2. 不要重复输出现有词条，也不要把现有词条换个写法再输出：同一实体（如已有“준희”，正文出现“준희 씨”）不得作为新词条。
3. 不要修改、点评现有词表。

【提取范围】
只提取真正的专有名词：人名、地名、组织/势力、种族或物种、特殊称谓、作品内设定术语、固定物品名。
不要提取：普通名词、动词、形容词、副词、数词、时间词、常见场所词（如学校、医院、便利店）、完整句子、口语短句。
只出现一次且不重要的词可以不提取。

【词形规范】
- 必须提取原形，去掉助词与词尾：正文中的“준희가”“준희는”“제원을”应统一提取为“준희”“제원”。
- 不要包含称呼后缀：不要带“씨/님/군/양/선생님”等。
- 同一实体只保留一条：多种写法取最常见、最完整的形式。

【译名规范】
- 中文译名一律使用简体中文，并与现有词表的用字风格保持一致。
- 优先使用常见汉字，避免生僻音译字；拿不准时也要给出最合理的译名，并在 note 中注明“待确认”。

【输出格式】
只输出一个 JSON 对象，不要输出解释或 Markdown 代码块。
字段：ko=韩文原词，zh=中文译名，kind=person|place|org|term|title，note=简短备注。
示例：{"entries":[{"ko":"제원","zh":"宰元","kind":"person","note":""}]}"""


def extract_more_glossary(
    llm: LLMClient,
    sample_text: str,
    existing: Glossary,
    limit: int,
) -> Glossary:
    """基于已有词表 + 新样章，提取词表中还没有的更多专有名词。"""
    sample = sample_text.strip()
    if not sample:
        raise ValueError("没有读取到可用样章，无法提取词表。请确认 EPUB/TXT 已成功解析。")

    existing_lines = []
    for entry in existing.valid_entries():
        mark = "（已确认）" if entry.confirmed else "（待确认）"
        existing_lines.append(f"- {entry.ko} -> {entry.zh} [{entry.kind}]{mark}")
    existing_text = "\n".join(existing_lines) if existing_lines else "（无）"

    user = (
        "现有词表：\n"
        f"{existing_text}\n\n"
        "请从下面这段新的小说样章中提取现有词表中还没有的专有名词。"
        f"最多提取 {limit} 条，按出现频率和重要性从高到低排序；"
        "只出现一次且不重要的词可以不提取。原文如下：\n\n"
        f"{sample[:200000]}"
    )
    messages = [
        {"role": "system", "content": EXTRACTION_MORE_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw_response = llm.chat(messages, temperature=0.1, json_mode=True)
    try:
        payload = extract_json(raw_response)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"模型没有返回合法 JSON：{raw_response[:300]}") from exc

    glossary = payload_to_glossary(payload, source_text=sample_text)
    glossary.raw_response = raw_response
    glossary.sample_chars = len(sample)
    glossary.entries = glossary.entries[:limit]
    return glossary


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
        )

    if isinstance(item, str):
        parts = [x.strip() for x in item.split("->", 1)]
        if len(parts) == 2:
            return GlossaryEntry(ko=parts[0], zh=parts[1])
    return None
