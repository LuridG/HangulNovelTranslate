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
from ...epub_correction import (
    DEFAULT_BODY_TITLE_PATTERN,
    DEFAULT_PREFIX_PATTERN,
    apply_add_format,
    apply_clear_format,
    apply_reassemble,
    apply_regex_resplit,
    detect_format,
    infer_body_title_match,
    infer_placeholder_pattern,
    preview_add_format,
    preview_clear_format,
    preview_reassemble,
    preview_regex_resplit,
)
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
        parent.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            parent,
            text="成品 EPUB 无损矫正（词表 / 标题 / 格式整理）",
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
            mode_frame, text="另存为新文件（按操作自动命名）", variable=self.fixer_mode_var, value="new"
        ).grid(row=0, column=0, padx=4, sticky="w")
        ctk.CTkRadioButton(
            mode_frame, text="原地覆盖（自动生成 .bak 备份）", variable=self.fixer_mode_var, value="overwrite"
        ).grid(row=0, column=1, padx=(18, 4), sticky="w")

        self.fixer_view = ctk.CTkTabview(parent)
        self.fixer_view.grid(row=3, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self._fixer_action_buttons: list = []
        self._build_fixer_glossary_subtab(self.fixer_view.add("词表矫正"))
        self._build_fixer_resplit_subtab(self.fixer_view.add("标题重分"))
        self._build_fixer_reassemble_subtab(self.fixer_view.add("标题重组"))
        self._build_fixer_format_subtab(self.fixer_view.add("格式整理"))
        self.fixer_view.configure(
            corner_radius=6,
            border_width=0,
            segmented_button_fg_color=THEME["card_alt"],
            segmented_button_selected_color=THEME["primary"],
            segmented_button_selected_hover_color=THEME["primary_hover"],
            segmented_button_unselected_color=THEME["card_alt"],
            segmented_button_unselected_hover_color=THEME["secondary_hover"],
        )
        self.fixer_view.set("词表矫正")
        self._fixer_refresh_glossary()


    def _fixer_subtab_frame(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)


    def _fixer_action_btn(self, parent, **kwargs):
        btn = ctk.CTkButton(parent, **kwargs)
        self._fixer_action_buttons.append(btn)
        return btn


    def _render_fixer_preview(self, textbox, content: str):
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        textbox.insert("end", content)
        textbox.configure(state="disabled")


    def _build_fixer_glossary_subtab(self, tab):
        self._fixer_subtab_frame(tab)
        run_frame = ctk.CTkFrame(tab, fg_color="transparent")
        run_frame.grid(row=0, column=0, padx=8, pady=(8, 6), sticky="ew")
        run_frame.grid_columnconfigure(3, weight=1)
        self._fixer_action_btn(
            run_frame, text="🔍 预检变更", width=104, command=self._fixer_preview_async,
        ).grid(row=0, column=0, padx=4)
        self._fixer_action_btn(
            run_frame, text="⚡ 开始无损矫正", width=132,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._fixer_run_async,
        ).grid(row=0, column=1, padx=4)
        self._fixer_action_btn(
            run_frame, text="刷新词表统计", width=104,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._fixer_refresh_glossary,
        ).grid(row=0, column=2, padx=4)
        self.fixer_preview = ctk.CTkTextbox(tab, height=320)
        self.fixer_preview.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.fixer_preview.configure(state="disabled")


    def _build_fixer_resplit_subtab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        # 顶部说明 + 正则
        ctk.CTkLabel(
            tab, text="针对 txt 自动切分导致标题与原文无关的情况：清除旧标题，按下面正则重新断章。",
            text_color=THEME["text_muted"], anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(8, 0), sticky="w")
        opt = ctk.CTkFrame(tab, fg_color="transparent")
        opt.grid(row=1, column=0, padx=8, pady=(6, 4), sticky="ew")
        opt.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(opt, text="章节标题正则").grid(row=0, column=0, padx=(4, 6), sticky="w")
        self.fixer_resplit_regex_var = tk.StringVar(value=r"^第\s*(\d+)\s*章")
        ctk.CTkEntry(opt, textvariable=self.fixer_resplit_regex_var).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        self.fixer_resplit_clean_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt, text="清理旧格式（字数行/制作说明）", variable=self.fixer_resplit_clean_var,
        ).grid(row=1, column=0, columnspan=2, padx=4, pady=(4, 0), sticky="w")
        self.fixer_resplit_group_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opt, text="用正则第 1 分组作为标题", variable=self.fixer_resplit_group_var,
        ).grid(row=2, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="w")

        run_frame = ctk.CTkFrame(tab, fg_color="transparent")
        run_frame.grid(row=2, column=0, padx=8, pady=(2, 6), sticky="ew")
        self._fixer_action_btn(
            run_frame, text="🔍 预检断章", width=104, command=self._fixer_resplit_preview_async,
        ).grid(row=0, column=0, padx=4)
        self._fixer_action_btn(
            run_frame, text="⚡ 执行重分", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._fixer_resplit_run_async,
        ).grid(row=0, column=1, padx=4)

        self.fixer_resplit_preview = ctk.CTkTextbox(tab, height=320)
        self.fixer_resplit_preview.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.fixer_resplit_preview.configure(state="disabled")
        tab.grid_rowconfigure(3, weight=1)


    def _build_fixer_reassemble_subtab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tab, text="针对 EP.0 等占位标题：清除每章字数统计后，从正文开头提取真正的短标题并重组。",
            text_color=THEME["text_muted"], anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(8, 0), sticky="w")
        opt = ctk.CTkFrame(tab, fg_color="transparent")
        opt.grid(row=1, column=0, padx=8, pady=(6, 4), sticky="ew")
        opt.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(opt, text="占位标题正则").grid(row=0, column=0, padx=(4, 6), sticky="w")
        self.fixer_reassemble_prefix_var = tk.StringVar(value=DEFAULT_PREFIX_PATTERN)
        ctk.CTkEntry(opt, textvariable=self.fixer_reassemble_prefix_var).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        ctk.CTkButton(
            opt, text="↺", width=30,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"],
            command=lambda: self.fixer_reassemble_prefix_var.set(DEFAULT_PREFIX_PATTERN),
        ).grid(row=0, column=2, padx=2)
        ctk.CTkLabel(opt, text="正文标题正则").grid(row=1, column=0, padx=(4, 6), pady=(4, 0), sticky="w")
        self.fixer_reassemble_title_var = tk.StringVar(value=DEFAULT_BODY_TITLE_PATTERN)
        ctk.CTkEntry(opt, textvariable=self.fixer_reassemble_title_var).grid(
            row=1, column=1, padx=4, pady=(4, 0), sticky="ew"
        )
        ctk.CTkButton(
            opt, text="↺", width=30,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"],
            command=lambda: self.fixer_reassemble_title_var.set(DEFAULT_BODY_TITLE_PATTERN),
        ).grid(row=1, column=2, padx=2, pady=(4, 0))
        self.fixer_reassemble_clean_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt, text="先批量清除每章字数统计", variable=self.fixer_reassemble_clean_var,
        ).grid(row=2, column=0, columnspan=2, padx=4, pady=(4, 0), sticky="w")
        self.fixer_reassemble_remove_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt, text="提取标题后从正文移除该行", variable=self.fixer_reassemble_remove_var,
        ).grid(row=3, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="w")
        self.fixer_reassemble_keep_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opt, text="保留原占位编号前缀（如 EP.1 起因 (1)）", variable=self.fixer_reassemble_keep_var,
        ).grid(row=4, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="w")

        run_frame = ctk.CTkFrame(tab, fg_color="transparent")
        run_frame.grid(row=2, column=0, padx=8, pady=(2, 6), sticky="ew")
        self._fixer_action_btn(
            run_frame, text="🤖 LLM 提取标题尝试", width=160,
            fg_color=THEME["accent"], hover_color=THEME["accent_hover"],
            command=self._fixer_reassemble_llm_async,
        ).grid(row=0, column=0, padx=4)
        self._fixer_action_btn(
            run_frame, text="🔍 检测结构", width=100, command=self._fixer_reassemble_detect_async,
        ).grid(row=0, column=1, padx=4)
        self._fixer_action_btn(
            run_frame, text="预检重组", width=96, command=self._fixer_reassemble_preview_async,
        ).grid(row=0, column=2, padx=4)
        self._fixer_action_btn(
            run_frame, text="⚡ 执行重组", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._fixer_reassemble_run_async,
        ).grid(row=0, column=3, padx=4)

        self.fixer_reassemble_preview = ctk.CTkTextbox(tab, height=320)
        self.fixer_reassemble_preview.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.fixer_reassemble_preview.configure(state="disabled")
        tab.grid_rowconfigure(3, weight=1)


    def _build_fixer_format_subtab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tab, text="检测旧制作说明与每章字数统计；可一键清理，也可按新版中文 EPUB 统计重新生成。",
            text_color=THEME["text_muted"], anchor="w",
        ).grid(row=0, column=0, padx=8, pady=(8, 0), sticky="w")
        opt = ctk.CTkFrame(tab, fg_color="transparent")
        opt.grid(row=1, column=0, padx=8, pady=(6, 4), sticky="ew")
        self.fixer_format_prod_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt, text="清理制作说明章节", variable=self.fixer_format_prod_var,
        ).grid(row=0, column=0, padx=4, sticky="w")
        self.fixer_format_wc_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opt, text="清理每章字数统计", variable=self.fixer_format_wc_var,
        ).grid(row=0, column=1, padx=(18, 4), sticky="w")

        run_frame = ctk.CTkFrame(tab, fg_color="transparent")
        run_frame.grid(row=2, column=0, padx=8, pady=(2, 6), sticky="ew")
        self._fixer_action_btn(
            run_frame, text="🔍 检测格式", width=104, command=self._fixer_format_detect_async,
        ).grid(row=0, column=0, padx=4)
        self._fixer_action_btn(
            run_frame, text="清理旧格式", width=104,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._fixer_format_clear_async,
        ).grid(row=0, column=1, padx=4)
        self._fixer_action_btn(
            run_frame, text="新增格式", width=104,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._fixer_format_add_async,
        ).grid(row=0, column=2, padx=4)

        self.fixer_format_preview = ctk.CTkTextbox(tab, height=320)
        self.fixer_format_preview.grid(row=3, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.fixer_format_preview.configure(state="disabled")
        tab.grid_rowconfigure(3, weight=1)


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
        for btn in getattr(self, "_fixer_action_buttons", []):
            btn.configure(state="disabled" if busy else "normal")


    def _fixer_output_path(self, src: Path, mode: str, suffix: str) -> tuple[Path, str]:
        if mode == "overwrite":
            backup = src.with_name(src.stem + ".bak.epub")
            if backup.exists():
                backup.unlink()
            shutil.copy2(src, backup)
            return src, "overwrite"
        return src.with_name(src.stem + suffix + ".epub"), "new"


    def _fixer_dispatch(self, fn, on_done):
        self._fixer_set_busy(True)
        self.status_var.set("正在处理…")

        def worker():
            try:
                result = fn()
                self.after(0, lambda: on_done(result))
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self.after(0, lambda: self._on_error(message))

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()


    def _fixer_offer_switch(self, msg: str, output, original) -> None:
        """操作成功后提示，并询问是否把「成品 EPUB 路径」切换到新文件。"""
        output = Path(output)
        original = Path(original)
        if output.resolve() == original.resolve():
            messagebox.showinfo("完成", msg, parent=self)
            return
        switch = messagebox.askyesno(
            "完成",
            msg
            + "\n\n如果还需要进行其他步骤，建议将「成品 EPUB 路径」切换为输出的新文件：\n"
            + f"{output}\n\n是否立即切换？",
            parent=self,
        )
        if switch:
            self.fixer_path_var.set(str(output))


    def _fixer_require_path(self, title: str = "提示") -> str | None:
        epub = self.fixer_path_var.get().strip()
        if not epub:
            messagebox.showwarning(title, "请先选择成品 EPUB", parent=self)
            return None
        if not Path(epub).exists():
            messagebox.showerror("错误", "EPUB 文件不存在", parent=self)
            return None
        return epub


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
            self.after(0, lambda: self._on_fixer_run_done(result, output, mode, src))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))


    def _on_fixer_run_done(self, result, output, mode, src):
        self._fixer_set_busy(False)
        self.status_var.set("矫正完成")
        msg = (
            f"矫正完成：修改 {result['modified_files']} 个文件，"
            f"共替换 {result['hit_count']} 处。\n"
            f"{'已另存为：' if mode == 'new' else '已原地覆盖（已备份 .bak）：'}{output}"
        )
        self.log(msg)
        self._fixer_offer_switch(msg, output, src)


    # ---------------- 标题重分（正则重新断章） ----------------
    def _fixer_resplit_preview_async(self):
        epub = self._fixer_require_path()
        if epub is None:
            return
        pattern = self.fixer_resplit_regex_var.get().strip()
        if not pattern:
            messagebox.showwarning("提示", "请输入章节标题正则", parent=self)
            return
        skip = self.fixer_resplit_clean_var.get()
        group = self.fixer_resplit_group_var.get()
        self._fixer_dispatch(
            lambda: preview_regex_resplit(
                epub, pattern, skip_format=skip, use_group=group
            ),
            self._on_fixer_resplit_preview_done,
        )


    def _on_fixer_resplit_preview_done(self, result):
        self._fixer_set_busy(False)
        self.status_var.set("预检断章完成")
        if not result["chapters"]:
            self._render_fixer_preview(
                self.fixer_resplit_preview,
                "未命中任何标题，全书将作为单章「正文」输出。\n",
            )
            return
        lines = [f"命中 {result['title_count']} 个标题，重新断章后共 {result['title_count']} 章：\n"]
        for chapter in result["chapters"]:
            lines.append(f"· {chapter['title']}（{chapter['paragraphs']} 段）")
        self._render_fixer_preview(self.fixer_resplit_preview, "\n".join(lines))


    def _fixer_resplit_run_async(self):
        epub = self._fixer_require_path()
        if epub is None:
            return
        pattern = self.fixer_resplit_regex_var.get().strip()
        if not pattern:
            messagebox.showwarning("提示", "请输入章节标题正则", parent=self)
            return
        src = Path(epub)
        mode = self.fixer_mode_var.get()
        output, _ = self._fixer_output_path(src, mode, "_resplit")
        skip = self.fixer_resplit_clean_var.get()
        group = self.fixer_resplit_group_var.get()
        self._fixer_dispatch(
            lambda: apply_regex_resplit(
                epub, output, pattern, skip_format=skip, use_group=group
            ),
            lambda result: self._on_fixer_resplit_run_done(result, output, src),
        )


    def _on_fixer_resplit_run_done(self, result, output, src):
        self._fixer_set_busy(False)
        self.status_var.set("重分完成")
        msg = f"标题重分完成：共 {result['chapter_count']} 章。\n输出：{output}"
        self.log(msg)
        self._fixer_offer_switch(msg, output, src)


    # ---------------- 标题重组（提取正文短标题） ----------------
    def _fixer_reassemble_detect_async(self):
        epub = self._fixer_require_path("预检提示")
        if epub is None:
            return
        self._fixer_reassemble_preview_async(epub, detect_only=True)


    def _fixer_reassemble_preview_async(self, epub: str | None = None, detect_only: bool = False):
        if epub is None:
            epub = self._fixer_require_path("预检提示")
            if epub is None:
                return
        prefix = self.fixer_reassemble_prefix_var.get().strip()
        title_pattern = self.fixer_reassemble_title_var.get().strip()
        clean = self.fixer_reassemble_clean_var.get()
        self._fixer_dispatch(
            lambda: preview_reassemble(
                epub,
                prefix_pattern=prefix,
                body_title_pattern=title_pattern,
                remove_word_count=clean,
            ),
            lambda result: self._on_fixer_reassemble_preview_done(result, detect_only),
        )


    def _on_fixer_reassemble_preview_done(self, result, detect_only: bool = False):
        self._fixer_set_busy(False)
        self.status_var.set("结构检测完成" if detect_only else "预检重组完成")
        lines: list[str] = []
        wc = result["would_remove_word_count_chapters"]
        if wc:
            lines.append(
                f"⚠ 在 {wc} 个占位标题下检测到每章字数统计，建议先清理，再进行批量重组。\n"
            )
        else:
            lines.append("未检测到占位标题下的每章字数统计。\n")
        updates = result["updates"]
        if not updates:
            lines.append("未发现可重组的正文短标题。")
        else:
            lines.append(f"预检到 {len(updates)} 个标题可重组：\n")
            for update in updates:
                lines.append(f"· [{update['index']}] {update['old']} → {update['new']}")
        self._render_fixer_preview(self.fixer_reassemble_preview, "\n".join(lines))


    def _fixer_reassemble_run_async(self):
        epub = self._fixer_require_path("执行提示")
        if epub is None:
            return
        src = Path(epub)
        mode = self.fixer_mode_var.get()
        output, _ = self._fixer_output_path(src, mode, "_titles")
        prefix = self.fixer_reassemble_prefix_var.get().strip()
        title_pattern = self.fixer_reassemble_title_var.get().strip()
        clean = self.fixer_reassemble_clean_var.get()
        remove_body = self.fixer_reassemble_remove_var.get()
        keep_prefix = self.fixer_reassemble_keep_var.get()
        self._fixer_dispatch(
            lambda: apply_reassemble(
                epub,
                output,
                prefix_pattern=prefix,
                body_title_pattern=title_pattern,
                keep_prefix=keep_prefix,
                remove_word_count=clean,
                remove_body_title=remove_body,
            ),
            lambda result: self._on_fixer_reassemble_run_done(result, output, src),
        )


    def _on_fixer_reassemble_run_done(self, result, output, src):
        self._fixer_set_busy(False)
        self.status_var.set("重组完成")
        msg = (
            f"标题重组完成：改写 {len(result['updated'])} 个标题，"
            f"清除字数行 {result['removed_word_count_lines']} 处。\n输出：{output}"
        )
        self.log(msg)
        self._fixer_offer_switch(msg, output, src)


    # ---------------- LLM 辅助推断标题筛选 ----------------
    def _fixer_reassemble_llm_async(self):
        epub = self._fixer_require_path("LLM 提取提示")
        if epub is None:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行", parent=self)
            return
        try:
            config = self._config_from_ui()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc), parent=self)
            return
        llm = LLMClient(config)
        self._fixer_dispatch(
            lambda: self._fixer_llm_extract_worker(llm, epub),
            self._on_fixer_llm_extract_done,
        )


    def _fixer_llm_extract_worker(self, llm, epub):
        prefix = infer_placeholder_pattern(llm, epub)
        body = {"pattern": "", "found": False, "chapters": 0}
        if prefix["pattern"]:
            try:
                re.compile(prefix["pattern"])
            except re.error as exc:  # noqa: PERF203
                return {
                    "ok": False,
                    "prefix_pattern": prefix["pattern"],
                    "body_pattern": "",
                    "chapters": 0,
                    "error": f"LLM 推断的占位标题正则无效：{exc}",
                }
            body = infer_body_title_match(
                llm, epub, prefix_pattern=prefix["pattern"]
            )
        return {
            "ok": True,
            "prefix_pattern": prefix["pattern"],
            "body_pattern": body.get("pattern", ""),
            "body_found": body.get("found", False),
            "chapters": body.get("chapters", 0),
        }


    def _on_fixer_llm_extract_done(self, result):
        self._fixer_set_busy(False)
        if not result.get("ok"):
            messagebox.showwarning(
                "LLM 提取提示", result.get("error", "标题筛选推断失败"), parent=self
            )
            return
        prefix = result.get("prefix_pattern", "")
        body = result.get("body_pattern", "")
        if not prefix:
            self.status_var.set("未推断出占位标题正则")
            messagebox.showwarning(
                "LLM 提取提示", "未能推断出占位标题正则，请手动设置。", parent=self
            )
            return
        self.fixer_reassemble_prefix_var.set(prefix)
        self.log(f"LLM 推断占位标题正则：{prefix}")
        self._render_fixer_preview(
            self.fixer_reassemble_preview,
            f"已推断占位标题正则：{prefix}\n"
            f"匹配到 {result.get('chapters', 0)} 个章节。\n\n"
            f"正文标题正则：{body or '（未发现，请手动设置）'}",
        )
        if body:
            self.fixer_reassemble_title_var.set(body)
            self.log(f"LLM 推断正文标题正则：{body}")
            self.status_var.set("标题筛选已更新")
            messagebox.showinfo(
                "完成",
                f"已推断占位标题正则：{prefix}\n已推断正文标题正则：{body}",
                parent=self,
            )
        else:
            self.status_var.set("未发现标题内容")
            messagebox.showinfo(
                "提示",
                "本小说似乎没有标题内容可供筛选。\n已填入占位标题正则，"
                "正文标题正则请手动设置或使用 ↺ 恢复默认。",
                parent=self,
            )


    # ---------------- 格式整理（检测/清理/新增） ----------------
    def _fixer_format_detect_async(self):
        epub = self._fixer_require_path("检测提示")
        if epub is None:
            return
        self._fixer_dispatch(lambda: detect_format(epub), self._on_fixer_format_detect_done)


    def _on_fixer_format_detect_done(self, result):
        self._fixer_set_busy(False)
        self.status_var.set("格式检测完成")
        lines = [
            f"章节数：{result['chapter_count']}，正文总字数：{result['total_chars']:,} 字",
            "",
        ]
        if result["has_production_note"]:
            prod = result["production"]
            lines.append(
                f"· 存在制作说明章节：{len(prod)} 个（{', '.join(p['title'] for p in prod)}）"
            )
        else:
            lines.append("· 未检测到制作说明章节")
        if result["word_count_count"]:
            lines.append(f"· 存在每章字数统计：{result['word_count_count']} 章")
        else:
            lines.append("· 未检测到每章字数统计")
        self._render_fixer_preview(self.fixer_format_preview, "\n".join(lines))


    def _fixer_format_clear_async(self):
        epub = self._fixer_require_path("清理提示")
        if epub is None:
            return
        remove_prod = self.fixer_format_prod_var.get()
        remove_wc = self.fixer_format_wc_var.get()
        self._fixer_dispatch(
            lambda: preview_clear_format(
                epub, remove_production=remove_prod, remove_word_count=remove_wc
            ),
            self._on_fixer_format_clear_preview_done,
        )


    def _on_fixer_format_clear_preview_done(self, result):
        self._fixer_set_busy(False)
        self.status_var.set("清理检测完成")
        prod = result["would_remove_production"]
        wc = result["would_remove_word_count_lines"]
        if not prod and not wc:
            self._render_fixer_preview(
                self.fixer_format_preview,
                "未检测到旧制作说明或每章字数统计，无需清理。\n",
            )
            return
        lines = ["检测到旧格式：", ""]
        if prod:
            lines.append(f"· 制作说明章节：{len(prod)} 个（{', '.join(prod)}）")
        if wc:
            lines.append(f"· 每章字数统计：共 {wc} 行")
        self._render_fixer_preview(self.fixer_format_preview, "\n".join(lines))
        ok = messagebox.askyesno(
            "确认清理",
            f"检测到制作说明 {len(prod)} 个、字数行 {wc} 行。\n是否清除？",
            parent=self,
        )
        if not ok:
            return
        epub = self.fixer_path_var.get().strip()
        src = Path(epub)
        mode = self.fixer_mode_var.get()
        output, _ = self._fixer_output_path(src, mode, "_clean")
        remove_prod = self.fixer_format_prod_var.get()
        remove_wc = self.fixer_format_wc_var.get()
        self._fixer_dispatch(
            lambda: apply_clear_format(
                epub, output, remove_production=remove_prod, remove_word_count=remove_wc
            ),
            lambda done: self._on_fixer_format_clear_done(done, output, src),
        )


    def _on_fixer_format_clear_done(self, result, output, src):
        self._fixer_set_busy(False)
        self.status_var.set("格式清理完成")
        msg = (
            f"格式清理完成：移除制作说明 {len(result['removed_production'])} 个，"
            f"清除字数行 {result['removed_word_count_lines']} 处，现共 {result['chapter_count']} 章。"
            f"\n输出：{output}"
        )
        self.log(msg)
        self._fixer_offer_switch(msg, output, src)


    def _fixer_format_add_async(self):
        epub = self._fixer_require_path("新增格式提示")
        if epub is None:
            return
        self._fixer_dispatch(lambda: preview_add_format(epub), self._on_fixer_format_add_preview_done)


    def _on_fixer_format_add_preview_done(self, result):
        self._fixer_set_busy(False)
        self.status_var.set("新增格式统计完成")
        preview = (
            f"将按新版中文 EPUB 统计新增格式：\n\n"
            f"· 正文总字数：{result['total_chars']:,} 字\n"
            f"· 章节数：{result['chapter_count']}\n"
            f"· 预计阅读时长：约 {result['predicted_minutes']} 分钟\n"
            f"· 将生成「制作说明」章节 + 每章字数统计。"
        )
        self._render_fixer_preview(self.fixer_format_preview, preview)
        ok = messagebox.askyesno(
            "确认新增",
            "将按新版统计生成制作说明与每章字数，是否继续？",
            parent=self,
        )
        if not ok:
            return
        epub = self.fixer_path_var.get().strip()
        src = Path(epub)
        mode = self.fixer_mode_var.get()
        output, _ = self._fixer_output_path(src, mode, "_styled")
        self._fixer_dispatch(
            lambda: apply_add_format(epub, output),
            lambda done: self._on_fixer_format_add_done(done, output, src),
        )


    def _on_fixer_format_add_done(self, result, output, src):
        self._fixer_set_busy(False)
        self.status_var.set("新增格式完成")
        msg = (
            f"新增格式完成：正文总字数 {result['total_chars']:,} 字，"
            f"共 {result['chapter_count']} 章。\n输出：{output}"
        )
        self.log(msg)
        self._fixer_offer_switch(msg, output, src)
