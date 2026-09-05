# hangul_novel_translator/gui/app.py
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

from typing import Any
from ..sanitizer import CustomRule, ExportSanitizer, SanitizerConfig
from ..book import load_book
from ..config import AppConfig
from ..epub_fixer import fix_finished_epub_in_place, preview_finished_epub
from ..glossary import Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary
from ..llm import LLMCancelled, LLMClient
from ..merge import archive_filename_title, audit_translation_state, book_from_state, detect_merge_title, export_merged, inspect_state, merge_books, preview_fix, repair_image_state, review_translation_state
from ..perspective import (
    PerspectiveBlock,
    PerspectiveConverter,
    PerspectiveFailedBlock,
    PerspectiveOptions,
    inspect_perspective_epub,
    load_failed_perspective_blocks,
    load_perspective_state,
    perspective_state_path,
    reset_perspective_state,
    rewrite_blocks,
    save_perspective_failure,
    save_manual_perspective_translation,
)
from ..translator import (
    FailedChunk,
    TranslationCancelled,
    TranslationResult,
    Translator,
    collect_sample_text_strided,
    detect_malformed_blocks,
    format_sample_chapters,
    load_failed_chunks,
    reconcile_paragraphs,
    save_manual_translation,
    sample_chapter_report,
)
from ..utils import extract_json, parse_paragraphs_from_payload


from .theme import THEME, _apply_ttk_theme
from .state import _load_ui_state, _save_ui_state, common_glossary_path, common_glossary_template_path
from .widgets import TreeviewTooltip, DebouncedScrollableFrame
from .dialogs import (
    GlossaryEditDialog,
    SanitizerRuleDialog,
    FailedChunkEditorDialog,
    MalformedBlockEditorDialog,
    PerspectiveFailedEditorDialog,
)


_DEFAULT_LEFT_WIDTH = 360


from .views import ShellMixin, LeftPanelMixin, GlossaryMixin, SettingsMixin, FixerMixin, MergeMixin, PerspectiveMixin

class App(ShellMixin, LeftPanelMixin, GlossaryMixin, SettingsMixin, FixerMixin, MergeMixin, PerspectiveMixin, ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("韩语小说批量翻译工具")
        ui_state = _load_ui_state()
        self._saved_window_state = ui_state.get("window_state")
        self._geometry_save_after_id: str | None = None
        saved_geom = ui_state.get("geometry")
        # 兼容旧版：旧代码用 winfo_geometry()（物理像素）保存且没有 window_state 标记。
        # 用当前窗口缩放因子把物理像素换算回逻辑单位，避免 CTk.geometry() 在 DPI 缩放屏上二次放大。
        if (
            isinstance(saved_geom, str)
            and "x" in saved_geom
            and "window_state" not in ui_state
        ):
            saved_geom = self._legacy_geometry_to_logical(saved_geom)
        if isinstance(saved_geom, str) and "x" in saved_geom:
            geom = saved_geom
        else:
            geom = self._default_geometry()
        try:
            self.geometry(self._clamp_geometry(geom))
        except Exception:
            try:
                self.geometry(self._clamp_geometry("1260x800"))
            except Exception:
                pass
        # 限制最小尺寸，并确保不超出屏幕：小屏/缩放屏下优先保持可读、可滚动，而不是被裁切。
        min_w, min_h = self._clamp_min_size(860, 540)
        self.minsize(min_w, min_h)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 用户拖动/移动窗口时防抖落盘，即使进程被强杀也能保留上次尺寸。
        self.bind("<Configure>", self._on_window_configure, add="+")
        _apply_ttk_theme(self)

        self.glossary = Glossary()
        # 通用词表：首次启动若不存在，则用入库模板（为空则写空词表）自动生成本地文件。
        self.common_glossary = Glossary.load(common_glossary_path())
        if not common_glossary_path().exists():
            try:
                template = common_glossary_template_path()
                if template.exists():
                    Path(common_glossary_path()).write_text(
                        template.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    self.common_glossary = Glossary.load(common_glossary_path())
                else:
                    self.common_glossary.save(common_glossary_path())
            except Exception:  # noqa: BLE001
                pass
        self._extract_round = 0
        self.ui_state = ui_state
        saved_app_config = ui_state.get("app_config")
        self.app_config = AppConfig.from_dict(saved_app_config) if isinstance(saved_app_config, dict) else AppConfig()
        self.sanitizer_config = SanitizerConfig()
        saved_sanitizer = ui_state.get("sanitizer_config")
        if isinstance(saved_sanitizer, dict):
            try:
                self.sanitizer_config = SanitizerConfig.from_dict(saved_sanitizer)
            except Exception:
                self.sanitizer_config = SanitizerConfig()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_layout()
        if self._saved_window_state == "zoomed":
            self.after(0, self._restore_zoomed_state)
        self.after(120, self._drain_log_queue)



def run_gui():
    app = App()
    app.mainloop()
