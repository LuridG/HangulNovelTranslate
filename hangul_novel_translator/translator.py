# hangul_novel_translator/translator.py
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .book import Book, Chapter, book_to_txt, export_epub, load_book
from .config import AppConfig
from .glossary import Glossary
from .llm import LLMClient
from .utils import extract_json, parse_paragraphs_from_payload, split_paragraph_smart


ProgressCallback = Callable[[str, int, int, str], None]


class TranslationCancelled(RuntimeError):
    pass


@dataclass
class Chunk:
    id: str
    chapter_index: int
    chapter_title: str
    chunk_index: int
    paragraphs: list[str]

    @property
    def char_count(self) -> int:
        return sum(len(p) for p in self.paragraphs)


@dataclass
class TranslationResult:
    input_path: Path
    output_dir: Path
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    output_paths: list[Path]


def build_chunks(book: Book, config: AppConfig) -> list[Chunk]:
    chunks: list[Chunk] = []
    paragraph_limit = min(config.chunk_chars, config.max_paragraph_chars)
    for chapter in book.chapters:
        pieces: list[str] = []
        for paragraph in chapter.paragraphs:
            pieces.extend(split_paragraph_smart(paragraph, paragraph_limit))

        current: list[str] = []
        current_chars = 0
        chunk_index = 0

        def flush() -> None:
            nonlocal current, current_chars, chunk_index
            if current:
                chunks.append(
                    Chunk(
                        id=f"ch-{chapter.index:05d}-{chunk_index:05d}",
                        chapter_index=chapter.index,
                        chapter_title=chapter.title,
                        chunk_index=chunk_index,
                        paragraphs=current,
                    )
                )
                chunk_index += 1
                current = []
                current_chars = 0

        for piece in pieces:
            if current and current_chars + len(piece) + 1 > config.chunk_chars:
                flush()
            current.append(piece)
            current_chars += len(piece) + 1
        flush()
    return chunks


def collect_sample_text(book: Book, config: AppConfig) -> str:
    """取字数最多的前几章、累计前 N 字用于词表提取，避免采到目录/版权页等短文档。"""
    sample: list[str] = []
    chars = 0
    limit_chapters = min(config.extract_sample_chapters, len(book.chapters))
    for chapter in sorted(book.chapters, key=len, reverse=True)[:limit_chapters]:
        for paragraph in chapter.paragraphs:
            sample.append(paragraph)
            chars += len(paragraph)
            if chars >= config.extract_sample_chars:
                break
        if chars >= config.extract_sample_chars:
            break
    return "\n".join(sample)


def reconcile_paragraphs(translated: list[str], expected_count: int) -> list[str]:
    """尽量让译文段落数与原文一致。"""
    translated = [str(x).strip() for x in translated if str(x).strip()]
    if len(translated) == expected_count:
        return translated

    if len(translated) == 1 and expected_count > 1:
        split = [x.strip() for x in re.split(r"\n+", translated[0]) if x.strip()]
        if len(split) > 1:
            translated = split

    if len(translated) > expected_count:
        return translated[:expected_count]

    # 数量不足：补空段落。宁可保留原文格式，也不伪造内容。
    translated.extend([""] * (expected_count - len(translated)))
    return translated


TRANSLATION_SYSTEM_TEMPLATE = """你是一名资深的韩语小说中文译者。请把用户提供的韩语小说内容翻译成{target_lang}。
要求：
1. 严格忠实原文，不增删情节，不改动事实。
2. 保留原句语气、对话感、修辞和段落顺序。
3. 以下专有名词词表是全书统一译名，必须严格遵守；如果词表与你的常识冲突，以词表为准。

【专有名词词表】
{glossary}

【输出格式】
只输出一个 JSON 对象，不要输出解释、前言或 Markdown 代码块。
JSON 格式：{{"paragraphs": ["译文段落1", "译文段落2", ...]}}
译文数组的段落数量必须与用户给出的原文段落数量完全一致，顺序也必须一致。"""


class Translator:
    def __init__(
        self,
        config: AppConfig,
        *,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.config = config
        self.llm = LLMClient(config)
        self.progress_callback = progress_callback or (lambda *_: None)
        self.cancel_event = cancel_event or threading.Event()
        self._state_lock = threading.Lock()

    # ---------------- 状态 ----------------
    def _state_path(self, output_dir: Path, source: Path) -> Path:
        # 一个源文件对应一个状态文件，避免不同书籍互相覆盖。
        safe = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", source.stem)
        return Path(output_dir) / f".{safe}.translation_state.json"

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
    ) -> TranslationResult:
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if auto_extract is None:
            auto_extract = self.config.extract_glossary

        self.progress_callback("读取书籍", 0, 1, str(input_path))
        book = load_book(input_path)
        self.progress_callback("拆分章节", 0, 1, f"共 {len(book.chapters)} 章，{book.total_chars} 字")

        if auto_extract and not glossary.valid_entries():
            sample = collect_sample_text(book, self.config)
            self.progress_callback("提取词表", 0, 1, "正在用 LLM 提取专有名词…")
            glossary = self._extract_glossary(sample)

        chunks = build_chunks(book, self.config)
        state_path = self._state_path(output_dir, input_path)
        state = self._load_state(state_path, input_path)
        completed: dict[str, list[str]] = state.setdefault("completed", {})
        failed: dict[str, str] = state.setdefault("failed", {})
        state["source"] = str(input_path)
        state["total_chunks"] = len(chunks)
        state["chunk_chars"] = self.config.chunk_chars
        state["max_paragraph_chars"] = self.config.max_paragraph_chars
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

        translated_book = self._assemble(book, chunks, completed, failed, glossary)
        output_paths: list[Path] = []
        base_stem = f"{book.title}.zh"
        if self.config.output_txt:
            txt_path = output_dir / f"{base_stem}.txt"
            book_to_txt(translated_book, txt_path, self.config.output_encoding)
            output_paths.append(txt_path)
        if self.config.output_epub:
            epub_path = output_dir / f"{base_stem}.epub"
            export_epub(translated_book, epub_path, source_title=book.title)
            output_paths.append(epub_path)

        return TranslationResult(
            input_path=input_path,
            output_dir=output_dir,
            total_chunks=total,
            completed_chunks=len(completed),
            failed_chunks=len(failed),
            output_paths=output_paths,
        )

    def _safe_process_one(self, chunk, glossary, completed, failed, state, state_path):
        try:
            self._process_one(chunk, glossary, completed, failed, state, state_path)
        except TranslationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                failed[chunk.id] = str(exc)
            self._save_state(state_path, state)
            raise

    def _process_one(self, chunk, glossary, completed, failed, state, state_path):
        if self.cancel_event.is_set():
            raise TranslationCancelled()
        try:
            translated = self._translate_chunk(chunk, glossary)
            with self._state_lock:
                completed[chunk.id] = translated
                failed.pop(chunk.id, None)
            self._save_state(state_path, state)
        except Exception as exc:  # noqa: BLE001
            with self._state_lock:
                failed[chunk.id] = str(exc)
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
            for chunk in sorted(by_chapter.get(chapter.index, []), key=lambda c: c.chunk_index):
                if chunk.id in completed:
                    paragraphs.extend(completed[chunk.id])
                else:
                    # 失败/未完成块保留原文，便于人工识别。
                    paragraphs.extend(chunk.paragraphs)
            if not paragraphs:
                paragraphs = chapter.paragraphs
            translated_chapters.append(
                Chapter(chapter.index, chapter.title, paragraphs, chapter.source_id)
            )

        return Book(title=book.title, chapters=translated_chapters, source_path=book.source_path)
