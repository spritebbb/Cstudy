"""DeepSeek 调用封装：统一超时、重试和报错。

DeepSeek 提供 OpenAI 兼容接口，所以直接用 openai SDK，只换 base_url。
重试自己做（关掉 SDK 自带的重试），这样重试过程能进日志，用户能看见。
"""

from __future__ import annotations

import time
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from ..config import Config
from ..logging_setup import get_logger

log = get_logger(__name__)


class AIError(RuntimeError):
    """调用 AI 失败。消息直接显示给用户，所以要说人话并给出下一步。"""


# 重试有意义的错误：网络抖动、超时、限流、服务端临时故障
RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


class DeepSeekClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.ai_model
        self.max_retries = int(config.get("ai.max_retries", 3))
        self.backoff = float(config.get("ai.retry_backoff_seconds", 5))
        self.temperature = float(config.get("ai.temperature", 0.3))
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        """第一次真正调用时才建连接。

        这样即使 .env 里没填密钥，界面也能正常打开，
        只在用户点「生成笔记」时才提示去填密钥。
        """
        if self._client is None:
            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.ai_base_url,
                timeout=float(self.config.get("ai.timeout_seconds", 180)),
                max_retries=0,  # 自己重试
            )
        return self._client

    def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """发一次对话请求，返回文本内容。失败会重试，最终失败抛 AIError。"""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                log.debug("请求 %s（第 %d/%d 次）", self.model, attempt, self.max_retries)
                response = self.client.chat.completions.create(**kwargs)
                content = (response.choices[0].message.content or "").strip()
                if not content:
                    raise AIError("AI 返回了空内容")
                usage = getattr(response, "usage", None)
                if usage:
                    log.info(
                        "AI 调用成功：输入 %s tokens，输出 %s tokens",
                        usage.prompt_tokens,
                        usage.completion_tokens,
                    )
                return content

            except AuthenticationError as exc:
                # 重试没意义，直接告诉用户去改密钥
                raise AIError(
                    "DeepSeek 密钥无效或被拒绝。请检查项目根目录 .env 里的 "
                    "DEEPSEEK_API_KEY 是否正确、账户是否还有余额。"
                ) from exc

            except BadRequestError as exc:
                # 请求本身有问题，重试也没用
                raise AIError(f"请求被 DeepSeek 拒绝（多半是内容过长）：{exc}") from exc

            except RETRYABLE as exc:
                last_error = exc
                if attempt < self.max_retries:
                    wait = self.backoff * attempt
                    log.warning("第 %d 次失败（%s），%.0f 秒后重试", attempt, type(exc).__name__, wait)
                    time.sleep(wait)
                else:
                    log.error("重试 %d 次后仍然失败", self.max_retries)

            except APIStatusError as exc:
                # 5xx 值得重试，4xx 不值得
                if exc.status_code >= 500 and attempt < self.max_retries:
                    last_error = exc
                    wait = self.backoff * attempt
                    log.warning("服务端 %d，%.0f 秒后重试", exc.status_code, wait)
                    time.sleep(wait)
                else:
                    raise AIError(f"DeepSeek 返回错误 {exc.status_code}：{exc.message}") from exc

        raise AIError(
            f"连接 DeepSeek 失败，已重试 {self.max_retries} 次。"
            f"请检查网络连接后重试。原始错误：{last_error}"
        )

    def chat_json(self, messages: list[dict[str, Any]], temperature: float | None = None) -> str:
        """要求模型返回 JSON。提示词里必须出现 'JSON' 字样，否则接口会报错。"""
        return self.chat(messages, temperature=temperature, json_mode=True)
