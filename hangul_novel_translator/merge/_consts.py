# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
# hangul_novel_translator/merge.py
"""多卷修正与合并：读取翻译存档 JSON，按当前词表修正并拼合输出 TXT/EPUB。"""



_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)

_CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.IGNORECASE)

_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\ud7b0-\ud7ff]+")



_STATE_FILE_SUFFIX = re.compile(r"\.translation_state(?:\.json)?$|\.json$", re.IGNORECASE)

_VOLUME_MARK_RE = re.compile(
    r"(?:[\s_\-·.:：、]*"
    r"(?:"
    r"第\s*[0-9一二三四五六七八九十百千〇零]+[卷권册部集篇]"
    r"|[0-9一二三四五六七八九十百千〇零]+[卷권册部集篇]"
    r"|(?:上|中|下)(?:卷|권|册|部|集|篇)?"
    r"|前传|后传|外传|番外|序章|终章"
    r")"
    r")$"
)



_IMAGE_MARKER_RE = re.compile("\u27e6img:([^\u27e7]*)\u27e7")
