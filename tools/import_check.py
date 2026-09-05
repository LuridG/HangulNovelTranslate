#!/usr/bin/env python3
"""用依赖桩导入目标模块，专用于在缺第三方库的环境里检测循环/导入错误。

用法：
    python tools/import_check.py pkg1 pkg2 ...
"""

from __future__ import annotations

import importlib
import os
import sys
import types


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install_stubs() -> None:
    # openai 仅缺 OpenAI 类。
    _stub("openai", OpenAI=object)
    # bs4 常在模块顶层 import，且很多类型用于 isinstance。
    _stub("bs4", BeautifulSoup=object, Comment=object, NavigableString=object, Tag=object)
    # customtkinter 的类被用作基类（在 import 时求值）。
    _stub(
        "customtkinter",
        CTk=object,
        CTkToplevel=object,
        CTkScrollableFrame=object,
        CTkFrame=object,
        CTkButton=object,
        CTkLabel=object,
        CTkEntry=object,
        CTkTextbox=object,
        CTkOptionMenu=object,
        CTkSegmentedButton=object,
        CTkCheckbox=object,
        CTkSwitch=object,
        CTkSlider=object,
    )


def main(argv: list[str]) -> int:
    sys.path.insert(0, os.getcwd())
    install_stubs()
    for name in argv[1:]:
        try:
            importlib.import_module(name)
            print("OK  ", name)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
