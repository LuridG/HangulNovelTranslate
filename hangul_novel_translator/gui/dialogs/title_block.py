# 由 tools/split_package.py 拆分生成
from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import (filedialog, messagebox, ttk)
from ...config import AppConfig
from ...glossary import Glossary
from ...llm import LLMClient
from ...merge import (TitleTranslationItem, apply_group_translation, detect_title_translations, group_title_items, save_title_translation, save_title_translations)
from ...utils import extract_json
from ..theme import THEME


try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("请先安装 customtkinter：pip install customtkinter") from exc


class TitleTranslationDialog(ctk.CTkToplevel):
    """缺失章节标题补翻弹窗：列出没翻译的章节名，可整批送 LLM 翻译，也可逐个手动改写。"""

    _BATCH_CHUNK = 80

    def __init__(self, master, files: list[Path], config: AppConfig, glossary: Glossary):
        super().__init__(master)
        self.master = master
        self.config = config
        self.glossary = glossary
        self.files = list(files)
        self.items: list[TitleTranslationItem] = []
        self.current: TitleTranslationItem | None = None
        self._busy = False
        self._batch_cancel = threading.Event()
        self._group_by_item: dict[int, str] = {}

        self.title("标题翻译 / 补翻")
        self.geometry("1000x680")
        self.minsize(900, 600)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            self,
            text="标题翻译 / 补翻（列出缺失标题译文，可整批送 LLM 翻译或逐个手动修改）",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=12, pady=(14, 6), sticky="w")

        # 模型配置行：可手动切换模型去翻译对应标题。
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
        self.translate_btn = ctk.CTkButton(
            cfg_frame, text="🔁 翻译当前标题", width=130,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._translate_current,
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
        cols = ("archive", "chapter", "ko", "status")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", style="Custom.Treeview")
        self.tree.heading("archive", text="存档")
        self.tree.heading("chapter", text="章节")
        self.tree.heading("ko", text="原标题")
        self.tree.heading("status", text="状态")
        self.tree.column("archive", width=170, anchor="w")
        self.tree.column("chapter", width=70, anchor="center")
        self.tree.column("ko", width=260, anchor="w")
        self.tree.column("status", width=70, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(left, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(right, text="原标题（韩文）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, padx=4, sticky="w")
        self.src_box = ctk.CTkTextbox(right, height=110)
        self.src_box.grid(row=1, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.src_box.configure(state="disabled")
        ctk.CTkLabel(right, text="标题译文（中文，可手动编辑或用所选模型翻译）", font=ctk.CTkFont(size=13, weight="bold")).grid(row=2, column=0, padx=4, sticky="sw")
        self.zh_box = ctk.CTkTextbox(right, height=110)
        self.zh_box.grid(row=3, column=0, padx=4, pady=(2, 8), sticky="nsew")
        self.error_label = ctk.CTkLabel(right, text="", wraplength=600, justify="left", anchor="w")
        self.error_label.grid(row=4, column=0, padx=4, sticky="w")
        self.status_var = tk.StringVar(value="")
        self.status_label = ctk.CTkLabel(right, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=5, column=0, padx=4, pady=(2, 0), sticky="w")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=12, pady=(0, 8), sticky="ew")
        self.batch_translate_btn = ctk.CTkButton(
            btn_frame, text="📚 批量翻译", width=120,
            fg_color=THEME["primary"], hover_color=THEME["primary_hover"], command=self._batch_translate_async,
        )
        self.batch_translate_btn.grid(row=0, column=0, padx=4)
        self.detect_groups_btn = ctk.CTkButton(
            btn_frame, text="🔍 分组检测", width=110,
            fg_color=THEME["secondary"], hover_color=THEME["secondary_hover"],
            border_width=1, border_color=THEME["card_border"], command=self._detect_groups,
        )
        self.detect_groups_btn.grid(row=0, column=1, padx=4)
        ctk.CTkButton(btn_frame, text="💾 保存当前", width=130, command=self._save_current).grid(row=0, column=2, padx=4)
        ctk.CTkButton(btn_frame, text="📂 加载词表", width=110, command=self._load_glossary_file).grid(row=0, column=3, padx=4)
        ctk.CTkButton(btn_frame, text="🗑 关闭", width=90, command=self.destroy).grid(row=0, column=4, padx=4)
        self.status_hint = ctk.CTkLabel(
            btn_frame,
            text="提示：保存后写入存档 chapter_titles.zh；批量翻译自动将相似标题（仅前后编号不同）分组，每组只送一个代表给 LLM 再回填编号，成功即自动落盘，失败的保留供手动修改。",
            wraplength=520, justify="left", anchor="w",
        )
        self.status_hint.grid(row=1, column=0, columnspan=4, padx=4, pady=(8, 0), sticky="w")

        self._load_items()

    # ---------------- 加载与列表 ----------------
    def _load_items(self):
        self.items = []
        for f in self.files:
            try:
                self.items.extend(detect_title_translations(f))
            except Exception as exc:  # noqa: BLE001
                self.master.log(f"标题检测失败：{Path(f).name}：{exc}")
        self._refresh_tree()
        if not self.items:
            self.status_var.set("所选存档没有缺失标题译文")
        else:
            self.status_var.set(f"共 {len(self.items)} 个标题缺失译文")

    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, item in enumerate(self.items):
            ko = item.ko
            display = ko if len(ko) <= 36 else ko[:33] + "…"
            if id(item) in self._group_by_item:
                status = "同组"
            elif not item.zh.strip():
                status = "缺译文"
            else:
                status = "待改写"
            self.tree.insert(
                "", "end", iid=str(idx),
                values=(
                    Path(item.archive).name,
                    f"第 {item.chapter_index + 1} 章",
                    display,
                    status,
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
        self.src_box.insert("end", item.ko)
        self.src_box.configure(state="disabled")
        self.zh_box.delete("1.0", "end")
        if item.zh:
            self.zh_box.insert("end", item.zh)
        self.error_label.configure(
            text=f"章节：第 {item.chapter_index + 1} 章    存档：{Path(item.archive).name}"
        )

    # ---------------- 词表就绪检查 / 现场加载 ----------------
    def _ensure_glossary_ready(self) -> bool:
        if self.glossary and self.glossary.valid_entries():
            return True
        ans = messagebox.askyesno(
            "未加载词表",
            "当前没有加载有效词表，翻译标题时专有名词可能不统一。\n"
            "是否现在选择并加载一份词表？选择“否”则按无词表继续。",
            parent=self,
        )
        if not ans:
            return True
        if not self._load_glossary_file():
            return False
        return bool(self.glossary and self.glossary.valid_entries())

    def _load_glossary_file(self) -> bool:
        path = filedialog.askopenfilename(
            title="加载词表", filetypes=[("JSON", "*.json")], parent=self
        )
        if not path:
            return False
        try:
            loaded = Glossary.load(Path(path))
            removed = loaded.dedupe()
            self.glossary = loaded
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

    # ---------------- 分组检测 ----------------
    def _detect_groups(self):
        if not self.items:
            self.status_var.set("没有需要检测的标题")
            messagebox.showinfo("分组检测", "当前没有缺失标题译文", parent=self)
            return
        groups = group_title_items(self.items)
        groupable = [g for g in groups if len(g["members"]) >= 2]
        grouping_map = {}
        for group in groupable:
            for item in group["members"]:
                grouping_map[id(item)] = group["core"]
        self._group_by_item = grouping_map
        self._refresh_tree()
        original = len(self.items)
        saved = sum(len(g["members"]) for g in groupable)
        calls = len(groupable) + (original - saved)
        saved_calls = original - calls
        self.status_var.set(
            f"检测到 {len(groupable)} 组相似标题（共 {saved} 个标题），"
            f"批量翻译约需 {calls} 次而非 {original} 次，可省 {saved_calls} 次调用。"
        )
        msg = (
            f"检测到 {len(groupable)} 组相似标题，可合并 {saved} 个标题为 {len(groupable)} 次翻译。\n"
            f"全部 {original} 个标题原需 {original} 次，现在约需 {calls} 次，节省约 {saved_calls} 次调用。\n\n"
            "点击“批量翻译”将自动按分组送 LLM，每组只翻译一个代表标题，再回填各自编号。"
        )
        messagebox.showinfo("分组检测", msg, parent=self)

    def _build_translation_plan(self, targets):
        """把待翻译标题转换成翻译计划：size>=2 的组走代表翻译，单标题走逐条。"""
        groups = group_title_items(targets)
        plan = []
        for group in groups:
            if len(group["members"]) >= 2:
                plan.append(("group", group))
            else:
                plan.append(("single", group["members"][0]))
        return plan

    @staticmethod
    def _plan_request_items(plan) -> list[TitleTranslationItem]:
        """取每个翻译单元真正送给 LLM 的代表标题。"""
        req = []
        for kind, entry in plan:
            if kind == "group":
                req.append(entry["representative"])
            else:
                req.append(entry)
        return req

    # ---------------- LLM 批量翻译 ----------------
    def _llm_translate_titles(self, targets: list[TitleTranslationItem]) -> list[str]:
        if not targets:
            return []
        cfg = AppConfig(
            base_url=self.base_url_var.get().strip(),
            api_key=self.api_key_var.get().strip(),
            model=self.model_var.get().strip(),
        )
        llm = LLMClient(cfg)
        glossary_text = self.glossary.prompt_text() if self.glossary else ""
        numbered = "\n".join(f"[{i}] {item.ko}" for i, item in enumerate(targets))
        system = (
            "你是一名资深的韩语小说中文译者。下面是小说里的一组韩语章节名，请翻译成简体中文。\n"
            "要求：保持编号与符号（如 IF…? 1、第3话、Chapter 1、结尾的 (1)）和原有风格；"
            "标题里开头与结尾的数字务必原样保留，只翻译韩文文字部分；人名/专有名词参考词表；\n"
            '只输出 JSON 对象 {"titles": {"0": "翻译1", "1": "翻译2", ...}}，'
            "键从 0 开始与输入编号一一对应，译文数量、顺序必须与输入完全一致。"
        )
        user = f"【专有名词词表】\n{glossary_text or '（无）'}\n\n【章节名列表】\n{numbered}"
        raw = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        payload = extract_json(raw)
        mapping = payload.get("titles") or {}
        if not isinstance(mapping, dict):
            mapping = {}
        results: list[str] = []
        for i, _item in enumerate(targets):
            val = mapping.get(str(i))
            results.append(str(val).strip() if val else "")
        return results

    # ---------------- 单条翻译 ----------------
    def _translate_current(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先在左侧选中一个标题", parent=self)
            return
        if not self.model_var.get().strip():
            messagebox.showwarning("提示", "请先填写模型名", parent=self)
            return
        if self._busy:
            return
        if not self._ensure_glossary_ready():
            return
        self._busy = True
        self.translate_btn.configure(state="disabled")
        self.status_var.set("正在翻译当前标题…")
        threading.Thread(target=self._do_translate_current, args=(item,), daemon=True).start()

    def _do_translate_current(self, item):
        try:
            zh = self._llm_translate_titles([item])[0]
            if not zh:
                raise ValueError("模型未返回标题译文")
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            self.after(0, lambda: self._on_translate_error(message))
            return
        self.after(0, lambda: self._on_translate_done(item, zh))

    def _on_translate_done(self, item, zh):
        self._busy = False
        self.translate_btn.configure(state="normal")
        self.zh_box.delete("1.0", "end")
        self.zh_box.insert("end", zh)
        item.zh = zh
        self.status_var.set("翻译完成，请人工核对后点“保存当前”")

    def _on_translate_error(self, message):
        self._busy = False
        self.translate_btn.configure(state="normal")
        self.status_var.set("翻译失败")
        messagebox.showerror("翻译失败", message, parent=self)

    # ---------------- 批量翻译 ----------------
    def _batch_translate_async(self):
        if self._busy:
            return
        targets = list(self.items)
        if not targets:
            messagebox.showinfo("提示", "没有需要翻译的标题", parent=self)
            return
        if not self.model_var.get().strip():
            messagebox.showwarning("提示", "请先填写模型名", parent=self)
            return
        if not self._ensure_glossary_ready():
            return
        self._batch_cancel.clear()
        self._busy = True
        self.batch_translate_btn.configure(state="disabled")
        self.detect_groups_btn.configure(state="disabled")
        self.translate_btn.configure(state="disabled")
        plan = self._build_translation_plan(targets)
        self.status_var.set(f"正在批量翻译 {len(targets)} 个标题（{len(plan)} 个翻译单元）…")
        threading.Thread(target=self._do_batch_translate, args=(targets,), daemon=True).start()

    def _do_batch_translate(self, targets):
        ok = 0
        failed = 0
        errors: list[str] = []
        persisted: set[tuple[str, int]] = set()
        plan = self._build_translation_plan(targets)
        total_units = len(plan)
        for start in range(0, total_units, self._BATCH_CHUNK):
            if self._batch_cancel.is_set():
                break
            unit_chunk = plan[start:start + self._BATCH_CHUNK]
            request_items = self._plan_request_items(unit_chunk)
            try:
                results = self._llm_translate_titles(request_items)
            except Exception as exc:  # noqa: BLE001
                unit_count = sum(len(entry["members"]) if kind == "group" else 1 for kind, entry in unit_chunk)
                failed += unit_count
                errors.append(f"第 {start + 1}-{start + len(unit_chunk)} 个翻译单元失败：{exc}")
                break
            pending: list[TitleTranslationItem] = []
            res_idx = 0
            for kind, entry in unit_chunk:
                zh = results[res_idx]
                res_idx += 1
                if not zh:
                    if kind == "group":
                        failed += len(entry["members"])
                        errors.append(f"{entry['core']}: 模型未返回译文")
                    else:
                        failed += 1
                        errors.append(f"{entry.ko}: 模型未返回译文")
                    continue
                if kind == "group":
                    for item, item_zh in apply_group_translation(entry, zh):
                        item_zh = item_zh.strip()
                        if item_zh:
                            item.zh = item_zh
                            pending.append(item)
                            ok += 1
                        else:
                            failed += 1
                else:
                    entry.zh = zh.strip()
                    pending.append(entry)
                    ok += 1
            if pending and self._persist_titles(pending, errors):
                persisted.update(
                    (str(Path(item.archive)), int(item.chapter_index)) for item in pending
                )
            self.after(
                0,
                lambda done=min(start + self._BATCH_CHUNK, total_units): self._on_batch_progress(done, total_units),
            )
        self.after(
            0,
            lambda: self._on_batch_translate_done(ok, failed, errors, persisted),
        )

    def _persist_titles(self, titles: list[TitleTranslationItem], errors: list[str]) -> bool:
        if not titles:
            return True
        try:
            save_title_translations(titles)
            return True
        except Exception as exc:  # noqa: BLE001
            for item in titles:
                item.zh = ""  # 回滚内存值，保留在列表供重试
            errors.append(f"落盘失败：{exc}")
            return False

    def _on_batch_progress(self, done, total):
        self.status_var.set(f"批量翻译单元 {done}/{total}…")

    def _on_batch_translate_done(self, ok, failed, errors, persisted):
        self._busy = False
        self.batch_translate_btn.configure(state="normal")
        self.detect_groups_btn.configure(state="normal")
        self.translate_btn.configure(state="normal")
        self.items = [
            it for it in self.items
            if (str(Path(it.archive)), int(it.chapter_index)) not in persisted
        ]
        self._group_by_item = {}
        self._refresh_tree()
        remaining = len(self.items)
        self.status_var.set(f"批量翻译完成：成功 {ok}，失败 {failed}，剩 {remaining} 个标题")
        msg = (
            f"批量翻译完成：成功 {ok} 个标题，失败 {failed} 个。\n"
            f"剩 {remaining} 个待处理。"
        )
        if ok:
            msg += "\n已写入存档 chapter_titles.zh。"
        if errors:
            msg += "\n\n失败原因：\n" + "\n".join(errors[:5])
        messagebox.showinfo("批量翻译", msg, parent=self)

    # ---------------- 保存当前手动译文 ----------------
    def _save_current(self):
        item = self.current
        if item is None:
            messagebox.showwarning("提示", "请先在左侧选中一个标题", parent=self)
            return
        text = self.zh_box.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("提示", "标题译文不能为空", parent=self)
            return
        try:
            save_title_translation(item.archive, item.chapter_index, item.ko, text)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"保存失败：{exc}", parent=self)
            return
        self.master.log(f"标题补翻保存：{Path(item.archive).name} 第 {item.chapter_index + 1} 章（{text}）")
        self.items = [
            it for it in self.items
            if not (Path(it.archive) == Path(item.archive) and it.chapter_index == item.chapter_index)
        ]
        self._refresh_tree()
        self.current = None
        self.src_box.configure(state="normal")
        self.src_box.delete("1.0", "end")
        self.src_box.configure(state="disabled")
        self.zh_box.delete("1.0", "end")
        self.error_label.configure(text="")
        self.status_var.set(f"已保存，剩余 {len(self.items)} 个缺失标题")
