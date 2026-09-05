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
from ...book import DEFAULT_TXT_PATTERNS, load_book
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


_TXT_PATTERN_TEMPLATES: dict[str, list[str]] = {
    "韩语+特殊章（默认）": [
        DEFAULT_TXT_PATTERNS[0],
        DEFAULT_TXT_PATTERNS[1],
    ],
    "数字标题（Chapter / EP / #）": [
        DEFAULT_TXT_PATTERNS[2],
        DEFAULT_TXT_PATTERNS[3],
        DEFAULT_TXT_PATTERNS[4],
    ],
    "中文章节（第N章/话/回）": [DEFAULT_TXT_PATTERNS[5]],
    "完整默认（全部）": list(DEFAULT_TXT_PATTERNS),
}


class LeftPanelMixin:

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
        self._txt_pattern_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._txt_pattern_frame.grid(row=row, column=0, columnspan=2, padx=0, pady=0, sticky="ew")
        self._txt_pattern_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self._txt_pattern_frame, text="TXT 章节正则（每行一条，空则用默认）", anchor="w").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(12, 2), sticky="w"
        )
        self.txt_pattern_selector = ctk.CTkOptionMenu(
            self._txt_pattern_frame,
            values=list(_TXT_PATTERN_TEMPLATES.keys()),
            command=self._on_txt_pattern_template,
            height=30,
            fg_color=THEME["card_alt"],
            button_color=THEME["primary"],
            button_hover_color=THEME["primary_hover"],
            text_color=THEME["text_main"],
            dropdown_fg_color=THEME["card_alt"],
            dropdown_hover_color=THEME["secondary_hover"],
            dropdown_text_color=THEME["text_main"],
        )
        self.txt_pattern_selector.set(list(_TXT_PATTERN_TEMPLATES.keys())[0])
        self.txt_pattern_selector.grid(row=1, column=0, columnspan=2, padx=12, pady=2, sticky="ew")

        self._txt_pattern_rows = ctk.CTkFrame(self._txt_pattern_frame, fg_color="transparent")
        self._txt_pattern_rows.grid(row=2, column=0, columnspan=2, padx=12, pady=2, sticky="ew")
        self._txt_pattern_rows.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            self._txt_pattern_frame,
            text="＋ 添加一条正则",
            width=130,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._append_txt_pattern_row,
        ).grid(row=3, column=0, columnspan=2, padx=12, pady=2, sticky="w")
        self._load_txt_pattern_into_box()
        self._active_txt_patterns = list(self._txt_patterns())
        self._active_ignore_zero = False

        txt_btns = ctk.CTkFrame(self._txt_pattern_frame, fg_color="transparent")
        txt_btns.grid(row=4, column=0, columnspan=2, padx=10, pady=2, sticky="ew")
        ctk.CTkButton(txt_btns, text="保存正则", width=112, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._save_txt_patterns).grid(row=0, column=0, padx=4)
        ctk.CTkButton(txt_btns, text="章节检测", width=112, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._detect_txt_chapters).grid(row=0, column=1, padx=4)
        row += 1

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
        self._update_txt_pattern_visibility()


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
        self._update_txt_pattern_visibility()


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


    # ---------------- TXT 章节正则 ----------------
    def _txt_patterns(self) -> list[str]:
        """读取逐条正则输入框的内容，过滤空条目。"""
        return [v for v in self._collect_txt_pattern_values() if v]


    def _has_txt_input(self) -> bool:
        return any(p.suffix.lower() == ".txt" for p in self.input_files)


    def _update_txt_pattern_visibility(self):
        """含 TXT 输入时显示章节正则区，否则隐藏（避免无文件/纯 EPUB 时显冗杂）。"""
        if not hasattr(self, "_txt_pattern_frame") or not hasattr(self, "input_files"):
            return
        if self._has_txt_input():
            self._txt_pattern_frame.grid()
        else:
            self._txt_pattern_frame.grid_remove()


    def _load_txt_pattern_into_box(self):
        """启动时按已保存正则或内置模板重建逐条输入框。"""
        saved = self.ui_state.get("txt_patterns")
        if isinstance(saved, list) and saved:
            patterns = [str(p) for p in saved if str(p).strip()]
        else:
            patterns = list(_TXT_PATTERN_TEMPLATES.get(self.txt_pattern_selector.get(), DEFAULT_TXT_PATTERNS))[:2]
        self._rebuild_txt_pattern_rows(patterns)


    def _on_txt_pattern_template(self, name: str | None = None):
        """选中内置模板后，重建逐条输入框。"""
        name = name or self.txt_pattern_selector.get()
        patterns = _TXT_PATTERN_TEMPLATES.get(name)
        if not patterns:
            return
        self._rebuild_txt_pattern_rows(list(patterns))


    def _collect_txt_pattern_values(self) -> list[str]:
        """收集所有正则输入框当前值。"""
        return [v.get().strip() for v in getattr(self, "_txt_pattern_vars", [])]


    def _rebuild_txt_pattern_rows(self, patterns: list[str]):
        """按给定正则列表重建逐条输入框（每条一个独立输入框 + 删除钮）。"""
        if not hasattr(self, "_txt_pattern_rows"):
            return
        for child in self._txt_pattern_rows.winfo_children():
            child.destroy()
        self._txt_pattern_vars = []
        for index, pattern in enumerate(patterns):
            var = tk.StringVar(value=pattern)
            self._txt_pattern_vars.append(var)
            entry = ctk.CTkEntry(
                self._txt_pattern_rows,
                textvariable=var,
                height=30,
                corner_radius=6,
                border_width=1,
                border_color=THEME["card_border"],
                fg_color=THEME["bg"],
                text_color=THEME["text_main"],
            )
            entry.grid(row=index, column=0, padx=(0, 4), pady=2, sticky="ew")
            ctk.CTkButton(
                self._txt_pattern_rows,
                text="✕",
                width=30,
                height=30,
                fg_color="transparent",
                hover_color=THEME["danger_hover"],
                text_color=THEME["text_muted"],
                border_width=1,
                border_color=THEME["card_border"],
                command=lambda idx=index: self._remove_txt_pattern_row(idx),
            ).grid(row=index, column=1, padx=2, pady=2)
        if not self._txt_pattern_vars:
            var = tk.StringVar(value="")
            self._txt_pattern_vars.append(var)
            ctk.CTkEntry(
                self._txt_pattern_rows,
                textvariable=var,
                height=30,
                corner_radius=6,
                border_width=1,
                border_color=THEME["card_border"],
                fg_color=THEME["bg"],
                text_color=THEME["text_main"],
            ).grid(row=0, column=0, padx=(0, 4), pady=2, sticky="ew")


    def _append_txt_pattern_row(self):
        values = self._collect_txt_pattern_values()
        values.append("")
        self._rebuild_txt_pattern_rows(values)


    def _remove_txt_pattern_row(self, index: int):
        values = self._collect_txt_pattern_values()
        if 0 <= index < len(values):
            values.pop(index)
        self._rebuild_txt_pattern_rows(values)


    def _save_txt_patterns(self):
        patterns = self._txt_patterns()
        if not patterns:
            messagebox.showinfo("提示", "当前没有可保存的章节正则，请先输入。", parent=self)
            return
        state = _load_ui_state()
        state["txt_patterns"] = patterns
        _save_ui_state(state)
        self.log(f"已保存 TXT 章节正则：{len(patterns)} 条")
        messagebox.showinfo("完成", f"已保存 {len(patterns)} 条 TXT 章节正则，下次启动自动生效。", parent=self)


    def _detect_txt_chapters(self):
        files = list(self.input_files)
        if not files:
            messagebox.showwarning("提示", "请先添加输入文件", parent=self)
            return
        patterns = self._txt_patterns()
        unknown = [p for p in patterns if not self._valid_regex(p)]
        if unknown:
            messagebox.showwarning("提示", f"以下正则无效，请修正后重试：\n" + "\n".join(unknown), parent=self)
            return
        self.log("开始章节检测…")
        self._show_chapter_detection(files, patterns)


    @staticmethod
    def _valid_regex(pattern: str) -> bool:
        try:
            re.compile(pattern, re.IGNORECASE)
            return True
        except re.error:
            return False


    def _show_chapter_detection(self, files: list[Path], patterns: list[str]):
        """弹窗展示按「卷 → 章 · 字数」树状目录预览：每个输入文件为一级卷，其下平铺章节与字数。"""
        win = ctk.CTkToplevel(self)
        self._preview_win = win
        win.title("目录预览（卷 → 章 · 字数）")
        win.geometry("700x620")
        win.minsize(500, 400)
        win.configure(fg_color=THEME["bg"])
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        # 逐文件加载为 book（txt 按正则、epub 按自身目录），每本即一级卷
        books: list[tuple[Path, object]] = []
        errors: list[str] = []
        total_chars = 0
        top_count = 0
        for path in files:
            try:
                book = load_book(path, txt_patterns=patterns)
                books.append((path, book))
                total_chars += book.total_chars
                top_count += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name}：{exc}")
        minutes = round(total_chars / 400) if total_chars else 0

        # 顶部汇总卡片：标题 + 绿色统计条 + 忽视零字章节开关
        header = ctk.CTkFrame(win, fg_color=THEME["card"], corner_radius=8, border_width=1, border_color=THEME["card_border"])
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="目录预览（卷 → 章 · 字数）", font=ctk.CTkFont(size=15, weight="bold"), text_color=THEME["text_main"]).grid(
            row=0, column=0, padx=14, pady=(12, 6), sticky="w"
        )
        ignore_zero_var = tk.BooleanVar(value=bool(getattr(self, "_active_ignore_zero", False)))
        ignore_zero_switch = ctk.CTkSwitch(
            header,
            text="忽视 0 字章节",
            variable=ignore_zero_var,
            progress_color=THEME["primary"],
            text_color=THEME["text_main"],
        )
        ignore_zero_switch.grid(row=0, column=1, padx=14, pady=(12, 6), sticky="e")
        self._preview_ignore_zero_var = ignore_zero_var
        summary_lbl = ctk.CTkLabel(
            header,
            text=f"共 {top_count} 个顶层条目 · 总字数 {total_chars:,} 字 · 约 {minutes} 分钟读完",
            anchor="w",
            fg_color=THEME["accent"],
            corner_radius=6,
            text_color="#FFFFFF",
            padx=14,
        )
        summary_lbl.grid(row=1, column=0, columnspan=2, padx=14, pady=(2, 12), sticky="ew")

        # 树状结构：卷 → 章 · 字数
        tree_frame = ctk.CTkFrame(win, fg_color="transparent")
        tree_frame.grid(row=1, column=0, padx=16, sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure("Preview.Treeview", background=THEME["card"], foreground="#E6EDF3", fieldbackground=THEME["card"], rowheight=32, borderwidth=0, relief="flat", font=("Microsoft YaHei UI", 11))
        style.map("Preview.Treeview", background=[("selected", THEME["primary"])], foreground=[("selected", "#FFFFFF")])
        style.configure("Preview.Treeview.Heading", background=THEME["secondary"], foreground=THEME["text_muted"], font=("Microsoft YaHei UI", 11, "bold"), relief="flat", padding=(8, 5))
        tree = ttk.Treeview(tree_frame, show="tree headings", columns=("chars",), style="Preview.Treeview")
        tree.heading("#0", text="章节")
        tree.heading("chars", text="字数", anchor="e")
        tree.column("#0", width=430, minwidth=280, stretch=True, anchor="w")
        tree.column("chars", width=100, minwidth=76, anchor="e", stretch=False)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(tree_frame, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        tree.tag_configure("volume", font=("Microsoft YaHei UI", 11, "bold"), foreground="#58A6FF")
        tree.tag_configure("chapter", foreground="#E6EDF3")
        tree.tag_configure("zero", foreground=THEME["text_subtle"])

        def render(ignore_zero: bool):
            for item in tree.get_children():
                tree.delete(item)
            for i, (path, book) in enumerate(books):
                vol_label = self._output_stem_for(i, path)
                vol_id = tree.insert("", "end", text=vol_label, values=(f"{book.total_chars:,} 字",), open=True, tags=("volume",))
                for ch in book.chapters:
                    chars = sum(len(p) for p in ch.paragraphs)
                    if ignore_zero and chars == 0:
                        # 0 字章节不单独成章，正文（通常为空）归入上一正常章。
                        continue
                    tag = "zero" if chars == 0 else "chapter"
                    tree.insert(vol_id, "end", text=ch.display_title, values=(f"{chars:,} 字",), tags=(tag,))
            for err in errors:
                tree.insert("", "end", text=err, values=("",), tags=("zero",))

        ignore_zero_var.trace_add("write", lambda *_args: render(ignore_zero_var.get()))
        render(ignore_zero_var.get())

        # 底部提示 + 关闭
        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.grid(row=2, column=0, padx=16, pady=(8, 16), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(footer, text="确认分章无误后，再开始翻译或提取词表。开启“忽视 0 字章节”会将空章并入上一章。", anchor="w", text_color=THEME["text_muted"]).grid(
            row=0, column=0, padx=4, sticky="w"
        )

        def confirm():
            active = list(patterns)
            ignore = bool(ignore_zero_var.get())
            self._active_txt_patterns = active
            self._active_ignore_zero = ignore
            self.log(
                f"已确认分章方案：{len(active)} 条正则，"
                f"忽视 0 字章节：{'开启' if ignore else '关闭'}"
            )
            messagebox.showinfo(
                "已确认",
                f"已应用当前分章方案：{len(active)} 条正则，"
                f"忽视 0 字章节：{'开启' if ignore else '关闭'}。\n\n"
                "后续「开始翻译」与「提取词表」将按此方案切分；"
                "如需调整，请改正则后重新「章节检测」并再次确认。",
                parent=win,
            )
            win.destroy()

        ctk.CTkButton(
            footer,
            text="应用此分章方案",
            width=150,
            fg_color=THEME["primary"],
            hover_color=THEME["primary_hover"],
            command=confirm,
        ).grid(row=0, column=1, padx=4, sticky="e")
        ctk.CTkButton(
            footer,
            text="关闭",
            width=104,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=win.destroy,
        ).grid(row=0, column=2, padx=4, sticky="e")
        win.transient(self)
        win.grab_set()
