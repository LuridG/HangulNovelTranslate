# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from hangul_novel_translator.sanitizer import (ExportSanitizer, SanitizerConfig)
import json
import threading
from concurrent.futures import (ThreadPoolExecutor, as_completed)
from pathlib import Path
from typing import (Any, Callable)
from ..book import (Book, Chapter, ParagraphStyle, book_to_txt, export_epub, is_decorative_title, load_book, metadata_to_dict)
from ..config import AppConfig
from ..glossary import Glossary
from ..sampling import (collect_sample_text_strided, format_sample_chapters, sample_chapter_report)
from ..utils import (extract_json, parse_paragraphs_from_payload, split_paragraph_smart)
from .models import Chunk
from .models import FailedChunk
from ._consts import ProgressCallback
from ._consts import TRANSLATION_SYSTEM_TEMPLATE
from .models import TranslationCancelled
from .models import TranslationResult
from .chunking import _chunk_from_failed_entry
from .chunking import _chunk_signature
from .chunking import _failed_entry
from .chunking import _retry_chunk_config
from .state import _sanitize_state_name
from .chunking import build_chunks
from .state import load_failed_chunks
from .response import normalize_completed_paragraphs
from .response import reconcile_paragraphs
from .response import sanitize_committed_paragraphs

# 延迟绑定：LLMClient 在调用时从 translator 包命名空间解析，
# 以便测试可以 patch `translator.LLMClient` 注入替身。
import sys as _sys

__pkg__ = _sys.modules[__package__]


class Translator:
    def __init__(
        self,
        config: AppConfig,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.llm = __pkg__.LLMClient(config)
        self.progress_callback = progress_callback or (lambda *_: None)
        self.cancel_event = cancel_event or threading.Event()
        self._state_lock = threading.Lock()

    # ---------------- 状态 ----------------
    def _state_path(
        self,
        output_dir: Path,
        source: Path,
        *,
        stem: str | None = None,
        legacy: bool = False,
    ) -> Path:
        # 一个源文件对应一个状态文件；stem 用于按“小说名 第X卷”命名，避免不同书籍互相覆盖。
        source = Path(source)
        name = _sanitize_state_name(stem or source.stem, legacy=legacy)
        return Path(output_dir) / f".{name}.translation_state.json"

    def _load_state(self, state_path: Path, source: Path) -> dict[str, Any]:
        if not self.config.resume or not state_path.exists():
            return {"source": str(source), "completed": {}, "failed": {}}
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if data.get("source") != str(source):
                return {"source": str(source), "completed": {}, "failed": {}}
            return data
        except Exception:
            return {"source": str(source), "completed": {}, "failed": {}}

    def _save_state(self, state_path: Path, state: dict[str, Any]) -> None:
        with self._state_lock:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # ---------------- 单块翻译 ----------------
    def _translate_chunk(self, chunk: Chunk, glossary: Glossary) -> list[str]:
        if self.cancel_event.is_set():
            raise TranslationCancelled()

        glossary_text = glossary.prompt_text()
        system = TRANSLATION_SYSTEM_TEMPLATE.format(
            target_lang=self.config.target_lang,
            glossary=glossary_text,
        )
        numbered = "\n".join(
            f"[{i}] {p}" for i, p in enumerate(chunk.paragraphs, start=1)
        )
        user = (
            f"章节：{chunk.chapter_title}\n"
            f"原文段落数量：{len(chunk.paragraphs)}\n"
            "请把下面每个编号段落翻译成中文，并按编号顺序返回相同数量的译文段落。\n\n"
            f"{numbered}"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        raw = self.llm.chat(messages, json_mode=True)
        try:
            payload = extract_json(raw)
        except Exception:
            # JSON 模式偶发返回纯文本时，退化为按行处理。
            payload = {"paragraphs": [x.strip() for x in raw.splitlines() if x.strip()]}

        paragraphs = parse_paragraphs_from_payload(payload)
        paragraphs = reconcile_paragraphs(paragraphs, len(chunk.paragraphs))
        return [glossary.apply_replacements(p) for p in paragraphs]

    # ---------------- 主流程 ----------------
    def translate_file(
        self,
        input_path: str | Path,
        output_dir: str | Path,
        glossary: Glossary,
        *,
        auto_extract: bool | None = None,
        output_stem: str | None = None,
    ) -> TranslationResult:
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if auto_extract is None:
            auto_extract = self.config.extract_glossary

        self.progress_callback("读取书籍", 0, 1, str(input_path))
        book = load_book(
            input_path,
            txt_patterns=self.config.txt_patterns,
            drop_zero=self.config.ignore_zero_chapters,
        )
        self.progress_callback("拆分章节", 0, 1, f"共 {len(book.chapters)} 章，{book.total_chars} 字")

        if auto_extract and not glossary.valid_entries():
            sample = collect_sample_text_strided(book, self.config)
            self.progress_callback("提取词表", 0, 1, "正在用 LLM 提取专有名词…")
            glossary = self._extract_glossary(sample)

        chunks = build_chunks(book, self.config)
        state_path = self._state_path(output_dir, input_path, stem=output_stem)
        legacy_path = self._state_path(output_dir, input_path, legacy=True)
        if not state_path.exists() and legacy_path.exists():
            state_path = legacy_path
        state = self._load_state(state_path, input_path)
        completed: dict[str, list[str]] = state.setdefault("completed", {})
        failed: dict[str, str] = state.setdefault("failed", {})
        # 结构防护：章节解析规则或分块参数变化会导致旧完成块按 id 错位，
        # 直接拒绝续传，避免把旧译文静默套到不同内容上。
        signature = _chunk_signature(chunks)
        saved_signature = state.get("chunk_signature")
        saved_total = state.get("total_chunks")
        saved_completed = state.get("completed") or {}
        saved_failed = state.get("failed") or {}
        if saved_total or saved_completed or saved_failed:
            structure_changed = False
            reason = ""
            if isinstance(saved_signature, str) and saved_signature:
                structure_changed = saved_signature != signature
                reason = "章节结构或分块参数已变化（解析规则更新或分块设置调整）"
            else:
                saved_chunk_chars = state.get("chunk_chars")
                saved_max_paragraph_chars = state.get("max_paragraph_chars")
                params_changed = (
                    isinstance(saved_chunk_chars, int)
                    and saved_chunk_chars != self.config.chunk_chars
                ) or (
                    isinstance(saved_max_paragraph_chars, int)
                    and saved_max_paragraph_chars != self.config.max_paragraph_chars
                )
                if params_changed:
                    structure_changed = True
                    reason = "翻译分块参数已变化（单块字数/段落上限）"
                elif (
                    isinstance(saved_total, int)
                    and saved_total > 0
                    and saved_total != len(chunks)
                ) or (
                    saved_completed
                    and not ({str(k) for k in saved_completed.keys()} & {c.id for c in chunks})
                ):
                    structure_changed = True
                    reason = "存档的章节结构与当前解析不一致（章节解析规则已更新）"
            if structure_changed:
                raise ValueError(f"{reason}，请删除存档 {state_path} 后重新翻译。")
        state["source"] = str(input_path)
        state["total_chunks"] = len(chunks)
        state["chunk_signature"] = signature
        state["chunk_chars"] = self.config.chunk_chars
        state["max_paragraph_chars"] = self.config.max_paragraph_chars
        state["txt_patterns"] = list(self.config.txt_patterns)
        state["ignore_zero_chapters"] = bool(self.config.ignore_zero_chapters)
        state["metadata"] = metadata_to_dict(book.metadata)
        self._save_state(state_path, state)

        pending = [c for c in chunks if c.id not in completed]
        done_this_run = 0
        failed_this_run = 0
        total = len(chunks)

        if total == 0:
            raise ValueError("书籍没有可翻译内容")

        # 已完成的先同步进度。
        base_completed = len(completed)
        self.progress_callback("翻译", base_completed, total, "续传：已存在完成块")

        if pending:
            max_workers = max(1, min(self.config.max_workers, len(pending)))
            if max_workers == 1:
                for chunk in pending:
                    self._process_one(chunk, glossary, completed, failed, state, state_path)
                    done_this_run += 1 if chunk.id in completed else 0
                    failed_this_run += 1 if chunk.id in failed else 0
                    self.progress_callback(
                        "翻译",
                        len(completed),
                        total,
                        f"{chunk.chapter_title} / {len(chunk.paragraphs)} 段",
                    )
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(self._safe_process_one, chunk, glossary, completed, failed, state, state_path): chunk for chunk in pending}
                    for future in as_completed(futures):
                        chunk = futures[future]
                        try:
                            future.result()
                        except TranslationCancelled:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            failed[str(chunk.id)] = str(exc)
                            failed_this_run += 1
                        else:
                            done_this_run += 1
                        self.progress_callback(
                            "翻译",
                            len(completed),
                            total,
                            f"完成 {len(completed)}/{total}",
                        )

        if self.cancel_event.is_set():
            self._save_state(state_path, state)
            raise TranslationCancelled()

        if failed:
            self.progress_callback("存在失败块", len(completed), total, f"{len(failed)} 块失败")
            # 失败块不阻断导出，但会保留原文并提示。
        else:
            self.progress_callback("组合书籍", total, total, "正在组装译文…")

        # 章节名：统一批量翻译并落盘（续传/多卷合并直接使用已保存结果，不再随正文翻译）。
        if not state.get("chapter_titles"):
            self._translate_chapter_titles(book, glossary)
            state["chapter_titles"] = {
                str(ch.index): {"ko": ch.title, "zh": ch.title_zh} for ch in book.chapters
            }
            self._save_state(state_path, state)
        else:
            saved_titles = state["chapter_titles"]
            for ch in book.chapters:
                saved = saved_titles.get(str(ch.index)) or {}
                if saved.get("zh") and saved.get("ko") == ch.title:
                    ch.title_zh = saved["zh"]

        translated_book = self._assemble(book, chunks, completed, failed, glossary)
        output_paths: list[Path] = []
        base_stem = f"{output_stem or book.title}.zh"
        sanitizer = ExportSanitizer(self.config.sanitizer_config)
        if self.config.output_txt:
            txt_path = output_dir / f"{base_stem}.txt"
            book_to_txt(
                translated_book,
                txt_path,
                self.config.output_encoding,
                sanitizer=sanitizer,
            )
            output_paths.append(txt_path)
        if self.config.output_epub:
            epub_path = output_dir / f"{base_stem}.epub"
            export_epub(
                translated_book,
                epub_path,
                source_title=book.title,
                sanitizer=sanitizer,
            )
            output_paths.append(epub_path)

        return TranslationResult(
            input_path=input_path,
            output_dir=output_dir,
            total_chunks=total,
            completed_chunks=len(completed),
            failed_chunks=len(failed),
            output_paths=output_paths,
        )

    # ---------------- 失败块重试 ----------------
    def retry_failed(
        self,
        state_path: str | Path,
        glossary: Glossary,
        *,
        max_attempts: int = 3,
    ) -> dict[str, int]:
        """读取翻译状态存档中的失败块，逐个重试翻译（最多 max_attempts 次），
        成功写回 completed 并移出 failed。

        新版存档会在 failed[chunk_id] 里持久化原文章节与段落，重试直接还原 Chunk，无需原书；
        旧版存档（failed 值为错误字符串）改为按原书重新分块定位。若无法定位，计入 missing。
        返回 {"found", "recovered", "still_failed", "missing"}。
        """
        state_path = Path(state_path)
        data = json.loads(state_path.read_text(encoding="utf-8"))
        source = Path(str(data.get("source", "")))
        failed = data.get("failed") or {}
        if not isinstance(failed, dict) or not failed:
            return {"found": 0, "recovered": 0, "still_failed": 0, "missing": 0}

        book = None
        by_id: dict[str, Chunk] | None = None
        found = 0
        recovered = 0
        still_failed = 0
        missing = 0
        dirty = False
        for chunk_id in list(failed.keys()):
            entry = failed[chunk_id]
            chunk = _chunk_from_failed_entry(chunk_id, entry) if isinstance(entry, dict) else None
            if chunk is None:
                # 旧版存档只存错误字符串：按原书重新分块定位；原书缺失则无法定位。
                if source and source.exists():
                    if by_id is None:
                        cfg = _retry_chunk_config(data, self.config)
                        book = load_book(
                            source,
                            txt_patterns=getattr(cfg, "txt_patterns", None),
                            drop_zero=getattr(cfg, "ignore_zero_chapters", False),
                        )
                        by_id = {c.id: c for c in build_chunks(book, cfg)}
                    chunk = by_id.get(chunk_id)
                if chunk is None:
                    found += 1
                    missing += 1
                    still_failed += 1
                    reason = "重试失败：找不到对应分块（结构不匹配或原书已变化）"
                    if isinstance(entry, str) and reason not in entry:
                        failed[chunk_id] = f"{entry}；{reason}"
                        dirty = True
                    continue
            found += 1
            ok = False
            last_error = ""
            for _ in range(max_attempts):
                if self.cancel_event.is_set():
                    raise TranslationCancelled()
                try:
                    translated = self._translate_chunk(chunk, glossary)
                except TranslationCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    continue
                completed = data.setdefault("completed", {})
                completed[chunk_id] = sanitize_committed_paragraphs(translated)
                failed.pop(chunk_id, None)
                self._save_state(state_path, data)
                ok = True
                break
            if ok:
                recovered += 1
            else:
                still_failed += 1
                # 把本次真实错误写回存档，用户打开 JSON 即可定位原因。
                if isinstance(entry, dict):
                    new_error = last_error or str(entry.get("error", ""))
                    if entry.get("error") != new_error:
                        entry["error"] = new_error
                        dirty = True
                elif last_error:
                    failed[chunk_id] = f"{entry}；重试错误：{last_error}"
                    dirty = True
        if dirty:
            self._save_state(state_path, data)
        return {"found": found, "recovered": recovered, "still_failed": still_failed, "missing": missing}

    def load_failed_chunks(
        self,
        state_path: str | Path,
        *,
        config: AppConfig | None = None,
    ) -> list[FailedChunk]:
        """读取存档里的失败块并还原原文章节/段落，供“失败块查看/手动编辑”使用。

        新版存档（failed 值为含 paragraphs 的字典）直接用持久化原文；旧版字符串存档按原书重新分块定位；
        仍无法定位时标记 missing=True（正文为空）。
        """
        return load_failed_chunks(state_path, config or self.config)

    def _safe_process_one(self, chunk, glossary, completed, failed, state, state_path):
        try:
            self._process_one(chunk, glossary, completed, failed, state, state_path)
        except TranslationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                failed[chunk.id] = _failed_entry(chunk, str(exc))
            self._save_state(state_path, state)
            raise

    def _process_one(self, chunk, glossary, completed, failed, state, state_path):
        if self.cancel_event.is_set():
            raise TranslationCancelled()
        try:
            translated = self._translate_chunk(chunk, glossary)
            with self._state_lock:
                completed[chunk.id] = sanitize_committed_paragraphs(translated)
                failed.pop(chunk.id, None)
            self._save_state(state_path, state)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                failed[chunk.id] = _failed_entry(chunk, str(exc))
            self._save_state(state_path, state)
            raise

    def _extract_glossary(self, sample: str) -> Glossary:
        from .glossary import extract_glossary_with_llm

        glossary = extract_glossary_with_llm(self.llm, sample, self.config.glossary_limit)
        self.progress_callback(
            "词表提取完成",
            len(glossary.valid_entries()),
            len(glossary.valid_entries()),
            f"提取到 {len(glossary.valid_entries())} 条，请人工确认",
        )
        return glossary

    def _translate_chapter_titles(self, book: Book, glossary: Glossary) -> None:
        """批量翻译章节名：标记好的章节名不随正文翻译，单独成批交给 LLM；失败保留原标题。"""
        entries = [
            (i, ch) for i, ch in enumerate(book.chapters) if ch.title and not is_decorative_title(ch.title)
        ]
        if not entries:
            return
        numbered = "\n".join(f"[{i}] {ch.title}" for i, ch in entries)
        system = (
            "你是一名资深韩语小说译者。下面是小说里的一组韩语章节名，请翻译成简体中文。\n"
            "要求：保持编号与符号（如 IF…? 1、第3话）和原有风格；人名/专有名词参考词表；"
            '只输出 JSON 对象 {"titles": {"0": "翻译1", "1": "翻译2", ...}}，键与编号一一对应。'
        )
        glossary_text = glossary.prompt_text()
        user = f"【专有名词词表】\n{glossary_text}\n\n【章节名列表】\n{numbered}"
        mapping: dict = {}
        try:
            raw = self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                json_mode=True,
            )
            payload = extract_json(raw)
            mapping = payload.get("titles") or {}
        except Exception:
            mapping = {}
        for i, ch in entries:
            zh = mapping.get(str(i))
            if zh:
                ch.title_zh = str(zh).strip()

    def _assemble(
        self,
        book: Book,
        chunks: list[Chunk],
        completed: dict[str, list[str]],
        failed: dict[str, str],
        glossary: Glossary,
    ) -> Book:
        by_chapter: dict[int, list[Chunk]] = {}
        for chunk in chunks:
            by_chapter.setdefault(chunk.chapter_index, []).append(chunk)

        translated_chapters: list[Chapter] = []
        for chapter in book.chapters:
            paragraphs: list[str] = []
            styles: list[ParagraphStyle | None] = []
            for chunk in sorted(by_chapter.get(chapter.index, []), key=lambda c: c.chunk_index):
                if chunk.id in completed:
                    paras = completed[chunk.id]
                    if isinstance(paras, list):
                        cleaned, _status = normalize_completed_paragraphs(
                            paras,
                            expected_count=len(chunk.paragraphs),
                            source_paragraphs=list(chunk.paragraphs),
                            fill_from_source=True,
                        )
                        paragraphs.extend(cleaned)
                        styles.extend(chunk.styles[: len(cleaned)])
                    else:
                        paragraphs.extend(chunk.paragraphs)
                        styles.extend(chunk.styles[: len(chunk.paragraphs)])
                else:
                    # 失败/未完成块保留原文，便于人工识别。
                    paragraphs.extend(chunk.paragraphs)
                    styles.extend(chunk.styles[: len(chunk.paragraphs)])
            if not paragraphs:
                paragraphs = chapter.paragraphs
                styles = list(chapter.styles)
            translated_chapters.append(
                Chapter(
                    chapter.index,
                    chapter.title,
                    paragraphs,
                    chapter.source_id,
                    styles,
                    title_zh=chapter.title_zh,
                    heading_level=chapter.heading_level,
                    parent_index=chapter.parent_index,
                    is_section=chapter.is_section,
                )
            )

        return Book(
            title=book.title,
            chapters=translated_chapters,
            source_path=book.source_path,
            metadata=book.metadata,
        )
