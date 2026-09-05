# 由 tools/split_class.py 拆分生成

from __future__ import annotations

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
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from typing import Any
from ...sanitizer import (CustomRule, ExportSanitizer, SanitizerConfig)
from ...book import load_book
from ...config import AppConfig
from ...epub_fixer import (fix_finished_epub_in_place, preview_finished_epub)
from ...glossary import (Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary)
from ...llm import (LLMCancelled, LLMClient)
from ...merge import (archive_filename_title, audit_translation_state, book_from_state, detect_merge_title, export_merged, inspect_state, merge_books, preview_fix, repair_image_state, review_translation_state)
from ...perspective import (PerspectiveBlock, PerspectiveConverter, PerspectiveFailedBlock, PerspectiveOptions, inspect_perspective_epub, load_failed_perspective_blocks, load_perspective_state, perspective_state_path, reset_perspective_state, rewrite_blocks, save_perspective_failure, save_manual_perspective_translation)
from ...translator import (FailedChunk, TranslationCancelled, TranslationResult, Translator, collect_sample_text_strided, detect_malformed_blocks, format_sample_chapters, load_failed_chunks, reconcile_paragraphs, save_manual_translation, sample_chapter_report)
from ...utils import (extract_json, parse_paragraphs_from_payload)
from ..theme import (THEME, _apply_ttk_theme)
from ..state import (_load_ui_state, _save_ui_state)
from ..widgets import (TreeviewTooltip, DebouncedScrollableFrame)
from ..dialogs import (GlossaryEditDialog, SanitizerRuleDialog, FailedChunkEditorDialog, MalformedBlockEditorDialog, PerspectiveFailedEditorDialog)


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class SettingsMixin:

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
