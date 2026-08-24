# hangul_novel_translator/llm.py
from __future__ import annotations

import queue
import threading
import time
from typing import Iterable

from openai import OpenAI

from .config import AppConfig
from .utils import extract_json


class LLMError(RuntimeError):
    pass


class LLMCancelled(LLMError):
    """调用方请求停止时抛出，不应被当作普通模型失败重试。"""


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
        cancel_event: threading.Event | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCancelled("LLM 调用已取消")
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

                if cancel_event is None:
                    response = self.client.chat.completions.create(**kwargs)
                else:
                    response = self._create_cancellable(kwargs, cancel_event)
                choice = response.choices[0]
                message = choice.message
                content = message.content
                if content is None:
                    detail = ""
                    finish_reason = getattr(choice, "finish_reason", None)
                    if finish_reason:
                        detail += f"（finish_reason={finish_reason}）"
                    refusal = getattr(message, "refusal", None)
                    if refusal:
                        detail += f"；refusal={str(refusal)[:200]}"
                    raise LLMError(f"模型返回了空 content{detail}")
                return content
            except LLMCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                wait = self.config.retry_delay * (2 ** (attempt - 1))
                if cancel_event is not None:
                    if cancel_event.wait(wait):
                        raise LLMCancelled("LLM 重试等待已取消")
                else:
                    time.sleep(wait)

        raise LLMError(f"调用 LLM 失败：{last_error}")

    def _create_cancellable(self, kwargs: dict, cancel_event: threading.Event):
        """让同步 HTTP 请求的等待可以被停止事件打断。"""
        if cancel_event.is_set():
            raise LLMCancelled("LLM 调用已取消")
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def request_worker() -> None:
            try:
                result_queue.put((True, self.client.chat.completions.create(**kwargs)))
            except Exception as exc:  # noqa: BLE001
                result_queue.put((False, exc))

        threading.Thread(target=request_worker, daemon=True).start()
        while True:
            if cancel_event.is_set():
                raise LLMCancelled("LLM 调用已取消")
            try:
                ok, value = result_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if ok:
                return value
            raise value

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        cancel_event: threading.Event | None = None,
    ):
        content = self.chat(
            messages,
            temperature=temperature,
            json_mode=True,
            cancel_event=cancel_event,
        )
        return extract_json(content)
