# hangul_novel_translator/gui/state.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_UI_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / ".gui_config.json"
_COMMON_GLOSSARY_PATH = Path(__file__).resolve().parent.parent.parent / "common_glossary.json"
_COMMON_GLOSSARY_TEMPLATE = Path(__file__).resolve().parent.parent.parent / "common_glossary.template.json"


def _load_ui_state() -> dict[str, Any]:
    try:
        if _UI_CONFIG_PATH.exists():
            return json.loads(_UI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_ui_state(state: dict[str, Any]) -> None:
    try:
        _UI_CONFIG_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def common_glossary_path() -> Path:
    """通用词表持久化路径（项目根目录，机器本地用户数据）。"""
    return _COMMON_GLOSSARY_PATH


def common_glossary_template_path() -> Path:
    """通用词表初始模板路径（入库文件，首次启动时据此生成本地通用词表）。"""
    return _COMMON_GLOSSARY_TEMPLATE



