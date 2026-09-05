# 由 tools/split_class.py 拆分生成

from __future__ import annotations

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
from tkinter import (filedialog, messagebox, simpledialog, ttk)
from typing import Any
from ...sanitizer import (CustomRule, ExportSanitizer, SanitizerConfig)
from ...book import load_book
from ...config import AppConfig
from ...epub_fixer import (fix_finished_epub_in_place, preview_finished_epub)
from ...glossary import (Glossary, GlossaryEntry, _MIN_ALTERNATIVE_LEN, enrich_glossary_with_nicknames, extract_glossary_with_llm, extract_more_glossary)
from ...llm import (LLMCancelled, LLMClient)
from ...merge import (archive_filename_title, audit_translation_state, book_from_state, detect_merge_title, export_merged, inspect_state, merge_books, preview_fix, repair_image_state, review_translation_state)
from ...perspective import (PerspectiveBlock, PerspectiveConverter, PerspectiveFailedBlock, PerspectiveOptions, inspect_perspective_epub, load_failed_perspective_blocks, load_perspective_state, perspective_state_path, reset_perspective_state, rewrite_blocks, save_perspective_failure, save_manual_perspective_translation)
from ...translator import (FailedChunk, TranslationCancelled, TranslationResult, Translator, collect_sample_text_strided, detect_malformed_blocks, format_sample_chapters, load_failed_chunks, reconcile_paragraphs, save_manual_translation, sample_chapter_report)
from ...utils import (extract_json, parse_paragraphs_from_payload)
from ..theme import (THEME, _apply_ttk_theme)
from ..state import (_load_ui_state, _save_ui_state, common_glossary_path)
from ..widgets import (TreeviewTooltip, DebouncedScrollableFrame)
from ..dialogs import (GlossaryEditDialog, SanitizerRuleDialog, FailedChunkEditorDialog, MalformedBlockEditorDialog, PerspectiveFailedEditorDialog)


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc



_DEFAULT_LEFT_WIDTH = 360


class ShellMixin:

    # ---------------- UI ----------------
    def _build_layout(self):
        self._left_panel_width = int(self.ui_state.get("left_panel_width", _DEFAULT_LEFT_WIDTH) or _DEFAULT_LEFT_WIDTH)
        self.grid_columnconfigure(0, weight=0, minsize=self._left_panel_width)
        self.grid_columnconfigure(1, weight=0, minsize=8)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0, minsize=156)

        left = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        left.grid(row=0, column=0, padx=(14, 0), pady=14, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        left_scroll = DebouncedScrollableFrame(left, fg_color="transparent")
        left_scroll.grid(row=0, column=0, sticky="nsew")
        left_scroll.grid_columnconfigure(1, weight=1)
        self._build_left(left_scroll)

        divider = ctk.CTkFrame(
            self,
            width=8,
            corner_radius=4,
            fg_color=THEME["card_border"],
            cursor="sb_h_double_arrow",
        )
        divider.grid(row=0, column=1, pady=14, sticky="ns")
        divider.bind("<ButtonPress-1>", self._on_divider_press)
        divider.bind("<B1-Motion>", self._on_divider_motion)
        divider.bind("<ButtonRelease-1>", self._on_divider_release)
        self._divider_active = False

        right = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        right.grid(row=0, column=2, padx=(0, 14), pady=14, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(right)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        self.tab_glossary = self.tabs.add("词表")
        self.tab_glossary.grid_columnconfigure(0, weight=1)
        self.tab_glossary.grid_rowconfigure(1, weight=1)
        self.tab_merge = self.tabs.add("多卷修正")
        self.tab_fixer = self.tabs.add("成品矫正")
        self.tab_settings = self.tabs.add("设置")
        self.tab_perspective = self.tabs.add("视角转换")
        self.tabs.configure(
            corner_radius=8,
            border_width=0,
            segmented_button_fg_color=THEME["card_alt"],
            segmented_button_selected_color=THEME["primary"],
            segmented_button_selected_hover_color=THEME["primary_hover"],
            segmented_button_unselected_color=THEME["card_alt"],
            segmented_button_unselected_hover_color=THEME["secondary_hover"],
        )
        self._build_right(self.tab_glossary)
        self._build_merge_tab(self.tab_merge)
        self._build_fixer_tab(self.tab_fixer)
        self._build_settings_tab(self.tab_settings)
        self._build_perspective_tab(self.tab_perspective)

        bottom = ctk.CTkFrame(self, corner_radius=8, border_width=1, border_color=THEME["card_border"])
        bottom.grid(row=1, column=0, columnspan=3, padx=14, pady=(0, 14), sticky="nsew")
        bottom.grid_columnconfigure(0, weight=1)
        self._build_bottom(bottom)


    def _on_divider_press(self, _event):
        self._divider_active = True


    def _on_divider_motion(self, event):
        if not self._divider_active:
            return
        try:
            pointer_x = event.x_root - self.winfo_rootx()
            total_w = self.winfo_width()
        except Exception:
            return
        min_left = 320
        min_right = 380
        left_w = max(min_left, pointer_x - 18)
        left_w = min(left_w, max(min_left, total_w - min_right - 8 - 28))
        self._left_panel_width = left_w
        self.grid_columnconfigure(0, minsize=left_w)


    def _on_divider_release(self, _event):
        self._divider_active = False
        # 拖拽结束即落盘，避免拖动后的宽度在重启时被旧配置覆盖。
        self._save_window_geometry()


    def _build_bottom(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        status_bar = ctk.CTkFrame(parent, fg_color="transparent")
        status_bar.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        status_bar.grid_columnconfigure(1, weight=1)
        self.progress = ctk.CTkProgressBar(status_bar, width=240, height=8, corner_radius=4, progress_color=THEME["primary"])
        self.progress.set(0)
        self.progress.grid(row=0, column=0, padx=(0, 12))
        self.status_var = tk.StringVar(value="就绪")
        ctk.CTkLabel(status_bar, textvariable=self.status_var, anchor="w", text_color=THEME["text_muted"]).grid(row=0, column=1, sticky="ew")
        self.start_btn = ctk.CTkButton(status_bar, text="▶ 开始翻译", width=120, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["accent"], hover_color=THEME["accent_hover"], command=self._start_translation)
        self.start_btn.grid(row=0, column=2, padx=6)
        self.stop_btn = ctk.CTkButton(status_bar, text="⏹ 停止", width=80, height=34, font=ctk.CTkFont(size=13, weight="bold"), fg_color=THEME["danger"], hover_color=THEME["danger_hover"], command=self._stop)
        self.stop_btn.grid(row=0, column=3, padx=6)
        self.stop_btn.configure(state="disabled")

        self.log_box = ctk.CTkTextbox(parent, height=110, corner_radius=6, border_width=1, border_color=THEME["card_border"], fg_color=THEME["bg"], text_color=THEME["text_muted"])
        self.log_box.grid(row=1, column=0, padx=14, pady=(4, 10), sticky="nsew")
        self.log_box.configure(state="disabled")


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
        data = self.app_config.to_dict()
        data.update({
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "chunk_chars": chunk_chars,
            "max_workers": workers,
            "extract_glossary": self.extract_var.get(),
            "txt_patterns": list(getattr(self, "_active_txt_patterns", self._txt_patterns())),
            "ignore_zero_chapters": bool(getattr(self, "_active_ignore_zero", False)),
            "output_txt": self.txt_var.get(),
            "output_epub": self.epub_var.get(),
            "sanitizer_config": self.sanitizer_config.to_dict(),
        })
        return AppConfig.from_dict(data)

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
                        book = load_book(
                            path,
                            txt_patterns=config.txt_patterns,
                            drop_zero=config.ignore_zero_chapters,
                        )
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
        if hasattr(self, "_fixer_set_busy"):
            self._fixer_set_busy(False)
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


    def _default_geometry(self) -> str:
        """无历史几何时，按当前屏幕展开一个接近全屏且保留左右分栏的大窗口。"""
        sw, sh = self._screen_logical_size()
        w = max(880, int(sw * 0.90))
        h = max(620, int(sh * 0.86))
        return f"{w}x{h}"


    def _screen_logical_size(self) -> tuple[int, int]:
        """返回以逻辑单位计的可视区域宽高，便于把窗口钳制到屏幕范围内。"""
        try:
            scale = float(self._get_window_scaling() or 1.0)
        except Exception:
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        return int(self.winfo_screenwidth() / scale), int(self.winfo_screenheight() / scale)


    def _clamp_geometry(self, geom: str) -> str:
        """把逻辑几何的宽高裁剪到屏幕范围（保留位置偏移），避免小屏下端头被裁掉。"""
        sw, sh = self._screen_logical_size()
        match = re.match(r"^(\d+)x(\d+)([+-]\d+[+-]\d+)?$", geom)
        if not match:
            return geom
        w = int(match.group(1))
        h = int(match.group(2))
        w = min(w, max(sw - 40, 600))
        h = min(h, max(sh - 40, 480))
        return f"{w}x{h}{match.group(3) or ''}"


    def _clamp_min_size(self, w: int, h: int) -> tuple[int, int]:
        sw, sh = self._screen_logical_size()
        return min(w, max(sw - 40, 480)), min(h, max(sh - 40, 360))


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
            state["left_panel_width"] = int(getattr(self, "_left_panel_width", _DEFAULT_LEFT_WIDTH) or _DEFAULT_LEFT_WIDTH)
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
            state["app_config"] = self._config_from_ui().to_dict()
            state["output"] = self.output_var.get().strip()
            _save_ui_state(state)
        except Exception:
            pass
        try:
            self.common_glossary.save(common_glossary_path())
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
