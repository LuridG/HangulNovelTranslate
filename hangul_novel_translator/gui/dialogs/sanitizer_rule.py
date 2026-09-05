# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import tkinter as tk
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from ...sanitizer import (CustomRule, ExportSanitizer, SanitizerConfig)
from ..theme import THEME


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

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
