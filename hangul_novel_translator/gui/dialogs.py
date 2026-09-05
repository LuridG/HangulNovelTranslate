# hangul_novel_translator/gui/dialogs.py
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
from ..merge import audit_translation_state, book_from_state, export_merged, inspect_state, merge_books, preview_fix, repair_image_state, review_translation_state
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
    MalformedBlock,
    TranslationCancelled,
    TranslationResult,
    Translator,
    collect_sample_text_strided,
    detect_malformed_blocks,
    format_sample_chapters,
    load_failed_chunks,
    reconcile_paragraphs,
    repair_malformed_blocks,
    save_manual_translation,
    sample_chapter_report,
)
from ..utils import coerce_raw_to_paragraphs, extract_json, parse_paragraphs_from_payload


class RetranslateResponseError(RuntimeError):
    """回翻时模型响应无法解析成 JSON，携带原始响应供弹窗展示/人工判读。"""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


# 配置固定在项目根目录，避免因启动目录不同导致读不到/写错位置。

from .theme import THEME
class GlossaryEditDialog(ctk.CTkToplevel):
    def __init__(self, master, entry: GlossaryEntry | None = None):
        super().__init__(master)
        self.title("编辑词条")
        self.geometry("560x490")
        self.grab_set()
        self.result: GlossaryEntry | None = None
        self.entry = entry or GlossaryEntry("", "", "term")

        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="韩文原文").grid(row=0, column=0, padx=12, pady=(18, 6), sticky="w")
        self.ko_var = tk.StringVar(value=self.entry.ko)
        ctk.CTkEntry(self, textvariable=self.ko_var).grid(row=0, column=1, padx=12, pady=(18, 6), sticky="ew")

        ctk.CTkLabel(self, text="中文译名").grid(row=1, column=0, padx=12, pady=6, sticky="w")
        self.zh_var = tk.StringVar(value=self.entry.zh)
        ctk.CTkEntry(self, textvariable=self.zh_var).grid(row=1, column=1, padx=12, pady=6, sticky="ew")

        ctk.CTkLabel(self, text="类型").grid(row=2, column=0, padx=12, pady=6, sticky="w")
        self.kind_var = tk.StringVar(value=self.entry.kind)
        ctk.CTkComboBox(
            self,
            values=["person", "person-nickname", "place", "org", "term", "title"],
            variable=self.kind_var,
            width=180,
        ).grid(row=2, column=1, padx=12, pady=6, sticky="w")

        ctk.CTkLabel(self, text="备注").grid(row=3, column=0, padx=12, pady=6, sticky="w")
        self.note_var = tk.StringVar(value=self.entry.note)
        ctk.CTkEntry(self, textvariable=self.note_var).grid(row=3, column=1, padx=12, pady=6, sticky="ew")

        ctk.CTkLabel(self, text="可能翻译（半角逗号分隔）").grid(row=4, column=0, padx=12, pady=6, sticky="w")
        self.alt_var = tk.StringVar(value=self.entry.alternatives)
        ctk.CTkEntry(self, textvariable=self.alt_var).grid(row=4, column=1, padx=12, pady=6, sticky="ew")

        self.confirmed_var = tk.BooleanVar(value=self.entry.confirmed)
        ctk.CTkCheckBox(self, text="已人工确认", variable=self.confirmed_var).grid(
            row=5, column=0, columnspan=2, padx=12, pady=12, sticky="w"
        )

        self.replace_short_var = tk.BooleanVar(value=self.entry.replace_short)
        ctk.CTkCheckBox(
            self,
            text="替换短称（可能译法中的简称也替换为全名，如“范镇”→“崔范镇”）",
            variable=self.replace_short_var,
        ).grid(row=6, column=0, columnspan=2, padx=12, pady=6, sticky="w")

        ctk.CTkButton(self, text="确定", command=self._ok).grid(
            row=7, column=0, columnspan=2, pady=16
        )

    def _ok(self):
        ko = self.ko_var.get().strip()
        zh = self.zh_var.get().strip()
        alternatives = self.alt_var.get().strip()
        if not ko:
            messagebox.showwarning("提示", "韩文原文不能为空", parent=self)
            return
        if not zh:
            first = next((x.strip() for x in alternatives.split(",") if x.strip()), "")
            if first:
                zh = first
            else:
                messagebox.showwarning("提示", "中文译名不能为空（可先填写“可能翻译”，自动取第一个作为译名）", parent=self)
                return
        short = [x.strip() for x in alternatives.split(",") if x.strip() and len(x.strip()) < 3]
        if short:
            if not messagebox.askyesno(
                "提示",
                "以下“可能翻译”过短（1-2 字），回传时容易被误替换：\n"
                + "、".join(short)
                + "\n\n建议写得更完整（例如补上姓氏）后再确认。\n仍要保存吗？",
                parent=self,
            ):
                return
        self.result = GlossaryEntry(
            ko=ko,
            zh=zh,
            kind=self.kind_var.get().strip() or "term",
            note=self.note_var.get().strip(),
            confirmed=self.confirmed_var.get(),
            alternatives=alternatives,
            replace_short=self.replace_short_var.get(),
        )
        self.destroy()


class SanitizerRuleDialog(ctk.CTkToplevel):
    """导出清洗器规则配置弹窗：内置规则开关 + 自定义正则/文本替换规则 + 实时预览。"""

    def __init__(self, master, config: SanitizerConfig, on_apply):
        super().__init__(master)
        self.title("自定义清洗规则")
        self.geometry("800x620")
        self.minsize(720, 520)
        self.config = config
        self.on_apply = on_apply
        self.transient(master)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            self, text="内置规则", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, padx=12, pady=(14, 4), sticky="w")
        self.strip_numbers_var = tk.BooleanVar(value=config.strip_numbers)
        self.strip_json_var = tk.BooleanVar(value=config.strip_json_residue)
        self.fix_quotes_var = tk.BooleanVar(value=config.fix_quotes)
        self.polish_punct_var = tk.BooleanVar(value=config.polish_punctuation)
        self.collapse_quotes_var = tk.BooleanVar(value=config.collapse_quotes)
        builtin = ctk.CTkFrame(self, fg_color="transparent")
        builtin.grid(row=1, column=0, padx=12, pady=(0, 6), sticky="ew")
        ctk.CTkCheckBox(
            builtin, text="自动清除段首段落编号 [1] / 1. 等", variable=self.strip_numbers_var
        ).grid(row=0, column=0, padx=(0, 18), sticky="w")
        ctk.CTkCheckBox(
            builtin, text='自动清除 JSON 结构残渣 {"paragraphs": 等', variable=self.strip_json_var
        ).grid(row=1, column=0, padx=(0, 18), sticky="w")
        ctk.CTkCheckBox(
            builtin, text='自动修正外层未剥离半角引号 "', variable=self.fix_quotes_var
        ).grid(row=2, column=0, padx=(0, 18), sticky="w")
        ctk.CTkCheckBox(
            builtin, text="标点美化（... → ……、重复感叹/问号收敛）", variable=self.polish_punct_var
        ).grid(row=3, column=0, padx=(0, 18), sticky="w")
        ctk.CTkCheckBox(
            builtin, text='折叠连叠引号/清理引号残留（防“四个引号”““““/””””、"text",）', variable=self.collapse_quotes_var
        ).grid(row=4, column=0, padx=(0, 18), sticky="w")

        rules_header = ctk.CTkFrame(self, fg_color="transparent")
        rules_header.grid(row=2, column=0, padx=12, pady=(6, 4), sticky="ew")
        rules_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            rules_header, text="自定义规则", font=ctk.CTkFont(size=14, weight="bold")
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            rules_header, text="＋ 添加", width=70, command=self._add_rule
        ).grid(row=0, column=1, padx=4)
        ctk.CTkButton(
            rules_header, text="✎ 编辑", width=70,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._edit_rule,
        ).grid(row=0, column=2, padx=4)
        ctk.CTkButton(
            rules_header, text="✕ 删除", width=70,
            fg_color=THEME["secondary"], hover_color=THEME["danger"],
            border_width=1, border_color=THEME["card_border"], command=self._delete_rule,
        ).grid(row=0, column=3, padx=4)

        tree_frame = ctk.CTkFrame(self)
        tree_frame.grid(row=3, column=0, padx=12, pady=(0, 6), sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        self.rules_tree = ttk.Treeview(
            tree_frame,
            columns=("type", "pattern", "replace", "enabled"),
            show="headings",
            height=8,
            style="Custom.Treeview",
        )
        self.rules_tree.heading("type", text="类型")
        self.rules_tree.heading("pattern", text="查找内容")
        self.rules_tree.heading("replace", text="替换为")
        self.rules_tree.heading("enabled", text="启用")
        self.rules_tree.column("type", width=80, anchor="center")
        self.rules_tree.column("pattern", width=360, anchor="w")
        self.rules_tree.column("replace", width=180, anchor="w")
        self.rules_tree.column("enabled", width=60, anchor="center")
        self.rules_tree.grid(row=0, column=0, sticky="nsew")
        rules_scroll = ctk.CTkScrollbar(tree_frame, command=self.rules_tree.yview)
        rules_scroll.grid(row=0, column=1, sticky="ns")
        self.rules_tree.configure(yscrollcommand=rules_scroll.set)
        self.rules_tree.bind("<Double-1>", lambda _e: self._edit_rule())
        self._refresh_rules()

        preview_frame = ctk.CTkFrame(self, fg_color="transparent")
        preview_frame.grid(row=4, column=0, padx=12, pady=(0, 6), sticky="ew")
        preview_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(preview_frame, text="测试文本").grid(row=0, column=0, padx=(4, 8), sticky="w")
        self.test_text_var = tk.StringVar(value="[1] 这是一个测试段落…")
        ctk.CTkEntry(preview_frame, textvariable=self.test_text_var).grid(
            row=0, column=1, padx=4, sticky="ew"
        )
        ctk.CTkButton(preview_frame, text="▶ 测试", width=70, command=self._run_test).grid(
            row=0, column=2, padx=4
        )
        self.preview_result_var = tk.StringVar(value="")
        ctk.CTkLabel(
            preview_frame, textvariable=self.preview_result_var, anchor="w",
            wraplength=620, justify="left",
        ).grid(row=1, column=0, columnspan=3, padx=4, pady=(4, 0), sticky="w")
        self._run_test()

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=5, column=0, padx=12, pady=(0, 12), sticky="e")
        ctk.CTkButton(bottom, text="取消", width=90, command=self.destroy).grid(row=0, column=0, padx=6)
        ctk.CTkButton(
            bottom, text="确定", width=90,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._ok,
        ).grid(row=0, column=1, padx=6)

    def _refresh_rules(self):
        for item in self.rules_tree.get_children():
            self.rules_tree.delete(item)
        for i, rule in enumerate(self.config.custom_rules):
            self.rules_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    "正则" if rule.is_regex else "文本",
                    rule.pattern,
                    rule.replace,
                    "✓" if rule.enabled else "",
                ),
            )

    def _selected_index(self) -> int | None:
        sel = self.rules_tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def _add_rule(self):
        self._edit_rule(index=None)

    def _edit_rule(self, index: int | None = None):
        if index is None:
            index = self._selected_index()
        rule = self.config.custom_rules[index] if index is not None else CustomRule("")
        dialog = RuleEditDialog(self, rule)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if index is None:
            self.config.custom_rules.append(dialog.result)
        else:
            self.config.custom_rules[index] = dialog.result
        self._refresh_rules()
        self._run_test()

    def _delete_rule(self):
        index = self._selected_index()
        if index is None:
            return
        del self.config.custom_rules[index]
        self._refresh_rules()
        self._run_test()

    def _run_test(self):
        from ..sanitizer import ExportSanitizer

        # 用当前弹窗的开关即时构建一个临时配置做预览。
        temp = SanitizerConfig(
            enabled=True,
            strip_numbers=self.strip_numbers_var.get(),
            strip_json_residue=self.strip_json_var.get(),
            fix_quotes=self.fix_quotes_var.get(),
            polish_punctuation=self.polish_punct_var.get(),
            collapse_quotes=self.collapse_quotes_var.get(),
            custom_rules=list(self.config.custom_rules),
        )
        result = ExportSanitizer(temp).clean_paragraph(self.test_text_var.get())
        self.preview_result_var.set(f"清洗后：{result}")

    def _ok(self):
        self.config.strip_numbers = self.strip_numbers_var.get()
        self.config.strip_json_residue = self.strip_json_var.get()
        self.config.fix_quotes = self.fix_quotes_var.get()
        self.config.polish_punctuation = self.polish_punct_var.get()
        self.config.collapse_quotes = self.collapse_quotes_var.get()
        self.on_apply()
        self.destroy()


class RuleEditDialog(ctk.CTkToplevel):
    """单条自定义规则的编辑弹窗。"""

    def __init__(self, master, rule: CustomRule):
        super().__init__(master)
        self.title("编辑规则")
        self.geometry("520x260")
        self.transient(master)
        self.grab_set()
        self.result: CustomRule | None = None

        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text="模式类型").grid(row=0, column=0, padx=12, pady=(16, 4), sticky="w")
        self.is_regex_var = tk.BooleanVar(value=rule.is_regex)
        ctk.CTkCheckBox(
            self, text="使用正则表达式（否则按纯文本替换）", variable=self.is_regex_var
        ).grid(row=0, column=1, padx=12, pady=(16, 4), sticky="w")
        ctk.CTkLabel(self, text="查找内容").grid(row=1, column=0, padx=12, pady=4, sticky="w")
        self.pattern_var = tk.StringVar(value=rule.pattern)
        ctk.CTkEntry(self, textvariable=self.pattern_var).grid(row=1, column=1, padx=12, pady=4, sticky="ew")
        ctk.CTkLabel(self, text="替换为（可空）").grid(row=2, column=0, padx=12, pady=4, sticky="w")
        self.replace_var = tk.StringVar(value=rule.replace)
        ctk.CTkEntry(self, textvariable=self.replace_var).grid(row=2, column=1, padx=12, pady=4, sticky="ew")
        self.enabled_var = tk.BooleanVar(value=rule.enabled)
        ctk.CTkCheckBox(self, text="启用该规则", variable=self.enabled_var).grid(
            row=3, column=1, padx=12, pady=4, sticky="w"
        )
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=4, column=0, columnspan=2, padx=12, pady=(10, 12), sticky="e")
        ctk.CTkButton(bottom, text="取消", width=80, command=self.destroy).grid(row=0, column=0, padx=6)
        ctk.CTkButton(
            bottom, text="确定", width=80,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._ok,
        ).grid(row=0, column=1, padx=6)

    def _ok(self):
        pattern = self.pattern_var.get().strip()
        if not pattern:
            messagebox.showwarning("提示", "查找内容不能为空", parent=self)
            return
        self.result = CustomRule(
            pattern=pattern,
            replace=self.replace_var.get(),
            is_regex=self.is_regex_var.get(),
            enabled=self.enabled_var.get(),
        )
        self.destroy()


class FailedChunkEditorDialog(ctk.CTkToplevel):
    """失败块查看 / 手动编辑弹窗：列出多卷存档里的失败块，展示原文，可手动填译文或切模型翻译。"""

    def __init__(self, master, files: list[Path], config: AppConfig, glossary: Glossary):
        super().__init__(master)
        self.master = master
        self.config = config
        self.glossary = glossary
        self.files = list(files)
        self.items: list[FailedChunk] = []
        self.current: FailedChunk | None = None
        self._busy = False

        self.title("失败块查看 / 手动编辑")
        self.geometry("980x640")
        self.minsize(860, 560)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="失败块查看 / 手动编辑（原文已持久化在翻译存档里，可直接补翻）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        # 模型配置行：可手动切换模型去翻译对应原文。
        cfg_frame = ctk.CTkFrame(self, fg_color="transparent")
        cfg_frame.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        cfg_frame.grid_columnconfigure(1, weight=1)
        cfg_frame.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(cfg_frame, text="Base URL").grid(row=0, column=0, padx=(4, 6), sticky="w")
        self.base_url_var = tk.StringVar(value=config.base_url)
        ctk.CTkEntry(cfg_frame, textvariable=self.base_url_var, width=240).grid(row=0, column=1, padx=4, sticky="w")
        ctk.CTkLabel(cfg_frame, text="API Key").grid(row=0, column=2, padx=(14, 6), sticky="w")
        self.api_key_var = tk.StringVar(value=config.api_key)
        ctk.CTkEntry(cfg_frame, textvariable=self.api_key_var, width=180, show="*").grid(row=0, column=3, padx=4, sticky="w")
        ctk.CTkLabel(cfg_frame, text="Model").grid(row=0, column=4, padx=(14, 6), sticky="w")
        self.model_var = tk.StringVar(value=config.model)
        ctk.CTkEntry(cfg_frame, textvariable=self.model_var, width=200).grid(row=0, column=5, padx=4, sticky="w")
        self.translate_btn = ctk.CTkButton(
            cfg_frame, text="🔁 用所选模型翻译", width=140,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._translate_with_model,
        )
        self.translate_btn.grid(row=0, column=6, padx=(12, 4))

        # 主区域：左列表 + 右详情。
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        cols = ("archive", "chunk", "chapter", "flag", "error")
        self.tree = ttk.Treeview(
            left, columns=cols, show="headings", style="Custom.Treeview",
        )
        self.tree.heading("archive", text="存档")
        self.tree.heading("chunk", text="块 ID")
        self.tree.heading("chapter", text="章名")
        self.tree.heading("flag", text="标记")
        self.tree.heading("error", text="原因")
        self.tree.column("archive", width=180, anchor="w")
        self.tree.column("chunk", width=150, anchor="w")
        self.tree.column("chapter", width=120, anchor="w")
        self.tree.column("flag", width=70, anchor="center")
        self.tree.column("error", width=200, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(left, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(right, text="原文（韩文）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=4, sticky="w")
        self.src_box = ctk.CTkTextbox(right, height=170)
        self.src_box.grid(row=1, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.src_box.configure(state="disabled")
        ctk.CTkLabel(right, text="译文（每行一段，可手动编辑或先用模型翻译）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=2, column=0, padx=4, sticky="sw")
        self.out_box = ctk.CTkTextbox(right, height=170)
        self.out_box.grid(row=3, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.error_label = ctk.CTkLabel(right, text="", wraplength=560, justify="left", anchor="w")
        self.error_label.grid(row=4, column=0, padx=4, sticky="w")
        self.status_var = tk.StringVar(value="")
        self.status_label = ctk.CTkLabel(right, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=5, column=0, padx=4, pady=(2, 0), sticky="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=12, pady=(0, 12), sticky="ew")
        ctk.CTkButton(btn_frame, text="💾 保存译文", width=130, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._save_translation).grid(row=0, column=0, padx=4)
        ctk.CTkButton(btn_frame, text="🗑 放弃/关闭", width=120, command=self.destroy).grid(row=0, column=1, padx=4)
        ctk.CTkLabel(btn_frame, text="提示：保存后写入该存档 completed 并从失败列表移除，可继续“修正并输出”。").grid(row=0, column=2, padx=16, sticky="w")

        self._load_items()

    # ---------------- 加载与列表 ----------------
    def _load_items(self):
        self.items = []
        for f in self.files:
            try:
                self.items.extend(load_failed_chunks(f, self.config))
            except Exception as exc:  # noqa: BLE001
                self.master.log(f"读取失败：{Path(f).name}：{exc}")
        self._refresh_tree()
        if not self.items:
            self.status_var.set("所选存档没有失败块")
        else:
            self.status_var.set(f"共 {len(self.items)} 个失败块")

    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, item in enumerate(self.items):
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(
                    Path(item.archive).name,
                    item.chunk_id,
                    item.chapter_title or "-",
                    "缺原文" if item.missing else "",
                    item.error[:50],
                ),
            )

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.items[int(sel[0])]
        self.current = item
        self.src_box.configure(state="normal")
        self.src_box.delete("1.0", "end")
        self.src_box.insert("end", "\n\n".join(item.paragraphs) if item.paragraphs else "（该失败块未持久化原文，需核对原书）")
        self.src_box.configure(state="disabled")
        self.out_box.delete("1.0", "end")
        err = item.error or ""
        self.error_label.configure(text=f"块 ID：{item.chunk_id}    失败原因：{err}")

    # ---------------- 用所选模型翻译 ----------------
    def _translate_with_model(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先在左侧选择一个失败块", parent=self)
            return
        if not item.paragraphs:
            messagebox.showwarning("提示", "该失败块没有持久化原文，无法用模型翻译", parent=self)
            return
        if not self.model_var.get().strip():
            messagebox.showwarning("提示", "请先填写模型名", parent=self)
            return
        if self._busy:
            return
        self._busy = True
        self.translate_btn.configure(state="disabled")
        self.status_var.set("正在用所选模型翻译…")
        threading.Thread(target=self._do_translate, args=(item,), daemon=True).start()

    def _do_translate(self, item: FailedChunk):
        try:
            config = AppConfig(
                base_url=self.base_url_var.get().strip(),
                api_key=self.api_key_var.get().strip(),
                model=self.model_var.get().strip(),
            )
            llm = LLMClient(config)
            glossary_text = self.glossary.prompt_text() if self.glossary else ""
            system = (
                "你是一名资深的韩语小说中文译者。请把用户提供的韩语小说内容翻译成简体中文。\n"
                "要求：忠实原文，不增删情节，保留段落顺序与语气。\n"
                f"【专有名词词表】\n{glossary_text or '（无）'}\n\n"
                '【输出格式】只输出一个 JSON 对象：{"paragraphs": ["译文段落1", "译文段落2", ...]}，'
                "译文数组的段落数量必须与原文完全一致，顺序也必须一致。"
            )
            numbered = "\n".join(
                f"[{i}] {p}" for i, p in enumerate(item.paragraphs, start=1)
            )
            user = (
                f"章节：{item.chapter_title}\n原文段落数量：{len(item.paragraphs)}\n"
                "请把下面每个编号段落翻译成中文，并按编号顺序返回相同数量的译文段落。\n\n"
                f"{numbered}"
            )
            raw = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_mode=True,
            )
            payload = extract_json(raw)
            paras = parse_paragraphs_from_payload(payload)
            paras = reconcile_paragraphs(paras, len(item.paragraphs))
            paras = [self.glossary.apply_replacements(p) if self.glossary else p for p in paras]
            self.after(0, lambda: self._on_translate_done(paras))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_translate_error(message))

    def _on_translate_done(self, paras: list[str]):
        self._busy = False
        self.translate_btn.configure(state="normal")
        self.out_box.delete("1.0", "end")
        self.out_box.insert("end", "\n".join(paras))
        self.status_var.set("翻译完成，请人工核对后保存")

    def _on_translate_error(self, message: str):
        self._busy = False
        self.translate_btn.configure(state="normal")
        self.status_var.set("翻译失败")
        messagebox.showerror("翻译失败", message, parent=self)

    # ---------------- 保存手动译文 ----------------
    def _save_translation(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先在左侧选择一个失败块", parent=self)
            return
        text = self.out_box.get("1.0", "end")
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        if not lines:
            messagebox.showwarning("提示", "译文不能为空", parent=self)
            return
        if item.paragraphs:
            paras = reconcile_paragraphs(lines, len(item.paragraphs))
        else:
            paras = lines
        if self.glossary:
            paras = [self.glossary.apply_replacements(p) for p in paras]
        try:
            save_manual_translation(item.archive, item.chunk_id, paras)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"保存失败：{exc}", parent=self)
            return
        self.master.log(f"手动补翻成功：{Path(item.archive).name} {item.chunk_id}（{len(paras)} 段）")
        self.items = [
            x for x in self.items
            if not (Path(x.archive) == Path(item.archive) and x.chunk_id == item.chunk_id)
        ]
        self._refresh_tree()
        self.current = None
        self.src_box.configure(state="normal")
        self.src_box.delete("1.0", "end")
        self.src_box.configure(state="disabled")
        self.out_box.delete("1.0", "end")
        self.error_label.configure(text="")
        self.status_var.set(f"已保存，剩余 {len(self.items)} 个失败块")


class MalformedBlockEditorDialog(ctk.CTkToplevel):
    """畸形块查看 / 修复弹窗：列出多卷存档里的 JSON 塌缩、引号连叠、残渣残留块。

    提供三类动作：
      - 机器批量修复（不耗 token，救回后缺段用原文占位）；
      - 单块 / 批量回传 LLM 重翻（供用户自行选择）；
      - 示例按钮展示标准正常块，便于比对。
    """

    def __init__(self, master, files: list[Path], config: AppConfig, glossary: Glossary):
        super().__init__(master)
        self.master = master
        self.config = config
        self.glossary = glossary
        self.files = list(files)
        self.items: list[MalformedBlock] = []
        self.current: MalformedBlock | None = None
        self._busy = False
        self._batch_cancel = threading.Event()

        self.title("畸形块检测 / 修复")
        self.geometry("1060x720")
        self.minsize(940, 620)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="畸形块检测 / 修复（机器修复不耗 token；未能救回的可回传 LLM 重翻，或手动编辑）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        # 模型配置行：可手动切换模型去回翻对应块。
        cfg_frame = ctk.CTkFrame(self, fg_color="transparent")
        cfg_frame.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        cfg_frame.grid_columnconfigure(1, weight=1)
        cfg_frame.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(cfg_frame, text="Base URL").grid(row=0, column=0, padx=(4, 6), sticky="w")
        self.base_url_var = tk.StringVar(value=config.base_url)
        ctk.CTkEntry(cfg_frame, textvariable=self.base_url_var, width=220).grid(row=0, column=1, padx=4, sticky="w")
        ctk.CTkLabel(cfg_frame, text="API Key").grid(row=0, column=2, padx=(14, 6), sticky="w")
        self.api_key_var = tk.StringVar(value=config.api_key)
        ctk.CTkEntry(cfg_frame, textvariable=self.api_key_var, width=170, show="*").grid(row=0, column=3, padx=4, sticky="w")
        ctk.CTkLabel(cfg_frame, text="Model").grid(row=0, column=4, padx=(14, 6), sticky="w")
        self.model_var = tk.StringVar(value=config.model)
        ctk.CTkEntry(cfg_frame, textvariable=self.model_var, width=190).grid(row=0, column=5, padx=4, sticky="w")
        self.retranslate_btn = ctk.CTkButton(
            cfg_frame, text="🔁 回翻当前块", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._retranslate_current,
        )
        self.retranslate_btn.grid(row=0, column=6, padx=(12, 4))

        # 主区域：左列表 + 右详情。
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        cols = ("archive", "chunk", "issue", "flag", "detail")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", style="Custom.Treeview")
        self.tree.heading("archive", text="存档")
        self.tree.heading("chunk", text="块 ID")
        self.tree.heading("issue", text="类型")
        self.tree.heading("flag", text="修复")
        self.tree.heading("detail", text="说明")
        self.tree.column("archive", width=170, anchor="w")
        self.tree.column("chunk", width=140, anchor="w")
        self.tree.column("issue", width=120, anchor="w")
        self.tree.column("flag", width=60, anchor="center")
        self.tree.column("detail", width=180, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(left, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(right, text="原文（韩文）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=4, sticky="w")
        self.src_box = ctk.CTkTextbox(right, height=120)
        self.src_box.grid(row=1, column=0, padx=4, pady=(2, 6), sticky="nsew")
        self.src_box.configure(state="disabled")
        ctk.CTkLabel(right, text="当前损坏值", font=ctk.CTkFont(size=13, weight="bold")).grid(row=2, column=0, padx=4, sticky="sw")
        self.cur_box = ctk.CTkTextbox(right, height=110)
        self.cur_box.grid(row=3, column=0, padx=4, pady=(2, 6), sticky="nsew")
        self.cur_box.configure(state="disabled")
        ctk.CTkLabel(right, text="机器修复预览（可手动修改后再保存）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=4, column=0, padx=4, sticky="sw")
        self.repaired_box = ctk.CTkTextbox(right, height=140)
        self.repaired_box.grid(row=5, column=0, padx=4, pady=(2, 6), sticky="nsew")
        self.error_label = ctk.CTkLabel(right, text="", wraplength=600, justify="left", anchor="w")
        self.error_label.grid(row=6, column=0, padx=4, sticky="w")
        self.status_var = tk.StringVar(value="")
        self.status_label = ctk.CTkLabel(right, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=7, column=0, padx=4, pady=(2, 0), sticky="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.batch_repair_btn = ctk.CTkButton(
            btn_frame, text="🤖 批量修复", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._batch_repair_async,
        ).grid(row=0, column=0, padx=4)
        self.batch_retranslate_btn = ctk.CTkButton(
            btn_frame, text="📚 批量回翻", width=120,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1,
            border_color=THEME["card_border"], command=self._batch_retranslate_async,
        ).grid(row=0, column=1, padx=4)
        ctk.CTkButton(btn_frame, text="💾 保存当前修复", width=130, command=self._save_current).grid(row=0, column=2, padx=4)
        ctk.CTkButton(btn_frame, text="📋 示例", width=80, command=self._show_example).grid(row=0, column=3, padx=4)
        ctk.CTkButton(btn_frame, text="📂 加载词表", width=110, command=self._load_glossary_file).grid(row=0, column=4, padx=4)
        ctk.CTkButton(btn_frame, text="🗑 关闭", width=90, command=self.destroy).grid(row=0, column=5, padx=4)
        self.status_hint = ctk.CTkLabel(
            btn_frame,
            text="提示：机器修复以“救回 > 原文占位”为原则，缺段会用原文补位；无法救回的需回翻或手动编辑。",
            wraplength=520, justify="left", anchor="w",
        )
        self.status_hint.grid(row=1, column=0, columnspan=6, padx=4, pady=(8, 0), sticky="w")

        self._load_items()

    # ---------------- 加载与列表 ----------------
    def _load_items(self):
        self.items = []
        for f in self.files:
            try:
                self.items.extend(detect_malformed_blocks(f, self.config))
            except Exception as exc:  # noqa: BLE001
                self.master.log(f"畸形块检测失败：{Path(f).name}：{exc}")
        self._refresh_tree()
        if not self.items:
            self.status_var.set("所选存档没有畸形块")
        else:
            repairable = sum(1 for b in self.items if b.repairable)
            self.status_var.set(f"共 {len(self.items)} 个畸形块，其中 {repairable} 个可机器修复")

    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, item in enumerate(self.items):
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(
                    Path(item.archive).name,
                    item.chunk_id,
                    item.issue,
                    "✓" if item.repairable else "需处理",
                    (f"救回 {len(item.repaired)} 段" if item.repairable else "无法救回/需回翻"),
                ),
            )

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = self.items[int(sel[0])]
        self.current = item
        self.src_box.configure(state="normal")
        self.src_box.delete("1.0", "end")
        self.src_box.insert("end", "\n\n".join(item.source_paragraphs) if item.source_paragraphs else "（无持久化原文）")
        self.src_box.configure(state="disabled")
        self.cur_box.configure(state="normal")
        self.cur_box.delete("1.0", "end")
        if item.current_values:
            preview = "\n".join(item.current_values[:6])
            if len(item.current_values) > 6:
                preview += "\n…（共 %d 段）" % len(item.current_values)
            self.cur_box.insert("end", preview)
        self.cur_box.configure(state="disabled")
        self.repaired_box.delete("1.0", "end")
        if item.repaired:
            self.repaired_box.insert("end", "\n\n".join(item.repaired))
        self.error_label.configure(
            text=f"块 ID：{item.chunk_id}    类型：{item.issue}    原文 {len(item.source_paragraphs)} 段"
        )

    # ---------------- 机器批量修复 ----------------
    def _batch_repair_async(self):
        if self._busy:
            return
        targets = [b for b in self.items if b.repairable]
        if not targets:
            messagebox.showinfo("提示", "没有可机器修复的畸形块", parent=self)
            return
        self._batch_cancel.clear()
        self._busy = True
        self.batch_repair_btn.configure(state="disabled")
        self.status_var.set("正在机器批量修复…")
        threading.Thread(target=self._do_batch_repair, args=(targets,), daemon=True).start()

    def _do_batch_repair(self, targets):
        try:
            result = repair_malformed_blocks(targets)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_batch_failed(message))
            return
        self.after(0, lambda: self._on_batch_repair_done(result, targets))

    def _on_batch_repair_done(self, result, targets):
        fixed_ids = {b.chunk_id for b in targets}
        self.items = [b for b in self.items if b.chunk_id not in fixed_ids]
        self._refresh_tree()
        self._busy = False
        self.batch_repair_btn.configure(state="normal")
        remaining = len(self.items)
        self.status_var.set(
            f"机器修复：本次 {result['fixed']} 块，仍剩 {remaining} 块需处理"
        )
        messagebox.showinfo(
            "畸形块修复完成",
            f"本次机器自动修复了 {result['fixed']} 个畸形块，"
            f"{result['unresolved']} 个无法救回（已保留，可供回翻/手动）。\n"
            f"仍剩 {remaining} 块待处理。\n\n"
            "提示：机器修复中缺译的位置已用原文占位，可点“复查”识别韩文后重新翻译。",
            parent=self,
        )

    def _on_batch_failed(self, message: str):
        self._busy = False
        self.batch_repair_btn.configure(state="normal")
        self.status_var.set("批量修复失败")
        messagebox.showerror("修复失败", message, parent=self)

    # ---------------- 词表就绪检查 / 现场加载 ----------------
    def _ensure_glossary_ready(self) -> bool:
        """回翻前确保词表就绪：无有效词表时提醒，并可现场加载一份。

        返回 True 允许继续回翻（已加载词表，或用户明确选择按无词表继续）；
        返回 False 表示用户想加载词表但未成功，应中止本次回翻。
        """
        if self.glossary and self.glossary.valid_entries():
            return True
        ans = messagebox.askyesno(
            "未加载词表",
            "当前没有加载有效词表，回翻时专有名词可能不统一。\n"
            "是否现在选择并加载一份词表？选择“否”则按无词表继续回翻。",
            parent=self,
        )
        if not ans:
            return True
        if not self._load_glossary_file():
            return False
        return bool(self.glossary and self.glossary.valid_entries())

    def _load_glossary_file(self) -> bool:
        """弹出文件选择框加载词表到本弹窗（并同步主界面备用），返回是否加载成功。"""
        path = filedialog.askopenfilename(
            title="加载词表", filetypes=[("JSON", "*.json")], parent=self
        )
        if not path:
            return False
        try:
            loaded = Glossary.load(Path(path))
            removed = loaded.dedupe()
            self.glossary = loaded
            # 同步到主界面，便于后续“修正并输出”继续使用同一份词表。
            if hasattr(self.master, "glossary"):
                self.master.glossary = loaded
            if hasattr(self.master, "_refresh_tree"):
                self.master._refresh_tree()
            valid = len(loaded.valid_entries())
            detail = f"，清理重复 {removed} 条" if removed else ""
            self.status_var.set(f"词表已加载：{Path(path).name}，有效 {valid} 条{detail}")
            return True
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("加载失败", f"无法加载词表：{exc}", parent=self)
            return False

    # ---------------- 单块回翻 ----------------
    def _retranslate_current(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先在左侧选中一个畸形块", parent=self)
            return
        if not item.source_paragraphs:
            messagebox.showwarning("提示", "该块没有持久化原文，无法回翻", parent=self)
            return
        if not self.model_var.get().strip():
            messagebox.showwarning("提示", "请先填写模型名", parent=self)
            return
        if self._busy:
            return
        if not self._ensure_glossary_ready():
            return
        self._busy = True
        self.retranslate_btn.configure(state="disabled")
        self.status_var.set("正在回翻当前块…")
        threading.Thread(target=self._do_retranslate, args=(item,), daemon=True).start()

    def _do_retranslate(self, item):
        try:
            paras = self._llm_retranslate(item)
        except RetranslateResponseError as exc:
            # 解析失败时带上模型原始响应，让用户能判断能否手动抢救。
            message = str(exc)
            raw = exc.raw
            self.after(0, lambda: self._on_retranslate_error(message, raw))
            return
        except Exception as exc:  # noqa: BLE001
            # Python 3 会删除 except 里的异常变量，这里先取出字符串再排队。
            message = str(exc)
            self.after(0, lambda: self._on_retranslate_error(message))
            return
        self.after(0, lambda: self._on_retranslate_done(item, paras))

    def _llm_retranslate(self, item) -> list[str]:
        cfg = AppConfig(
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )
        llm = LLMClient(cfg)
        glossary_text = self.glossary.prompt_text() if self.glossary else ""
        system = (
            "你是一名资深的韩语小说中文译者。请把用户提供的韩语小说内容翻译成简体中文。\n"
            "要求：忠实原文，不增删情节，保留段落顺序与语气。\n"
            f"【专有名词词表】\n{glossary_text or '（无）'}\n\n"
            "【输出格式】只输出一个 JSON 对象，不要输出任何其他文字、说明、编号或 Markdown 代码块。\n"
            '格式：{"paragraphs": ["译文段落1", "译文段落2", ...]}\n'
            "规则：译文数组的段落数量必须与原文完全一致，顺序也必须一致；"
            "所有引号一律使用半角英文双引号，特殊字符使用 JSON 转义。"
        )
        numbered = "\n".join(f"[{i}] {p}" for i, p in enumerate(item.source_paragraphs, start=1))
        user = (
            f"章节：{item.chapter_title}\n原文段落数量：{len(item.source_paragraphs)}\n"
            "请把下面每个编号段落翻译成中文，并按编号顺序返回相同数量的译文段落。\n\n"
            f"{numbered}"
        )
        raw = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True)
        try:
            payload = extract_json(raw)
            paras = parse_paragraphs_from_payload(payload)
        except Exception as exc:
            raise RetranslateResponseError(str(exc), raw=str(raw)) from exc
        paras = reconcile_paragraphs(paras, len(item.source_paragraphs))
        if self.glossary:
            paras = [self.glossary.apply_replacements(p) for p in paras]
        return paras

    def _on_retranslate_done(self, item, paras):
        self._busy = False
        self.retranslate_btn.configure(state="normal")
        self.repaired_box.delete("1.0", "end")
        self.repaired_box.insert("end", "\n\n".join(paras))
        self.status_var.set("回翻完成，请人工核对后点“保存当前修复”")

    def _on_retranslate_error(self, message: str, raw: str | None = None):
        self._busy = False
        self.retranslate_btn.configure(state="normal")
        self.status_var.set("回翻失败")
        if raw:
            self._show_raw_response(message, raw)
            return
        messagebox.showerror("回翻失败", message, parent=self)

    def _show_raw_response(self, message: str, raw: str):
        """解析失败时展示模型原始响应，供用户判断能否使用 / 手动抢救。"""
        win = ctk.CTkToplevel(self)
        win.title("回翻失败 — 模型原始响应")
        win.geometry("760x600")
        win.minsize(620, 420)
        win.transient(self)
        win.grid_columnconfigure(0, weight=1)
        win.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            win,
            text=f"回翻失败：{message}\n下方是模型原始返回，可自行判断、修改后点“导入修复预览”；"
            "若完全不可用，请调整 Prompt/词表后重试。",
            wraplength=720, justify="left", anchor="w",
        ).grid(row=0, column=0, padx=12, pady=(14, 8), sticky="ew")

        txt = ctk.CTkTextbox(win, wrap="word", undo=True)
        txt.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        txt.insert("1.0", raw)

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.grid(row=2, column=0, padx=12, pady=(0, 12), sticky="w")

        def _import():
            content = txt.get("1.0", "end").strip()
            if not content:
                messagebox.showwarning("提示", "内容为空", parent=win)
                return
            # 先按原流程抢救一次（用户可能已手动改成合法 JSON/文本）；
            # 救不回就按行拆开，至少能看到、可复制。
            paras = coerce_raw_to_paragraphs(content) or [content]
            self.repaired_box.delete("1.0", "end")
            self.repaired_box.insert("end", "\n\n".join(paras))
            self.status_var.set("已导入原始响应到修复预览，请人工核对后再保存")
            win.destroy()

        def _copy():
            win.clipboard_clear()
            win.clipboard_append(txt.get("1.0", "end"))
            win.update()

        ctk.CTkButton(btns, text="✅ 导入修复预览", width=150, command=_import).grid(row=0, column=0, padx=(0, 6))
        ctk.CTkButton(btns, text="📋 复制", width=90, command=_copy).grid(row=0, column=1, padx=6)
        ctk.CTkButton(btns, text="🗑 关闭", width=90, command=win.destroy).grid(row=0, column=2, padx=6)

    # ---------------- 批量回翻 ----------------
    def _batch_retranslate_async(self):
        if self._busy:
            return
        targets = [b for b in self.items if not b.repairable]
        if not targets:
            messagebox.showinfo("提示", "没有需要回翻的畸形块", parent=self)
            return
        if not self.model_var.get().strip():
            messagebox.showwarning("提示", "请先填写模型名", parent=self)
            return
        if not self._ensure_glossary_ready():
            return
        self._batch_cancel.clear()
        self._busy = True
        self.batch_retranslate_btn.configure(state="disabled")
        self.status_var.set(f"正在批量回翻 {len(targets)} 块…")
        threading.Thread(target=self._do_batch_retranslate, args=(targets,), daemon=True).start()

    def _do_batch_retranslate(self, targets):
        ok = 0
        failed = 0
        ok_ids: list[str] = []
        errors: list[str] = []
        for idx, b in enumerate(targets):
            if self._batch_cancel.is_set():
                break
            try:
                paras = self._llm_retranslate(b)
                save_manual_translation(b.archive, b.chunk_id, paras)
                ok += 1
                ok_ids.append(b.chunk_id)
                self.after(0, lambda i=idx: self._on_batch_progress(i + 1, len(targets)))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                hint = ""
                if getattr(exc, "raw", None):
                    hint = "（原始响应可查看：选中该块后点“回翻当前块”）"
                errors.append(f"{b.chunk_id}: {exc}{hint}")
        self.after(0, lambda: self._on_batch_retranslate_done(ok, failed, errors, ok_ids))

    def _on_batch_progress(self, done, total):
        self.status_var.set(f"批量回翻 {done}/{total}…")

    def _on_batch_retranslate_done(self, ok, failed, errors, ok_ids):
        ok_ids = set(ok_ids)
        self.items = [b for b in self.items if b.chunk_id not in ok_ids]
        self._refresh_tree()
        self._busy = False
        self.batch_retranslate_btn.configure(state="normal")
        self.status_var.set(f"批量回翻完成：成功 {ok}，失败 {failed}，剩 {len(self.items)} 块")
        msg = f"批量回翻完成：成功 {ok} 块，失败 {failed} 块。\n剩 {len(self.items)} 块待处理。"
        if errors:
            msg += "\n\n失败原因：\n" + "\n".join(errors[:5])
        messagebox.showinfo("批量回翻", msg, parent=self)

    # ---------------- 保存当前修复 ----------------
    def _save_current(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先选中一个畸形块", parent=self)
            return
        text = self.repaired_box.get("1.0", "end")
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        if not lines:
            messagebox.showwarning("提示", "修复内容不能为空", parent=self)
            return
        if item.source_paragraphs:
            paras = reconcile_paragraphs(lines, len(item.source_paragraphs))
        else:
            paras = lines
        try:
            save_manual_translation(item.archive, item.chunk_id, paras)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"保存失败：{exc}", parent=self)
            return
        self.master.log(f"畸形块修复保存：{Path(item.archive).name} {item.chunk_id}（{len(paras)} 段）")
        self.items = [b for b in self.items if b.chunk_id != item.chunk_id]
        self._refresh_tree()
        self.current = None
        self.repaired_box.delete("1.0", "end")
        self.error_label.configure(text="")
        self.status_var.set(f"已保存，剩余 {len(self.items)} 个畸形块")

    # ---------------- 示例 ----------------
    def _show_example(self):
        example = (
            "【标准正常块示例】\n\n"
            "completed[\"ch-00000-00000\"] 应当是普通字符串列表：\n"
            '[\n'
            '  "“你好，伊森。”",\n'
            '  "“……嗯。”",\n'
            '  "那么，我们走吧。",\n'
            '  "（说明文字）"\n'
            ']\n\n'
            "不应出现的形态：\n"
            '❌ 整段 {"paragraphs": ["…", …]} 塞进第 1 段；\n'
            '❌ ““““……明白了。”””” 这类连叠引号；\n'
            '❌ "text", 这种带 JSON 半角引号/逗号的残留；\n'
            '❌ 只保留第 1 段、其余全是空串。'
        )
        messagebox.showinfo("正常块示例", example, parent=self)


class PerspectiveFailedEditorDialog(ctk.CTkToplevel):
    """视角转换失败块查看器：每个文本节点作为一个可编辑分段保存。"""

    def __init__(
        self,
        master,
        state_path: Path,
        config: AppConfig,
        glossary: Glossary,
        options: PerspectiveOptions,
    ):
        super().__init__(master)
        self.master = master
        self.state_path = Path(state_path)
        self.config = config
        self.glossary = glossary
        self.options = options
        self.items: list[PerspectiveFailedBlock] = []
        self.current: PerspectiveFailedBlock | None = None
        self._busy = False
        self._single_cancel_event = threading.Event()
        self._batch_active = False
        self._batch_cancel_event = threading.Event()
        self._close_requested = False

        self.title("视角转换失败块")
        self.geometry("1000x680")
        self.minsize(860, 580)
        self.protocol("WM_DELETE_WINDOW", self._close_dialog)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="视角转换失败块（原文已保存，可切换模型或手动编辑后写回）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        cfg = ctk.CTkFrame(self, fg_color="transparent")
        cfg.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        for column in (1, 3, 5):
            cfg.grid_columnconfigure(column, weight=1)
        ctk.CTkLabel(cfg, text="Base URL").grid(row=0, column=0, padx=(4, 6), sticky="w")
        self.base_url_var = tk.StringVar(value=config.base_url)
        ctk.CTkEntry(cfg, textvariable=self.base_url_var, width=220).grid(row=0, column=1, padx=4, sticky="ew")
        ctk.CTkLabel(cfg, text="API Key").grid(row=0, column=2, padx=(12, 6), sticky="w")
        self.api_key_var = tk.StringVar(value=config.api_key)
        ctk.CTkEntry(cfg, textvariable=self.api_key_var, width=180, show="*").grid(row=0, column=3, padx=4, sticky="ew")
        ctk.CTkLabel(cfg, text="Model").grid(row=0, column=4, padx=(12, 6), sticky="w")
        self.model_var = tk.StringVar(value=config.model)
        ctk.CTkEntry(cfg, textvariable=self.model_var, width=180).grid(row=0, column=5, padx=4, sticky="ew")
        self.translate_btn = ctk.CTkButton(
            cfg,
            text="🔁 用所选模型改写",
            width=140,
            fg_color=THEME["primary"],
            hover_color=THEME["primary_hover"],
            command=self._translate_with_model,
        )
        self.translate_btn.grid(row=0, column=6, padx=(12, 4))
        self.single_stop_btn = ctk.CTkButton(
            cfg,
            text="⏹ 停止当前",
            width=100,
            fg_color=THEME["danger"],
            hover_color=THEME["danger_hover"],
            command=self._stop_single_retry,
        )
        self.single_stop_btn.grid(row=0, column=7, padx=4)
        self.single_stop_btn.configure(state="disabled")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body)
        left.grid(row=0, column=0, padx=(0, 10), sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            left,
            columns=("block", "file", "kind", "error"),
            show="headings",
            style="Custom.Treeview",
        )
        for column, title, width in (
            ("block", "块 ID", 170),
            ("file", "正文文件", 150),
            ("kind", "类型", 95),
            ("error", "原因", 230),
        ):
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(left, command=tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=scroll.set)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree = tree

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(right, text="原文分段", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=4, sticky="w")
        self.source_box = ctk.CTkTextbox(right, height=180)
        self.source_box.grid(row=1, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.source_box.configure(state="disabled")
        ctk.CTkLabel(right, text="第三人称译文（保留分段标记，可直接编辑）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=2, column=0, padx=4, sticky="w")
        self.output_box = ctk.CTkTextbox(right, height=180)
        self.output_box.grid(row=3, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.error_label = ctk.CTkLabel(right, text="", wraplength=580, justify="left", anchor="w")
        self.error_label.grid(row=4, column=0, padx=4, sticky="w")
        self.status_var = tk.StringVar(value="")
        ctk.CTkLabel(right, textvariable=self.status_var, anchor="w").grid(row=5, column=0, padx=4, pady=(2, 0), sticky="w")

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, padx=12, pady=(0, 12), sticky="ew")
        self.save_btn = ctk.CTkButton(
            buttons,
            text="💾 保存当前块",
            width=130,
            fg_color=THEME["primary"],
            hover_color=THEME["primary_hover"],
            command=self._save_translation,
        )
        self.save_btn.grid(row=0, column=0, padx=4)
        self.batch_btn = ctk.CTkButton(
            buttons,
            text="▶ 批量重试失败块",
            width=145,
            fg_color=THEME["primary"],
            hover_color=THEME["primary_hover"],
            command=self._batch_retry_failed,
        )
        self.batch_btn.grid(row=0, column=1, padx=4)
        self.batch_stop_btn = ctk.CTkButton(
            buttons,
            text="⏹ 停止批量",
            width=100,
            fg_color=THEME["danger"],
            hover_color=THEME["danger_hover"],
            command=self._stop_batch_retry,
        )
        self.batch_stop_btn.grid(row=0, column=2, padx=4)
        self.batch_stop_btn.configure(state="disabled")
        self.close_btn = ctk.CTkButton(buttons, text="关闭", width=90, command=self._close_dialog)
        self.close_btn.grid(row=0, column=3, padx=4)
        self.batch_progress = ctk.CTkProgressBar(
            buttons,
            width=150,
            height=8,
            progress_color=THEME["primary"],
        )
        self.batch_progress.set(0)
        self.batch_progress.grid(row=0, column=4, padx=(16, 6))
        self.batch_status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            buttons,
            textvariable=self.batch_status_var,
            text_color=THEME["text_muted"],
            anchor="w",
        ).grid(row=0, column=5, padx=4, sticky="w")

        self._load_items()

    @staticmethod
    def _format_segments(segments: list[str]) -> str:
        return "\n\n".join(
            f"【分段 {index + 1}】\n{segment}"
            for index, segment in enumerate(segments)
        )

    @staticmethod
    def _parse_segments(text: str, count: int) -> list[str]:
        marker = re.compile(r"(?:^|\n)【分段\s*(\d+)】\s*\n")
        matches = list(marker.finditer(text))
        if count == 1 and not matches:
            value = text.strip()
            if not value:
                raise ValueError("译文不能为空")
            return [value]
        if len(matches) != count or [int(m.group(1)) for m in matches] != list(range(1, count + 1)):
            raise ValueError(f"请保留并按顺序填写全部 {count} 个“【分段 N】”标记")
        values: list[str] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = text[match.end():end].strip()
            if not value:
                raise ValueError(f"分段 {index + 1} 的译文不能为空")
            values.append(value)
        return values

    def _load_items(self):
        try:
            self.items = load_failed_perspective_blocks(self.state_path)
        except Exception as exc:  # noqa: BLE001
            self.items = []
            self.status_var.set(f"读取失败：{exc}")
        self._refresh_tree()
        if self.items:
            self.status_var.set(f"共 {len(self.items)} 个失败块")
        elif not self.status_var.get():
            self.status_var.set("当前没有失败块")

    def _refresh_tree(self, selected_block_id: str | None = None):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, item in enumerate(self.items):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(item.block_id, Path(item.file).name, item.classification, item.error[:80]),
            )
        if selected_block_id:
            for index, item in enumerate(self.items):
                if item.block_id == selected_block_id:
                    iid = str(index)
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        item = self.items[int(selection[0])]
        self.current = item
        source = self._format_segments(item.source_segments)
        translated = item.translated_segments or [""] * len(item.source_segments)
        self.source_box.configure(state="normal")
        self.source_box.delete("1.0", "end")
        self.source_box.insert("end", source)
        self.source_box.configure(state="disabled")
        self.output_box.delete("1.0", "end")
        self.output_box.insert("end", self._format_segments(translated))
        self.error_label.configure(text=f"块 ID：{item.block_id}    失败原因：{item.error}")

    def _translate_with_model(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先选择一个失败块", parent=self)
            return
        if self._busy:
            return
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if not base_url or not model:
            messagebox.showwarning("提示", "Base URL 和 Model 不能为空", parent=self)
            return
        self._busy = True
        self._close_requested = False
        self._single_cancel_event.clear()
        self.translate_btn.configure(state="disabled")
        self.single_stop_btn.configure(state="normal")
        self.batch_btn.configure(state="disabled")
        self.save_btn.configure(state="disabled")
        self.status_var.set("正在用所选模型改写…")
        threading.Thread(
            target=self._translate_worker,
            args=(item, base_url, api_key, model),
            daemon=True,
        ).start()

    def _translate_worker(self, item: PerspectiveFailedBlock, base_url: str, api_key: str, model: str):
        try:
            data = self.config.to_dict()
            data.update({"base_url": base_url, "api_key": api_key, "model": model})
            llm = LLMClient(AppConfig.from_dict(data))
            block = PerspectiveBlock(
                id=item.block_id,
                file=item.file,
                index=0,
                source_segments=item.source_segments,
                source_text="".join(item.source_segments),
                classification=item.classification,
            )
            translated = rewrite_blocks(
                llm,
                self.options,
                self.glossary,
                [block],
                cancel_event=self._single_cancel_event,
            )[item.block_id]
            self.after(0, lambda: self._on_model_done(translated))
        except LLMCancelled:
            self.after(0, self._on_model_cancelled)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_model_error(message))

    def _on_model_cancelled(self):
        self._busy = False
        self.single_stop_btn.configure(state="disabled")
        self.translate_btn.configure(state="normal")
        self.batch_btn.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.status_var.set("当前块请求已停止，原失败块仍保留")
        if self._close_requested:
            self.destroy()

    def _batch_retry_failed(self):
        if self._busy:
            return
        try:
            items = load_failed_perspective_blocks(self.state_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败块失败", str(exc), parent=self)
            return
        if not items:
            self.items = []
            self._refresh_tree()
            self.batch_progress.set(1)
            self.batch_status_var.set("没有待重试的失败块")
            self.status_var.set("当前没有失败块")
            return

        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        model = self.model_var.get().strip()
        if not base_url or not model:
            messagebox.showwarning("提示", "Base URL 和 Model 不能为空", parent=self)
            return

        self.items = items
        self._refresh_tree(self.current.block_id if self.current else None)
        self._busy = True
        self._batch_active = True
        self._close_requested = False
        self._batch_cancel_event.clear()
        self.translate_btn.configure(state="disabled")
        self.batch_btn.configure(state="disabled")
        self.batch_stop_btn.configure(state="normal")
        self.save_btn.configure(state="disabled")
        self.close_btn.configure(state="disabled")
        self.batch_progress.set(0)
        self.batch_status_var.set(f"批量重试：0/{len(items)}")
        self.status_var.set(f"批量重试失败块：0/{len(items)}")
        threading.Thread(
            target=self._batch_retry_worker,
            args=(items, base_url, api_key, model),
            daemon=True,
        ).start()

    def _batch_retry_worker(
        self,
        items: list[PerspectiveFailedBlock],
        base_url: str,
        api_key: str,
        model: str,
    ):
        completed = 0
        failed = 0
        processed = 0
        cancelled = False
        try:
            data = self.config.to_dict()
            data.update({"base_url": base_url, "api_key": api_key, "model": model})
            llm = LLMClient(AppConfig.from_dict(data))
            for item in items:
                if self._batch_cancel_event.is_set():
                    cancelled = True
                    break
                block = PerspectiveBlock(
                    id=item.block_id,
                    file=item.file,
                    index=0,
                    source_segments=item.source_segments,
                    source_text="".join(item.source_segments),
                    classification=item.classification,
                )
                try:
                    translated = rewrite_blocks(
                        llm,
                        self.options,
                        self.glossary,
                        [block],
                        cancel_event=self._batch_cancel_event,
                    )[item.block_id]
                    if self._batch_cancel_event.is_set():
                        cancelled = True
                        break
                    save_manual_perspective_translation(
                        self.state_path,
                        item.block_id,
                        translated,
                    )
                    completed += 1
                    success = True
                    error = ""
                except Exception as exc:  # noqa: BLE001
                    if self._batch_cancel_event.is_set():
                        cancelled = True
                        break
                    error = str(exc)
                    save_perspective_failure(self.state_path, item.block_id, error)
                    failed += 1
                    success = False
                processed += 1
                self.after(
                    0,
                    lambda block_id=item.block_id, ok=success, message=error, current=processed, total=len(items), good=completed, bad=failed: self._on_batch_item_done(
                        block_id, ok, message, current, total, good, bad
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            self._batch_active = False
            message = str(exc)
            self.after(0, lambda: self._on_batch_worker_error(message))
            return
        self.after(
            0,
            lambda: self._on_batch_finished(
                processed,
                len(items),
                completed,
                failed,
                cancelled or self._batch_cancel_event.is_set(),
            ),
        )

    def _on_batch_item_done(
        self,
        block_id: str,
        success: bool,
        error: str,
        processed: int,
        total: int,
        completed: int,
        failed: int,
    ):
        if success:
            selected_id = self.current.block_id if self.current else None
            self.items = [item for item in self.items if item.block_id != block_id]
            if selected_id == block_id:
                self.current = None
                self._clear_editor()
            self._refresh_tree(selected_id if selected_id != block_id else None)
        elif error:
            for item in self.items:
                if item.block_id == block_id:
                    item.error = error
                    break
            self._refresh_tree(self.current.block_id if self.current else None)
        self.batch_progress.set((processed / total) if total else 1)
        self.batch_status_var.set(f"批量重试：{processed}/{total}，成功 {completed}，失败 {failed}")
        self.status_var.set(self.batch_status_var.get())
        if not success and error:
            self.error_label.configure(text=f"最近失败块：{block_id}    原因：{error}")

    def _on_batch_finished(
        self,
        processed: int,
        total: int,
        completed: int,
        failed: int,
        cancelled: bool,
    ):
        self._busy = False
        self._batch_active = False
        close_requested = self._close_requested
        self._close_requested = False
        self.translate_btn.configure(state="normal")
        self.single_stop_btn.configure(state="disabled")
        self.batch_btn.configure(state="normal")
        self.batch_stop_btn.configure(state="disabled")
        self.save_btn.configure(state="normal")
        self.close_btn.configure(state="normal")
        self.batch_progress.set((processed / total) if total else 1)
        if cancelled:
            status = f"批量已停止：处理 {processed}/{total}，成功 {completed}，失败 {failed}"
        else:
            status = f"批量完成：成功 {completed}，失败 {failed}"
        self.batch_status_var.set(status)
        self._load_items()
        self.status_var.set(status)
        if close_requested:
            self.destroy()

    def _on_batch_worker_error(self, message: str):
        self._busy = False
        self._batch_active = False
        close_requested = self._close_requested
        self._close_requested = False
        self.translate_btn.configure(state="normal")
        self.single_stop_btn.configure(state="disabled")
        self.batch_btn.configure(state="normal")
        self.batch_stop_btn.configure(state="disabled")
        self.save_btn.configure(state="normal")
        self.close_btn.configure(state="normal")
        self._load_items()
        self.status_var.set("批量重试出错")
        if close_requested:
            self.destroy()
        else:
            messagebox.showerror("批量重试出错", message, parent=self)

    def _stop_batch_retry(self):
        if self._batch_active:
            self._batch_cancel_event.set()
            self.batch_stop_btn.configure(state="disabled")
            self.batch_status_var.set("正在停止批量…")

    def _stop_single_retry(self):
        if self._busy and not self._batch_active:
            self._single_cancel_event.set()
            self.single_stop_btn.configure(state="disabled")
            self.status_var.set("正在停止当前块请求…")

    def _close_dialog(self):
        if not self._busy:
            self.destroy()
            return
        self._close_requested = True
        if not self._batch_active:
            self._single_cancel_event.set()
            self.single_stop_btn.configure(state="disabled")
            self.status_var.set("正在停止当前块请求，完成后关闭窗口…")
            return
        if self._batch_cancel_event.is_set():
            return
        self._batch_cancel_event.set()
        self.batch_stop_btn.configure(state="disabled")
        self.batch_status_var.set("正在停止批量，完成后关闭窗口…")
        return

    def _clear_editor(self):
        self.source_box.configure(state="normal")
        self.source_box.delete("1.0", "end")
        self.source_box.configure(state="disabled")
        self.output_box.delete("1.0", "end")
        self.error_label.configure(text="")

    def _on_model_done(self, translated: list[str]):
        self._busy = False
        self.single_stop_btn.configure(state="disabled")
        self.translate_btn.configure(state="normal")
        self.batch_btn.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.insert("end", self._format_segments(translated))
        self.status_var.set("模型改写完成，请人工核对后保存")
        if self._close_requested:
            self.destroy()

    def _on_model_error(self, message: str):
        self._busy = False
        self.single_stop_btn.configure(state="disabled")
        self.translate_btn.configure(state="normal")
        self.batch_btn.configure(state="normal")
        self.save_btn.configure(state="normal")
        self.status_var.set("模型改写失败")
        if self._close_requested:
            self.destroy()
        else:
            messagebox.showerror("改写失败", message, parent=self)

    def _save_translation(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先选择一个失败块", parent=self)
            return
        try:
            translated = self._parse_segments(
                self.output_box.get("1.0", "end"),
                len(item.source_segments),
            )
            translated = [self.glossary.apply_replacements(value) for value in translated]
            save_manual_perspective_translation(self.state_path, item.block_id, translated)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.items = [value for value in self.items if value.block_id != item.block_id]
        self.current = None
        self._refresh_tree()
        self.source_box.configure(state="normal")
        self.source_box.delete("1.0", "end")
        self.source_box.configure(state="disabled")
        self.output_box.delete("1.0", "end")
        self.error_label.configure(text="")
        self.status_var.set(f"已保存，剩余 {len(self.items)} 个失败块")
