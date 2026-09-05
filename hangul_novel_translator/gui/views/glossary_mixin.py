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
from ..state import (_load_ui_state, _save_ui_state, common_glossary_path)
from ..widgets import (TreeviewTooltip, DebouncedScrollableFrame)
from ..dialogs import (GlossaryEditDialog, SanitizerRuleDialog, FailedChunkEditorDialog, MalformedBlockEditorDialog, PerspectiveFailedEditorDialog)


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class GlossaryMixin:

    def _build_right(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.grid(row=0, column=0, padx=12, pady=(14, 6), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.glossary_mode = "dedicated"
        self.glossary_title_label = ctk.CTkLabel(
            header, text="专有名词词表（专用）", font=ctk.CTkFont(size=16, weight="bold")
        )
        self.glossary_title_label.grid(row=0, column=0, sticky="w")
        self.glossary_mode_btn = ctk.CTkButton(
            header, text="通用词表", width=88,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._toggle_glossary_mode,
        )
        self.glossary_mode_btn.grid(row=0, column=1, padx=4)
        self._extract_btn = ctk.CTkButton(
            header, text="⚡ 提取词表", width=96,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._extract_glossary_async,
        )
        self._extract_btn.grid(row=0, column=2, padx=4)
        ctk.CTkButton(
            header, text="💾 保存词表", width=92,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._save_glossary,
        ).grid(row=0, column=3, padx=4)
        ctk.CTkButton(
            header, text="📂 加载词表", width=92,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._load_glossary,
        ).grid(row=0, column=4, padx=4)
        self._extract_more_btn = ctk.CTkButton(
            header, text="提取更多词表", width=110, command=self._extract_more_glossary_async,
        )
        self._extract_more_btn.grid(row=0, column=5, padx=4)
        self._enrich_btn = ctk.CTkButton(
            header, text="完善信息", width=90, command=self._enrich_glossary_async,
        )
        self._enrich_btn.grid(row=0, column=6, padx=4)
        self.enrich_alts_var = tk.BooleanVar(value=True)
        self._enrich_alts_chk = ctk.CTkCheckBox(header, text="补可能译法", variable=self.enrich_alts_var)
        self._enrich_alts_chk.grid(row=0, column=7, padx=(6, 0))
        self.enrich_nick_var = tk.BooleanVar(value=True)
        self._enrich_nick_chk = ctk.CTkCheckBox(header, text="检测昵称", variable=self.enrich_nick_var)
        self._enrich_nick_chk.grid(row=0, column=8, padx=(0, 4))
        self._glossary_llm_widgets = [
            self._extract_btn,
            self._extract_more_btn,
            self._enrich_btn,
            self._enrich_alts_chk,
            self._enrich_nick_chk,
        ]

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
        self.confirm_selected_btn = ctk.CTkButton(
            actions, text="✓ 确认选中", width=112,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._confirm_selected,
        )
        self.confirm_selected_btn.grid(row=0, column=3, padx=4)
        ctk.CTkButton(actions, text="✓✓ 全部确认", width=100, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._confirm_all).grid(row=0, column=4, padx=4)
        ctk.CTkButton(actions, text="🧹 清理重复", width=96, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._cleanup_duplicates).grid(row=0, column=5, padx=4)
        ctk.CTkButton(actions, text="🗑 清空词表", width=96, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._clear_glossary).grid(row=0, column=6, padx=4)
        self.count_label = ctk.CTkLabel(actions, text="0 条")
        self.count_label.grid(row=0, column=7, padx=12, sticky="e")
        self._update_glossary_controls()


    # ---------------- 词表 ----------------
    def _current_glossary(self):
        return self.common_glossary if self.glossary_mode == "common" else self.glossary


    def _update_glossary_controls(self):
        """根据当前是专用/通用词表，切换标题、按钮与可用操作。"""
        common = self.glossary_mode == "common"
        self.glossary_title_label.configure(text="通用词表" if common else "专有名词词表（专用）")
        self.glossary_mode_btn.configure(text="专用词表" if common else "通用词表")
        for widget in self._glossary_llm_widgets:
            widget.configure(state="disabled" if common else "normal")
        if common:
            self.confirm_selected_btn.configure(text="✓ 确认选中", command=self._confirm_selected)
        else:
            self.confirm_selected_btn.configure(text="＋ 追加到通用词表", command=self._append_selected_to_common)


    def _toggle_glossary_mode(self):
        self.glossary_mode = "common" if self.glossary_mode == "dedicated" else "dedicated"
        self._refresh_tree()
        self._update_glossary_controls()


    def _append_selected_to_common(self):
        """把专用词表选中的词条追加到通用词表；相同 ko 保留通用词表译名（通用词表为主）。"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择要追加到通用词表的词条（可多选）", parent=self)
            return
        selected_kos = {str(self.tree.item(i, "values")[0]) for i in selection}
        entries = [e for e in self.glossary.entries if e.ko in selected_kos and e.zh]
        if not entries:
            messagebox.showwarning("提示", "选中的词条缺少中文译名，无法追加", parent=self)
            return
        added, skipped = self.common_glossary.append_unique(entries)
        self.common_glossary.save(common_glossary_path())
        self.log(f"已追加 {added} 条到通用词表（跳过已存在 {skipped} 条）")
        messagebox.showinfo(
            "完成",
            f"已将 {added} 条加入通用词表，跳过已存在 {skipped} 条。\n"
            "通用词表的确认译名将在后续修正中优先覆盖专用词表。",
            parent=self,
        )


    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        glossary = self._current_glossary()
        for e in glossary.entries:
            alts_display = e.alternatives
            if e.alternatives and e.replace_short:
                alts_display = "含短称|" + e.alternatives
            self.tree.insert(
                "",
                "end",
                values=(e.ko, e.zh, e.kind, e.note, alts_display, "✓" if e.confirmed else ""),
            )
        valid = len(glossary.valid_entries(allow_missing_ko=(self.glossary_mode == "common")))
        self.count_label.configure(text=f"{valid} 条有效词条")
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
        dialog = GlossaryEditDialog(self, require_ko=(self.glossary_mode == "dedicated"))
        self.wait_window(dialog)
        if dialog.result:
            glossary = self._current_glossary()
            glossary.upsert(dialog.result)
            self._refresh_tree()
            if self.glossary_mode == "common":
                self.common_glossary.save(common_glossary_path())
            self.log(f"已添加词条：{dialog.result.ko} -> {dialog.result.zh}")


    def _edit_selected(self):
        ko = self._selected_ko()
        if not ko:
            messagebox.showinfo("提示", "请先选择一个词条", parent=self)
            return
        glossary = self._current_glossary()
        entry = next((e for e in glossary.entries if e.ko == ko), None)
        dialog = GlossaryEditDialog(self, entry, require_ko=(self.glossary_mode == "dedicated"))
        self.wait_window(dialog)
        if dialog.result:
            glossary.upsert(dialog.result)
            self._refresh_tree()
            if self.glossary_mode == "common":
                self.common_glossary.save(common_glossary_path())


    def _remove_selected(self):
        ko = self._selected_ko()
        if not ko:
            return
        glossary = self._current_glossary()
        glossary.remove(ko)
        self._refresh_tree()
        if self.glossary_mode == "common":
            self.common_glossary.save(common_glossary_path())


    def _confirm_selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("提示", "请先选择要确认的词条（可多选）", parent=self)
            return
        glossary = self._current_glossary()
        selected_kos = {str(self.tree.item(i, "values")[0]) for i in selection}
        changed = sum(1 for e in glossary.entries if e.ko in selected_kos and not e.confirmed)
        for e in glossary.entries:
            if e.ko in selected_kos:
                e.confirmed = True
        self._refresh_tree()
        if self.glossary_mode == "common":
            self.common_glossary.save(common_glossary_path())
        self.log(f"已确认 {len(selected_kos)} 条（新增 {changed} 条）")


    def _confirm_all(self):
        glossary = self._current_glossary()
        if not glossary.entries:
            messagebox.showinfo("提示", "当前没有词表", parent=self)
            return
        changed = sum(1 for e in glossary.entries if not e.confirmed)
        for e in glossary.entries:
            e.confirmed = True
        self._refresh_tree()
        if self.glossary_mode == "common":
            self.common_glossary.save(common_glossary_path())
        self.log(f"已全部确认：{len(glossary.entries)} 条（新增 {changed} 条）")


    def _save_glossary(self):
        glossary = self._current_glossary()
        if not glossary.entries:
            messagebox.showinfo("提示", "当前没有词表可保存", parent=self)
            return
        if self.glossary_mode == "common":
            self.common_glossary.save(common_glossary_path())
            self.log(f"通用词表已保存：{common_glossary_path()}")
            return
        path = filedialog.asksaveasfilename(
            title="保存词表",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            glossary.save(Path(path))
            self.log(f"词表已保存：{path}")


    def _load_glossary(self):
        kwargs: dict[str, Any] = {
            "title": "加载词表",
            "filetypes": [("JSON", "*.json")],
        }
        if self.glossary_mode == "common":
            kwargs["initialdir"] = str(Path(common_glossary_path()).parent)
        path = filedialog.askopenfilename(**kwargs)
        if not path:
            return
        path = Path(path)
        is_common_file = self._looks_like_common_glossary(path)

        if self.glossary_mode == "common" and not is_common_file:
            # 在「通用词表」界面却选了不像通用表的文件 → 询问是否为专用词表。
            is_dedicated = messagebox.askyesno(
                "加载词表",
                "当前位于「通用词表」界面，但所选文件不像通用词表。\n\n"
                "该文件是否属于「专用词表」？\n"
                "选择“是”将切换到专用词表界面并加载；选择“否”将作为通用词表加载。",
                parent=self,
            )
            if is_dedicated:
                self.glossary_mode = "dedicated"
                self._update_glossary_controls()
                glossary = Glossary.load(path)
                removed = glossary.dedupe()
                self.glossary = glossary
                self._refresh_tree()
                detail = f"，清理重复 {removed} 条" if removed else ""
                self.log(f"词表已加载：{path}，共 {len(glossary.entries)} 条{detail}")
                return
        elif self.glossary_mode == "dedicated" and is_common_file:
            # 在「专用词表」界面却识别到通用词表 → 自动切换到通用词表界面。
            self.glossary_mode = "common"
            self._update_glossary_controls()

        glossary = Glossary.load(path)
        removed = glossary.dedupe()
        if self.glossary_mode == "common":
            self.common_glossary = glossary
            self.common_glossary.save(common_glossary_path())
        else:
            self.glossary = glossary
        self._refresh_tree()
        detail = f"，清理重复 {removed} 条" if removed else ""
        self.log(f"词表已加载：{path}，共 {len(glossary.entries)} 条{detail}")


    def _looks_like_common_glossary(self, path) -> bool:
        """判断文件是否更像“通用词表”。

        先按固定文件名匹配；否则看内容特征：专用词表不允许 ko 为空，
        存在“无韩文原文但有可匹配来源”的词条即可判定为通用词表。
        """
        try:
            if Path(path).resolve() == common_glossary_path().resolve():
                return True
            return Glossary.load(Path(path)).looks_common()
        except Exception:  # noqa: BLE001
            return False

    def _clear_glossary(self):
        """清空当前词表，便于换一本新书重新提取。"""
        glossary = self._current_glossary()
        if not glossary.entries:
            messagebox.showinfo("提示", "当前词表已是空的", parent=self)
            return
        if not messagebox.askyesno(
            "确认清空",
            "确定要清空当前词表吗？\n已确认的词条也会一并移除，此操作不可撤销。",
            parent=self,
        ):
            return
        if self.glossary_mode == "common":
            self.common_glossary = Glossary()
            self.common_glossary.save(common_glossary_path())
        else:
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
                    book = load_book(
                        path,
                        txt_patterns=config.txt_patterns,
                        drop_zero=config.ignore_zero_chapters,
                    )
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
                    book = load_book(
                        self.input_files[0],
                        txt_patterns=config.txt_patterns,
                        drop_zero=config.ignore_zero_chapters,
                    )
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
        glossary = self._current_glossary()
        if not glossary.entries:
            messagebox.showinfo("提示", "当前没有词表", parent=self)
            return
        removed = glossary.dedupe()
        self._refresh_tree()
        if self.glossary_mode == "common":
            self.common_glossary.save(common_glossary_path())
        if removed:
            self.log(f"已清理重复词条：{removed} 条")
            messagebox.showinfo("完成", f"已清理 {removed} 条重复词条。", parent=self)
        else:
            messagebox.showinfo("提示", "没有发现重复词条。", parent=self)
