# 由 tools/split_package.py 拆分生成
"""自动回导出包：保持对外接口与旧模块一致。"""
from __future__ import annotations

from .models import BlockStyle
from .models import Book
from .models import Chapter
from .models import ParagraphStyle
from .markup import _attrs_to_html
from .markup import _close_marker_tag
from .markup import _decode_inline_marker
from .markup import _document_attrs
from .markup import _encode_inline_marker
from .markup import _inline_markers_to_html
from .markup import _inline_style_css
from .markup import _open_marker_tag
from .markup import _outer_container_snapshot
from .markup import _safe_tag_attrs
from .markup import strip_inline_markers
from .metadata import _as_text
from .metadata import _contains_cjk_text
from .metadata import _looks_like_cover_image
from .metadata import _mime_for_name
from .metadata import _needs_cjk_font_fallback
from .metadata import metadata_from_dict
from .metadata import metadata_to_dict
from .txt import _chunk_paragraphs_by_chars
from .txt import _looks_like_heading
from .txt import _read_text_auto
from .txt import book_to_txt
from .txt import DEFAULT_TXT_PATTERNS
from .txt import drop_zero_chapters
from .txt import parse_txt
from .txt import parse_txt_with_patterns
from .txt import preview_txt_chapters
from .text import _decode_bytes
from .text import _strip_invisible_chars
from .epub import _ancestor_styles
from .epub import _ancestors_close
from .epub import _ancestors_key
from .epub import _ancestors_open
from .epub import _block_text_markers
from .epub import _block_to_html
from .epub import _build_toc_tree
from .epub import _chapter_volume
from .epub import _collect_block_style
from .epub import _collect_css_resources
from .epub import _collect_image_items
from .epub import _collect_toc_hierarchy
from .epub import _css_urls
from .epub import _epub_package_path
from .epub import _has_skip_text_marker
from .epub import _href_basename
from .epub import _inline_markup
from .epub import _inline_markup_children
from .epub import _is_skip_marker_line
from .epub import _is_standalone_image_container
from .epub import _is_weak_title
from .epub import _normalize_title
from .epub import _paragraph_style_for
from .epub import _prepare_epub_for_reader
from .epub import _register_image
from .epub import _relative_epub_href
from .epub import _resolve_epub_path
from .epub import _resolve_href
from .epub import _restore_document_attrs
from .epub import _rewrite_inline_image_hrefs
from .epub import _rewrite_internal_doc_links
from .epub import _standalone_image_blocks
from .epub import _style_attrs
from .epub import _toc_href
from .epub import _toc_title
from .epub import export_epub
from .epub import is_decorative_title
from .epub import load_book
from .epub import parse_epub
from ._consts import _CHAPTER_PATTERNS
from ._consts import _DOC_PAGE_SUFFIXES
from ._consts import _HREF_ATTR_RE
from ._consts import _INLINE_ATTRS
from ._consts import _INLINE_STYLE_KEYS
from ._consts import _INLINE_TAGS
from ._consts import _MARKER_RE
from ._consts import _SKIP_PAGE_MARKERS

__all__ = [
    'BlockStyle',
    'Book',
    'Chapter',
    'DEFAULT_TXT_PATTERNS',
    'ParagraphStyle',
    'book_to_txt',
    'drop_zero_chapters',
    'export_epub',
    'is_decorative_title',
    'load_book',
    'metadata_from_dict',
    'metadata_to_dict',
    'parse_epub',
    'parse_txt',
    'parse_txt_with_patterns',
    'preview_txt_chapters',
    'strip_inline_markers',
]
