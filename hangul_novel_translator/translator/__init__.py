# 由 tools/split_package.py 拆分生成
"""自动回导出包：保持对外接口与旧模块一致。"""
from __future__ import annotations

from ..llm import LLMClient

from .models import Chunk
from .models import FailedChunk
from .models import MalformedBlock
from .models import TranslationCancelled
from .models import TranslationResult
from .response import _align_cleaned
from .response import _classify_malformed
from .response import _extract_json_strings
from .response import _has_json_residue
from .response import _has_quote_run
from .response import _looks_like_json_fragment
from .response import _recover_from_blob
from .response import _recover_from_fragments
from .response import _repair_clean_paragraph
from .response import _split_json_array_text
from .response import normalize_completed_paragraphs
from .response import reconcile_paragraphs
from .response import sanitize_committed_paragraphs
from .malformed import detect_malformed_blocks
from .malformed import repair_malformed_blocks
from .chunking import _chunk_from_failed_entry
from .chunking import _chunk_signature
from .chunking import _failed_entry
from .chunking import _retry_chunk_config
from .chunking import build_chunks
from .chunking import collect_sample_text
from .state import _sanitize_state_name
from .state import load_failed_chunks
from .state import save_manual_translation
from .pipeline import Translator
from ._consts import ProgressCallback
from ._consts import TRANSLATION_SYSTEM_TEMPLATE
from ._consts import _JSON_STR_RE
from ._consts import _QUOTE_SANITIZER
from ._consts import _STRUCT_JSON_KEYS

# 历史兼容：旧 translator 模块顶层把 sampling 的这些名字一并透传给调用方。
from ..sampling import collect_sample_text_strided, format_sample_chapters, sample_chapter_report

__all__ = [
    'Chunk',
    'FailedChunk',
    'MalformedBlock',
    'ProgressCallback',
    'TRANSLATION_SYSTEM_TEMPLATE',
    'TranslationCancelled',
    'TranslationResult',
    'Translator',
    'build_chunks',
    'collect_sample_text',
    'detect_malformed_blocks',
    'load_failed_chunks',
    'normalize_completed_paragraphs',
    'reconcile_paragraphs',
    'repair_malformed_blocks',
    'sanitize_committed_paragraphs',
    'save_manual_translation',
]
