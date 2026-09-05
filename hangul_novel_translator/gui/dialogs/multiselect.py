from __future__ import annotations

import tkinter as tk
from tkinter import messagebox


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class MultiSelectDialog(ctk.CTkToplevel):
    """带复选框的批量选择弹窗：items 为 [(key, label), ...]，返回选中的 key 列表。"""

    def __init__(
        self,
        master,
        title: str,
        items: list[tuple],
        *,
        preselect: list | None = None,
        width: int = 560,
        height: int = 520,
    ):
        super().__init__(master)
        self.title(title)
        self.geometry(f"{width}x{height}")
        self.minsize(420, 360)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.result: list | None = None
        self._vars: dict = {}

        header = ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        header.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")

        preselected = set(preselect or [])
        scroll = ctk.CTkScrollableFrame(self)
        scroll.grid(row=1, column=0, padx=12, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        if not items:
            ctk.CTkLabel(scroll, text="（没有可选项）", text_color="gray").grid(
                row=0, column=0, padx=8, pady=8, sticky="w"
            )
        for row, (key, label) in enumerate(items):
            var = tk.BooleanVar(value=key in preselected)
            self._vars[key] = var
            ctk.CTkCheckBox(scroll, text=str(label), variable=var).grid(
                row=row, column=0, padx=8, pady=4, sticky="w"
            )

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=12, pady=(8, 12), sticky="ew")
        btn_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=90,
            fg_color="transparent",
            border_width=1,
            command=self._cancel,
        ).grid(row=0, column=0, padx=4, sticky="e")
        ctk.CTkButton(
            btn_frame,
            text="确定",
            width=110,
            command=self._confirm,
        ).grid(row=0, column=1, padx=4, sticky="e")
        self.transient(master)
        self.grab_set()

    def _confirm(self):
        selected = [key for key, var in self._vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("提示", "请至少选择一个章节", parent=self)
            return
        self.result = selected
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
