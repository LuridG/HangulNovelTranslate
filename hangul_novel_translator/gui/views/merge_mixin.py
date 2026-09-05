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
from ..dialogs import (GlossaryEditDialog, SanitizerRuleDialog, FailedChunkEditorDialog, MalformedBlockEditorDialog, PerspectiveFailedEditorDialog, TitleTranslationDialog)


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class MergeMixin:

    def _build_merge_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        self.merge_files: list[Path] = []
        self.merge_title_var = tk.StringVar(value="")
        self._merge_title_auto_last = ""
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
        ctk.CTkButton(run_frame, text="🧩 畸形块检测", width=118, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._open_malformed_editor).grid(row=0, column=4, padx=4)
        ctk.CTkButton(run_frame, text="🖼 图片补集", width=110, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_repair_images_async).grid(row=0, column=5, padx=4)
        ctk.CTkButton(run_frame, text="🔍 复查", width=80, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_review_async).grid(row=0, column=6, padx=4)
        ctk.CTkLabel(run_frame, text="单段韩文阈值").grid(row=1, column=0, padx=4, pady=(8, 0), sticky="e")
        ctk.CTkEntry(run_frame, textvariable=self.merge_review_threshold_var, width=70).grid(row=1, column=1, padx=4, pady=(8, 0), sticky="w")
        ctk.CTkButton(
            run_frame,
            text="🔖 标题翻译",
            width=120,
            fg_color=THEME["secondary"],
            hover_color=THEME["secondary_hover"],
            border_width=1,
            border_color=THEME["card_border"],
            command=self._open_title_editor,
        ).grid(row=1, column=2, padx=4, pady=(8, 0), sticky="w")

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
            text="提示：每个存档需与其对应的原书（.txt/.epub）保持在原路径；“复查”统计韩文字符数；“自检修复块”按自定义正则识别混入译文的失败字段；“标题翻译”检查章节标题是否已翻译，可批量或手动补翻。",
            wraplength=620,
            justify="left",
            anchor="w",
        ).grid(row=7, column=0, padx=12, pady=(0, 12), sticky="w")

        self._refresh_audit_patterns()


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
        detected = detect_merge_title(titles)
        if detected:
            current = self.merge_title_var.get().strip()
            # 用户没有手动改名（当前值等于上次自动值或为空）时才覆盖，
            # 避免清空再添加后不再自动提取，也保留用户自定义书名。
            if not current or current == self._merge_title_auto_last:
                self.merge_title_var.set(detected)
                self._merge_title_auto_last = detected


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


    def _open_malformed_editor(self):
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
        dialog = MalformedBlockEditorDialog(self, files, config, self.glossary)
        self.wait_window(dialog)


    def _open_title_editor(self):
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
        dialog = TitleTranslationDialog(self, files, config, self.glossary)
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
        if not self.glossary.valid_entries() and not self.common_glossary.valid_entries(allow_missing_ko=True):
            messagebox.showinfo("提示", "专用词表与通用词表均为空，请先加载或提取词表", parent=self)
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
        if not self.glossary.valid_entries() and not self.common_glossary.valid_entries(allow_missing_ko=True):
            messagebox.showinfo("提示", "专用词表与通用词表均为空，请先加载或提取词表", parent=self)
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
                stats = preview_fix(merged, self.glossary, self.common_glossary)
                self.after(0, lambda: self._on_merge_preview_done(stats))
            else:
                auto_fixed = 0
                for p in files:
                    try:
                        blocks = detect_malformed_blocks(p, config)
                        auto_fixed += sum(1 for b in blocks if b.repairable)
                    except Exception:  # noqa: BLE001
                        pass
                result = export_merged(
                    books,
                    self.glossary,
                    config,
                    Path(self.output_var.get().strip()),
                    title=self.merge_title_var.get(),
                    output_txt=self.merge_txt_var.get(),
                    output_epub=self.merge_epub_var.get(),
                    common_glossary=self.common_glossary,
                )
                result["auto_repaired_malformed"] = auto_fixed
                self.after(0, lambda: self._on_merge_run_done(result))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_error(message))


    def _on_merge_preview_done(self, stats: dict[str, int]):
        self._set_busy(False)
        self.progress.set(1)
        self.status_var.set("预览完成")
        common = int(stats.get("common_sources", 0) or 0)
        dedicated = int(stats.get("dedicated_sources", 0) or 0)
        self.log(
            f"预览：预计命中 {stats['hit_paragraphs']} 个段落、"
            f"{stats['hit_sources']} 种旧写法（专用 {dedicated} / 通用 {common}）"
        )
        messagebox.showinfo(
            "预览",
            f"预计将修正 {stats['hit_paragraphs']} 个段落，"
            f"涉及 {stats['hit_sources']} 种旧写法（专用 {dedicated} / 通用 {common}）。\n点“修正并输出”执行。",
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
        auto_fixed = int(result.get("auto_repaired_malformed", 0) or 0)
        override = int(result.get("common_override_count", 0) or 0)
        extra = ""
        if auto_fixed > 0:
            extra = (
                f"\n\n⚠ 导出时自动兜底修复了 {auto_fixed} 个畸形块"
                f"（未先过“畸形块检测”）。建议打开“多卷修正 → 畸形块检测”复查确认。"
            )
        if override > 0:
            extra += f"\n\n通用词表已覆盖专用词表 {override} 条译名。"
        messagebox.showinfo(
            "完成",
            f"已合并 {result['chapters']} 章，修正命中 {result['hit_paragraphs']} 个段落。\n"
            + "\n".join(f"· {p}" for p in paths)
            + extra,
            parent=self,
        )
