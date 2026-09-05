# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import threading
import tkinter as tk
from pathlib import Path
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from ...config import AppConfig
from ...glossary import (Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary)
from ...llm import (LLMCancelled, LLMClient)
from ...translator import (FailedChunk, MalformedBlock, TranslationCancelled, TranslationResult, Translator, collect_sample_text_strided, detect_malformed_blocks, format_sample_chapters, load_failed_chunks, reconcile_paragraphs, repair_malformed_blocks, save_manual_translation, sample_chapter_report)
from ...utils import (coerce_raw_to_paragraphs, extract_json, parse_paragraphs_from_payload)
from ..theme import THEME


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

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
