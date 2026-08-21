# hangul_novel_translator/gui.py
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

from typing import Any
from .sanitizer import CustomRule, ExportSanitizer, SanitizerConfig
from .book import load_book
from .config import AppConfig
from .epub_fixer import fix_finished_epub_in_place, preview_finished_epub
from .glossary import Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary
from .llm import LLMClient
from .merge import book_from_state, export_merged, inspect_state, merge_books, preview_fix
from .translator import (
    TranslationCancelled,
    TranslationResult,
    Translator,
    collect_sample_text_strided,
    format_sample_chapters,
    sample_chapter_report,
)


# 配置固定在项目根目录，避免因启动目录不同导致读不到/写错位置。
_UI_CONFIG_PATH = Path(__file__).resolve().parent.parent / ".gui_config.json"


def _load_ui_state() -> dict[str, Any]:
    try:
        if _UI_CONFIG_PATH.exists():
            return json.loads(_UI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_ui_state(state: dict[str, Any]) -> None:
    try:
        _UI_CONFIG_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


class TreeviewTooltip:
    """悬停浮窗：当 Treeview 单元格文本较长时，鼠标悬停展示完整内容/路径。"""

    def __init__(self, tree: ttk.Treeview):
        self.tree = tree
        self.tip_window: tk.Toplevel | None = None
        self.last_item: str | None = None
        self.last_col: str | None = None
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._on_leave)

    def _on_motion(self, event):
        item = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        if not item or not column:
            self._hide()
            return
        if item == self.last_item and column == self.last_col and self.tip_window:
            return
        self.last_item = item
        self.last_col = column
        try:
            col_idx = int(column.replace("#", "")) - 1
            values = self.tree.item(item, "values")
            if not values or col_idx >= len(values):
                self._hide()
                return
            text = str(values[col_idx]).strip()
        except Exception:
            self._hide()
            return
        if not text:
            self._hide()
            return
        if len(text) > 16 or "\\" in text or "/" in text or "\n" in text:
            self._show(event.x_root + 15, event.y_root + 15, text)
        else:
            self._hide()

    def _show(self, x: int, y: int, text: str):
        self._hide()
        self.tip_window = tw = tk.Toplevel(self.tree)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        label = tk.Label(
            tw,
            text=text,
            justify=tk.LEFT,
            background="#2b2b2b",
            foreground="#f0f0f0",
            relief=tk.SOLID,
            borderwidth=1,
            font=("Segoe UI", 10),
            padx=8,
            pady=4,
            wraplength=600,
        )
        label.pack(ipadx=1)

    def _hide(self):
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None
        self.last_item = None
        self.last_col = None

    def _on_leave(self, _event):
        self._hide()


# ---------------- Theme & UI Style Configuration ----------------
THEME = {
    "bg": "#0D1117",
    "card": "#161B22",
    "card_border": "#30363D",
    "input_bg": "#0D1117",
    "accent": "#238636",
    "accent_hover": "#2EA043",
    "primary": "#1F6FEB",
    "primary_hover": "#388BFD",
    "danger": "#DA3633",
    "danger_hover": "#F85149",
    "secondary": "#21262D",
    "secondary_hover": "#30363D",
    "text_main": "#F0F6FC",
    "text_muted": "#8B949E",
}


def _apply_ttk_theme(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        "Custom.Treeview",
        background="#161B22",
        foreground="#E6EDF3",
        fieldbackground="#161B22",
        rowheight=36,
        font=("Microsoft YaHei UI", 12),
        borderwidth=0,
    )
    style.map(
        "Custom.Treeview",
        background=[("selected", "#1F6FEB")],
        foreground=[("selected", "#FFFFFF")],
    )
    style.configure(
        "Custom.Treeview.Heading",
        background="#21262D",
        foreground="#C9D1D9",
        font=("Microsoft YaHei UI", 12, "bold"),
        relief="flat",
        padding=(8, 6),
    )
    style.map(
        "Custom.Treeview.Heading",
        background=[("active", "#30363D")],
        foreground=[("active", "#58A6FF")],
    )


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
        from .sanitizer import ExportSanitizer

        # 用当前弹窗的开关即时构建一个临时配置做预览。
        temp = SanitizerConfig(
            enabled=True,
            strip_numbers=self.strip_numbers_var.get(),
            strip_json_residue=self.strip_json_var.get(),
            fix_quotes=self.fix_quotes_var.get(),
            polish_punctuation=self.polish_punct_var.get(),
            custom_rules=list(self.config.custom_rules),
        )
        result = ExportSanitizer(temp).clean_paragraph(self.test_text_var.get())
        self.preview_result_var.set(f"清洗后：{result}")

    def _ok(self):
        self.config.strip_numbers = self.strip_numbers_var.get()
        self.config.strip_json_residue = self.strip_json_var.get()
        self.config.fix_quotes = self.fix_quotes_var.get()
        self.config.polish_punctuation = self.polish_punct_var.get()
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
            try:
                self.geometry(saved_geom)
            except Exception:
                self.geometry("1180x760")
        else:
            self.geometry("1180x760")
        self.minsize(960, 640)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 用户拖动/移动窗口时防抖落盘，即使进程被强杀也能保留上次尺寸。
        self.bind("<Configure>", self._on_window_configure, add="+")
        _apply_ttk_theme(self)

        self.glossary = Glossary()
        self._extract_round = 0
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
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        left = ctk.CTkFrame(self, width=420)
        left.grid(row=0, column=0, padx=12, pady=12, sticky="nsw")
        left.grid_columnconfigure(1, weight=1)
        self._build_left(left)

        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, padx=(0, 12), pady=12, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(right)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tab_glossary = self.tabs.add("词表")
        self.tab_glossary.grid_columnconfigure(0, weight=1)
        self.tab_glossary.grid_rowconfigure(1, weight=1)
        self.tab_merge = self.tabs.add("多卷修正")
        self.tab_fixer = self.tabs.add("成品矫正")
        self._build_right(self.tab_glossary)
        self._build_merge_tab(self.tab_merge)
        self._build_fixer_tab(self.tab_fixer)

        bottom = ctk.CTkFrame(self)
        bottom.grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 12), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)
        self._build_bottom(bottom)

    def _build_left(self, parent):
        row = 0
        ctk.CTkLabel(parent, text="书籍文件（多本=同一小说的不同卷，按顺序）", anchor="w").grid(
            row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w"
        )
        self.input_files: list[Path] = []
        input_style = ttk.Style(self)
        input_style.configure("Input.Treeview", font=tkfont.Font(size=13), rowheight=30)
        input_style.configure("Input.Treeview.Heading", font=tkfont.Font(size=13, weight="bold"))
        list_frame = ctk.CTkFrame(parent, fg_color="transparent")
        list_frame.grid(row=row + 1, column=0, columnspan=2, padx=12, pady=2, sticky="ew")
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
        self.input_tree.column("file", width=450, minwidth=280, anchor="w", stretch=False)
        self.input_tree.grid(row=0, column=0, sticky="nsew")
        input_scroll = ctk.CTkScrollbar(list_frame, command=self.input_tree.yview)
        input_scroll.grid(row=0, column=1, sticky="ns")
        input_xscroll = ctk.CTkScrollbar(list_frame, orientation="horizontal", command=self.input_tree.xview)
        input_xscroll.grid(row=1, column=0, sticky="ew")
        self.input_tree.configure(yscrollcommand=input_scroll.set, xscrollcommand=input_xscroll.set)
        TreeviewTooltip(self.input_tree)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.grid(row=row + 2, column=0, columnspan=2, padx=12, pady=2, sticky="ew")
        ctk.CTkButton(btns, text="＋ 添加文件", width=105, fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._add_input_files).grid(row=0, column=0, padx=4)
        ctk.CTkButton(btns, text="↑ 上移", width=56, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=lambda: self._move_input_file(-1)).grid(row=0, column=1, padx=4)
        ctk.CTkButton(btns, text="↓ 下移", width=56, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=lambda: self._move_input_file(1)).grid(row=0, column=2, padx=4)
        ctk.CTkButton(btns, text="✕ 移除", width=56, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._remove_input_files).grid(row=0, column=3, padx=4)
        ctk.CTkButton(btns, text="🗑 清空", width=56, fg_color=THEME["secondary"], hover_color=THEME["danger"], border_width=1, border_color=THEME["card_border"], command=self._clear_input_files).grid(row=0, column=4, padx=4)

        row += 3
        ctk.CTkLabel(parent, text="小说名（可空，留空则按源文件名命名）", anchor="w").grid(
            row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w"
        )
        self.novel_name_var = tk.StringVar(value="")
        ctk.CTkEntry(parent, textvariable=self.novel_name_var).grid(
            row=row + 1, column=0, columnspan=2, padx=12, pady=2, sticky="ew"
        )

        row += 2
        ctk.CTkLabel(parent, text="输出目录", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.output_var = tk.StringVar(value=str(Path.cwd() / "output"))
        ctk.CTkEntry(parent, textvariable=self.output_var).grid(row=row + 1, column=0, columnspan=2, padx=12, pady=2, sticky="ew")
        ctk.CTkButton(parent, text="📁 选择输出目录", width=140, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._choose_output).grid(
            row=row + 2, column=0, columnspan=2, padx=12, pady=4, sticky="w"
        )

        row += 3
        ctk.CTkLabel(parent, text="API 设置", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.base_url_var = tk.StringVar(value="http://localhost:8045/v1")
        self.api_key_var = tk.StringVar(value="sk-8d9f9de70d2b42a3b350ab332d8619f0")
        self.model_var = tk.StringVar(value="gemini-3.7-flash-tiered")

        ctk.CTkLabel(parent, text="Base URL").grid(row=row + 1, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.base_url_var).grid(row=row + 1, column=1, padx=12, pady=2, sticky="ew")
        ctk.CTkLabel(parent, text="API Key").grid(row=row + 2, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.api_key_var, show="*").grid(row=row + 2, column=1, padx=12, pady=2, sticky="ew")
        ctk.CTkLabel(parent, text="Model").grid(row=row + 3, column=0, padx=12, pady=2, sticky="w")
        ctk.CTkEntry(parent, textvariable=self.model_var).grid(row=row + 3, column=1, padx=12, pady=2, sticky="ew")

        row += 4
        ctk.CTkLabel(parent, text="翻译参数", anchor="w").grid(row=row, column=0, columnspan=2, padx=12, pady=(14, 2), sticky="w")
        self.chunk_var = tk.StringVar(value="1800")
        self.workers_var = tk.StringVar(value="1")
        self.extract_var = tk.BooleanVar(value=True)
        self.txt_var = tk.BooleanVar(value=True)
        self.epub_var = tk.BooleanVar(value=True)

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
        style.configure("Glossary.Treeview", font=tkfont.Font(size=13), rowheight=34)
        style.configure("Glossary.Treeview.Heading", font=tkfont.Font(size=13, weight="bold"))

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
        self.merge_title_var = tk.StringVar(value="多卷合集")
        self.merge_txt_var = tk.BooleanVar(value=True)
        self.merge_epub_var = tk.BooleanVar(value=True)

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
        ctk.CTkEntry(cfg_frame, textvariable=self.merge_title_var, width=240).grid(row=0, column=1, padx=4, sticky="w")
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
        ctk.CTkButton(run_frame, text="🔄 校验并重试", width=120, fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"], border_width=1, border_color=THEME["card_border"], command=self._merge_retry_async).grid(row=0, column=2, padx=4)

        ctk.CTkLabel(
            parent,
            text="提示：每个存档需与其对应的原书（.txt/.epub）保持在原路径；修正按当前已加载词表执行（含译名历史自动替换）。点“校验并重试”可补翻失败块（最多 3 次/块），仍有失败块的存档不会参与“修正并输出”。",
            wraplength=620,
            justify="left",
            anchor="w",
        ).grid(row=5, column=0, padx=12, pady=(0, 12), sticky="w")

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

    def _build_bottom(self, parent):
        status_bar = ctk.CTkFrame(parent, fg_color="transparent")
        status_bar.grid(row=0, column=0, padx=12, pady=(10, 4), sticky="ew")
        status_bar.grid_columnconfigure(1, weight=1)
        self.progress = ctk.CTkProgressBar(status_bar, width=260)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=(0, 12))
        self.status_var = tk.StringVar(value="就绪")
        ctk.CTkLabel(status_bar, textvariable=self.status_var, anchor="w").grid(row=0, column=1, sticky="ew")
        self.start_btn = ctk.CTkButton(status_bar, text="▶ 开始翻译", width=120, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["accent"], hover_color=THEME["accent_hover"], command=self._start_translation)
        self.start_btn.grid(row=0, column=2, padx=6)
        self.stop_btn = ctk.CTkButton(status_bar, text="⏹ 停止", width=80, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["danger"], hover_color=THEME["danger_hover"], command=self._stop)
        self.stop_btn.grid(row=0, column=3, padx=6)
        self.stop_btn.configure(state="disabled")

        self.log_box = ctk.CTkTextbox(parent, height=110)
        self.log_box.grid(row=1, column=0, padx=12, pady=(4, 10), sticky="ew")
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
            self.after(0, lambda: self._on_error(str(exc)))

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
            self.after(0, lambda: self._on_error(str(exc)))

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
            self.input_tree.insert("", "end", iid=str(index), values=(index + 1, str(path)))

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
        return AppConfig(
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
            chunk_chars=chunk_chars,
            max_workers=workers,
            extract_glossary=self.extract_var.get(),
            output_txt=self.txt_var.get(),
            output_epub=self.epub_var.get(),
            sanitizer_config=self.sanitizer_config,
        )

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
        for index, path in enumerate(self.merge_files):
            try:
                info = inspect_state(path)
                detail = (
                    f"{info['title']} · 完成 {info['completed']}/{info['total_chunks']} 块"
                )
            except Exception as exc:  # noqa: BLE001
                detail = f"读取失败：{exc}"
            self.merge_tree.insert("", "end", iid=str(index), values=(index + 1, str(path), detail))

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
                self.log(
                    f"重试：{item['path'].name} 共 {item['found']} 块失败，"
                    f"恢复 {item['recovered']} 块，仍失败 {item['still_failed']} 块"
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
