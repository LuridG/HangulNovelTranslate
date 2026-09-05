# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import (Any, Callable)
from bs4 import (BeautifulSoup, Comment, NavigableString, Tag)
from ..glossary import Glossary
from ..llm import LLMCancelled
from .models import PerspectiveBlock
from .models import PerspectiveCancelled
from .models import PerspectiveOptions
from .rewrite import _batch_blocks
from .analysis import _block_from_node
from .state import _block_state
from .analysis import _eligible
from .analysis import _is_html
from .analysis import _iter_content_blocks
from .analysis import _replace_raw_blocks
from .analysis import _skip_document
from .state import _source_fingerprint
from .state import _state_options
from .state import perspective_state_path
from .rewrite import rewrite_blocks


def inspect_perspective_epub(
    input_path: str | Path,
    options: PerspectiveOptions,
) -> dict[str, Any]:
    options = options.normalized()
    total_blocks = 0
    eligible_blocks = 0
    protected_blocks = 0
    total_chars = 0
    files: list[str] = []
    samples: list[str] = []
    with zipfile.ZipFile(input_path, "r") as archive:
        for info in archive.infolist():
            if not _is_html(info.filename):
                continue
            soup = BeautifulSoup(archive.read(info.filename), "html.parser", from_encoding="utf-8")
            if _skip_document(info.filename, soup):
                continue
            file_has_eligible = False
            for node, classification in _iter_content_blocks(soup):
                block = _block_from_node(info.filename, total_blocks, node, classification)
                if block is None:
                    continue
                total_blocks += 1
                total_chars += block.char_count
                if _eligible(classification, options):
                    eligible_blocks += 1
                    file_has_eligible = True
                    if len(samples) < 5:
                        samples.append(f"[{info.filename}] {block.source_text[:180]}")
                else:
                    protected_blocks += 1
            if file_has_eligible:
                files.append(info.filename)
    return {
        "total_blocks": total_blocks,
        "eligible_blocks": eligible_blocks,
        "protected_blocks": protected_blocks,
        "total_chars": total_chars,
        "files": files,
        "samples": samples,
    }



class PerspectiveConverter:
    """在源 EPUB 上执行可恢复的第一人称改第三人称转换。"""

    def __init__(
        self,
        llm: Any,
        options: PerspectiveOptions,
        glossary: Glossary | None = None,
        *,
        cancel_event: Any = None,
        progress_callback: Callable[[str, int, int, str], None] | None = None,
    ):
        self.llm = llm
        self.options = options.normalized()
        self.glossary = glossary
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback or (lambda *_: None)

    def _cancelled(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    def _save_state(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".perspective-state-", suffix=".json", dir=str(path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _scan_archive(self, archive: zipfile.ZipFile, options: PerspectiveOptions) -> dict[str, PerspectiveBlock]:
        blocks: dict[str, PerspectiveBlock] = {}
        for info in archive.infolist():
            if not _is_html(info.filename):
                continue
            soup = BeautifulSoup(archive.read(info.filename), "html.parser", from_encoding="utf-8")
            if _skip_document(info.filename, soup):
                continue
            block_number = 0
            for node, classification in _iter_content_blocks(soup):
                block = _block_from_node(info.filename, block_number, node, classification)
                block_number += 1
                if block is not None and _eligible(classification, options):
                    blocks[block.id] = block
        return blocks

    def _new_state(
        self,
        input_path: Path,
        output_path: Path,
        blocks: dict[str, PerspectiveBlock],
    ) -> dict[str, Any]:
        return {
            "mode": "first_to_third",
            "prompt_version": self.options.prompt_version,
            "source_path": str(input_path),
            "source_fingerprint": _source_fingerprint(input_path),
            "output_path": str(output_path),
            "options": _state_options(self.options),
            "blocks": {block_id: _block_state(block) for block_id, block in blocks.items()},
        }

    def _load_or_create_state(
        self,
        path: Path,
        input_path: Path,
        output_path: Path,
        blocks: dict[str, PerspectiveBlock],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        if not resume or not path.exists():
            state = self._new_state(input_path, output_path, blocks)
            self._save_state(path, state)
            return state
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("source_path") != str(input_path):
            raise ValueError("视角转换存档对应的输入 EPUB 已变化，请重置该存档后再开始。")
        if data.get("source_fingerprint") != _source_fingerprint(input_path):
            raise ValueError("输入 EPUB 已变化，为避免错写请重置视角转换存档。")
        saved_options = data.get("options")
        if not isinstance(saved_options, dict):
            raise ValueError("视角转换存档缺少 options")
        normalized_saved_options = _state_options(PerspectiveOptions(**saved_options).normalized())
        if normalized_saved_options != _state_options(self.options):
            raise ValueError("视角转换参数已变化，请先重置存档再开始。")
        data["options"] = normalized_saved_options
        old_blocks = data.get("blocks") or {}
        for block_id, block in blocks.items():
            old = old_blocks.get(block_id)
            if not isinstance(old, dict) or old.get("source_segments") != block.source_segments:
                old_blocks[block_id] = _block_state(block)
        for block_id in list(old_blocks):
            if block_id not in blocks:
                del old_blocks[block_id]
        data["blocks"] = old_blocks
        self._save_state(path, data)
        return data

    def _translate_pending(
        self,
        state_path: Path,
        state: dict[str, Any],
        blocks: dict[str, PerspectiveBlock],
    ) -> None:
        pending = []
        for block_id, block in blocks.items():
            item = state["blocks"].get(block_id) or {}
            if item.get("status") != "completed":
                pending.append(block)
        batches = _batch_blocks(pending, self.options.chunk_chars)
        total = len(blocks)
        completed = sum(
            1
            for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "completed"
        )
        attempted_failed: set[str] = set()
        self.progress_callback(
            "视角转换",
            completed,
            total,
            f"待处理 {total - completed} 块",
        )
        for batch in batches:
            if self._cancelled():
                raise PerspectiveCancelled("视角转换已停止，已完成块已保存")
            try:
                translated = rewrite_blocks(
                    self.llm,
                    self.options,
                    self.glossary,
                    batch,
                    cancel_event=self.cancel_event,
                )
                if self._cancelled():
                    raise PerspectiveCancelled("视角转换已停止，已完成块已保存")
                for block in batch:
                    item = state["blocks"][block.id]
                    item["translated_segments"] = translated[block.id]
                    item["status"] = "completed"
                    item["error"] = ""
            except LLMCancelled as exc:
                self._save_state(state_path, state)
                raise PerspectiveCancelled("视角转换已停止，已完成块已保存") from exc
            except PerspectiveCancelled:
                self._save_state(state_path, state)
                raise
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                for block in batch:
                    item = state["blocks"][block.id]
                    item["status"] = "failed"
                    item["error"] = error
                    attempted_failed.add(block.id)
            completed = sum(
                1
                for item in state["blocks"].values()
                if isinstance(item, dict) and item.get("status") == "completed"
            )
            failed = sum(
                1
                for item in state["blocks"].values()
                if isinstance(item, dict) and item.get("status") == "failed"
            )
            processed = completed + len(attempted_failed)
            self._save_state(state_path, state)
            self.progress_callback(
                "视角转换",
                processed,
                total,
                f"成功 {completed} 块，失败 {failed} 块，待处理 {max(total - processed, 0)} 块",
            )

    def _render(self, input_path: Path, output_path: Path, state: dict[str, Any]) -> dict[str, int]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if input_path.resolve() == output_path.resolve():
            raise ValueError("输出 EPUB 不能覆盖输入 EPUB")
        stats = {"modified_files": 0, "completed_blocks": 0, "failed_blocks": 0}
        fd, temp_name = tempfile.mkstemp(prefix=".perspective-", suffix=".epub", dir=str(output_path.parent))
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(temp_path, "w") as target:
                for info in source.infolist():
                    content = source.read(info.filename)
                    file_changed = False
                    if _is_html(info.filename):
                        relevant = {
                            block_id: item
                            for block_id, item in (state.get("blocks") or {}).items()
                            if isinstance(item, dict) and item.get("file") == info.filename
                        }
                        if relevant:
                            soup = BeautifulSoup(content, "html.parser", from_encoding="utf-8")
                            ordered_blocks: list[PerspectiveBlock] = []
                            block_number = 0
                            for node, classification in _iter_content_blocks(soup):
                                block = _block_from_node(info.filename, block_number, node, classification)
                                block_number += 1
                                if block is not None:
                                    ordered_blocks.append(block)
                            replaced, _, mismatched = _replace_raw_blocks(
                                content,
                                ordered_blocks,
                                relevant,
                            )
                            for block_id in mismatched:
                                item = relevant[block_id]
                                item["status"] = "failed"
                                item["error"] = "输出时正文节点结构发生变化，未写入该块"
                            completed_ids = {
                                block_id
                                for block_id, item in relevant.items()
                                if item.get("status") == "completed"
                            }
                            stats["completed_blocks"] += len(completed_ids - mismatched)
                            stats["failed_blocks"] += sum(
                                item.get("status") == "failed"
                                for item in relevant.values()
                            )
                            if replaced != content:
                                content = replaced
                                file_changed = True
                            if file_changed:
                                stats["modified_files"] += 1
                    target.writestr(info, content)
            os.replace(temp_path, output_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return stats

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        resume: bool = True,
        state_path: str | Path | None = None,
    ) -> dict[str, Any]:
        input_path = Path(input_path)
        output_path = Path(output_path)
        if not input_path.exists():
            raise FileNotFoundError(f"输入 EPUB 不存在：{input_path}")
        if input_path.resolve() == output_path.resolve():
            raise ValueError("输出 EPUB 不能覆盖输入 EPUB")
        state_file = Path(state_path) if state_path is not None else perspective_state_path(input_path, output_path)
        with zipfile.ZipFile(input_path, "r") as archive:
            blocks = self._scan_archive(archive, self.options)
        state = self._load_or_create_state(state_file, input_path, output_path, blocks, resume=resume)
        cancelled = False
        try:
            self._translate_pending(state_file, state, blocks)
        except PerspectiveCancelled:
            cancelled = True
        stats = self._render(input_path, output_path, state)
        self._save_state(state_file, state)
        failed = sum(
            1 for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "failed"
        )
        completed = sum(
            1 for item in state["blocks"].values()
            if isinstance(item, dict) and item.get("status") == "completed"
        )
        return {
            **stats,
            "cancelled": cancelled,
            "state_path": state_file,
            "output_path": output_path,
            "total_blocks": len(blocks),
            "completed_blocks": completed,
            "failed_blocks": failed,
        }

    def render_state(self, input_path: str | Path, output_path: str | Path, state_path: str | Path) -> dict[str, int]:
        state_file = Path(state_path)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        source = Path(input_path)
        if data.get("source_path") != str(source):
            raise ValueError("视角转换存档与输入 EPUB 不匹配")
        if data.get("source_fingerprint") != _source_fingerprint(source):
            raise ValueError("输入 EPUB 已变化，为避免错写请重置视角转换存档")
        stats = self._render(source, Path(output_path), data)
        self._save_state(state_file, data)
        return stats



def render_perspective_state(
    input_path: str | Path,
    output_path: str | Path,
    state_path: str | Path,
) -> dict[str, int]:
    """不调用模型，按现有状态重新生成 EPUB（供手动补写后使用）。"""
    data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    options = PerspectiveOptions(**(data.get("options") or {}))
    converter = PerspectiveConverter(None, options)
    return converter.render_state(input_path, output_path, state_path)



def reset_perspective_state(
    input_path: str | Path,
    output_path: str | Path,
    state_path: str | Path | None = None,
) -> Path:
    path = Path(state_path) if state_path is not None else perspective_state_path(input_path, output_path)
    if path.exists():
        path.unlink()
    return path
