# hangul_novel_translator/llm.py
from __future__ import annotations

import time
from typing import Iterable

from openai import OpenAI

from .config import AppConfig
from .utils import extract_json


class LLMError(RuntimeError):
    pass


class LLMClient:
    """OpenAI 兼容聊天接口的薄封装，带重试。"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                kwargs = {
                    "model": self.config.model,
                    "messages": messages,
                    "temperature": (
                        self.config.temperature
                        if temperature is None
                        else temperature
                    ),
                    "timeout": self.config.timeout,
                }
                # 大多数 OpenAI 兼容服务支持 response_format。
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content is None:
                    raise LLMError("模型返回了空 content")
                return content
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                wait = self.config.retry_delay * (2 ** (attempt - 1))
                time.sleep(wait)

        raise LLMError(f"调用 LLM 失败：{last_error}")

    def chat_json(self, messages: list[dict[str, str]], *, temperature: float | None = None):
        content = self.chat(messages, temperature=temperature, json_mode=True)
        return extract_json(content)
