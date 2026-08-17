# hangul_novel_translator/config.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


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
    glossary_limit: int = 120

    # 断点续传。
    resume: bool = True

    # 输出。
    output_txt: bool = True
    output_epub: bool = True
    output_encoding: str = "utf-8-sig"

    # 进度文件会默认放在输出目录下。
    state_filename: str = ".translation_state.json"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
