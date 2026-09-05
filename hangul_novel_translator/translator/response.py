# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
from pathlib import Path
from typing import (Any, Callable)
from ..utils import (extract_json, parse_paragraphs_from_payload, split_paragraph_smart)
from .models import MalformedBlock
from ._consts import _JSON_STR_RE
from ._consts import _QUOTE_SANITIZER
from ._consts import _STRUCT_JSON_KEYS


def reconcile_paragraphs(translated: list[str], expected_count: int) -> list[str]:
    """尽量让译文段落数与原文一致。"""
    translated = [str(x).strip() for x in translated if str(x).strip()]
    if len(translated) == expected_count:
        return translated

    if len(translated) == 1 and expected_count > 1:
        split = [x.strip() for x in re.split(r"\n+", translated[0]) if x.strip()]
        if len(split) > 1:
            translated = split

    if len(translated) > expected_count:
        return translated[:expected_count]

    # 数量不足：补空段落。宁可保留原文格式，也不伪造内容。
    translated.extend([""] * (expected_count - len(translated)))
    return translated



def _repair_clean_paragraph(text: str) -> str:
    return _QUOTE_SANITIZER.clean_paragraph(str(text))



def sanitize_committed_paragraphs(paras: list[str]) -> list[str]:
    """写回 completed 前的轻量清洗：折叠连叠引号、剥 JSON 残留、能救回的 blob 直接救回。

    不主动回退到原文（写入端不注入韩文）；救不回的原样保留，留给“畸形块检测”识别。
    """
    if not isinstance(paras, list):
        return list(paras)
    raw = [str(p) for p in paras]
    nonempty = [p for p in raw if p.strip()]
    if len(nonempty) == 1:
        recovered = _recover_from_blob(raw)
        if recovered:
            return recovered
    return [_repair_clean_paragraph(p) for p in raw]



def _extract_json_strings(text: str) -> list[str]:
    """从一段文本里尽力抽出 JSON 字符串数组元素（容忍 JSON 语法破损）。"""
    out: list[str] = []
    for token in _JSON_STR_RE.findall(text):
        token = token.strip()
        if not token:
            continue
        if token in _STRUCT_JSON_KEYS or re.fullmatch(r"[pP]\d+", token):
            continue
        if len(token) < 2:
            continue
        out.append(token)
    return out



def _looks_like_json_fragment(raw: list[str]) -> bool:
    """整条 completed 值是“原样 JSON 按行拆开”的碎片（首行 {"paragraphs": [，后续 "text",）。"""
    if not raw:
        return False
    if raw[0].strip().startswith('{"paragraphs"'):
        # 首元素是结构头；只有当后面还有非空碎片时才视为“逐行拆开”，
        # 否则（首元素是大块 JSON、其余为空串）属于“整段塌缩”。
        return any(x.strip() for x in raw[1:])
    frag_tail = [x for x in raw[1:] if x.strip().endswith('",')]
    return len(frag_tail) >= 2



def _recover_from_fragments(raw: list[str]) -> list[str] | None:
    """把“原样 JSON 按行拆开”的列表还原成干净段落。"""
    paras: list[str] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        if line.startswith('{"paragraphs"') or line.startswith("{"):
            continue
        if line == "]" or line == "]}" or line == "}":
            continue
        # 去掉数组元素封装：段首 "、段尾 " 或 ",。
        line = re.sub(r'^\s*"+', "", line)
        line = re.sub(r'"\s*,?\s*$', "", line)
        if line.endswith("]"):
            line = line[:-1].rstrip()
        line = _repair_clean_paragraph(line)
        if line:
            paras.append(line)
    return paras or None



def _recover_from_blob(raw: list[str]) -> list[str] | None:
    """把“整段塌缩进首元素的 JSON”拆回段落。"""
    text = raw[0] if raw else ""
    paras: list[str] = []
    try:
        payload = extract_json(text)
        paras = parse_paragraphs_from_payload(payload)
    except Exception:  # noqa: BLE001
        paras = []
    if len(paras) < 2:
        # 语法破损时优先按 JSON 数组分隔符切分（对内嵌 ASCII 引号更稳）。
        paras = _split_json_array_text(text)
    if len(paras) < 2:
        # 最后退化为正则抽字符串。
        paras = _extract_json_strings(text)
    cleaned = [_repair_clean_paragraph(p) for p in paras]
    cleaned = [p for p in cleaned if p]
    return cleaned if len(cleaned) >= 2 else None



def _split_json_array_text(text: str) -> list[str]:
    """按 JSON 数组元素分隔符 `", "` 切分，容忍内部 ASCII 引号导致的 JSON 破损。"""
    body = text
    m = re.search(r'"\s*:\s*\[\s*', body)
    if m:
        body = body[m.end() :]
    # 剥掉模型回显的尾部格式说明（如 `format: {"paragraphs": [...]}`）。
    body = re.sub(r"\s*format\s*:\s*\{.*\}\s*$", "", body, flags=re.DOTALL)
    # 剥掉可能的数组/对象收尾括号。
    body = re.sub(r'\s*\]\s*\}\s*$', "", body)
    body = body.rstrip("]}")
    parts = re.split(r'",\s*"', body)
    out: list[str] = []
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if part.startswith('"'):
            part = part[1:]
        if part.endswith('"'):
            part = part[:-1]
        # 残留的列表/对象括号与格式说明。
        part = re.sub(r'[\s}]+$', "", part)
        part = re.sub(r'\s*]\s*(?:format\s*:\s*\{.*\})?\s*$', "", part)
        out.append(part)
    return out



def _has_quote_run(text: str) -> bool:
    return bool(re.search(r"[\u201c]{2,}|[\u201d]{2,}", text))



def _has_json_residue(text: str) -> bool:
    return text.startswith('"') or bool(re.search(r'[\u201d"]\s*,\s*$', text))



def _align_cleaned(
    items: list[str],
    expected_count: int | None,
    source: list[str] | None,
    fill_from_source: bool,
) -> list[str]:
    """按原文段数对齐：空位回退源码或留空，避免导出空段/乱码。"""
    if expected_count is None:
        return [p for p in items if p]
    out: list[str] = []
    for i in range(expected_count):
        if i < len(items) and items[i].strip():
            out.append(items[i].strip())
        elif fill_from_source and source and i < len(source) and str(source[i]).strip():
            out.append(str(source[i]).strip())
        else:
            out.append("")
    return out



def normalize_completed_paragraphs(
    values: Any,
    expected_count: int | None = None,
    source_paragraphs: list[str] | None = None,
    *,
    fill_from_source: bool = False,
) -> tuple[list[str], str]:
    """归一化 completed[chunk_id]，返回 (段落, 状态)。

    状态：ok（本来就干净）、recovered（从 JSON 拆回）、cleaned（去残留/折叠引号）、
          fallback_source（无法修复，回退原文）、unresolved（无可救）。
    """
    if not isinstance(values, list):
        if fill_from_source and source_paragraphs:
            return list(source_paragraphs), "fallback_source"
        return [], "unresolved"

    raw = [str(v) for v in values]

    # 情形 A：整条是“原样 JSON 按行拆开”的碎片。
    if _looks_like_json_fragment(raw):
        recovered = _recover_from_fragments(raw)
        if recovered:
            paras = _align_cleaned(recovered, expected_count, source_paragraphs, fill_from_source)
            return paras, "recovered"
        paras = _align_cleaned(raw, expected_count, source_paragraphs, fill_from_source)
        return paras, "fallback_source" if fill_from_source else "unresolved"

    # 情形 B：整段塌缩进首元素（首元素含 JSON 对象，其余多为空）。
    nonempty = [p for p in raw if p.strip()]
    if len(nonempty) == 1:
        recovered = _recover_from_blob(raw)
        if recovered:
            paras = _align_cleaned(recovered, expected_count, source_paragraphs, fill_from_source)
            return paras, "recovered"

    # 情形 C：常规列表，逐段清洗。
    cleaned = [_repair_clean_paragraph(p) for p in raw]
    residual = any(_has_quote_run(p) or _has_json_residue(p) for p in cleaned if p)
    paras = _align_cleaned(cleaned, expected_count, source_paragraphs, fill_from_source)
    if not any(p.strip() for p in paras):
        if fill_from_source and source_paragraphs:
            return list(source_paragraphs), "fallback_source"
        return paras, "unresolved"
    return paras, ("cleaned" if residual else "ok")



def _classify_malformed(
    chunk_id: str,
    values: Any,
    source_paragraphs: list[str],
    parsed: list[str],
    status: str,
    expected_count: int | None,
) -> MalformedBlock:
    """根据归一化结果生成畸形块对象与原因描述。"""
    raw = [str(v) for v in values] if isinstance(values, list) else []
    issue = "其他"
    if _looks_like_json_fragment(raw):
        issue = "JSON 逐行残留"
    elif len([p for p in raw if p.strip()]) == 1:
        issue = "JSON 整段塌缩"
    elif any(_has_quote_run(p) for p in parsed if p):
        issue = "连叠引号"
    elif any(not p.strip() for p in parsed):
        issue = "含空段/缺译文"
    elif status == "fallback_source":
        issue = "无法自动修复"

    filled = [p for p in parsed if p.strip()]
    # 救回 ≥2 段即视为机器可修复（缺段位置写回时用原文补齐，避免空段）；
    # 完全救不回（如只有 {"paragraphs": 前缀）才留给手动/回翻。
    meaningful = len(filled) >= 2
    repairable = meaningful and status in ("recovered", "cleaned")
    repaired: list[str] = []
    if repairable and expected_count:
        repaired, _rep_status = normalize_completed_paragraphs(
            values, expected_count, source_paragraphs, fill_from_source=True
        )
    return MalformedBlock(
        archive=Path(""),
        chunk_id=chunk_id,
        chapter_index=0,
        chapter_title="",
        issue=issue,
        source_paragraphs=list(source_paragraphs),
        current_values=raw,
        repaired=repaired,
        repairable=repairable,
    )
