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


class FixerMixin:

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
