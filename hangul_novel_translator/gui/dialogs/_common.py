# 由 tools/split_package.py 拆分生成
from __future__ import annotations


class RetranslateResponseError(RuntimeError):
    """回翻时模型响应无法解析成 JSON，携带原始响应供弹窗展示/人工判读。"""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw
