# hangul_novel_translator/sanitizer.py
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class CustomRule:
    pattern: str
    replace: str = ""
    is_regex: bool = False
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CustomRule":
        return cls(
            pattern=data.get("pattern", ""),
            replace=data.get("replace", ""),
            is_regex=bool(data.get("is_regex", False)),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class SanitizerConfig:
    enabled: bool = True
    strip_numbers: bool = True
    strip_json_residue: bool = True
    fix_quotes: bool = True
    # 标点排版美化：韩式省略号 ... -> ……、重复感叹/问号收敛为单全角。
    polish_punctuation: bool = True
    # 折叠连叠引号与清理 JSON 数组引号残留：防“四个引号”““““/””””及 "text", 这类污染。
    collapse_quotes: bool = True
    custom_rules: list[CustomRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "strip_numbers": self.strip_numbers,
            "strip_json_residue": self.strip_json_residue,
            "fix_quotes": self.fix_quotes,
            "polish_punctuation": self.polish_punctuation,
            "collapse_quotes": self.collapse_quotes,
            "custom_rules": [r.to_dict() for r in self.custom_rules],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SanitizerConfig":
        rules = [
            CustomRule.from_dict(r) for r in data.get("custom_rules", []) if isinstance(r, dict)
        ]
        return cls(
            enabled=bool(data.get("enabled", True)),
            strip_numbers=bool(data.get("strip_numbers", True)),
            strip_json_residue=bool(data.get("strip_json_residue", True)),
            fix_quotes=bool(data.get("fix_quotes", True)),
            polish_punctuation=bool(data.get("polish_punctuation", True)),
            collapse_quotes=bool(data.get("collapse_quotes", True)),
            custom_rules=rules,
        )


def _collapse_quote_runs(text: str) -> str:
    """把同方向连续 ≥2 个的中文/半角引号折叠为 1 个。

    模型常把引号内套引号错写成同向连叠（““““……明白了。””””），
    中文规范里嵌套引用应交替使用 “”/‘’，同向连叠本身就是异常，折叠是安全的。
    """
    text = re.sub("\u201c{2,}", "\u201c", text)
    text = re.sub("\u201d{2,}", "\u201d", text)
    text = re.sub(r'"{2,}', '"', text)
    return text


def _strip_json_quote_edges(text: str) -> str:
    """清理 JSON 数组元素残留：段首多余的半角引号、段尾的 ", / ”", 等。

    例如 '"““““……明白了。””””",'（折叠后为 '"“……明白了。”",'）
    应变成 '“……明白了。”'。
    """
    # 段首：半角引号后紧跟中文引号或汉字，属 JSON 封装残留，去掉。
    m = re.match(r'^"+(\s*)([\u201c\u201d\u2018\u2019\u4e00-\u9fff])', text)
    if m:
        text = text[m.end() - 1 :]
    # 段尾：“”"，/ "，/ 反引号 + 逗号（JSON 数组元素分隔残留）。
    text = re.sub(r'[\u201d"]\s*,\s*$', "", text)
    # 段尾：中文右引号后再跟一个半角引号（封装残留）→ 保留中文右引号。
    text = re.sub(r'\u201d["\u201d]*$', "\u201d", text)
    return text


class ExportSanitizer:
    def __init__(self, config: SanitizerConfig | None = None):
        self.config = config or SanitizerConfig()

    def clean_paragraph(self, text: str) -> str:
        if not text or not self.config.enabled:
            return text

        # 1. Strip prompt/JSON key residue e.g. {"paragraphs": [ or "p1":
        if self.config.strip_json_residue:
            text = re.sub(
                r'^\s*\{?\"(?:paragraphs|translation|text|content|p\d*)\"\s*:\s*\[?\s*',
                "",
                text,
            )

        # 2. Strip numbered prefixes like [1], (1), 1., etc. BEFORE general bracket strip
        if self.config.strip_numbers:
            text = re.sub(r"^\s*\[\d+\]\s*", "", text)
            text = re.sub(r"^\s*\(\d+\)\s*", "", text)
            text = re.sub(r"^\s*\d+\.\s+", "", text)

        # 3. Clean stray JSON array/object brackets if any
        if self.config.strip_json_residue:
            if text.startswith("{") and not text.endswith("}"):
                text = text.lstrip("{\n\r\t ")
            if text.endswith("}") and not text.startswith("{"):
                text = text.rstrip("}\n\r\t ")
            if text.endswith("]") and not text.startswith("["):
                text = text.rstrip("]\n\r\t ")
            if text.endswith(",") and not text.startswith(","):
                text = text.rstrip(",\n\r\t ")

        # 4. Strip outer double quotes left from JSON string packaging
        if self.config.fix_quotes:
            # Case A: unclosed leading quote (e.g. '"男人平静地解释着。')
            if text.startswith('"') and not text.endswith('"'):
                if text.count('"') % 2 != 0:
                    text = text[1:].lstrip()

            # Case B: outer double quotes wrapping dialogue with Chinese quotes
            # e.g. '"“新身份证的照片不是那张吧？”"' -> '“新身份证的照片不是那张吧？”'
            if text.startswith('"“') and text.endswith('”"'):
                text = text[1:-1]
            elif text.startswith('"') and text.endswith('"') and text.count('"') == 2:
                # E.g. '"男人平静地解释着。"' -> '男人平静地解释着。'
                # Do not strip if it is pure ascii dialogue quote without inner quotes
                if not (
                    text.startswith('"')
                    and text.endswith('"')
                    and any(c in text[1:-1] for c in ["“", "”"])
                ):
                    text = text[1:-1]

        # 5. 折叠连叠引号 + 清理 JSON 数组元素引号残留（防“四个引号” ““““/””””、"text", 污染）。
        if self.config.collapse_quotes:
            text = _collapse_quote_runs(text)
            text = _strip_json_quote_edges(text)

        # 6. 标点排版美化：韩式省略号 -> 中文省略号；重复感叹/问号收敛为单全角。
        if self.config.polish_punctuation:
            text = re.sub(r"\.{3,}", "……", text)
            text = re.sub(r"[!！]{2,}", "！", text)
            text = re.sub(r"[?？]{2,}", "？", text)

        # 7. Apply custom rules
        for rule in self.config.custom_rules:
            if not rule.enabled or not rule.pattern:
                continue
            try:
                if rule.is_regex:
                    text = re.sub(rule.pattern, rule.replace, text)
                else:
                    text = text.replace(rule.pattern, rule.replace)
            except Exception:
                # Ignore invalid regex in user-defined rules safely
                pass

        return text.strip()

    def clean_paragraphs(self, paragraphs: list[str]) -> list[str]:
        if not self.config.enabled:
            return paragraphs
        cleaned = [self.clean_paragraph(p) for p in paragraphs]
        return [p for p in cleaned if p]
