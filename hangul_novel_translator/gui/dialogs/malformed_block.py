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
from ._common import RetranslateResponseError


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

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
