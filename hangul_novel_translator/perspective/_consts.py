# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
"""中文小说第一人称改第三人称。

视角转换是独立于韩语翻译和多卷合并的流程。输入 EPUB 作为 ZIP 读取，只有正文
XHTML/HTML 中选定的可见文本节点会被改写；CSS、图片、字体、OPF、目录以及其它
包内资源按原条目复制。每个块的原文和分段译文都写入独立状态存档，便于恢复和手动
补写失败块。
"""



PERSPECTIVE_PROMPT_VERSION = "perspective-v1"

_HTML_SUFFIXES = (".xhtml", ".html", ".htm")

_BLOCK_TAGS = {
    "p", "li", "blockquote", "pre", "td", "th", "dt", "dd", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6",
}

_SKIP_FILE_MARKERS = ("nav", "toc", "contents", "titlepage", "cover", "copyright")

_QUOTE_PAIR_RE = re.compile(r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|\"[^\"]+\"|'[^']+'")

_INNER_RE = re.compile(r"(?:inner|monologue|thought|soliloquy|心理|内心|独白|心声)", re.IGNORECASE)

_LETTER_RE = re.compile(r"(?:letter|diary|journal|chat|message|mail|书信|日记|聊天|消息|邮件|引用|quote)", re.IGNORECASE)

_MARKUP_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")



_RAW_TOKEN_RE = re.compile(r"<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<[^>]*>|[^<]+")

_RAW_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9:_-]*)[^>]*?>\s*$", re.S)

_RAW_SKIP_TAGS = {"head", "script", "style", "noscript", "title"}
