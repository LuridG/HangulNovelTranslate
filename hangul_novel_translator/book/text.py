# 由 tools/split_package.py 拆分生成
from __future__ import annotations
import unicodedata


def _decode_bytes(data: bytes) -> str:
    last_error: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "cp949", "euc-kr", "utf-16"):
        try:
            return data.decode(encoding)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    return data.decode("utf-8", errors="replace")



def _strip_invisible_chars(text: str) -> str:
    """去掉零宽/不可见格式字符（U+200B~U+200F、U+2060~U+2064、U+FEFF 等 Cf 类字符）。"""
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
