# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import json
import re
from dataclasses import (dataclass, field, asdict)
from pathlib import Path
from typing import Any
from ._consts import _MIN_ALTERNATIVE_LEN


def _clean_alternatives(value: Any, ko: str = "", zh: str = "") -> str:
    """把 LLM 返回的可能译法（字符串或列表）规范化为半角逗号分隔字符串。"""
    if isinstance(value, list):
        tokens = [str(x) for x in value]
    else:
        tokens = str(value or "").split(",")
    cleaned: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token or token in cleaned:
            continue
        if token == ko or token == zh:
            continue
        if len(token) < _MIN_ALTERNATIVE_LEN:
            continue
        cleaned.append(token)
    return ",".join(cleaned)



def _merge_alternatives(existing: str, incoming: str, ko: str = "", zh: str = "") -> str:
    """合并已有与新增的可能译法，去重并按长度过滤。"""
    return _clean_alternatives(existing + "," + incoming, ko=ko, zh=zh)


def merge_replacement_pairs(
    dedicated: "Glossary",
    common: "Glossary | None",
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """合并专用 + 通用词表的替换对，通用词表优先；返回 (pairs, src_origin)。

    ``src_origin`` 记录每个源串来自 ``common`` 还是 ``dedicated``，便于展示分组。
    """
    if common is None:
        return dedicated.replacement_pairs(), {}
    try:
        common_pairs = common.replacement_pairs(allow_missing_ko=True)
    except TypeError:
        common_pairs = common.replacement_pairs()
    common_srcs = {src for src, _ in common_pairs if src}
    pair_map: dict[str, str] = {}
    for src, dst in common_pairs:
        if src:
            pair_map.setdefault(src, dst)
    for src, dst in dedicated.replacement_pairs():
        if src:
            pair_map.setdefault(src, dst)
    pairs = sorted(pair_map.items(), key=lambda kv: len(kv[0]), reverse=True)
    origin = {src: ("common" if src in common_srcs else "dedicated") for src, _ in pairs}
    return pairs, origin


def apply_replacement_pairs(text: str, pairs: list[tuple[str, str]]) -> str:
    """单次遍历替换全部源串：最长优先，避免替换结果被再次命中（防链式污染）。

    例如 ``俊希 -> 俊熙（通用）`` 之后再命中 ``俊熙 -> 俊熙（通用）`` 的旧译名对时，
    不会把刚插入的“俊熙（通用）”再次改写。
    """
    if not pairs:
        return text
    uniq = sorted(set(pairs), key=lambda p: len(p[0]), reverse=True)
    sources = [src for src, _ in uniq if src]
    if not sources:
        return text
    pattern = re.compile("|".join(re.escape(src) for src in sources))
    dst = dict(uniq)
    return pattern.sub(lambda m: dst[m.group(0)], text)



@dataclass
class GlossaryEntry:
    ko: str
    zh: str
    kind: str = "term"
    note: str = ""
    confirmed: bool = False
    alternatives: str = ""
    replace_short: bool = False
    # 曾经确认过的译名历史：词表调整译名后，用于把旧译文中的旧译名替换为新译名。
    zh_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GlossaryEntry":
        history = data.get("zh_history") or []
        if not isinstance(history, list):
            history = []
        return cls(
            ko=str(data.get("ko", "")).strip(),
            zh=str(data.get("zh", "")).strip(),
            kind=str(data.get("kind", "term")).strip() or "term",
            note=str(data.get("note", "")).strip(),
            confirmed=bool(data.get("confirmed", False)),
            alternatives=str(data.get("alternatives", "")).strip(),
            replace_short=bool(data.get("replace_short", False)),
            zh_history=[str(x).strip() for x in history if str(x).strip()],
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

    def valid_entries(self, allow_missing_ko: bool = False) -> list[GlossaryEntry]:
        """返回可用的词条。

        专用词表要求 ``ko`` 与 ``zh`` 都存在；通用词表允许 ``ko`` 为空，
        但必须要有 ``zh``，且至少存在 ``ko``/可能译法/历史译名之一，否则没有可匹配的来源。
        """
        out: list[GlossaryEntry] = []
        for e in self.entries:
            if not e.zh:
                continue
            if not e.ko and not allow_missing_ko:
                continue
            if e.ko or e.alternatives or e.zh_history:
                out.append(e)
        return out


    def looks_common(self) -> bool:
        """是否更像通用词表：存在“无韩文原文但有可匹配来源”的词条。"""
        return any((not e.ko) and (e.alternatives or e.zh_history) for e in self.entries)


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
                if old.zh != entry.zh and old.zh and old.zh not in old.zh_history:
                    old.zh_history = list(old.zh_history) + [old.zh]
                old.zh = entry.zh
                old.kind = entry.kind
                old.note = entry.note
                old.confirmed = entry.confirmed
                old.alternatives = entry.alternatives
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

    def replacement_pairs(self, allow_missing_ko: bool = False) -> list[tuple[str, str]]:
        """生成全部保底替换对：(ko→zh) 与已确认词条的 (可能译法/历史译名→zh)。

        供翻译回传矫正与“多卷修正”复用；按源串长度降序，避免短词先替换破坏长词。
        """
        nickname_zhs = {e.zh for e in self.valid_entries() if e.kind == "person-nickname"}
        pairs: list[tuple[str, str]] = []
        for entry in self.valid_entries(allow_missing_ko):
            if entry.ko:
                pairs.append((entry.ko, entry.zh))
            if entry.confirmed:
                for alt in entry.alternative_list():
                    if len(alt) < _MIN_ALTERNATIVE_LEN:
                        continue
                    if alt in entry.zh and not entry.replace_short:
                        # 可能译法是全名字串时视为合法短称/昵称（如“范镇”），
                        # 未开启“短称替换”则不强制替换，保留文中亲昵的称呼。
                        continue
                    pairs.append((alt, entry.zh))
                for old_zh in entry.zh_history:
                    if len(old_zh) < _MIN_ALTERNATIVE_LEN or old_zh == entry.zh:
                        continue
                    if old_zh in nickname_zhs:
                        # 该旧译名现在被独立维护为昵称词条，交给昵称词条处理。
                        continue
                    pairs.append((old_zh, entry.zh))
        return sorted(set(pairs), key=lambda pair: len(pair[0]), reverse=True)


    def common_replacement_pairs(self) -> list[tuple[str, str]]:
        """通用词表专用：允许 ``ko`` 为空的替换对（如 “番外”→“外传”）。"""
        return self.replacement_pairs(allow_missing_ko=True)


    def apply_common_override(self, common: "Glossary") -> int:
        """用通用词表确认译名覆盖专用词表：相同 ``ko`` 时采用通用词表的 ``zh``。

        返回被覆盖的词条数；修改在 ``self.entries`` 原地生效。
        """
        common_by_ko: dict[str, GlossaryEntry] = {}
        for e in common.valid_entries(allow_missing_ko=True):
            if e.ko:
                common_by_ko[e.ko] = e
        changed = 0
        for e in self.entries:
            ce = common_by_ko.get(e.ko)
            if ce and ce.zh and ce.zh != e.zh:
                if e.zh and e.zh not in e.zh_history:
                    e.zh_history = list(e.zh_history) + [e.zh]
                e.zh = ce.zh
                changed += 1
        return changed
    def apply_replacements(self, text: str) -> str:
        """翻译后再做一层保底替换：韩文专名替换为人工译名；
        已确认词条的可能误译（alternatives）与历史旧译名（zh_history）也会替换为人工译名。"""
        return apply_replacement_pairs(text, self.replacement_pairs())
