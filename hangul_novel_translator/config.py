# hangul_novel_translator/config.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from hangul_novel_translator.sanitizer import SanitizerConfig


@dataclass
class AppConfig:
    base_url: str = "http://localhost:8045/v1"
    api_key: str = "sk-8d9f9de70d2b42a3b350ab332d8619f0"
    model: str = "gemini-3.7-flash-tiered"

    source_lang: str = "韩语"
    target_lang: str = "简体中文"

    # 每个翻译请求中最多送入多少原文汉字/韩文字符。
    chunk_chars: int = 1800
    # 单段拆句阈值：超过该长度的段落会先按句号/问号/感叹号再拆。
    max_paragraph_chars: int = 2600
    max_retries: int = 4
    retry_delay: float = 3.0
    timeout: float = 600.0
    temperature: float = 0.2
    max_workers: int = 1

    # 词表提取。
    extract_glossary: bool = True
    extract_sample_chars: int = 30000
    extract_sample_chapters: int = 6
    # 全书跨度采样走向：前/中/后 3 个区域，每个区域抽 2 章；预算随书长放大。
    extract_sample_regions: int = 3
    extract_sample_per_region: int = 2
    extract_sample_chars_per_100k: int = 20000
    extract_sample_chars_cap: int = 60000
    glossary_limit: int = 120

    # TXT 章节标题正则：每个元素是一条原始正则字符串，用于解析/拆分 TXT 章节。
    # 留空表示使用内置默认正则；多本 TXT 共用同一组正则并按文件顺序切分。
    txt_patterns: list[str] = field(default_factory=list)
    # 是否剔除正文为 0 字的空章节（预览里「忽视 0 字章节」确认后生效）。
    ignore_zero_chapters: bool = False

    # 断点续传。
    resume: bool = True

    # 输出。
    output_txt: bool = True
    output_epub: bool = True
    output_encoding: str = "utf-8-sig"

    # 导出文本清洗过滤。
    sanitizer_config: SanitizerConfig = field(default_factory=SanitizerConfig)

    # 进度文件会默认放在输出目录下。
    state_filename: str = ".translation_state.json"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {}
        for k, v in data.items():
            if k == "sanitizer_config" and isinstance(v, dict):
                filtered[k] = SanitizerConfig.from_dict(v)
            elif k in known:
                filtered[k] = v
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sanitizer_config"] = self.sanitizer_config.to_dict()
        return d
