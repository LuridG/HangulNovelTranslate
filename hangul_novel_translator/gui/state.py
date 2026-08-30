# hangul_novel_translator/gui/state.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_UI_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / ".gui_config.json"


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



