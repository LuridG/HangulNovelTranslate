# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from hangul_novel_translator.sanitizer import (ExportSanitizer, SanitizerConfig)
import re
from typing import (Any, Callable)


ProgressCallback = Callable[[str, int, int, str], None]



# ---------------- 畸形块检测 / 归一化 / 修复 ----------------
# 翻译时偶发把模型原始 JSON 响应当成整段写入 completed，导致导出出现
# “四个引号”““““/””””、JSON 数组残留 "text",、整段塌缩进首段等情况。
# 下面是一套“尽量自动救回、救不回则回退原文”的兜底逻辑，供：
#   - 导出侧（book_from_state / _assemble）渲染时归一化；
#   - 多卷修正页的“畸形块检测”做机器批量修复。

_QUOTE_SANITIZER = ExportSanitizer(
    SanitizerConfig(
        enabled=True,
        # 只做“引号连叠折叠 + JSON 残留清理”，不剥编号、不美化标点，
        # 以免在导出侧覆盖用户“自定义清洗规则”里关掉的相关开关。
        strip_numbers=False,
        strip_json_residue=True,
        fix_quotes=True,
        polish_punctuation=False,
        collapse_quotes=True,
    )
)


_STRUCT_JSON_KEYS = {"paragraphs", "translation", "text", "content"}

_JSON_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')



TRANSLATION_SYSTEM_TEMPLATE = """你是一名资深的韩语小说中文译者。请把用户提供的韩语小说内容翻译成{target_lang}。
要求：
1. 严格忠实原文，不增删情节，不改动事实。
2. 保留原句语气、对话感、修辞和段落顺序。
3. 以下专有名词词表是全书统一译名，必须严格遵守；如果词表与你的常识冲突，以词表为准。
4. 原文中的行内格式标记（⟦b⟧⟦i⟧⟦u⟧⟦s:样式⟧⟦img:路径⟧⟦fn:编号⟧⟦br⟧）必须原样保留在对应译文位置，不要删除、改写或添加；标记不是正文内容。

【专有名词词表】
{glossary}

【输出格式】
只输出一个 JSON 对象，不要输出解释、前言或 Markdown 代码块。
JSON 格式：{{"paragraphs": ["译文段落1", "译文段落2", ...]}}
译文数组的段落数量必须与用户给出的原文段落数量完全一致，顺序也必须一致。"""
