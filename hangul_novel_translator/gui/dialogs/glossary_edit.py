# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import tkinter as tk
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from ...glossary import (Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary)
try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

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
