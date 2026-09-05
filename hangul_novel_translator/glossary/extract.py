# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
from ..llm import LLMClient
from ..utils import (extract_json, parse_paragraphs_from_payload)
from ._consts import ENRICH_SYSTEM
from ._consts import EXTRACTION_MORE_SYSTEM
from ._consts import EXTRACTION_SYSTEM
from .models import Glossary
from .models import GlossaryEntry
from ._consts import NICKNAME_JUDGE_SYSTEM
from ._consts import _NICKNAME_PARTICLE_PATTERN
from .models import _merge_alternatives
from .payload import payload_to_glossary


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



def enrich_glossary(llm: LLMClient, glossary: Glossary) -> dict[str, int]:
    """把现有词表送回 LLM 完善可能译法等信息（不传原文），返回更新统计。"""
    entries = glossary.valid_entries()
    if not entries:
        raise ValueError("当前没有可完善的词表，请先提取或加载词表")

    lines: list[str] = []
    for entry in entries:
        alts = ",".join(entry.alternative_list())
        lines.append(
            f"- ko={entry.ko} | zh={entry.zh} | kind={entry.kind} | note={entry.note or ''} | alts={alts}"
        )
    user = (
        "请为下面的每个词条补充可能译法等信息：\n\n"
        + "\n".join(lines)
        + "\n\n只输出一个 JSON 对象，每个词条的 ko 必须与输入完全一致。"
    )
    messages = [
        {"role": "system", "content": ENRICH_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw_response = llm.chat(messages, temperature=0.1, json_mode=True)
    try:
        payload = extract_json(raw_response)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"模型没有返回合法 JSON：{raw_response[:300]}") from exc

    suggested = payload_to_glossary(payload)
    by_ko = {e.ko: e for e in glossary.entries}
    updated_alts = 0
    updated_notes = 0
    for item in suggested.entries:
        target = by_ko.get(item.ko)
        if target is None:
            continue
        if item.alternatives:
            merged = _merge_alternatives(
                target.alternatives,
                item.alternatives,
                ko=target.ko,
                zh=target.zh,
            )
            if merged != target.alternatives:
                target.alternatives = merged
                updated_alts += 1
        if not target.note and item.note:
            target.note = item.note
            updated_notes += 1
    return {"updated_alts": updated_alts, "updated_notes": updated_notes}



def _strip_full_name_occurrences(source: str, full_name: str) -> str:
    """把原文中的“全名+（可选助词）”整体剔除，用于判断昵称是否独立出现。"""
    pattern = re.compile(re.escape(full_name) + f"(?:{_NICKNAME_PARTICLE_PATTERN})?")
    return pattern.sub("", source)



def find_nickname_entries(
    llm: LLMClient,
    glossary: Glossary,
    source_text: str,
) -> dict[str, int]:
    """让 LLM 分析 person 词条的可能昵称，并在原文中搜索韩文昵称；
    确认独立出现后新增 kind=person-nickname 词条。返回 {"nickname_added", "nickname_skipped", "nickname_not_found"}。"""
    persons = [e for e in glossary.valid_entries() if e.kind == "person"]
    source = (source_text or "").strip()
    if not persons or not source:
        return {"nickname_added": 0, "nickname_skipped": 0, "nickname_not_found": 0}

    lines = [f"- ko={e.ko} | zh={e.zh} | note={e.note or ''}" for e in persons]
    user = (
        "请分析下列人物词条可能出现的昵称/短称：\n\n"
        + "\n".join(lines)
        + "\n\n只输出一个 JSON 对象，每个词条的 ko 必须与输入完全一致。"
    )
    messages = [
        {"role": "system", "content": NICKNAME_JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw_response = llm.chat(messages, temperature=0.1, json_mode=True)
    try:
        payload = extract_json(raw_response)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"模型没有返回合法 JSON：{raw_response[:300]}") from exc

    raw_entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        raise ValueError("模型返回结构不正确：缺少 entries 数组")

    existing_kos = {e.ko for e in glossary.entries}
    by_ko = {e.ko: e for e in persons}
    added = 0
    skipped = 0
    not_found = 0
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        person = by_ko.get(str(item.get("ko", "")).strip())
        if person is None:
            continue
        nick_list = item.get("nicknames") or []
        if not isinstance(nick_list, list):
            continue
        for nick in nick_list:
            if not isinstance(nick, dict):
                skipped += 1
                continue
            ko = str(nick.get("ko", "")).strip()
            zh = str(nick.get("zh", "")).strip()
            if not ko or not zh or len(ko) < 2 or ko == person.ko or zh == person.zh:
                skipped += 1
                continue
            if ko in existing_kos:
                skipped += 1
                continue
            search_source = _strip_full_name_occurrences(source, person.ko)
            if ko not in search_source:
                not_found += 1
                continue
            glossary.entries.append(
                GlossaryEntry(
                    ko=ko,
                    zh=zh,
                    kind="person-nickname",
                    note=f"{person.zh}的昵称",
                    confirmed=False,
                )
            )
            existing_kos.add(ko)
            added += 1
    return {
        "nickname_added": added,
        "nickname_skipped": skipped,
        "nickname_not_found": not_found,
    }



def enrich_glossary_with_nicknames(
    llm: LLMClient,
    glossary: Glossary,
    source_text: str,
    *,
    do_alts: bool = True,
    do_nick: bool = True,
) -> dict[str, int]:
    """完善信息总流程：先检测并新增昵称词条，再为全部词条（含新昵称）补充可能译法与备注。

    顺序很关键：昵称先入库、alts 后处理，本次新增的 person-nickname 也能在同一次
    运行里拿到可能译法，且不增加 LLM 调用次数。
    """
    stats: dict[str, int] = {}
    if do_nick:
        stats.update(find_nickname_entries(llm, glossary, source_text))
    if do_alts:
        stats.update(enrich_glossary(llm, glossary))
    return stats
