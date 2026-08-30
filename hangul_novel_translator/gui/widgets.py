# hangul_novel_translator/gui/widgets.py
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class TreeviewTooltip:
    """悬停浮窗：当 Treeview 单元格文本较长时，鼠标悬停展示完整内容/路径。"""

    def __init__(self, tree: ttk.Treeview, text_provider=None):
        self.tree = tree
        self.text_provider = text_provider
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
            if self.text_provider is not None:
                provided = self.text_provider(item, col_idx)
                text = str(provided).strip() if provided is not None else ""
            else:
                text = str(values[col_idx]).strip()
        except Exception:
            self._hide()
            return
        if not text:
            self._hide()
            return
        if self.text_provider is not None or len(text) > 16 or "\\" in text or "/" in text or "\n" in text:
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

class DebouncedScrollableFrame(ctk.CTkScrollableFrame):
    """拖动缩放时把高频的 canvas 重排合并到空闲期执行，减少卡顿。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fit_after_id = None

    def _fit_frame_dimensions_to_canvas(self, event=None):
        if self._fit_after_id is not None:
            return
        self._fit_after_id = self.after_idle(self._do_fit_frame)

    def _do_fit_frame(self):
        self._fit_after_id = None
        try:
            if getattr(self, "_parent_canvas", None) is None:
                return
            if self._orientation == "horizontal":
                self._parent_canvas.itemconfigure(
                    self._create_window_id,
                    height=self._parent_canvas.winfo_height(),
                )
            else:
                self._parent_canvas.itemconfigure(
                    self._create_window_id,
                    width=self._parent_canvas.winfo_width(),
                )
        except Exception:
            pass
