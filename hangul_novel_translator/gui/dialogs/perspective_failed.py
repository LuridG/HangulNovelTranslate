# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from ...config import AppConfig
from ...glossary import (Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary)
from ...llm import (LLMCancelled, LLMClient)
from ...perspective import (PerspectiveBlock, PerspectiveConverter, PerspectiveFailedBlock, PerspectiveOptions, inspect_perspective_epub, load_failed_perspective_blocks, load_perspective_state, perspective_state_path, reset_perspective_state, rewrite_blocks, save_perspective_failure, save_manual_perspective_translation)
from ..theme import THEME


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc

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
