# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import hashlib
import json
import re
from dataclasses import (asdict, dataclass)
from pathlib import Path
from typing import (Any, Callable)
from .models import PerspectiveBlock
from .models import PerspectiveFailedBlock
from .models import PerspectiveOptions
from .rewrite import _preserve_edge_whitespace


def _safe_state_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff\uac00-\ud7af]+", "_", value or "book")
    safe = re.sub(r"_+", "_", safe).strip("._")
    return safe or "book"



def _source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            part = handle.read(1024 * 1024)
            if not part:
                break
            digest.update(part)
    return digest.hexdigest()



def perspective_state_path(input_path: str | Path, output_path: str | Path) -> Path:
    """状态文件与输出文件同目录，且不会覆盖翻译主流程的 state。"""
    output = Path(output_path)
    return output.parent / f".{_safe_state_name(output.stem)}.perspective_state.json"



def _state_options(options: PerspectiveOptions) -> dict[str, Any]:
    return asdict(options.normalized())



def _block_state(
    block: PerspectiveBlock,
    *,
    status: str = "pending",
    error: str = "",
    translated: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "file": block.file,
        "index": block.index,
        "classification": block.classification,
        "source": block.source_text,
        "source_segments": list(block.source_segments),
        "translated_segments": list(translated or []),
        "status": status,
        "error": error,
    }



def load_failed_perspective_blocks(state_path: str | Path) -> list[PerspectiveFailedBlock]:
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    result: list[PerspectiveFailedBlock] = []
    for block_id, item in (data.get("blocks") or {}).items():
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        source_segments = item.get("source_segments") or [str(item.get("source", ""))]
        translated = item.get("translated_segments") or []
        result.append(
            PerspectiveFailedBlock(
                state_path=path,
                block_id=str(block_id),
                file=str(item.get("file", "")),
                source_segments=[str(x) for x in source_segments],
                translated_segments=[str(x) for x in translated],
                error=str(item.get("error", "")),
                classification=str(item.get("classification", "narration")),
            )
        )
    return result



def load_perspective_state(state_path: str | Path) -> dict[str, Any]:
    """读取并校验视角转换存档，供 GUI 直接恢复一次作业。"""
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("视角转换存档必须是 JSON 对象")
    if data.get("mode") != "first_to_third":
        raise ValueError("这不是第一人称改第三人称存档")
    if not str(data.get("source_path", "")).strip():
        raise ValueError("视角转换存档缺少输入 EPUB 路径")
    if not str(data.get("output_path", "")).strip():
        raise ValueError("视角转换存档缺少输出 EPUB 路径")
    options = data.get("options")
    if not isinstance(options, dict):
        raise ValueError("视角转换存档缺少 options")
    normalized_options = PerspectiveOptions(**options).normalized()
    data["options"] = _state_options(normalized_options)
    blocks = data.get("blocks")
    if not isinstance(blocks, dict):
        raise ValueError("视角转换存档缺少 blocks")
    return data



def save_manual_perspective_translation(
    state_path: str | Path,
    block_id: str,
    translated_segments: list[str],
) -> dict[str, int]:
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    item = (data.get("blocks") or {}).get(block_id)
    if not isinstance(item, dict):
        raise ValueError(f"找不到视角转换块：{block_id}")
    source_segments = item.get("source_segments") or []
    if len(source_segments) != len(translated_segments):
        raise ValueError(f"译文分段数量不匹配：需要 {len(source_segments)} 段")
    if not all(isinstance(value, str) and value.strip() for value in translated_segments):
        raise ValueError("译文不能为空")
    item["translated_segments"] = [
        _preserve_edge_whitespace(str(source), str(value))
        for source, value in zip(source_segments, translated_segments)
    ]
    item["status"] = "completed"
    item["error"] = ""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    statuses = [
        value.get("status")
        for value in (data.get("blocks") or {}).values()
        if isinstance(value, dict)
    ]
    return {
        "completed": sum(status == "completed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
    }



def save_perspective_failure(
    state_path: str | Path,
    block_id: str,
    error: str,
) -> dict[str, int]:
    """更新一次失败重试的错误原因，并保留该块的 failed 状态。"""
    path = Path(state_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    item = (data.get("blocks") or {}).get(block_id)
    if not isinstance(item, dict):
        raise ValueError(f"找不到视角转换块：{block_id}")
    item["status"] = "failed"
    item["error"] = str(error)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    statuses = [
        value.get("status")
        for value in (data.get("blocks") or {}).values()
        if isinstance(value, dict)
    ]
    return {
        "completed": sum(status == "completed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
    }
