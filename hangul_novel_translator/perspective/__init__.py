# 由 tools/split_package.py 拆分生成
"""自动回导出包：保持对外接口与旧模块一致。"""
from __future__ import annotations

from .models import PerspectiveBlock
from .models import PerspectiveCancelled
from .models import PerspectiveError
from .models import PerspectiveFailedBlock
from .models import PerspectiveOptions
from .analysis import _apply_segments
from .analysis import _block_from_node
from .analysis import _contains_block_ancestor
from .analysis import _decode_xhtml
from .analysis import _document_has_nav
from .analysis import _eligible
from .analysis import _has_dialogue
from .analysis import _has_marker
from .analysis import _is_html
from .analysis import _is_inner_monologue
from .analysis import _is_letter_or_quote
from .analysis import _iter_content_blocks
from .analysis import _marker_value
from .analysis import _node_text
from .analysis import _raw_text_positions
from .analysis import _replace_raw_blocks
from .analysis import _skip_document
from .analysis import _style_instruction
from .analysis import _text_nodes
from .analysis import build_perspective_prompt
from .rewrite import _batch_blocks
from .rewrite import _parse_rewrite_payload
from .rewrite import _preserve_edge_whitespace
from .rewrite import rewrite_blocks
from .state import _block_state
from .state import _safe_state_name
from .state import _source_fingerprint
from .state import _state_options
from .state import load_failed_perspective_blocks
from .state import load_perspective_state
from .state import perspective_state_path
from .state import save_manual_perspective_translation
from .state import save_perspective_failure
from .converter import PerspectiveConverter
from .converter import inspect_perspective_epub
from .converter import render_perspective_state
from .converter import reset_perspective_state
from ._consts import PERSPECTIVE_PROMPT_VERSION
from ._consts import _BLOCK_TAGS
from ._consts import _HTML_SUFFIXES
from ._consts import _INNER_RE
from ._consts import _LETTER_RE
from ._consts import _MARKUP_RE
from ._consts import _QUOTE_PAIR_RE
from ._consts import _RAW_SKIP_TAGS
from ._consts import _RAW_TAG_RE
from ._consts import _RAW_TOKEN_RE
from ._consts import _SKIP_FILE_MARKERS

__all__ = [
    'PERSPECTIVE_PROMPT_VERSION',
    'PerspectiveBlock',
    'PerspectiveCancelled',
    'PerspectiveConverter',
    'PerspectiveError',
    'PerspectiveFailedBlock',
    'PerspectiveOptions',
    'build_perspective_prompt',
    'inspect_perspective_epub',
    'load_failed_perspective_blocks',
    'load_perspective_state',
    'perspective_state_path',
    'render_perspective_state',
    'reset_perspective_state',
    'rewrite_blocks',
    'save_manual_perspective_translation',
    'save_perspective_failure',
]
