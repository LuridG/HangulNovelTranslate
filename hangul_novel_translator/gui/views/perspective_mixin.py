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


class PerspectiveMixin:

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
