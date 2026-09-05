from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, messagebox

from ...epub_correction import (
    FormatTemplate,
    _DEFAULT_PRODUCTION_LINES,
    _DEFAULT_PRODUCTION_TITLE,
    _DEFAULT_WORD_COUNT_TEMPLATE,
    _style_dict_to_css,
)
from ..theme import THEME


def _str_value(value: object) -> str:
    return "" if value is None else str(value)


_DECORATION_OPTIONS = ["无", "下划线", "删除线", "上划线"]
_DECORATION_CSS = {
    "无": "",
    "下划线": "underline",
    "删除线": "line-through",
    "上划线": "overline",
}


def _decoration_label(css: str) -> str:
    for label, value in _DECORATION_CSS.items():
        if value == (css or ""):
            return label
    return "无"


def _parse_font_size(value: object) -> float:
    if not value:
        return 16
    text = str(value).strip()
    try:
        if text.endswith("em"):
            return max(8, min(72, round(14 * float(text[:-2]), 1)))
        if text.endswith("px"):
            return max(8, min(72, round(float(text[:-2]) * 0.75, 1)))
        return max(8, min(72, float(text)))
    except Exception:  # noqa: BLE001
        return 16


def _margin_px(value: object) -> int:
    if not value:
        return 8
    text = str(value).strip()
    try:
        if text.endswith("em"):
            return max(0, int(round(14 * float(text[:-2]))))
        if text.endswith("px"):
            return max(0, int(round(float(text[:-2]))))
        return max(0, int(round(float(text))))
    except Exception:  # noqa: BLE001
        return 8


def _anchor_for(align: object) -> str:
    return {"center": "center", "left": "w", "right": "e"}.get(align or "", "center")


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class FormatTemplateEditDialog(ctk.CTkToplevel):
    """编辑制作说明文字模板与每章字数统计格式；侧边选项便于后续扩展模板类型。"""

    def __init__(self, master, template: FormatTemplate | None = None, on_apply=None):
        super().__init__(master)
        self.title("编辑格式模板")
        self.geometry("860x640")
        self.minsize(760, 560)
        self.on_apply = on_apply
        self.transient(master)
        self.grab_set()
        self._template = template or FormatTemplate()
        self._option_buttons: dict[str, ctk.CTkButton] = {}
        self._pages: dict[str, ctk.CTkFrame] = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="编辑格式模板（制作说明 / 字数统计）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, columnspan=2, padx=12, pady=(14, 6), sticky="w")

        sidebar = ctk.CTkFrame(self, width=176, fg_color=THEME["card_alt"], corner_radius=6)
        sidebar.grid(row=1, column=0, padx=(12, 4), pady=(0, 8), sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=1, column=1, padx=(4, 12), pady=(0, 8), sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, columnspan=2, padx=12, pady=(0, 12), sticky="ew")
        ctk.CTkButton(
            bottom, text="恢复默认", width=96,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._restore_default,
        ).grid(row=0, column=0, padx=(0, 8), sticky="w")
        ctk.CTkButton(
            bottom, text="取消", width=96,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self.destroy,
        ).grid(row=0, column=2, padx=8, sticky="e")
        ctk.CTkButton(
            bottom, text="确定", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"],
            command=self._ok,
        ).grid(row=0, column=3, padx=(8, 0), sticky="e")

        self._add_option(sidebar, "production", "制作说明模板", "🅰 文字模板")
        self._add_option(sidebar, "word_count", "字数统计格式", "✎ 格式设计")
        self._add_option(sidebar, "title", "标题排版", "🔠 标题样式")
        self._build_production_page()
        self._build_word_count_page()
        self._build_title_page()
        self._switch("production")

    # ---------------- 侧边选项 ----------------
    def _add_option(self, parent, key: str, label: str, emoji: str):
        btn = ctk.CTkButton(
            parent, text=f"{emoji}  {label}", height=40, anchor="w",
            fg_color="transparent", text_color=THEME["text_main"],
            hover_color=THEME["secondary_hover"],
            command=lambda k=key: self._switch(k),
        )
        btn.grid(row=len(self._option_buttons), column=0, padx=6, pady=4, sticky="ew")
        self._option_buttons[key] = btn

    def _switch(self, key: str):
        for page in self._pages.values():
            page.grid_remove()
        for name, btn in self._option_buttons.items():
            if name == key:
                btn.configure(
                    fg_color=THEME["primary"], text_color="#FFFFFF",
                    hover_color=THEME["primary_hover"],
                )
            else:
                btn.configure(
                    fg_color="transparent", text_color=THEME["text_main"],
                    hover_color=THEME["secondary_hover"],
                )
        self._pages[key].grid(row=0, column=0, sticky="nsew")

    # ---------------- 样式控件 ----------------
    def _style_widget(self, parent, row: int, label: str, style: dict):
        """构建一组内联样式控件（含色盘取色），返回对应 tk 变量字典。"""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=0, padx=0, pady=(2, 4), sticky="ew")
        style_vars = {
            "center": tk.BooleanVar(value=(style.get("align") == "center")),
            "bold": tk.BooleanVar(value=bool(style.get("bold"))),
            "italic": tk.BooleanVar(value=bool(style.get("italic"))),
            "color": tk.StringVar(value=_str_value(style.get("color"))),
            "font_size": tk.StringVar(value=_str_value(style.get("font_size"))),
            "font_family": tk.StringVar(value=_str_value(style.get("font_family"))),
            "letter_spacing": tk.StringVar(value=_str_value(style.get("letter_spacing"))),
            "line_height": tk.StringVar(value=_str_value(style.get("line_height"))),
            "margin_bottom": tk.StringVar(value=_str_value(style.get("margin_bottom"))),
            "decoration": tk.StringVar(value=_decoration_label(style.get("text_decoration"))),
        }
        ctk.CTkLabel(frame, text=label, width=86, anchor="w").grid(
            row=0, column=0, padx=(0, 6), sticky="w"
        )
        ctk.CTkCheckBox(frame, text="居中", variable=style_vars["center"], width=64).grid(
            row=0, column=1, padx=2, sticky="w"
        )
        ctk.CTkCheckBox(frame, text="加粗", variable=style_vars["bold"], width=64).grid(
            row=0, column=2, padx=2, sticky="w"
        )
        ctk.CTkCheckBox(frame, text="斜体", variable=style_vars["italic"], width=64).grid(
            row=0, column=3, padx=2, sticky="w"
        )
        ctk.CTkLabel(frame, text="颜色").grid(row=0, column=4, padx=(10, 4), sticky="w")
        self._build_color_picker(frame, style_vars["color"], col=5)
        ctk.CTkLabel(frame, text="装饰").grid(row=0, column=7, padx=(8, 4), sticky="w")
        ctk.CTkComboBox(
            frame, values=_DECORATION_OPTIONS, variable=style_vars["decoration"], width=92,
            state="readonly",
        ).grid(row=0, column=8, padx=2, sticky="w")

        ctk.CTkLabel(frame, text="字号").grid(row=1, column=0, padx=(0, 4), sticky="w")
        ctk.CTkEntry(frame, textvariable=style_vars["font_size"], width=70).grid(
            row=1, column=1, padx=2, sticky="w"
        )
        ctk.CTkLabel(frame, text="字体").grid(row=1, column=2, padx=(8, 4), sticky="w")
        ctk.CTkEntry(frame, textvariable=style_vars["font_family"], width=110).grid(
            row=1, column=3, padx=2, sticky="w"
        )
        ctk.CTkLabel(frame, text="字间距").grid(row=1, column=4, padx=(8, 4), sticky="w")
        ctk.CTkEntry(frame, textvariable=style_vars["letter_spacing"], width=60).grid(
            row=1, column=5, padx=2, sticky="w"
        )
        ctk.CTkLabel(frame, text="行高").grid(row=1, column=6, padx=(8, 4), sticky="w")
        ctk.CTkEntry(frame, textvariable=style_vars["line_height"], width=60).grid(
            row=1, column=7, padx=2, sticky="w"
        )
        ctk.CTkLabel(frame, text="底间距").grid(row=1, column=8, padx=(8, 4), sticky="w")
        ctk.CTkEntry(frame, textvariable=style_vars["margin_bottom"], width=60).grid(
            row=1, column=9, padx=2, sticky="w"
        )
        return style_vars

    def _build_color_picker(self, parent, var, col: int):
        hex_label = ctk.CTkLabel(parent, text=var.get() or "默认", width=60, anchor="w")
        hex_label.grid(row=0, column=col + 1, padx=(2, 6), sticky="w")
        swatch = ctk.CTkButton(
            parent, width=44, height=28, text="",
            fg_color=var.get() or THEME["card"],
            hover_color=var.get() or THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"],
            command=lambda: self._pick_color(var, swatch, hex_label),
        )
        swatch.grid(row=0, column=col, padx=2, sticky="w")

    def _pick_color(self, var, swatch, hex_label):
        current = var.get().strip()
        try:
            _, hex_color = colorchooser.askcolor(
                color=current or "#FFFFFF", title="选择颜色", parent=self
            )
        except Exception:  # noqa: BLE001
            return
        if hex_color:
            var.set(hex_color)
            swatch.configure(fg_color=hex_color, hover_color=hex_color)
            hex_label.configure(text=hex_color)

    def _read_style(self, vars: dict) -> dict:
        style: dict = {}
        if vars.get("center") and vars["center"].get():
            style["align"] = "center"
        if vars.get("bold") and vars["bold"].get():
            style["bold"] = True
        if vars.get("italic") and vars["italic"].get():
            style["italic"] = True
        for key in (
            "color",
            "font_size",
            "font_family",
            "letter_spacing",
            "line_height",
            "margin_bottom",
        ):
            value = (vars.get(key) or "").get().strip() if vars.get(key) else ""
            if value:
                style[key] = value
        decoration = _DECORATION_CSS.get(
            (vars.get("decoration") or "").get(), ""
        ) if vars.get("decoration") else ""
        if decoration:
            style["text_decoration"] = decoration
        return style

    # ---------------- 制作说明页面 ----------------
    def _build_production_page(self):
        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)

        self.title_var = tk.StringVar(value=self._template.production_title)
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, padx=0, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(header, text="章节标题").grid(row=0, column=0, padx=(0, 8), sticky="w")
        ctk.CTkEntry(header, textvariable=self.title_var).grid(
            row=0, column=1, sticky="ew"
        )
        header.grid_columnconfigure(1, weight=1)

        self.body_style_vars = self._style_widget(
            page, 1, "正文样式", self._template.production_style
        )

        ctk.CTkLabel(
            page, text="制作说明正文（每行一条，支持占位符）", anchor="w"
        ).grid(row=2, column=0, padx=0, pady=(4, 2), sticky="w")
        self.lines_textbox = ctk.CTkTextbox(page, height=240, wrap="none")
        self.lines_textbox.grid(row=3, column=0, padx=0, pady=(0, 6), sticky="nsew")
        self.lines_textbox.insert(
            "1.0", "\n".join(self._template.production_lines or [])
        )
        page.grid_rowconfigure(3, weight=1)

        help_frame = ctk.CTkFrame(page, fg_color=THEME["card_alt"], corner_radius=6)
        help_frame.grid(row=4, column=0, padx=0, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            help_frame,
            text="可用占位符:\n"
            "${generated_at} 生成时间   ${total_chars} 全书总字数(自动千分位)\n"
            "${minutes} 阅读时长   ${chapter_count} 章节总数(也可用 ${chapteramount})   ${cover_font} 封面字体",
            justify="left", anchor="w", text_color=THEME["text_muted"],
        ).grid(row=0, column=0, padx=8, pady=6, sticky="w")

        preview_row = ctk.CTkFrame(page, fg_color="transparent")
        preview_row.grid(row=5, column=0, padx=0, pady=(0, 4), sticky="ew")
        ctk.CTkButton(
            preview_row, text="预览制作说明", width=116,
            command=self._preview_production,
        ).grid(row=0, column=0, sticky="w")
        self.prod_preview = ctk.CTkTextbox(page, height=120)
        self.prod_preview.grid(row=6, column=0, padx=0, pady=(0, 0), sticky="nsew")
        self.prod_preview.configure(state="disabled")
        self._pages["production"] = page

    def _preview_production(self):
        tpl = self._current_template()
        body = tpl.render_note(
            total_chars=2_544_524,
            minutes=6361,
            chapter_count=673,
            cover_font="source.ttf",
        )
        self._render_preview(self.prod_preview, "\n".join(body))

    # ---------------- 字数统计页面 ----------------
    def _build_word_count_page(self):
        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)

        self.wc_template_var = tk.StringVar(value=self._template.word_count_template)
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, padx=0, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(header, text="每章字数模板").grid(row=0, column=0, padx=(0, 8), sticky="w")
        ctk.CTkEntry(header, textvariable=self.wc_template_var).grid(
            row=0, column=1, sticky="ew"
        )
        header.grid_columnconfigure(1, weight=1)

        self.wc_style_vars = self._style_widget(
            page, 1, "字数样式", self._template.word_count_style
        )
        ctk.CTkLabel(
            page,
            text="${chars} 会替换为每章实际字数；样式会自动转成内联 CSS 写入 EPub，"
            "例如 居中/颜色/字号/底间距。",
            justify="left", anchor="w", text_color=THEME["text_muted"],
        ).grid(row=2, column=0, padx=0, pady=(6, 6), sticky="w")

        preview_row = ctk.CTkFrame(page, fg_color="transparent")
        preview_row.grid(row=3, column=0, padx=0, pady=(0, 4), sticky="ew")
        ctk.CTkButton(
            preview_row, text="预览效果", width=116, command=self._preview_word_count
        ).grid(row=0, column=0, sticky="w")
        self.wc_preview_text = ctk.CTkEntry(page, state="readonly")
        self.wc_preview_text.grid(row=4, column=0, padx=0, pady=0, sticky="ew")
        self.wc_preview_css = ctk.CTkTextbox(page, height=90)
        self.wc_preview_css.grid(row=5, column=0, padx=0, pady=(6, 0), sticky="nsew")
        self.wc_preview_css.configure(state="disabled")
        page.grid_rowconfigure(5, weight=1)
        self._pages["word_count"] = page

    def _preview_word_count(self):
        tpl = self._current_template()
        line = tpl.render_word_count(4277)
        self.wc_preview_text.configure(state="normal")
        self.wc_preview_text.delete(0, "end")
        self.wc_preview_text.insert(0, line)
        self.wc_preview_text.configure(state="readonly")
        css = _style_dict_to_css(tpl.word_count_style)
        self._render_preview(self.wc_preview_css, "内联样式：\n" + (css or "无（普通段落）"))

    # ---------------- 标题排版页面 ----------------
    def _build_title_page(self):
        page = ctk.CTkFrame(self.content, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)

        self.title_enabled_var = tk.BooleanVar(value=self._template.title_enabled)
        switch_frame = ctk.CTkFrame(page, fg_color=THEME["card_alt"], corner_radius=6)
        switch_frame.grid(row=0, column=0, padx=0, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(
            switch_frame, text="开启标题 CSS 排版",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        self.title_switch = ctk.CTkSwitch(
            switch_frame, text="", variable=self.title_enabled_var,
            command=self._on_title_switch, width=46,
        )
        self.title_switch.grid(row=0, column=1, padx=(0, 10), sticky="e")

        self.title_style_vars = self._style_widget(
            page, 1, "标题排版", self._template.title_style
        )
        ctk.CTkLabel(
            page,
            text="开启后，会把标题样式写入独立 CSS 文件（.chapter-title），"
            "并给每个章节标题加上该 class 关联。",
            justify="left", anchor="w", text_color=THEME["text_muted"],
        ).grid(row=2, column=0, padx=0, pady=(6, 6), sticky="w")

        preview_row = ctk.CTkFrame(page, fg_color="transparent")
        preview_row.grid(row=3, column=0, padx=0, pady=(0, 4), sticky="ew")
        ctk.CTkButton(
            preview_row, text="刷新预览", width=120, command=self._refresh_title_preview
        ).grid(row=0, column=0, sticky="w")
        self._build_title_visual(page)
        self.title_preview = ctk.CTkTextbox(page, height=130)
        self.title_preview.grid(row=5, column=0, padx=0, pady=(6, 0), sticky="nsew")
        self.title_preview.configure(state="disabled")
        page.grid_rowconfigure(4, weight=1)
        self._pages["title"] = page
        self._bind_title_traces()

    def _on_title_switch(self):
        self._refresh_title_preview()

    def _build_title_visual(self, page):
        self.title_visual = ctk.CTkFrame(page, fg_color=THEME["card"], corner_radius=6)
        self.title_visual.grid(row=4, column=0, padx=0, pady=(0, 4), sticky="nsew")
        self.title_visual.grid_columnconfigure(0, weight=1)
        self.title_visual_label = ctk.CTkLabel(
            self.title_visual, text="第一章 起因 (1)", anchor="center"
        )
        self.title_visual_label.grid(row=0, column=0, padx=12, pady=(10, 2), sticky="ew")
        self.body_labels: list[ctk.CTkLabel] = []
        for i, text in enumerate(
            [
                "这是正文第一段，用来演示标题排版的实时效果。",
                "字号、行高、字间距、装饰线会在这里即时体现。",
                "—— 下一章的标题也会沿用到同样的排版 ——",
            ]
        ):
            lbl = ctk.CTkLabel(
                self.title_visual, text=text, anchor="w",
                text_color=THEME["text_main"], font=ctk.CTkFont(size=12),
            )
            lbl.grid(row=i + 1, column=0, padx=12, pady=1, sticky="w")
            self.body_labels.append(lbl)

    def _bind_title_traces(self):
        for var in list(self.title_style_vars.values()):
            try:
                var.trace_add("write", lambda *a: self._refresh_title_preview())
            except Exception:  # noqa: BLE001
                pass
        try:
            self.title_enabled_var.trace_add(
                "write", lambda *a: self._refresh_title_preview()
            )
        except Exception:  # noqa: BLE001
            pass

    def _refresh_title_preview(self):
        tpl = self._current_template()
        css = tpl.title_css()
        if not css:
            content = "标题 CSS 排版未开启，将按原样式输出。"
        else:
            content = "标题样式（写入 Styles 下的 CSS 文件并关联 .chapter-title）：\n\n" + css
        self._render_preview(self.title_preview, content)
        self._refresh_title_visual()

    def _refresh_title_visual(self):
        style = self._read_style(self.title_style_vars)
        decoration = style.get("text_decoration") or ""
        try:
            font = ctk.CTkFont(
                family=(style.get("font_family") or "Microsoft YaHei"),
                size=_parse_font_size(style.get("font_size")),
                weight="bold" if style.get("bold") else "normal",
                slant="italic" if style.get("italic") else "roman",
                underline="underline" in decoration,
                overstrike="line-through" in decoration,
            )
        except Exception:  # noqa: BLE001
            font = ctk.CTkFont(size=16, weight="bold")
        self.title_visual_label.configure(
            font=font,
            text_color=(style.get("color") or THEME["text_main"]),
            anchor=_anchor_for(style.get("align")),
        )

    # ---------------- 通用 ----------------
    def _current_template(self) -> FormatTemplate:
        lines = self.lines_textbox.get("1.0", "end").rstrip("\n").split("\n")
        return FormatTemplate(
            production_title=(self.title_var.get().strip() or "制作说明"),
            production_lines=[line for line in lines if line.strip()],
            production_style=self._read_style(self.body_style_vars),
            word_count_template=(self.wc_template_var.get().strip() or "(本章字数: ${chars})"),
            word_count_style=self._read_style(self.wc_style_vars),
            title_enabled=bool(self.title_enabled_var.get()),
            title_style=self._read_style(self.title_style_vars),
        )

    def _render_preview(self, textbox, content: str):
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        textbox.insert("end", content)
        textbox.configure(state="disabled")

    def _restore_default(self):
        if not messagebox.askyesno("确认", "恢复为内置默认模板？", parent=self):
            return
        self._template = FormatTemplate()
        self.title_var.set(_DEFAULT_PRODUCTION_TITLE)
        self.lines_textbox.delete("1.0", "end")
        self.lines_textbox.insert("1.0", "\n".join(_DEFAULT_PRODUCTION_LINES))
        self.wc_template_var.set(_DEFAULT_WORD_COUNT_TEMPLATE)
        self.title_enabled_var.set(False)
        for style_vars in (
            self.title_style_vars,
            self.body_style_vars,
            self.wc_style_vars,
        ):
            self._reset_style_vars(style_vars)
        self._refresh_title_preview()

    def _reset_style_vars(self, vars: dict):
        for key in (
            "center",
            "bold",
            "italic",
        ):
            if key in vars:
                vars[key].set(False)
        for key in (
            "color",
            "font_size",
            "font_family",
            "letter_spacing",
            "line_height",
            "margin_bottom",
        ):
            if key in vars:
                vars[key].set("")
        if "decoration" in vars:
            vars["decoration"].set("无")

    def _ok(self):
        tpl = self._current_template()
        if self.on_apply:
            self.on_apply(tpl)
        self.destroy()
