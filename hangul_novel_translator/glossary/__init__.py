# 由 tools/split_package.py 拆分生成
"""自动回导出包：保持对外接口与旧模块一致。"""
from __future__ import annotations

from .models import Glossary
from .models import GlossaryEntry
from .models import _clean_alternatives
from .models import _merge_alternatives
from .models import apply_replacement_pairs
from .models import merge_replacement_pairs
from .extract import _strip_full_name_occurrences
from .extract import enrich_glossary
from .extract import enrich_glossary_with_nicknames
from .extract import extract_glossary_with_llm
from .extract import extract_more_glossary
from .extract import find_nickname_entries
from .payload import _entry_from_item
from .payload import payload_to_glossary
from ._consts import ENRICH_SYSTEM
from ._consts import EXTRACTION_MORE_SYSTEM
from ._consts import EXTRACTION_SYSTEM
from ._consts import NICKNAME_JUDGE_SYSTEM
from ._consts import _MIN_ALTERNATIVE_LEN
from ._consts import _NICKNAME_PARTICLE_PATTERN

__all__ = [
    'ENRICH_SYSTEM',
    'EXTRACTION_MORE_SYSTEM',
    'EXTRACTION_SYSTEM',
    'Glossary',
    'GlossaryEntry',
    'NICKNAME_JUDGE_SYSTEM',
    'enrich_glossary',
    'enrich_glossary_with_nicknames',
    'extract_glossary_with_llm',
    'extract_more_glossary',
    'find_nickname_entries',
    'apply_replacement_pairs',
    'merge_replacement_pairs',
    'payload_to_glossary',
]
