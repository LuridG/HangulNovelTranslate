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
    format_sample_chapters,
    load_failed_chunks,
    reconcile_paragraphs,
    save_manual_translation,
    sample_chapter_report,
)
from ..utils import extract_json, parse_paragraphs_from_payload


from .theme import THEME, _apply_ttk_theme
from .state import _load_ui_state, _save_ui_state
from .widgets import TreeviewTooltip, DebouncedScrollableFrame
from .dialogs import (
    GlossaryEditDialog,
    SanitizerRuleDialog,
    FailedChunkEditorDialog,
    PerspectiveFailedEditorDialog,
)


_DEFAULT_LEFT_WIDTH = 360


class App(ctk.CTk):
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

    # ---------------- UI ----------------
    def _build_layout(self):
        self._left_panel_width = int(self.ui_state.get("left_panel_width", _DEFAULT_LEFT_WIDTH) or _DEFAULT_LEFT_WIDTH)
        self.grid_columnconfigure(0, weight=0, minsize=self._left_panel_width)
        self.grid_columnconfigure(1, weight=0, minsize=8)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0, minsize=156)

        left = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        left.grid(row=0, column=0, padx=(14, 0), pady=14, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        left_scroll = DebouncedScrollableFrame(left, fg_color="transparent")
        left_scroll.grid(row=0, column=0, sticky="nsew")
        left_scroll.grid_columnconfigure(1, weight=1)
        self._build_left(left_scroll)

        divider = ctk.CTkFrame(
            self,
            width=8,
            corner_radius=4,
            fg_color=THEME["card_border"],
            cursor="sb_h_double_arrow",
        )
        divider.grid(row=0, column=1, pady=14, sticky="ns")
        divider.bind("<ButtonPress-1>", self._on_divider_press)
        divider.bind("<B1-Motion>", self._on_divider_motion)
        divider.bind("<ButtonRelease-1>", self._on_divider_release)
        self._divider_active = False

        right = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        right.grid(row=0, column=2, padx=(0, 14), pady=14, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(right)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tab_glossary = self.tabs.add("词表")
        self.tab_glossary.grid_columnconfigure(0, weight=1)
        self.tab_glossary.grid_rowconfigure(1, weight=1)
        self.tab_merge = self.tabs.add("多卷修正")
        self.tab_fixer = self.tabs.add("成品矫正")
        self.tab_settings = self.tabs.add("设置")
        self.tab_perspective = self.tabs.add("视角转换")
        self.tabs.configure(
            corner_radius=8,
            border_width=0,
            segmented_button_fg_color=THEME["card_alt"],
            segmented_button_selected_color=THEME["primary"],
            segmented_button_selected_hover_color=THEME["primary_hover"],
            segmented_button_unselected_color=THEME["card_alt"],
            segmented_button_unselected_hover_color=THEME["secondary_hover"],
        )
        self._build_right(self.tab_glossary)
        self._build_merge_tab(self.tab_merge)
        self._build_fixer_tab(self.tab_fixer)
        self._build_settings_tab(self.tab_settings)
        self._build_perspective_tab(self.tab_perspective)

        bottom = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        bottom.grid(row=1, column=0, columnspan=3, padx=14, pady=(0, 14), sticky="nsew")
        bottom.grid_columnconfigure(0, weight=1)
        self._build_bottom(bottom)

    def _on_divider_press(self, _event):
        self._divider_active = True

    def _on_divider_motion(self, event):
        if not self._divider_active:
            return
        try:
            pointer_x = event.x_root - self.winfo_rootx()
            total_w = self.winfo_width()
        except Exception:
            return
        min_left = 320
        min_right = 380
        left_w = max(min_left, pointer_x - 18)
        left_w = min(left_w, max(min_left, total_w - min_right - 8 - 28))
        self._left_panel_width = left_w
        self.grid_columnconfigure(0, minsize=left_w)

    def _on_divider_release(self, _event):
        self._divider_active = False
        # 拖拽结束即落盘，避免拖动后的宽度在重启时被旧配置覆盖。
        self._save_window_geometry()

    def _build_left(self, parent):
        row = 0
        ctk.CTkLabel(parent, text="工作区", font=ctk.CTkFont(size=16, weight="bold"), text_color=THEME["text_main"]).grid(
            row=row, column=0, columnspan=2, padx=14, pady=(14, 2), sticky="w"
        )
        row += 1
        ctk.CTkLabel(parent, text="书籍文件（多本=同一小说的不同卷，按顺序）", anchor="w").grid(
            row=row, column=0, columnspan=2, padx=14, pady=(10, 2), sticky="w"
        )
        self.input_files: list[Path] = []
        input_style = ttk.Style(self)
        input_style.configure("Input.Treeview", font=tkfont.Font(size=11), rowheight=30)
        input_style.configure("Input.Treeview.Heading", font=tkfont.Font(size=11, weight="bold"))
        list_frame = ctk.CTkFrame(parent, fg_color="transparent")
        list_frame.grid(row=row + 1, column=0, columnspan=2, padx=14, pady=4, sticky="nsew")
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)
        self.input_tree = ttk.Treeview(
            list_frame,
            columns=("order", "file"),
            show="headings",
            height=7,
            style="Custom.Treeview",
        )
        self.input_tree.heading("order", text="序")
        self.input_tree.heading("file", text="文件")
        self.input_tree.column("order", width=40, minwidth=30, anchor="center")
        self.input_tree.column("file", width=450, minwidth=280, anchor="w", stretch=True)
        self.input_tree.grid(row=0, column=0, sticky="nsew")
        input_scroll = ctk.CTkScrollbar(list_frame, command=self.input_tree.yview)
        input_scroll.grid(row=0, column=1, sticky="ns")
        self.input_tree.configure(yscrollcommand=input_scroll.set)
        TreeviewTooltip(self.input_tree, self._input_tree_tooltip)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=row + 2, column=0, columnspan=2, padx=10, pady=4, sticky="ew")
        ctk.CTkButton(btns, text="＋ 添加文件", width=105, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._add_input_files).grid(row=0, column=0, padx=4)
        ctk.CTkButton(btns, text="↑ 上移", width=56, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=lambda: self._move_input_file(-1)).grid(row=0, column=1, padx=4)
        ctk.CTkButton(btns, text="↓ 下移", width=56, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=lambda: self._move_input_file(1)).grid(row=0, column=2, padx=4)
        ctk.CTkButton(btns, text="✕ 移除", width=56, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._remove_input_files).grid(row=0, column=3, padx=4)
        ctk.CTkButton(btns, text="🗑 清空", width=56, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._clear_input_files).grid(row=0, column=4, padx=4)

        row += 3
        ctk.CTkLabel(parent, text="小说名（可空，留空则按源文件名命名）", anchor="w").grid(
            row=row, column=0, columnspan=2, padx=14, pady=(12, 2), sticky="w"
        )
        self.novel_name_var = tk.StringVar(value="")
        ctk.CTkEntry(parent, textvariable=self.novel_name_var).grid(
            row=row + 1, column=0, columnspan=2, padx=12, pady=2, sticky="ew"
        )

        row += 2
        ctk.CTkLabel(parent, text="输出目录", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.output_var = tk.StringVar(value=self.ui_state.get("output", str(Path.cwd() / "output")))
        ctk.CTkEntry(parent, textvariable=self.output_var).grid(row=row + 1, column=0, columnspan=2, padx=12, pady=2, sticky="ew")
        ctk.CTkButton(parent, text="📁 选择输出目录", width=140, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._choose_output).grid(
            row=row + 2, column=0, columnspan=2, padx=12, pady=4, sticky="w"
        )

        row += 3
        ctk.CTkLabel(parent, text="API 设置", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.base_url_var = tk.StringVar(value=self.app_config.base_url)
        self.api_key_var = tk.StringVar(value=self.app_config.api_key)
        self.model_var = tk.StringVar(value=self.app_config.model)

        ctk.CTkLabel(parent, text="Base URL").grid(row=row + 1, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.base_url_var).grid(row=row + 1, column=1, padx=12, pady=2, sticky="ew")
        ctk.CTkLabel(parent, text="API Key").grid(row=row + 2, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.api_key_var, show="*").grid(row=row + 2, column=1, padx=12, pady=2, sticky="ew")
        ctk.CTkLabel(parent, text="Model").grid(row=row + 3, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.model_var).grid(row=row + 3, column=1, padx=12, pady=2, sticky="ew")

        row += 4
        ctk.CTkLabel(parent, text="翻译参数", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.chunk_var = tk.StringVar(value=str(self.app_config.chunk_chars))
        self.workers_var = tk.StringVar(value=str(self.app_config.max_workers))
        self.extract_var = tk.BooleanVar(value=self.app_config.extract_glossary)
        self.txt_var = tk.BooleanVar(value=self.app_config.output_txt)
        self.epub_var = tk.BooleanVar(value=self.app_config.output_epub)

        ctk.CTkLabel(parent, text="每批字符数").grid(row=row + 1, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.chunk_var, width=90).grid(row=row + 1, column=1, padx=12, pady=2, sticky="w")
        ctk.CTkLabel(parent, text="并发数").grid(row=row + 2, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.workers_var, width=90).grid(row=row + 2, column=1, padx=12, pady=2, sticky="w")

        ctk.CTkCheckBox(parent, text="翻译前自动提取词表（空词表时）", variable=self.extract_var).grid(
            row=row + 3, column=0, columnspan=2, padx=12, pady=(8, 2), sticky="w"
        )
        ctk.CTkCheckBox(parent, text="输出 TXT", variable=self.txt_var).grid(
            row=row + 4, column=0, padx=12, pady=2, sticky="w"
        )
        ctk.CTkCheckBox(parent, text="输出 EPUB", variable=self.epub_var).grid(
            row=row + 4, column=1, padx=12, pady=2, sticky="w"
        )

        self.sanitizer_enabled_var = tk.BooleanVar(value=self.sanitizer_config.enabled)
        ctk.CTkCheckBox(
            parent,
            text="导出时自动清洗翻译残余",
            variable=self.sanitizer_enabled_var,
        ).grid(row=row + 5, column=0, columnspan=2, padx=12, pady=(8, 2), sticky="w")
        ctk.CTkButton(
            parent,
            text="⚙ 自定义清洗规则...",
            width=170,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._open_sanitizer_dialog,
        ).grid(row=row + 6, column=0, columnspan=2, padx=12, pady=2, sticky="w")

    def _build_settings_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(parent, text="设置", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=18, pady=(16, 8), sticky="w"
        )
        body = DebouncedScrollableFrame(parent)
        body.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        body.grid_columnconfigure(1, weight=1)

        # 设置页直接使用左侧控件的变量，避免出现两套配置互相覆盖。
        self.settings_vars = {
            "base_url": self.base_url_var,
            "api_key": self.api_key_var,
            "model": self.model_var,
            "chunk_chars": self.chunk_var,
            "max_workers": self.workers_var,
            "max_paragraph_chars": tk.StringVar(value=str(self.app_config.max_paragraph_chars)),
            "max_retries": tk.StringVar(value=str(self.app_config.max_retries)),
            "retry_delay": tk.StringVar(value=str(self.app_config.retry_delay)),
            "timeout": tk.StringVar(value=str(self.app_config.timeout)),
            "temperature": tk.StringVar(value=str(self.app_config.temperature)),
            "extract_sample_chars": tk.StringVar(value=str(self.app_config.extract_sample_chars)),
            "extract_sample_per_region": tk.StringVar(value=str(self.app_config.extract_sample_per_region)),
            "glossary_limit": tk.StringVar(value=str(self.app_config.glossary_limit)),
            "output_encoding": tk.StringVar(value=self.app_config.output_encoding),
            "output": self.output_var,
        }
        self.llm_profiles = self.ui_state.get("llm_profiles", [])
        if not isinstance(self.llm_profiles, list):
            self.llm_profiles = []
        if not self.llm_profiles:
            self.llm_profiles = [{"name": "当前配置", "base_url": self.app_config.base_url, "api_key": self.app_config.api_key, "model": self.app_config.model}]
        self.settings_bools = {
            "extract_glossary": self.extract_var,
            "output_txt": self.txt_var,
            "output_epub": self.epub_var,
            "resume": tk.BooleanVar(value=self.app_config.resume),
        }
        row = 0
        sections = [
            ("LLM API", [("Base URL", "base_url", False), ("API Key", "api_key", True), ("Model", "model", False)]),
            ("翻译参数", [("每批字符数", "chunk_chars", False), ("并发数", "max_workers", False), ("单段拆句阈值", "max_paragraph_chars", False), ("最大重试次数", "max_retries", False), ("重试间隔（秒）", "retry_delay", False), ("请求超时（秒）", "timeout", False), ("温度", "temperature", False)]),
            ("词表采样", [("样章字符预算", "extract_sample_chars", False), ("每区域采样章数", "extract_sample_per_region", False), ("词条上限", "glossary_limit", False)]),
            ("输出", [("默认输出目录", "output", False), ("文本编码", "output_encoding", False)]),
        ]
        for title, fields in sections:
            ctk.CTkLabel(body, text=title, font=ctk.CTkFont(size=14, weight="bold")).grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 6), sticky="w")
            row += 1
            for label, key, secret in fields:
                ctk.CTkLabel(body, text=label).grid(row=row, column=0, padx=12, pady=4, sticky="w")
                ctk.CTkEntry(body, textvariable=self.settings_vars[key], show="*" if secret else "").grid(row=row, column=1, padx=12, pady=4, sticky="ew")
                row += 1
        ctk.CTkLabel(body, text="LLM API 预设", font=ctk.CTkFont(size=14, weight="bold")).grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 6), sticky="w")
        row += 1
        profile_frame = ctk.CTkFrame(body, fg_color="transparent")
        profile_frame.grid(row=row, column=0, columnspan=2, padx=8, pady=4, sticky="ew")
        profile_frame.grid_columnconfigure(0, weight=1)
        self.profile_var = tk.StringVar()
        self.profile_menu = ctk.CTkComboBox(profile_frame, variable=self.profile_var, values=[], command=self._apply_selected_profile)
        self.profile_menu.grid(row=0, column=0, padx=4, sticky="ew")
        ctk.CTkButton(profile_frame, text="保存为预设", width=100, command=self._save_profile).grid(row=0, column=1, padx=4)
        ctk.CTkButton(profile_frame, text="删除", width=65, fg_color=THEME["secondary"], hover_color=THEME["danger"], command=self._delete_profile).grid(row=0, column=2, padx=4)
        ctk.CTkButton(profile_frame, text="↑", width=35, command=lambda: self._move_profile(-1)).grid(row=0, column=3, padx=2)
        ctk.CTkButton(profile_frame, text="↓", width=35, command=lambda: self._move_profile(1)).grid(row=0, column=4, padx=2)
        row += 1
        ctk.CTkLabel(body, text="开关", font=ctk.CTkFont(size=14, weight="bold")).grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 6), sticky="w")
        row += 1
        for label, key in [("翻译前自动提取词表", "extract_glossary"), ("断点续传", "resume"), ("输出 TXT", "output_txt"), ("输出 EPUB", "output_epub")]:
            ctk.CTkCheckBox(body, text=label, variable=self.settings_bools[key]).grid(row=row, column=0, columnspan=2, padx=12, pady=4, sticky="w")
            row += 1
        ctk.CTkLabel(body, text="界面字体", font=ctk.CTkFont(size=14, weight="bold")).grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 6), sticky="w")
        row += 1
        self.font_size_var = tk.StringVar(value=str(self.ui_state.get("fonts", {}).get("font_size", 13)))
        self.row_height_var = tk.StringVar(value=str(self.ui_state.get("fonts", {}).get("row_height", 34)))
        for label, var in [("字体大小", self.font_size_var), ("行高", self.row_height_var)]:
            ctk.CTkLabel(body, text=label).grid(row=row, column=0, padx=12, pady=4, sticky="w")
            ctk.CTkComboBox(body, variable=var, values=[str(x) for x in range(11, 19)] if label == "字体大小" else [str(x) for x in range(26, 51, 2)], command=lambda _value: self._apply_font_settings()).grid(row=row, column=1, padx=12, pady=4, sticky="w")
            row += 1
        self._refresh_profiles()
        self._apply_font_settings()
        self.settings_status = tk.StringVar(value="设置保存在 .gui_config.json")
        ctk.CTkButton(body, text="保存设置", command=self._save_settings).grid(row=row, column=0, padx=12, pady=16, sticky="w")
        ctk.CTkLabel(body, textvariable=self.settings_status, text_color=THEME["text_muted"]).grid(row=row, column=1, padx=12, pady=16, sticky="w")

    def _save_settings(self):
        integer_keys = {"chunk_chars", "max_workers", "max_paragraph_chars", "max_retries", "extract_sample_chars", "extract_sample_per_region", "glossary_limit"}
        float_keys = {"retry_delay", "timeout", "temperature"}
        try:
            values = {key: var.get().strip() for key, var in self.settings_vars.items()}
            for key in integer_keys:
                values[key] = int(values[key])
            for key in float_keys:
                values[key] = float(values[key])
            if not values["base_url"] or not values["model"]:
                raise ValueError("Base URL 和 Model 不能为空")
            data = self.app_config.to_dict()
            data.update(values)
            data.update({key: var.get() for key, var in self.settings_bools.items()})
            self.app_config = AppConfig.from_dict(data)
            self._sync_legacy_variables()
            state = _load_ui_state()
            state["app_config"] = self.app_config.to_dict()
            state["output"] = values["output"]
            state["llm_profiles"] = self.llm_profiles
            state["fonts"] = {"font_size": int(self.font_size_var.get()), "row_height": int(self.row_height_var.get())}
            _save_ui_state(state)
            self.settings_status.set("设置已保存")
        except (ValueError, TypeError) as exc:
            messagebox.showerror("设置无效", str(exc), parent=self)

    def _refresh_profiles(self):
        names = [str(item.get("name", "未命名")) for item in self.llm_profiles]
        self.profile_menu.configure(values=names)
        if names and self.profile_var.get() not in names:
            self.profile_var.set(names[0])

    def _save_profile(self):
        name = simpledialog.askstring("保存预设", "预设名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        profile = {"name": name, "base_url": self.settings_vars["base_url"].get().strip(), "api_key": self.settings_vars["api_key"].get().strip(), "model": self.settings_vars["model"].get().strip()}
        self.llm_profiles = [item for item in self.llm_profiles if item.get("name") != name]
        self.llm_profiles.append(profile)
        self.profile_var.set(name)
        self._refresh_profiles()

    def _apply_selected_profile(self, name):
        profile = next((item for item in self.llm_profiles if item.get("name") == name), None)
        if not profile:
            return
        for key in ("base_url", "api_key", "model"):
            self.settings_vars[key].set(profile.get(key, ""))

    def _delete_profile(self):
        name = self.profile_var.get()
        if len(self.llm_profiles) <= 1:
            messagebox.showinfo("提示", "至少保留一个 API 预设", parent=self)
            return
        self.llm_profiles = [item for item in self.llm_profiles if item.get("name") != name]
        self._refresh_profiles()

    def _move_profile(self, direction):
        index = next((i for i, item in enumerate(self.llm_profiles) if item.get("name") == self.profile_var.get()), -1)
        target = index + direction
        if index < 0 or target < 0 or target >= len(self.llm_profiles):
            return
        self.llm_profiles[index], self.llm_profiles[target] = self.llm_profiles[target], self.llm_profiles[index]
        self._refresh_profiles()

    def _apply_font_settings(self):
        try:
            size = max(11, min(18, int(self.font_size_var.get())))
            height = max(26, int(self.row_height_var.get()))
        except (AttributeError, ValueError):
            return
        for style_name in ("Custom.Treeview", "Glossary.Treeview", "Input.Treeview"):
            style = ttk.Style(self)
            style.configure(style_name, font=tkfont.Font(size=size), rowheight=height)

    def _sync_legacy_variables(self):
        if not hasattr(self, "base_url_var"):
            return
        self.base_url_var.set(self.app_config.base_url)
        self.api_key_var.set(self.app_config.api_key)
        self.model_var.set(self.app_config.model)
        self.chunk_var.set(str(self.app_config.chunk_chars))
        self.workers_var.set(str(self.app_config.max_workers))
        self.extract_var.set(self.app_config.extract_glossary)
        self.txt_var.set(self.app_config.output_txt)
        self.epub_var.set(self.app_config.output_epub)

    def _build_right(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=12, pady=(14, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="专有名词词表", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="⚡ 提取词表", width=96, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._extract_glossary_async).grid(row=0, column=1, padx=4)
        ctk.CTkButton(header, text="提取更多词表", width=110, command=self._extract_more_glossary_async).grid(row=0, column=4, padx=4)
        ctk.CTkButton(header, text="完善信息", width=90, command=self._enrich_glossary_async).grid(row=0, column=5, padx=4)
        self.enrich_alts_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(header, text="补可能译法", variable=self.enrich_alts_var).grid(row=0, column=6, padx=(6, 0))
        self.enrich_nick_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(header, text="检测昵称", variable=self.enrich_nick_var).grid(row=0, column=7, padx=(0, 4))
        ctk.CTkButton(header, text="💾 保存词表", width=92, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._save_glossary).grid(row=0, column=2, padx=4)
        ctk.CTkButton(header, text="📂 加载词表", width=92, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._load_glossary).grid(row=0, column=3, padx=4)

        tree_frame = ctk.CTkFrame(parent)
        tree_frame.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.configure("Glossary.Treeview", font=tkfont.Font(size=11), rowheight=32)
        style.configure("Glossary.Treeview.Heading", font=tkfont.Font(size=11, weight="bold"))

        self.columns = ("ko", "zh", "kind", "note", "alternatives", "confirmed")
        self.sort_col = ""
        self.sort_desc = False
        self.tree = ttk.Treeview(
            tree_frame,
            columns=self.columns,
            show="headings",
            height=18,
            selectmode="extended",
            style="Custom.Treeview",
        )
        self.headings = {
            "ko": "韩文原文",
            "zh": "中文译名",
            "kind": "类型",
            "note": "备注",
            "alternatives": "可能译法",
            "confirmed": "确认",
        }
        widths = {
            "ko": 150,
            "zh": 150,
            "kind": 110,
            "note": 110,
            "alternatives": 130,
            "confirmed": 60,
        }
        for col in self.columns:
            self.tree.heading(col, text=self.headings[col], command=lambda c=col: self._sort_by(c))
            self.tree.column(
                col,
                width=widths[col],
                anchor="w",
                stretch=(col in ("ko", "zh", "note", "alternatives")),
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(tree_frame, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree_xscroll = ctk.CTkScrollbar(tree_frame, orientation="horizontal", command=self.tree.xview)
        tree_xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=tree_xscroll.set)
        self.tree.bind("<Double-1>", lambda _e: self._edit_selected())
        TreeviewTooltip(self.tree)

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="ew")
        ctk.CTkButton(actions, text="新增", width=70, command=self._add_entry).grid(row=0, column=0, padx=4)
        ctk.CTkButton(actions, text="✎ 编辑", width=76, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._edit_selected).grid(row=0, column=1, padx=4)
        ctk.CTkButton(actions, text="✕ 删除", width=76, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._remove_selected).grid(row=0, column=2, padx=4)
        ctk.CTkButton(actions, text="✓ 确认选中", width=96, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._confirm_selected).grid(row=0, column=3, padx=4)
        ctk.CTkButton(actions, text="✓✓ 全部确认", width=100, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._confirm_all).grid(row=0, column=4, padx=4)
        ctk.CTkButton(actions, text="🧹 清理重复", width=96, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._cleanup_duplicates).grid(row=0, column=5, padx=4)
        ctk.CTkButton(actions, text="🗑 清空词表", width=96, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._clear_glossary).grid(row=0, column=6, padx=4)
        self.count_label = ctk.CTkLabel(actions, text="0 条")
        self.count_label.grid(row=0, column=7, padx=12, sticky="e")

    def _build_merge_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        self.merge_files: list[Path] = []
        self.merge_title_var = tk.StringVar(value="")
        self._merge_title_manual = False
        self.merge_txt_var = tk.BooleanVar(value=True)
        self.merge_epub_var = tk.BooleanVar(value=True)
        self.merge_review_threshold_var = tk.StringVar(value="30")
        self.audit_patterns = list(self.ui_state.get("audit_patterns") or [])
        self.audit_pattern_names = [str(item.get("name", "未命名规则")) for item in self.audit_patterns]
        self.audit_pattern_var = tk.StringVar(value=self.audit_pattern_names[0] if self.audit_pattern_names else "")
        self._audit_pattern_loading = False

        ctk.CTkLabel(
            parent,
            text="多卷翻译存档（.translation_state.json，按列表顺序拼合）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        list_frame = ctk.CTkFrame(parent)
        list_frame.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        self.merge_tree = ttk.Treeview(
            list_frame,
            columns=("order", "file", "info"),
            show="headings",
            height=10,
            style="Custom.Treeview",
        )
        self.merge_tree.heading("order", text="顺序")
        self.merge_tree.heading("file", text="存档文件")
        self.merge_tree.heading("info", text="信息")
        self.merge_tree.column("order", width=50, anchor="center")
        self.merge_tree.column("file", width=380, anchor="w", stretch=True)
        self.merge_tree.column("info", width=220, anchor="w")
        self.merge_tree.grid(row=0, column=0, sticky="nsew")
        merge_scroll = ctk.CTkScrollbar(list_frame, command=self.merge_tree.yview)
        merge_scroll.grid(row=0, column=1, sticky="ns")
        merge_xscroll = ctk.CTkScrollbar(list_frame, orientation="horizontal", command=self.merge_tree.xview)
        merge_xscroll.grid(row=1, column=0, sticky="ew")
        self.merge_tree.configure(yscrollcommand=merge_scroll.set, xscrollcommand=merge_xscroll.set)
        TreeviewTooltip(self.merge_tree)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")
        ctk.CTkButton(btns, text="＋ 添加存档", width=100, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._add_merge_file).grid(row=0, column=0, padx=4)
        ctk.CTkButton(btns, text="上移", width=70, command=lambda: self._move_merge_file(-1)).grid(row=0, column=1, padx=4)
        ctk.CTkButton(btns, text="下移", width=70, command=lambda: self._move_merge_file(1)).grid(row=0, column=2, padx=4)
        ctk.CTkButton(btns, text="移除", width=70, command=self._remove_merge_files).grid(row=0, column=3, padx=4)
        ctk.CTkButton(btns, text="清空", width=70, command=self._clear_merge_files).grid(row=0, column=4, padx=4)

        cfg_frame = ctk.CTkFrame(parent, fg_color="transparent")
        cfg_frame.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(cfg_frame, text="合并书名").grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.merge_title_entry = ctk.CTkEntry(cfg_frame, textvariable=self.merge_title_var, width=240)
        self.merge_title_entry.grid(row=0, column=1, padx=4, sticky="w")
        self.merge_title_entry.bind("<KeyRelease>", self._on_merge_title_edited)
        ctk.CTkCheckBox(cfg_frame, text="输出 TXT", variable=self.merge_txt_var).grid(row=0, column=2, padx=(18, 4))
        ctk.CTkCheckBox(cfg_frame, text="输出 EPUB", variable=self.merge_epub_var).grid(row=0, column=3, padx=4)
        ctk.CTkCheckBox(
            cfg_frame,
            text="导出时自动清洗翻译残余",
            variable=self.sanitizer_enabled_var,
        ).grid(row=1, column=0, columnspan=2, padx=(4, 8), pady=(10, 0), sticky="w")
        ctk.CTkButton(
            cfg_frame,
            text="⚙ 自定义清洗规则...",
            width=150,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._open_sanitizer_dialog,
        ).grid(row=1, column=2, padx=4, pady=(10, 0), sticky="w")

        run_frame = ctk.CTkFrame(parent, fg_color="transparent")
        run_frame.grid(row=4, column=0, padx=12, pady=(0, 8), sticky="ew")
        ctk.CTkButton(run_frame, text="预览修正", width=110, command=self._merge_preview_async).grid(row=0, column=0, padx=4)
        ctk.CTkButton(run_frame, text="修正并输出", width=130, command=self._merge_run_async).grid(row=0, column=1, padx=4)
        ctk.CTkButton(run_frame, text="✏ 失败块查看", width=110, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._open_failed_editor).grid(row=0, column=2, padx=4)
        ctk.CTkButton(run_frame, text="🔄 校验并重试", width=120, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_retry_async).grid(row=0, column=3, padx=4)
        ctk.CTkButton(run_frame, text="🖼 图片补集", width=110, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_repair_images_async).grid(row=0, column=4, padx=4)
        ctk.CTkButton(run_frame, text="🔍 复查", width=80, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_review_async).grid(row=0, column=5, padx=4)
        ctk.CTkLabel(run_frame, text="单段韩文阈值").grid(row=1, column=0, padx=4, pady=(8, 0), sticky="e")
        ctk.CTkEntry(run_frame, textvariable=self.merge_review_threshold_var, width=70).grid(row=1, column=1, padx=4, pady=(8, 0), sticky="w")

        audit_frame = ctk.CTkFrame(parent, fg_color="transparent")
        audit_frame.grid(row=5, column=0, padx=12, pady=(0, 8), sticky="ew")
        audit_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(audit_frame, text="自检修复规则", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.audit_pattern_menu = ctk.CTkComboBox(
            audit_frame,
            variable=self.audit_pattern_var,
            values=self.audit_pattern_names,
            width=180,
            command=self._on_audit_pattern_selected,
        )
        self.audit_pattern_menu.grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkButton(
            audit_frame, text="💾 保存规则", width=90,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._save_audit_pattern,
        ).grid(row=0, column=2, padx=4)
        self._delete_audit_pattern_btn = ctk.CTkButton(
            audit_frame, text="🗑 删除规则", width=90,
            fg_color=THEME["secondary"], hover_color=THEME["danger"],
            border_width=1, border_color=THEME["card_border"], command=self._delete_audit_pattern,
        )
        self._delete_audit_pattern_btn.grid(row=0, column=3, padx=4, sticky="e")

        audit_edit_frame = ctk.CTkFrame(parent, fg_color="transparent")
        audit_edit_frame.grid(row=6, column=0, padx=12, pady=(0, 8), sticky="ew")
        audit_edit_frame.grid_columnconfigure(0, weight=1)
        self.audit_pattern_box = ctk.CTkTextbox(audit_edit_frame, height=4, border_width=1, border_color=THEME["card_border"])
        self.audit_pattern_box.grid(row=0, column=0, padx=4, sticky="ew")
        ctk.CTkButton(
            audit_edit_frame, text="🧰 自检修复块", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._merge_audit_async,
        ).grid(row=0, column=1, padx=(8, 4), sticky="e")

        ctk.CTkLabel(
            parent,
            text="提示：每个存档需与其对应的原书（.txt/.epub）保持在原路径；“复查”统计韩文字符数；“自检修复块”按自定义正则识别混入译文的失败字段。",
            wraplength=620,
            justify="left",
            anchor="w",
        ).grid(row=7, column=0, padx=12, pady=(0, 12), sticky="w")

        self._refresh_audit_patterns()

    def _build_fixer_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            parent,
            text="成品 EPUB 词表无损矫正（不破坏封面/CSS/图片/目录）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        input_frame = ctk.CTkFrame(parent, fg_color="transparent")
        input_frame.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        input_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(input_frame, text="成品 EPUB 路径").grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.fixer_path_var = tk.StringVar(value="")
        ctk.CTkEntry(input_frame, textvariable=self.fixer_path_var).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        ctk.CTkButton(
            input_frame, text="📂 浏览", width=80,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._fixer_browse,
        ).grid(row=0, column=2, padx=4)
        self.fixer_glossary_label = ctk.CTkLabel(input_frame, text="")
        self.fixer_glossary_label.grid(row=0, column=3, padx=12, sticky="e")

        mode_frame = ctk.CTkFrame(parent, fg_color="transparent")
        mode_frame.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.fixer_mode_var = tk.StringVar(value="new")
        ctk.CTkRadioButton(
            mode_frame, text="另存为新文件 (_fixed.epub)", variable=self.fixer_mode_var, value="new"
        ).grid(row=0, column=0, padx=4, sticky="w")
        ctk.CTkRadioButton(
            mode_frame, text="原地覆盖（自动生成 .bak 备份）", variable=self.fixer_mode_var, value="overwrite"
        ).grid(row=0, column=1, padx=(18, 4), sticky="w")

        run_frame = ctk.CTkFrame(parent, fg_color="transparent")
        run_frame.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")
        ctk.CTkButton(run_frame, text="🔍 预检变更", width=110, command=self._fixer_preview_async).grid(row=0, column=0, padx=4)
        ctk.CTkButton(
            run_frame, text="⚡ 开始无损矫正", width=140,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._fixer_run_async,
        ).grid(row=0, column=1, padx=4)
        ctk.CTkButton(
            run_frame, text="刷新词表统计", width=110,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._fixer_refresh_glossary,
        ).grid(row=0, column=2, padx=4)

        self.fixer_preview = ctk.CTkTextbox(parent, height=320)
        self.fixer_preview.grid(row=4, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self.fixer_preview.configure(state="disabled")
        self._fixer_refresh_glossary()

    # ---------------- 第一人称改第三人称 ----------------
    def _build_perspective_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(6, weight=1)
        ctk.CTkLabel(
            parent,
            text="中文 EPUB 第一人称改第三人称（独立流程，不覆盖原书）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        input_frame = ctk.CTkFrame(parent, fg_color="transparent")
        input_frame.grid(row=1, column=0, padx=12, pady=(0, 6), sticky="ew")
        input_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(input_frame, text="输入中文 EPUB").grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.perspective_input_var = tk.StringVar(value="")
        ctk.CTkEntry(input_frame, textvariable=self.perspective_input_var).grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkButton(
            input_frame,
            text="📂 浏览",
            width=80,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._perspective_browse_input,
        ).grid(row=0, column=2, padx=4)

        output_frame = ctk.CTkFrame(parent, fg_color="transparent")
        output_frame.grid(row=2, column=0, padx=12, pady=(0, 6), sticky="ew")
        output_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(output_frame, text="输出 EPUB").grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.perspective_output_var = tk.StringVar(value="")
        ctk.CTkEntry(output_frame, textvariable=self.perspective_output_var).grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkButton(
            output_frame,
            text="📁 选择",
            width=80,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._perspective_browse_output,
        ).grid(row=0, column=2, padx=4)

        options = ctk.CTkFrame(parent)
        options.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")
        options.grid_columnconfigure(1, weight=1)
        options.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(options, text="叙述者/主角名称").grid(row=0, column=0, padx=(8, 6), pady=6, sticky="w")
        self.perspective_name_var = tk.StringVar(value="")
        ctk.CTkEntry(options, textvariable=self.perspective_name_var).grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        ctk.CTkLabel(options, text="简称（可空）").grid(row=0, column=2, padx=(12, 6), pady=6, sticky="w")
        self.perspective_short_name_var = tk.StringVar(value="")
        ctk.CTkEntry(options, textvariable=self.perspective_short_name_var).grid(row=0, column=3, padx=6, pady=6, sticky="ew")
        ctk.CTkLabel(options, text="主角代词").grid(row=1, column=0, padx=(8, 6), pady=6, sticky="w")
        self.perspective_pronoun_var = tk.StringVar(value="他")
        ctk.CTkComboBox(options, variable=self.perspective_pronoun_var, values=["他", "她", "它"]).grid(row=1, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(options, text="替换风格").grid(row=1, column=2, padx=(12, 6), pady=6, sticky="w")
        self.perspective_style_var = tk.StringVar(value="优先使用代词")
        ctk.CTkComboBox(
            options,
            variable=self.perspective_style_var,
            values=["使用全名", "使用简称", "优先使用代词"],
        ).grid(row=1, column=3, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(options, text="每批字符数").grid(row=2, column=0, padx=(8, 6), pady=6, sticky="w")
        self.perspective_chunk_var = tk.StringVar(value=str(self.app_config.chunk_chars))
        ctk.CTkEntry(options, textvariable=self.perspective_chunk_var, width=110).grid(row=2, column=1, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(options, text="转换策略").grid(row=2, column=2, padx=(12, 6), pady=6, sticky="w")
        self.perspective_strategy_var = tk.StringVar(value="策略 1：保守筛选")
        self.perspective_strategy_control = ctk.CTkSegmentedButton(
            options,
            variable=self.perspective_strategy_var,
            values=["策略 1：保守筛选", "策略 2：正文全覆盖"],
            command=self._perspective_strategy_changed,
        )
        self.perspective_strategy_control.grid(row=2, column=3, padx=6, pady=6, sticky="ew")

        self.perspective_dialogue_var = tk.BooleanVar(value=False)
        self.perspective_letters_var = tk.BooleanVar(value=False)
        self.perspective_inner_var = tk.BooleanVar(value=False)
        self.perspective_dialogue_check = ctk.CTkCheckBox(options, text="改写含对白段落", variable=self.perspective_dialogue_var)
        self.perspective_dialogue_check.grid(row=3, column=0, padx=8, pady=6, sticky="w")
        self.perspective_letters_check = ctk.CTkCheckBox(options, text="改写书信/聊天/引用", variable=self.perspective_letters_var)
        self.perspective_letters_check.grid(row=3, column=1, padx=6, pady=6, sticky="w")
        self.perspective_inner_check = ctk.CTkCheckBox(options, text="改写内心独白", variable=self.perspective_inner_var)
        self.perspective_inner_check.grid(row=3, column=2, padx=12, pady=6, sticky="w")
        self.perspective_strategy_note = ctk.CTkLabel(options, text="", text_color=THEME["text_muted"], anchor="w")
        self.perspective_strategy_note.grid(row=4, column=0, columnspan=4, padx=8, pady=(0, 6), sticky="w")
        self._perspective_strategy_changed(self.perspective_strategy_var.get())

        glossary_frame = ctk.CTkFrame(parent, fg_color="transparent")
        glossary_frame.grid(row=4, column=0, padx=12, pady=(0, 6), sticky="ew")
        glossary_frame.grid_columnconfigure(1, weight=1)
        self.perspective_glossary_var = tk.StringVar(value="使用当前 GUI 词表")
        ctk.CTkLabel(glossary_frame, text="保护词表").grid(row=0, column=0, padx=(4, 8), sticky="w")
        ctk.CTkEntry(glossary_frame, textvariable=self.perspective_glossary_var, state="readonly").grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkButton(glossary_frame, text="📂 加载词表", width=100, command=self._perspective_load_glossary).grid(row=0, column=2, padx=4)
        ctk.CTkButton(glossary_frame, text="清除外部词表", width=100, command=self._perspective_clear_external_glossary).grid(row=0, column=3, padx=4)
        self.perspective_external_glossary: Glossary | None = None
        self.perspective_state_override: Path | None = None

        action_frame = ctk.CTkFrame(parent, fg_color="transparent")
        action_frame.grid(row=5, column=0, padx=12, pady=(0, 8), sticky="ew")
        ctk.CTkButton(action_frame, text="🔍 预览范围", width=105, command=self._perspective_preview_async).grid(row=0, column=0, padx=4)
        ctk.CTkButton(action_frame, text="▶ 开始转换", width=115, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._perspective_start_async).grid(row=0, column=1, padx=4)
        ctk.CTkButton(action_frame, text="✏ 查看失败块", width=115, command=self._perspective_open_failed).grid(row=0, column=2, padx=4)
        ctk.CTkButton(action_frame, text="📂 加载上次作业", width=125, command=self._perspective_load_state).grid(row=0, column=3, padx=4)
        ctk.CTkButton(action_frame, text="↻ 重置存档", width=105, fg_color=THEME["secondary"], hover_color=THEME["danger"], command=self._perspective_reset_state).grid(row=0, column=4, padx=4)
        ctk.CTkLabel(action_frame, text="默认保护对白、书信、聊天、日记、引用和内心独白。", text_color=THEME["text_muted"]).grid(row=0, column=5, padx=12, sticky="w")

        self.perspective_preview_box = ctk.CTkTextbox(parent, height=230)
        self.perspective_preview_box.grid(row=6, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self.perspective_preview_box.configure(state="disabled")

    def _perspective_options_from_ui(self) -> PerspectiveOptions:
        style_map = {"使用全名": "full_name", "使用简称": "short_name", "优先使用代词": "pronoun"}
        strategy_map = {"策略 1：保守筛选": "conservative", "策略 2：正文全覆盖": "coverage"}
        try:
            chunk_chars = int(self.perspective_chunk_var.get().strip())
        except ValueError as exc:
            raise ValueError("视角转换每批字符数必须是整数") from exc
        return PerspectiveOptions(
            narrator_name=self.perspective_name_var.get(),
            short_name=self.perspective_short_name_var.get(),
            pronoun=self.perspective_pronoun_var.get(),
            style=style_map.get(self.perspective_style_var.get(), "pronoun"),
            rewrite_dialogue=self.perspective_dialogue_var.get(),
            rewrite_letters=self.perspective_letters_var.get(),
            rewrite_inner_monologue=self.perspective_inner_var.get(),
            strategy=strategy_map.get(self.perspective_strategy_var.get(), "conservative"),
            chunk_chars=chunk_chars,
        ).normalized()

    def _perspective_strategy_changed(self, value: str):
        coverage = value == "策略 2：正文全覆盖"
        state = "disabled" if coverage else "normal"
        for checkbox in (self.perspective_dialogue_check, self.perspective_letters_check, self.perspective_inner_check):
            checkbox.configure(state=state)
        note = (
            "策略 2 会送入全部非标题、非代码正文，由模型区分旁白与人物原话。"
            if coverage
            else "策略 1 按本地分类筛选；可单独决定是否纳入对白、书信和内心独白。"
        )
        self.perspective_strategy_note.configure(text=note)

    def _perspective_paths_from_ui(self) -> tuple[Path, Path]:
        source_text = self.perspective_input_var.get().strip()
        output_text = self.perspective_output_var.get().strip()
        if not source_text:
            raise ValueError("请先选择输入中文 EPUB")
        source = Path(source_text)
        if not source.exists():
            raise ValueError("输入中文 EPUB 不存在")
        if source.suffix.lower() != ".epub":
            raise ValueError("视角转换输入必须是 EPUB")
        if not output_text:
            output = source.with_name(source.stem + ".第三人称.epub")
            self.perspective_output_var.set(str(output))
        else:
            output = Path(output_text)
        if source.resolve() == output.resolve():
            raise ValueError("输出 EPUB 不能覆盖输入 EPUB")
        return source, output

    def _perspective_browse_input(self):
        path = filedialog.askopenfilename(title="选择中文 EPUB", filetypes=[("EPUB", "*.epub")], parent=self)
        if path:
            self.perspective_input_var.set(path)
            source = Path(path)
            self.perspective_output_var.set(str(source.with_name(source.stem + ".第三人称.epub")))
            self.perspective_state_override = None

    def _perspective_browse_output(self):
        path = filedialog.asksaveasfilename(
            title="选择第三人称 EPUB 输出路径",
            defaultextension=".epub",
            filetypes=[("EPUB", "*.epub")],
            parent=self,
        )
        if path:
            self.perspective_output_var.set(path)
            self.perspective_state_override = None

    def _perspective_load_state(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行，完成后再加载存档。", parent=self)
            return
        path = filedialog.askopenfilename(
            title="加载视角转换作业存档",
            filetypes=[("视角转换 JSON", "*.perspective_state.json"), ("JSON", "*.json")],
            parent=self,
        )
        if not path:
            return
        try:
            state_path = Path(path)
            data = load_perspective_state(state_path)
            source = Path(str(data["source_path"]))
            output = Path(str(data["output_path"]))
            options = PerspectiveOptions(**data["options"]).normalized()
            if not source.exists():
                raise ValueError(f"存档对应的输入 EPUB 不存在：{source}")
            if source.suffix.lower() != ".epub":
                raise ValueError("存档对应的输入文件不是 EPUB")
            style_labels = {"full_name": "使用全名", "short_name": "使用简称", "pronoun": "优先使用代词"}
            self.perspective_input_var.set(str(source))
            self.perspective_output_var.set(str(output))
            self.perspective_name_var.set(options.narrator_name)
            self.perspective_short_name_var.set(options.short_name)
            self.perspective_pronoun_var.set(options.pronoun)
            self.perspective_style_var.set(style_labels.get(options.style, "优先使用代词"))
            self.perspective_chunk_var.set(str(options.chunk_chars))
            self.perspective_dialogue_var.set(options.rewrite_dialogue)
            self.perspective_letters_var.set(options.rewrite_letters)
            self.perspective_inner_var.set(options.rewrite_inner_monologue)
            strategy_label = "策略 2：正文全覆盖" if options.strategy == "coverage" else "策略 1：保守筛选"
            self.perspective_strategy_var.set(strategy_label)
            self._perspective_strategy_changed(strategy_label)
            self.perspective_state_override = state_path
            blocks = data.get("blocks") or {}
            completed = sum(isinstance(item, dict) and item.get("status") == "completed" for item in blocks.values())
            failed = sum(isinstance(item, dict) and item.get("status") == "failed" for item in blocks.values())
            pending = len(blocks) - completed - failed
            self.status_var.set(f"已加载视角转换作业：完成 {completed}，失败 {failed}，待处理 {max(pending, 0)}")
            self.log(f"已加载视角转换作业存档：{state_path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("加载存档失败", str(exc), parent=self)

    def _perspective_load_glossary(self):
        path = filedialog.askopenfilename(title="加载视角转换词表", filetypes=[("JSON", "*.json")], parent=self)
        if path:
            try:
                self.perspective_external_glossary = Glossary.load(Path(path))
                self.perspective_glossary_var.set(f"外部词表：{path}（{len(self.perspective_external_glossary.valid_entries())} 条）")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("词表加载失败", str(exc), parent=self)

    def _perspective_clear_external_glossary(self):
        self.perspective_external_glossary = None
        self.perspective_glossary_var.set("使用当前 GUI 词表")

    def _perspective_glossary(self) -> Glossary:
        return self.perspective_external_glossary or self.glossary

    def _perspective_preview_async(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        try:
            source, _ = self._perspective_paths_from_ui()
            options = self._perspective_options_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        self._set_busy(True)
        self.status_var.set("扫描视角转换范围…")
        self.worker = threading.Thread(
            target=self._perspective_preview_worker,
            args=(source, options),
            daemon=True,
        )
        self.worker.start()

    def _perspective_preview_worker(self, source: Path, options: PerspectiveOptions):
        try:
            result = inspect_perspective_epub(source, options)
            self.after(0, lambda: self._on_perspective_preview_done(result))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_perspective_preview_done(self, result: dict[str, Any]):
        self._set_busy(False)
        self.status_var.set("视角转换范围扫描完成")
        self.perspective_preview_box.configure(state="normal")
        self.perspective_preview_box.delete("1.0", "end")
        self.perspective_preview_box.insert(
            "end",
            f"正文块：{result['total_blocks']}\n"
            f"可改写块：{result['eligible_blocks']}\n"
            f"保护块：{result['protected_blocks']}\n"
            f"扫描字符：{result['total_chars']}\n"
            f"涉及文件：{len(result['files'])}\n\n"
            "示例：\n"
            + "\n".join(result["samples"] or ["（无可改写正文块）"]),
        )
        self.perspective_preview_box.configure(state="disabled")

    def _perspective_start_async(self):
        try:
            source, output = self._perspective_paths_from_ui()
            options = self._perspective_options_from_ui()
            config = self._config_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("视角转换中…")
        self.log(f"开始第一人称改第三人称：{source.name}")
        self.worker = threading.Thread(
            target=self._perspective_start_worker,
            args=(source, output, options, config),
            daemon=True,
        )
        self.worker.start()

    def _perspective_start_worker(self, source: Path, output: Path, options: PerspectiveOptions, config: AppConfig):
        try:
            glossary = self._perspective_glossary()
            converter = PerspectiveConverter(
                LLMClient(config),
                options,
                glossary,
                cancel_event=self.cancel_event,
                progress_callback=lambda stage, done, total, message: self.after(
                    0,
                    lambda: self._set_progress(stage, done, total, message),
                ),
            )
            result = converter.convert(
                source,
                output,
                resume=True,
                state_path=self.perspective_state_override,
            )
            self.after(0, lambda: self._on_perspective_done(result))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_perspective_done(self, result: dict[str, Any]):
        self._set_busy(False)
        if result.get("cancelled"):
            total = result.get("total_blocks", 0)
            completed = result.get("completed_blocks", 0)
            self.progress.set((completed / total) if total else 0)
            self.status_var.set("视角转换已停止")
            self.log(
                f"视角转换已停止：已完成 {completed}/{total} 块；"
                f"已输出当前进度，剩余块可再次点击“开始转换”续传"
            )
            return
        self.progress.set(1)
        self.status_var.set("视角转换完成" if not result["failed_blocks"] else "视角转换完成（有失败）")
        self.log(
            f"视角转换完成：{result['completed_blocks']} 块成功，"
            f"{result['failed_blocks']} 块失败；输出 {result['output_path']}"
        )
        if result["failed_blocks"]:
            messagebox.showwarning(
                "完成（部分失败）",
                f"已输出，但有 {result['failed_blocks']} 个块失败并保留原文。\n"
                "可点“查看失败块”补写后再开始转换。",
                parent=self,
            )
        else:
            messagebox.showinfo("完成", f"第三人称 EPUB 已输出：\n{result['output_path']}", parent=self)

    def _perspective_open_failed(self):
        try:
            source, output = self._perspective_paths_from_ui()
            options = self._perspective_options_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        state_path = self.perspective_state_override or perspective_state_path(source, output)
        if not state_path.exists():
            messagebox.showinfo("提示", "还没有视角转换存档，请先开始转换。", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        try:
            state_data = load_perspective_state(state_path)
            saved_options = PerspectiveOptions(**state_data["options"]).normalized()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("存档无效", str(exc), parent=self)
            return
        dialog = PerspectiveFailedEditorDialog(
            self,
            state_path,
            self._config_from_ui(),
            self._perspective_glossary(),
            saved_options,
        )
        self.wait_window(dialog)

    def _perspective_reset_state(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        try:
            source, output = self._perspective_paths_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        state_path = self.perspective_state_override or perspective_state_path(source, output)
        if not state_path.exists():
            messagebox.showinfo("提示", "当前没有视角转换存档。", parent=self)
            return
        if not messagebox.askyesno("确认重置", "删除视角转换存档后，已完成的块也会重新调用模型。确定继续吗？", parent=self):
            return
        reset_perspective_state(source, output, state_path)
        if self.perspective_state_override == state_path:
            self.perspective_state_override = None
        self.log(f"视角转换存档已重置：{state_path.name}")
        self.status_var.set("视角转换存档已重置")

    def _build_bottom(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        status_bar = ctk.CTkFrame(parent, fg_color="transparent")
        status_bar.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        status_bar.grid_columnconfigure(1, weight=1)
        self.progress = ctk.CTkProgressBar(status_bar, width=240, height=8, corner_radius=4, progress_color=THEME["primary"])
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=(0, 12))
        self.status_var = tk.StringVar(value="就绪")
        ctk.CTkLabel(status_bar, textvariable=self.status_var, anchor="w", text_color=THEME["text_muted"]).grid(row=0, column=1, sticky="ew")
        self.start_btn = ctk.CTkButton(status_bar, text="▶ 开始翻译", width=120, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["accent"], hover_color=THEME["accent_hover"], command=self._start_translation)
        self.start_btn.grid(row=0, column=2, padx=6)
        self.stop_btn = ctk.CTkButton(status_bar, text="⏹ 停止", width=80, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["danger"], hover_color=THEME["danger_hover"], command=self._stop)
        self.stop_btn.grid(row=0, column=3, padx=6)
        self.stop_btn.configure(state="disabled")

        self.log_box = ctk.CTkTextbox(parent, height=110, corner_radius=6, border_width=1, border_color=THEME["card_border"], fg_color=THEME["bg"], text_color=THEME["text_muted"])
        self.log_box.grid(row=1, column=0, padx=14, pady=(4, 10), sticky="nsew")
        self.log_box.configure(state="disabled")

    # ---------------- 导出清洗器 ----------------
    def _open_sanitizer_dialog(self):
        self.sanitizer_enabled_var.set(self.sanitizer_config.enabled)
        dialog = SanitizerRuleDialog(
            self, self.sanitizer_config, on_apply=self._after_sanitizer_config
        )
        self.wait_window(dialog)

    def _after_sanitizer_config(self):
        self.sanitizer_config.enabled = self.sanitizer_enabled_var.get()
        self.log("清洗规则已更新")

    # ---------------- 成品矫正 ----------------
    def _fixer_browse(self):
        path = filedialog.askopenfilename(
            title="选择成品 EPUB",
            filetypes=[("EPUB", "*.epub")],
            parent=self,
        )
        if path:
            self.fixer_path_var.set(path)

    def _fixer_refresh_glossary(self):
        valid = len(self.glossary.valid_entries())
        confirmed = sum(1 for e in self.glossary.valid_entries() if e.confirmed)
        self.fixer_glossary_label.configure(
            text=f"当前词表：{valid} 条有效 / {confirmed} 条已确认"
        )

    def _fixer_set_busy(self, busy: bool):
        self.start_btn.configure(state="disabled" if busy else "normal")

    def _fixer_preview_async(self):
        epub = self.fixer_path_var.get().strip()
        if not epub:
            messagebox.showwarning("提示", "请先选择成品 EPUB", parent=self)
            return
        if not Path(epub).exists():
            messagebox.showerror("错误", "EPUB 文件不存在", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showwarning("提示", "当前词表为空，请先加载或提取词表", parent=self)
            return
        self._fixer_set_busy(True)
        self.status_var.set("预检变更…")
        self.worker = threading.Thread(
            target=self._fixer_preview_worker, args=(epub,), daemon=True
        )
        self.worker.start()

    def _fixer_preview_worker(self, epub):
        try:
            result = preview_finished_epub(epub, self.glossary)
            self.after(0, lambda: self._on_fixer_preview_done(result))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_fixer_preview_done(self, result):
        self._fixer_set_busy(False)
        self.status_var.set("预检完成")
        lines = result["lines"]
        self.fixer_preview.configure(state="normal")
        self.fixer_preview.delete("1.0", "end")
        if not lines:
            self.fixer_preview.insert(
                "end", "未发现需要修正的译名（当前已确认词条在本书中无命中）。\n"
            )
        else:
            self.fixer_preview.insert(
                "end",
                f"共命中 {result['total_hits']} 处，涉及 {len(result['files'])} 个正文文件：\n\n",
            )
            for line in lines:
                self.fixer_preview.insert("end", line + "\n")
        self.fixer_preview.configure(state="disabled")

    def _fixer_run_async(self):
        epub = self.fixer_path_var.get().strip()
        if not epub:
            messagebox.showwarning("提示", "请先选择成品 EPUB", parent=self)
            return
        if not Path(epub).exists():
            messagebox.showerror("错误", "EPUB 文件不存在", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showwarning("提示", "当前词表为空，请先加载或提取词表", parent=self)
            return
        mode = self.fixer_mode_var.get()
        self._fixer_set_busy(True)
        self.status_var.set("开始无损矫正…")
        self.worker = threading.Thread(
            target=self._fixer_run_worker, args=(epub, mode), daemon=True
        )
        self.worker.start()

    def _fixer_run_worker(self, epub, mode):
        try:
            src = Path(epub)
            if mode == "new":
                output = src.with_name(src.stem + "_fixed.epub")
            else:
                backup = src.with_name(src.stem + ".bak.epub")
                if backup.exists():
                    backup.unlink()
                shutil.copy2(src, backup)
                output = src
            result = fix_finished_epub_in_place(src, self.glossary, output)
            self.after(0, lambda: self._on_fixer_run_done(result, output, mode))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_fixer_run_done(self, result, output, mode):
        self._fixer_set_busy(False)
        self.status_var.set("矫正完成")
        msg = (
            f"矫正完成：修改 {result['modified_files']} 个文件，"
            f"共替换 {result['hit_count']} 处。\n"
            f"{'已另存为：' if mode == 'new' else '已原地覆盖（已备份 .bak）：'}{output}"
        )
        self.log(msg)
        messagebox.showinfo("完成", msg, parent=self)

    # ---------------- 基础操作 ----------------
    def _add_input_files(self):
        files = filedialog.askopenfilenames(
            title="选择书籍（可多选，列表顺序即卷序）",
            filetypes=[("书籍文件", "*.txt *.epub"), ("文本文件", "*.txt"), ("EPUB", "*.epub")],
            parent=self,
        )
        added = 0
        for raw in files:
            path = Path(raw)
            if path in self.input_files:
                continue
            self.input_files.append(path)
            added += 1
        if added:
            self.log(f"已添加 {added} 本书")
        self._refresh_input_tree()

    def _move_input_file(self, delta: int):
        selection = self.input_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        target = index + delta
        if 0 <= target < len(self.input_files):
            self.input_files[index], self.input_files[target] = (
                self.input_files[target],
                self.input_files[index],
            )
            self._refresh_input_tree()
            self.input_tree.selection_set(str(target))

    def _remove_input_files(self):
        for iid in sorted(self.input_tree.selection(), key=int, reverse=True):
            index = int(iid)
            if 0 <= index < len(self.input_files):
                del self.input_files[index]
        self._refresh_input_tree()

    def _clear_input_files(self):
        self.input_files = []
        self._refresh_input_tree()

    def _refresh_input_tree(self):
        for item in self.input_tree.get_children():
            self.input_tree.delete(item)
        for index, path in enumerate(self.input_files):
            display_name = unicodedata.normalize("NFC", path.name)
            self.input_tree.insert("", "end", iid=str(index), values=(index + 1, display_name))

    def _input_tree_tooltip(self, item: str, column_index: int) -> str | None:
        if column_index != 1:
            return None
        try:
            path = self.input_files[int(item)]
        except (ValueError, IndexError):
            return None
        return unicodedata.normalize("NFC", str(path))

    def _output_stem_for(self, index: int, path: Path) -> str:
        """多本输出文件名主名：填了小说名用“小说名 第X卷”，否则用源文件名（通常已含卷号）。"""
        novel = self.novel_name_var.get().strip()
        if novel:
            return f"{novel} 第{index + 1}卷"
        return path.stem

    def _choose_output(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_var.set(path)

    def log(self, message: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{stamp}] {message}")

    def _drain_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", message + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain_log_queue)

    def _set_busy(self, busy: bool):
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _config_from_ui(self) -> AppConfig:
        try:
            chunk_chars = int(self.chunk_var.get())
            workers = int(self.workers_var.get())
        except ValueError:
            raise ValueError("每批字符数和并发数必须是整数")
        self.sanitizer_config.enabled = self.sanitizer_enabled_var.get()
        data = self.app_config.to_dict()
        data.update({
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "chunk_chars": chunk_chars,
            "max_workers": workers,
            "extract_glossary": self.extract_var.get(),
            "output_txt": self.txt_var.get(),
            "output_epub": self.epub_var.get(),
            "sanitizer_config": self.sanitizer_config.to_dict(),
        })
        return AppConfig.from_dict(data)

    # ---------------- 词表 ----------------
    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for e in self.glossary.entries:
            alts_display = e.alternatives
            if e.alternatives and e.replace_short:
                alts_display = "含短称|" + e.alternatives
            self.tree.insert(
                "",
                "end",
                values=(e.ko, e.zh, e.kind, e.note, alts_display, "✓" if e.confirmed else ""),
            )
        self.count_label.configure(text=f"{len(self.glossary.valid_entries())} 条有效词条")
        if self.sort_col:
            self._apply_sort()

    def _sort_key(self, col: str, value: str):
        if col == "confirmed":
            return (1 if value.strip() == "✓" else 0,)
        return (value.strip(),)

    def _sort_by(self, col: str):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = False
        self._apply_sort()

    def _apply_sort(self):
        if not self.sort_col:
            return
        col = self.sort_col
        items = sorted(
            self.tree.get_children(""),
            key=lambda iid: self._sort_key(col, self.tree.set(iid, col)),
            reverse=self.sort_desc,
        )
        for index, iid in enumerate(items):
            self.tree.move(iid, "", index)
        for col_name in self.columns:
            text = self.headings[col_name]
            if col_name == self.sort_col:
                text += " ▼" if self.sort_desc else " ▲"
            self.tree.heading(col_name, text=text)

    def _selected_ko(self) -> str | None:
        selection = self.tree.selection()
        if not selection:
            return None
        values = self.tree.item(selection[0], "values")
        return str(values[0])

    def _add_entry(self):
        dialog = GlossaryEditDialog(self)
        self.wait_window(dialog)
        if dialog.result:
            self.glossary.upsert(dialog.result)
            self._refresh_tree()
            self.log(f"已添加词条：{dialog.result.ko} -> {dialog.result.zh}")

    def _edit_selected(self):
        ko = self._selected_ko()
        if not ko:
            messagebox.showinfo("提示", "请先选择一个词条", parent=self)
            return
        entry = next((e for e in self.glossary.entries if e.ko == ko), None)
        dialog = GlossaryEditDialog(self, entry)
        self.wait_window(dialog)
        if dialog.result:
            self.glossary.upsert(dialog.result)
            self._refresh_tree()

    def _remove_selected(self):
        ko = self._selected_ko()
        if not ko:
            return
        self.glossary.remove(ko)
        self._refresh_tree()

    def _confirm_selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择要确认的词条（可多选）", parent=self)
            return
        selected_kos = {str(self.tree.item(i, "values")[0]) for i in selection}
        changed = sum(1 for e in self.glossary.entries if e.ko in selected_kos and not e.confirmed)
        for e in self.glossary.entries:
            if e.ko in selected_kos:
                e.confirmed = True
        self._refresh_tree()
        self.log(f"已确认 {len(selected_kos)} 条（新增 {changed} 条）")

    def _confirm_all(self):
        if not self.glossary.entries:
            messagebox.showinfo("提示", "当前没有词表", parent=self)
            return
        changed = sum(1 for e in self.glossary.entries if not e.confirmed)
        for e in self.glossary.entries:
            e.confirmed = True
        self._refresh_tree()
        self.log(f"已全部确认：{len(self.glossary.entries)} 条（新增 {changed} 条）")

    def _save_glossary(self):
        if not self.glossary.entries:
            messagebox.showinfo("提示", "当前没有词表可保存", parent=self)
            return
        path = filedialog.asksaveasfilename(
            title="保存词表",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            self.glossary.save(Path(path))
            self.log(f"词表已保存：{path}")

    def _load_glossary(self):
        path = filedialog.askopenfilename(title="加载词表", filetypes=[("JSON", "*.json")])
        if path:
            self.glossary = Glossary.load(Path(path))
            removed = self.glossary.dedupe()
            self._refresh_tree()
            detail = f"，清理重复 {removed} 条" if removed else ""
            self.log(f"词表已加载：{path}，共 {len(self.glossary.entries)} 条{detail}")

    def _clear_glossary(self):
        """清空当前词表，便于换一本新书重新提取。"""
        if not self.glossary.entries:
            messagebox.showinfo("提示", "当前词表已是空的", parent=self)
            return
        if not messagebox.askyesno(
            "确认清空",
            "确定要清空当前词表吗？\n已确认的词条也会一并移除，此操作不可撤销。",
            parent=self,
        ):
            return
        self.glossary = Glossary()
        self._refresh_tree()
        self.log("词表已清空")

    def _extract_glossary_async(self):
        files = list(self.input_files)
        if not files:
            messagebox.showwarning("提示", "请先添加输入文件", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("提取词表中…")
        self._extract_round = 0
        self.log(f"开始提取词表：{len(files)} 本书")
        self.worker = threading.Thread(
            target=self._extract_worker, args=(files, "new", 0), daemon=True
        )
        self.worker.start()

    def _extract_more_glossary_async(self):
        files = list(self.input_files)
        if not files:
            messagebox.showwarning("提示", "请先添加输入文件", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showinfo("提示", "当前没有已加载的词表，请先“提取词表”或“加载词表”", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("提取更多词表中…")
        self._extract_round += 1
        self.log(
            f"开始基于现有词表补充提取（第 {self._extract_round} 轮）：{len(files)} 本书"
        )
        self.worker = threading.Thread(
            target=self._extract_worker,
            args=(files, "more", self._extract_round),
            daemon=True,
        )
        self.worker.start()

    def _extract_worker(self, files: list[Path], mode: str, sample_round: int = 0):
        """多本依次提取词表：mode=new 且当前词表为空时第一本新建，其余每本走“提取更多”追加；
        mode=more 时所有书都追加到现有词表；sample_round 用于“提取更多”的章节偏移。
        单本失败继续下一本，最后统一汇总。"""
        try:
            config = self._config_from_ui()
            glossary = self.glossary
            first_new = mode == "new" and not glossary.valid_entries()
            stats: list[dict] = []
            for index, path in enumerate(files):
                if self.cancel_event.is_set():
                    break
                name = path.name
                self.after(
                    0,
                    lambda i=index, n=len(files), nm=name: self.status_var.set(
                        f"提取词表：第 {i + 1}/{n} 本 {nm}"
                    ),
                )
                try:
                    book = load_book(path)
                    sample = collect_sample_text_strided(book, config, sample_round=sample_round)
                    report = sample_chapter_report(book, config, sample_round=sample_round)
                    round_text = f"第{sample_round}轮" if sample_round else ""
                    self.log(
                        f"第 {index + 1} 本 {name}：{round_text}样章 {len(sample)} 字；"
                        f"分章校验：{format_sample_chapters(report)}"
                    )
                    if not sample:
                        self.log(f"第 {index + 1} 本 {name}：{round_text}已无可抽样章节，跳过")
                        stats.append({"path": path, "mode": mode, "added": 0, "skipped": 0})
                        continue
                    llm = LLMClient(config)
                    if first_new and index == 0:
                        new_glossary = extract_glossary_with_llm(llm, sample, config.glossary_limit)
                        glossary = new_glossary
                        stats.append(
                            {"path": path, "mode": "new", "added": len(new_glossary.valid_entries())}
                        )
                    else:
                        new_glossary = extract_more_glossary(llm, sample, glossary, config.glossary_limit)
                        added, skipped = glossary.append_unique(new_glossary.valid_entries())
                        stats.append({"path": path, "mode": "more", "added": added, "skipped": skipped})
                except Exception as exc:  # noqa: BLE001
                    stats.append({"path": path, "error": str(exc)})
                    continue
            self.after(
                0,
                lambda: self._on_extract_all_done(glossary, stats, stopped=self.cancel_event.is_set()),
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_extract_all_done(self, glossary: Glossary, stats: list[dict], stopped: bool = False):
        self.glossary = glossary
        self._refresh_tree()
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("词表提取完成" if not stopped else "提取已停止")
        total_added = 0
        errors: list[str] = []
        for item in stats:
            if item.get("error"):
                errors.append(f"{item['path'].name}：{item['error']}")
            else:
                total_added += item.get("added", 0)
        self.log(f"提取完成：累计新增 {total_added} 条，当前共 {len(glossary.valid_entries())} 条")
        for error in errors:
            self.log(f"提取失败：{error}")
        lines = []
        for index, item in enumerate(stats):
            if item.get("error"):
                lines.append(f"· {index + 1}. {item['path'].name} 失败：{item['error']}")
            elif item["mode"] == "new":
                lines.append(f"· {index + 1}. {item['path'].name} 新建词表 {item['added']} 条")
            else:
                lines.append(
                    f"· {index + 1}. {item['path'].name} 新增 {item['added']} 条、跳过 {item['skipped']} 条"
                )
        if not stats:
            messagebox.showinfo("提示", "没有处理任何书籍（可能已停止）。", parent=self)
        elif errors:
            messagebox.showwarning(
                "完成（部分失败）",
                "\n".join(lines) + "\n\n失败的书已跳过，其余已追加到词表。",
                parent=self,
            )
        else:
            messagebox.showinfo("完成", "\n".join(lines), parent=self)

    def _enrich_glossary_async(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showinfo("提示", "当前没有词表，请先“提取词表”或“加载词表”", parent=self)
            return
        do_alts = self.enrich_alts_var.get()
        do_nick = self.enrich_nick_var.get()
        if not do_alts and not do_nick:
            messagebox.showinfo("提示", "请至少勾选“补可能译法”或“检测昵称”中的一项", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        steps = []
        if do_alts:
            steps.append("补充可能译法/备注")
        if do_nick:
            steps.append("检测人物昵称")
        self.status_var.set("完善词表中…")
        self.log("开始完善词表：" + " + ".join(steps))
        self.worker = threading.Thread(
            target=self._enrich_worker,
            args=(do_alts, do_nick),
            daemon=True,
        )
        self.worker.start()

    def _enrich_worker(self, do_alts: bool, do_nick: bool):
        try:
            config = self._config_from_ui()
            source_text = self.glossary.source_text
            if self.input_files:
                try:
                    book = load_book(self.input_files[0])
                    source_text = "\n".join(ch.text for ch in book.chapters)
                except Exception:  # noqa: BLE001
                    pass
            llm = LLMClient(config)
            stats = enrich_glossary_with_nicknames(
                llm,
                self.glossary,
                source_text,
                do_alts=do_alts,
                do_nick=do_nick,
            )
            self.after(0, lambda: self._on_enrich_done(stats))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_enrich_done(self, stats: dict[str, int]):
        self._refresh_tree()
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("词表完善完成")
        added_nick = stats.get("nickname_added", 0)
        nick_skip = stats.get("nickname_skipped", 0)
        nick_miss = stats.get("nickname_not_found", 0)
        updated_alts = stats.get("updated_alts", 0)
        updated_notes = stats.get("updated_notes", 0)
        self.log(
            f"完善完成：{updated_alts} 条补充/更新了可能译法，"
            f"{updated_notes} 条补充了备注，"
            f"新增昵称词条 {added_nick} 条（跳过 {nick_skip}，原文未找到 {nick_miss}）"
        )
        messagebox.showinfo(
            "完成",
            f"已完善 {updated_alts} 条词条的可能译法。\n"
            f"新增昵称词条 {added_nick} 条（kind=person-nickname）。\n请在表格中复核后确认。",
            parent=self,
        )


    def _cleanup_duplicates(self):
        if not self.glossary.entries:
            messagebox.showinfo("提示", "当前没有词表", parent=self)
            return
        removed = self.glossary.dedupe()
        self._refresh_tree()
        if removed:
            self.log(f"已清理重复词条：{removed} 条")
            messagebox.showinfo("完成", f"已清理 {removed} 条重复词条。", parent=self)
        else:
            messagebox.showinfo("提示", "没有发现重复词条。", parent=self)

    # ---------------- 多卷修正 ----------------
    def _add_merge_file(self):
        files = filedialog.askopenfilenames(
            title="选择翻译存档 JSON（可多选）",
            filetypes=[("JSON 存档", "*.json"), ("所有文件", "*.*")],
            parent=self,
        )
        added = 0
        for raw in files:
            path = Path(raw)
            if path in self.merge_files:
                continue
            try:
                inspect_state(path)
            except Exception as exc:  # noqa: BLE001
                messagebox.showwarning("无法读取", f"{path.name}：{exc}", parent=self)
                continue
            self.merge_files.append(path)
            added += 1
        if added:
            self.log(f"已添加 {added} 个翻译存档")
        self._refresh_merge_tree()

    def _move_merge_file(self, delta: int):
        selection = self.merge_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        target = index + delta
        if 0 <= target < len(self.merge_files):
            self.merge_files[index], self.merge_files[target] = (
                self.merge_files[target],
                self.merge_files[index],
            )
            self._refresh_merge_tree()
            self.merge_tree.selection_set(str(target))

    def _remove_merge_files(self):
        for iid in sorted(self.merge_tree.selection(), key=int, reverse=True):
            index = int(iid)
            if 0 <= index < len(self.merge_files):
                del self.merge_files[index]
        self._refresh_merge_tree()

    def _clear_merge_files(self):
        self.merge_files = []
        self._refresh_merge_tree()

    def _refresh_merge_tree(self):
        for item in self.merge_tree.get_children():
            self.merge_tree.delete(item)
        titles: list[str] = []
        for index, path in enumerate(self.merge_files):
            try:
                info = inspect_state(path)
                # 优先用存档文件名提取书名，取不到再退回原书/元数据名。
                display_title = archive_filename_title(path) or info["title"]
                detail = (
                    f"{display_title} · 完成 {info['completed']}/{info['total_chunks']} 块"
                )
                titles.append(display_title)
            except Exception as exc:  # noqa: BLE001
                detail = f"读取失败：{exc}"
            self.merge_tree.insert("", "end", iid=str(index), values=(index + 1, str(path), detail))
        if not self._merge_title_manual:
            detected = detect_merge_title(titles)
            if detected:
                self.merge_title_var.set(detected)

    def _on_merge_title_edited(self, _event=None):
        # 用户手动改动后，不再自动覆盖，保留其自定义书名。
        self._merge_title_manual = True

    def _merge_review_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        try:
            threshold = int(self.merge_review_threshold_var.get().strip())
            if threshold < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("输入无效", "单段韩文阈值必须是大于 0 的整数", parent=self)
            return
        selection = self.merge_tree.selection()
        files = [self.merge_files[int(iid)] for iid in selection] if selection else list(self.merge_files)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("复查中…")
        self.log(f"开始复查：{len(files)} 个存档，单段韩文阈值 {threshold} 字")
        self.worker = threading.Thread(target=self._merge_review_worker, args=(files, threshold), daemon=True)
        self.worker.start()

    def _merge_review_worker(self, files: list[Path], threshold: int):
        stats: list[dict] = []
        try:
            config = self._config_from_ui()
            for index, path in enumerate(files, start=1):
                if self.cancel_event.is_set():
                    break
                try:
                    result = review_translation_state(path, config, threshold=threshold)
                    stats.append({"path": path, **result})
                except Exception as exc:  # noqa: BLE001
                    stats.append({"path": path, "error": str(exc)})
                self.after(0, lambda value=index, total=len(files): self.progress.set(value / total))
            self.after(0, lambda: self._on_merge_review_done(stats, self.cancel_event.is_set()))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_merge_review_done(self, stats: list[dict], stopped: bool):
        self._set_busy(False)
        self.progress.set(1 if not stopped else 0)
        self.status_var.set("复查已停止" if stopped else "复查完成")
        self._refresh_merge_tree()
        flagged = 0
        missing = 0
        for item in stats:
            if item.get("error"):
                self.log(f"复查失败：{item['path'].name}：{item['error']}")
                continue
            flagged += item.get("flagged", 0)
            missing += item.get("missing", 0)
            self.log(f"复查：{item['path'].name} 检查 {item.get('reviewed', 0)} 块，转入失败块 {item.get('flagged', 0)} 块")
        if missing:
            self.log(f"复查注意：有 {missing} 块无法按当前原书定位，未修改存档")
        if not stopped:
            messagebox.showinfo("复查完成", f"共发现 {flagged} 个疑似漏译块。\n这些块已转入失败列表，可点击“失败块查看”或“校验并重试”。", parent=self)

    def _merge_audit_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        pattern = self.audit_pattern_box.get("1.0", "end").strip()
        pattern = pattern.strip()
        if not pattern:
            messagebox.showwarning("输入无效", "请填写失败定义正则", parent=self)
            return
        selection = self.merge_tree.selection()
        files = [self.merge_files[int(iid)] for iid in selection] if selection else list(self.merge_files)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("自检修复块中…")
        self.log(f"开始自检修复块：{len(files)} 个存档，规则 {pattern}")
        self.worker = threading.Thread(target=self._merge_audit_worker, args=(files, pattern), daemon=True)
        self.worker.start()

    def _merge_audit_worker(self, files: list[Path], pattern: str):
        stats: list[dict] = []
        try:
            config = self._config_from_ui()
            for index, path in enumerate(files, start=1):
                if self.cancel_event.is_set():
                    break
                try:
                    result = audit_translation_state(path, config, pattern=pattern)
                    stats.append({"path": path, **result})
                except Exception as exc:  # noqa: BLE001
                    stats.append({"path": path, "error": str(exc)})
                self.after(0, lambda value=index, total=len(files): self.progress.set(value / total))
            self.after(0, lambda: self._on_merge_audit_done(stats, self.cancel_event.is_set()))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_merge_audit_done(self, stats: list[dict], stopped: bool):
        self._set_busy(False)
        self.progress.set(1 if not stopped else 0)
        self.status_var.set("自检修复已停止" if stopped else "自检修复完成")
        self._refresh_merge_tree()
        flagged = 0
        missing = 0
        for item in stats:
            if item.get("error"):
                self.log(f"自检失败：{item['path'].name}：{item['error']}")
                continue
            flagged += item.get("flagged", 0)
            missing += item.get("missing", 0)
            self.log(f"自检：{item['path'].name} 检查 {item.get('reviewed', 0)} 块，转入失败块 {item.get('flagged', 0)} 块")
        if missing:
            self.log(f"自检注意：有 {missing} 块无法按当前原书定位，未修改存档")
        if not stopped:
            messagebox.showinfo("自检修复完成", f"共发现 {flagged} 个命中失败定义的块。\n这些块已转入失败列表，可点击“失败块查看”或“校验并重试”。", parent=self)

    # ---------------- 自检修复规则管理 ----------------
    def _get_audit_pattern(self) -> dict[str, Any] | None:
        name = self.audit_pattern_var.get().strip()
        if not name:
            return None
        return next((item for item in self.audit_patterns if item.get("name") == name), None)

    def _on_audit_pattern_selected(self, name):
        if self._audit_pattern_loading:
            return
        item = next((it for it in self.audit_patterns if it.get("name") == name), None)
        if item:
            self._audit_pattern_loading = True
            self.audit_pattern_box.delete("1.0", "end")
            self.audit_pattern_box.insert("end", str(item.get("pattern", "")))
            self._audit_pattern_loading = False

    def _save_audit_pattern(self):
        pattern = self.audit_pattern_box.get("1.0", "end").strip()
        if not pattern:
            messagebox.showwarning("提示", "请先在上方文本框填写失败定义正则", parent=self)
            return
        name = simpledialog.askstring("保存自检规则", "规则名称：", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        self.audit_patterns = [item for item in self.audit_patterns if item.get("name") != name]
        self.audit_patterns.append({"name": name, "pattern": pattern})
        self.audit_pattern_names = [str(item.get("name")) for item in self.audit_patterns]
        self.audit_pattern_var.set(name)
        self._refresh_audit_patterns()
        self._persist_audit_patterns()

    def _delete_audit_pattern(self):
        name = self.audit_pattern_var.get().strip()
        if not name:
            return
        old = len(self.audit_patterns)
        self.audit_patterns = [item for item in self.audit_patterns if item.get("name") != name]
        if len(self.audit_patterns) == old:
            return
        self.audit_pattern_names = [str(item.get("name")) for item in self.audit_patterns]
        if self.audit_pattern_names:
            self.audit_pattern_var.set(self.audit_pattern_names[0])
        else:
            self.audit_pattern_var.set("")
        self._refresh_audit_patterns()
        self._persist_audit_patterns()

    def _refresh_audit_patterns(self):
        self._audit_pattern_loading = True
        self.audit_pattern_menu.configure(values=self.audit_pattern_names)
        current = self.audit_pattern_var.get()
        if current and current in self.audit_pattern_names:
            item = next((it for it in self.audit_patterns if it.get("name") == current), None)
            if item:
                self.audit_pattern_box.delete("1.0", "end")
                self.audit_pattern_box.insert("end", str(item.get("pattern", "")))
        elif self.audit_pattern_names:
            self.audit_pattern_var.set(self.audit_pattern_names[0])
            item = next(it for it in self.audit_patterns if it.get("name") == self.audit_pattern_names[0])
            self.audit_pattern_box.delete("1.0", "end")
            self.audit_pattern_box.insert("end", str(item.get("pattern", "")))
        else:
            self.audit_pattern_box.delete("1.0", "end")
        self._audit_pattern_loading = False

    def _persist_audit_patterns(self):
        state = _load_ui_state()
        state["audit_patterns"] = self.audit_patterns
        _save_ui_state(state)

    def _merge_retry_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showinfo("提示", "当前没有词表，请先“提取词表”或“加载词表”", parent=self)
            return
        selection = self.merge_tree.selection()
        files = [self.merge_files[int(iid)] for iid in selection] if selection else list(self.merge_files)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("校验并重试中…")
        self.log(f"开始校验并重试：{len(files)} 个存档（每块最多重试 3 次）")
        self.worker = threading.Thread(target=self._merge_retry_worker, args=(files,), daemon=True)
        self.worker.start()

    def _merge_retry_worker(self, files: list[Path]):
        try:
            config = self._config_from_ui()
            translator = Translator(config, cancel_event=self.cancel_event)
            stats: list[dict] = []
            for path in files:
                if self.cancel_event.is_set():
                    break
                try:
                    result = translator.retry_failed(path, self.glossary, max_attempts=3)
                    stats.append(
                        {
                            "path": path,
                            "found": result["found"],
                            "recovered": result["recovered"],
                            "still_failed": result["still_failed"],
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    stats.append({"path": path, "error": str(exc)})
            self.after(
                0,
                lambda: self._on_merge_retry_done(stats, stopped=self.cancel_event.is_set()),
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_merge_retry_done(self, stats: list[dict], stopped: bool = False):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("校验重试已停止" if stopped else "校验重试完成")
        self._refresh_merge_tree()
        total_recovered = 0
        still = 0
        for item in stats:
            if item.get("error"):
                self.log(f"校验失败：{item['path'].name}：{item['error']}")
            else:
                total_recovered += item["recovered"]
                still += item["still_failed"]
                missing_hint = ""
                if item.get("missing"):
                    missing_hint = f"，找不到对应分块 {item['missing']} 块"
                self.log(
                    f"重试：{item['path'].name} 共 {item['found']} 块失败，"
                    f"恢复 {item['recovered']} 块，仍失败 {item['still_failed']} 块{missing_hint}"
                )
        missing_total = sum(item.get("missing", 0) for item in stats)
        if missing_total:
            self.log(
                f"注意：有 {missing_total} 块在存档里找不到对应原文（结构不匹配或原书已变化），"
                "请核对存档与原书是否一致；具体失败原因已写入各存档 JSON。"
            )
        if still:
            self.log("以下存档仍有失败块，将不参与“修正并输出”：")
            for item in stats:
                if item.get("still_failed"):
                    self.log(f"· {item['path'].name}（仍失败 {item['still_failed']} 块）")
        messagebox.showinfo(
            "校验重试完成",
            f"共恢复 {total_recovered} 块，仍失败 {still} 块。\n"
            "仍有失败块的存档不会参与“修正并输出”（详见日志）。",
            parent=self,
        )

    def _open_failed_editor(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        selection = self.merge_tree.selection()
        files = [self.merge_files[int(iid)] for iid in selection] if selection else list(self.merge_files)
        try:
            config = self._config_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", str(exc), parent=self)
            return
        dialog = FailedChunkEditorDialog(self, files, config, self.glossary)
        self.wait_window(dialog)

    def _merge_repair_images_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        selection = self.merge_tree.selection()
        files = [self.merge_files[int(iid)] for iid in selection] if selection else list(self.merge_files)
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("图片补集中…")
        self.log(f"开始图片补集：检测 {len(files)} 个翻译存档与原 EPUB")
        self.worker = threading.Thread(target=self._merge_repair_images_worker, args=(files,), daemon=True)
        self.worker.start()

    def _merge_repair_images_worker(self, files: list[Path]):
        try:
            config = self._config_from_ui()
            stats = []
            for path in files:
                if self.cancel_event.is_set():
                    break
                stats.append(repair_image_state(path, config))
            self.after(0, lambda: self._on_merge_repair_images_done(stats, self.cancel_event.is_set()))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_merge_repair_images_done(self, stats: list[dict], stopped: bool):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("图片补集已停止" if stopped else "图片补集完成")
        self._refresh_merge_tree()
        added = sum(int(item.get("added", 0)) for item in stats)
        for item in stats:
            self.log(f"图片补集：{item['path'].name} 新增 {item.get('added', 0)} 个图片位置")
        messagebox.showinfo("图片补集完成", f"共补回 {added} 个图片位置。\n现在可点击“修正并输出”重新生成 EPUB。", parent=self)

    def _merge_preview_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showinfo("提示", "当前没有词表，请先“提取词表”或“加载词表”", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("预览修正中…")
        self.log(f"开始预览：{len(self.merge_files)} 个存档，按当前词表统计替换")
        self.worker = threading.Thread(target=self._merge_worker, args=("preview",), daemon=True)
        self.worker.start()

    def _merge_run_async(self):
        if not self.merge_files:
            messagebox.showinfo("提示", "请先添加翻译存档", parent=self)
            return
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("提示", "请先选择输出目录", parent=self)
            return
        if not self.glossary.valid_entries():
            messagebox.showinfo("提示", "当前没有词表，请先“提取词表”或“加载词表”", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        if not self.merge_txt_var.get() and not self.merge_epub_var.get():
            messagebox.showinfo("提示", "请至少勾选“输出 TXT”或“输出 EPUB”", parent=self)
            return
        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("修正并输出中…")
        self.log(f"开始修正并输出：{len(self.merge_files)} 个存档拼合")
        self.worker = threading.Thread(target=self._merge_worker, args=("run",), daemon=True)
        self.worker.start()

    def _merge_worker(self, mode: str):
        try:
            config = self._config_from_ui()
            files = list(self.merge_files)
            if mode == "run":
                usable: list[Path] = []
                for path in files:
                    info = inspect_state(path)
                    if info["failed"]:
                        self.log(
                            f"跳过：{path.name} 仍有 {info['failed']} 块失败，不参与输出"
                            "（请先点“校验并重试”补翻）"
                        )
                    else:
                        usable.append(path)
                if not usable:
                    raise ValueError("没有可输出的存档：所选存档都存在失败块，请先点“校验并重试”")
                files = usable
            books = [book_from_state(p, config) for p in files]
            self.after(0, lambda: self.progress.set(0.6))
            if mode == "preview":
                merged = merge_books(books)
                stats = preview_fix(merged, self.glossary)
                self.after(0, lambda: self._on_merge_preview_done(stats))
            else:
                result = export_merged(
                    books,
                    self.glossary,
                    config,
                    Path(self.output_var.get().strip()),
                    title=self.merge_title_var.get(),
                    output_txt=self.merge_txt_var.get(),
                    output_epub=self.merge_epub_var.get(),
                )
                self.after(0, lambda: self._on_merge_run_done(result))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _on_merge_preview_done(self, stats: dict[str, int]):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("预览完成")
        self.log(
            f"预览：预计命中 {stats['hit_paragraphs']} 个段落、"
            f"{stats['hit_sources']} 种旧写法"
        )
        messagebox.showinfo(
            "预览",
            f"预计将修正 {stats['hit_paragraphs']} 个段落，"
            f"涉及 {stats['hit_sources']} 种旧写法。\n点“修正并输出”执行。",
            parent=self,
        )

    def _on_merge_run_done(self, result: dict):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("修正输出完成")
        paths = result["paths"]
        self.log(
            f"修正输出完成：{result['chapters']} 章、{result['paragraphs']} 段，"
            f"命中 {result['hit_paragraphs']} 个段落"
        )
        for path in paths:
            self.log(f"已输出：{path}")
        messagebox.showinfo(
            "完成",
            f"已合并 {result['chapters']} 章，修正命中 {result['hit_paragraphs']} 个段落。\n"
            + "\n".join(f"· {p}" for p in paths),
            parent=self,
        )
    # ---------------- 翻译 ----------------
    def _start_translation(self):
        files = [Path(p) for p in self.input_files]
        output_dir = self.output_var.get().strip()
        if not files:
            messagebox.showwarning("提示", "请先添加输入文件", parent=self)
            return
        if not output_dir:
            messagebox.showwarning("提示", "请选择输出目录", parent=self)
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return

        try:
            config = self._config_from_ui()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return

        self.cancel_event.clear()
        self._set_busy(True)
        self.progress.set(0)
        self.status_var.set("准备翻译…")
        self.log(f"开始翻译：{len(files)} 本书")
        glossary = self.glossary
        self.worker = threading.Thread(
            target=self._translation_worker,
            args=(files, Path(output_dir), config, glossary),
            daemon=True,
        )
        self.worker.start()

    def _translation_worker(
        self,
        files: list[Path],
        output_dir: Path,
        config: AppConfig,
        glossary: Glossary,
    ):
        """多本逐本翻译：失败继续下一本，最后汇总失败清单；每本按“小说名 第X卷”或源文件名输出。"""
        total_books = len(files)
        results: list[TranslationResult] = []
        failed_books: list[dict] = []
        cancelled = False
        try:
            for index, path in enumerate(files):
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                name = path.name
                self.after(
                    0,
                    lambda i=index, n=total_books, nm=name: self.status_var.set(
                        f"翻译中：第 {i + 1}/{n} 本 {nm}"
                    ),
                )

                def progress_cb(stage, done, total, message, i=index, n=total_books):
                    ratio = (done / total) if total else 0
                    self.after(
                        0,
                        lambda s=stage, d=done, t=total, m=message, r=ratio, i=i, n=n: self._set_progress(
                            s, d, t, m, ratio=r, book_index=i, total_books=n
                        ),
                    )

                translator = Translator(
                    config,
                    progress_callback=progress_cb,
                    cancel_event=self.cancel_event,
                )

                # 翻译前自动提取词表：空词表时第一本新建、其余追加。
                if config.extract_glossary and not glossary.valid_entries():
                    try:
                        llm = LLMClient(config)
                        book = load_book(path)
                        sample = collect_sample_text_strided(book, config)
                        report = sample_chapter_report(book, config)
                        self.log(
                            f"自动提取词表：{name} 样章 {len(sample)} 字；"
                            f"分章校验：{format_sample_chapters(report)}"
                        )
                        if index == 0:
                            glossary = extract_glossary_with_llm(llm, sample, config.glossary_limit)
                            self.log(f"已新建词表：{len(glossary.valid_entries())} 条")
                        else:
                            new_glossary = extract_more_glossary(llm, sample, glossary, config.glossary_limit)
                            added, skipped = glossary.append_unique(new_glossary.valid_entries())
                            self.log(f"已追加词表：新增 {added} 条、跳过 {skipped} 条")
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"自动提取词表失败（继续翻译）：{name}：{exc}")

                try:
                    result = translator.translate_file(
                        path,
                        output_dir,
                        glossary,
                        output_stem=self._output_stem_for(index, path),
                    )
                    results.append(result)
                    self.log(
                        f"完成：{name} 成功 {result.completed_chunks}/{result.total_chunks} 块，"
                        f"失败 {result.failed_chunks} 块"
                    )
                    if result.failed_chunks:
                        failed_books.append(
                            {"path": path, "reason": f"{result.failed_chunks} 块失败（已保留原文并输出）"}
                        )
                except TranslationCancelled:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    failed_books.append({"path": path, "reason": str(exc)})
                    self.log(f"失败：{name}：{exc}")
                    continue
            self.after(
                0,
                lambda: self._on_translation_batch_done(results, failed_books, cancelled=cancelled),
            )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))

    def _set_progress(
        self,
        stage: str,
        done: int,
        total: int,
        message: str,
        *,
        ratio: float | None = None,
        book_index: int | None = None,
        total_books: int | None = None,
    ):
        if ratio is None:
            ratio = (done / total) if total else 0
        if book_index is not None and total_books:
            ratio = (book_index + ratio) / total_books
        self.progress.set(max(0.0, min(1.0, ratio)))
        if book_index is not None and total_books and book_index + 1 < total_books:
            label = f"第 {book_index + 1}/{total_books} 本 · {stage}"
        else:
            label = stage
        self.status_var.set(f"{label}：{done}/{total}  {message}")
        if message and (done == 0 or done == total):
            self.log(f"{label}：{message}")

    def _on_translation_batch_done(
        self,
        results: list[TranslationResult],
        failed_books: list[dict],
        cancelled: bool = False,
    ):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("已停止" if cancelled else "翻译完成")
        total_ok = sum(r.completed_chunks for r in results)
        for result in results:
            for path in result.output_paths:
                self.log(f"输出：{path}")
        if cancelled:
            self.log(f"任务已停止：完成 {len(results)} 本，失败/未完成 {len(failed_books)} 本")
            return
        if failed_books:
            self.log(f"翻译完成（有失败）：成功 {len(results)} 本，失败 {len(failed_books)} 本")
            for item in failed_books:
                self.log(f"失败：{item['path'].name}：{item['reason']}")
            messagebox.showwarning(
                "完成（部分失败）",
                f"成功 {len(results)} 本，失败 {len(failed_books)} 本。\n"
                + "\n".join(f"· {item['path'].name}：{item['reason']}" for item in failed_books)
                + "\n\n失败块可在“多卷修正”tab 添加对应存档后点“校验并重试”补翻。",
                parent=self,
            )
        else:
            self.log(f"翻译完成：{len(results)} 本，累计成功 {total_ok} 块")
            messagebox.showinfo("完成", f"全部完成：{len(results)} 本。", parent=self)

    def _on_cancelled(self):
        self._set_busy(False)
        self.status_var.set("已停止")
        self.log("任务已停止，已完成块已保存，可再次开始续传")

    def _on_error(self, message: str):
        self._set_busy(False)
        self.status_var.set("出错")
        self.log(f"错误：{message}")
        messagebox.showerror("错误", message, parent=self)

    # ---------------- 窗口尺寸记忆 ----------------
    def _legacy_geometry_to_logical(self, geom: str) -> str:
        """把旧版保存的物理像素几何字符串换算为逻辑单位。"""
        try:
            scale = float(self._get_window_scaling() or 1.0)
        except Exception:
            scale = 1.0
        match = re.match(r"^(\d+)x(\d+)([+-]\d+[+-]\d+)?$", geom)
        if not match:
            return geom
        width = round(int(match.group(1)) / scale)
        height = round(int(match.group(2)) / scale)
        return f"{width}x{height}{match.group(3) or ''}"

    def _default_geometry(self) -> str:
        """无历史几何时，按当前屏幕展开一个接近全屏且保留左右分栏的大窗口。"""
        sw, sh = self._screen_logical_size()
        w = max(880, int(sw * 0.90))
        h = max(620, int(sh * 0.86))
        return f"{w}x{h}"

    def _screen_logical_size(self) -> tuple[int, int]:
        """返回以逻辑单位计的可视区域宽高，便于把窗口钳制到屏幕范围内。"""
        try:
            scale = float(self._get_window_scaling() or 1.0)
        except Exception:
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        return int(self.winfo_screenwidth() / scale), int(self.winfo_screenheight() / scale)

    def _clamp_geometry(self, geom: str) -> str:
        """把逻辑几何的宽高裁剪到屏幕范围（保留位置偏移），避免小屏下端头被裁掉。"""
        sw, sh = self._screen_logical_size()
        match = re.match(r"^(\d+)x(\d+)([+-]\d+[+-]\d+)?$", geom)
        if not match:
            return geom
        w = int(match.group(1))
        h = int(match.group(2))
        w = min(w, max(sw - 40, 600))
        h = min(h, max(sh - 40, 480))
        return f"{w}x{h}{match.group(3) or ''}"

    def _clamp_min_size(self, w: int, h: int) -> tuple[int, int]:
        sw, sh = self._screen_logical_size()
        return min(w, max(sw - 40, 480)), min(h, max(sh - 40, 360))

    def _on_window_configure(self, event):
        # 只处理主窗口自身的变化，忽略子控件触发的大量 Configure。
        if event.widget is not self:
            return
        if self._geometry_save_after_id is not None:
            self.after_cancel(self._geometry_save_after_id)
        self._geometry_save_after_id = self.after(600, self._save_window_geometry)

    def _save_window_geometry(self):
        self._geometry_save_after_id = None
        try:
            # 用 CTk 的 getter 拿“逻辑单位”，与 CTk.geometry() 的缩放方向一致，
            # 避免 winfo_geometry() 的物理像素在 DPI 缩放屏上二次放大。
            state = _load_ui_state()
            state["geometry"] = self.geometry()
            state["window_state"] = self.state()
            state["left_panel_width"] = int(getattr(self, "_left_panel_width", _DEFAULT_LEFT_WIDTH) or _DEFAULT_LEFT_WIDTH)
            _save_ui_state(state)
        except Exception:
            pass

    def _restore_zoomed_state(self):
        try:
            self.state("zoomed")
        except Exception:
            pass

    def _on_close(self):
        self._save_window_geometry()
        try:
            state = _load_ui_state()
            state["sanitizer_config"] = self.sanitizer_config.to_dict()
            state["app_config"] = self._config_from_ui().to_dict()
            state["output"] = self.output_var.get().strip()
            _save_ui_state(state)
        except Exception:
            pass
        if self.worker and self.worker.is_alive():
            if messagebox.askyesno("退出确认", "当前正在执行翻译任务，确定要强制退出吗？", parent=self):
                self.cancel_event.set()
                self.destroy()
        else:
            self.destroy()

    def _stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.log("正在停止…")


def run_gui():
    app = App()
    app.mainloop()
