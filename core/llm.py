"""LLM 客户端（OpenAI 兼容接口）。三档：main / cheap / vision。

骨架阶段用 requests 直连，不引入 SDK，保持依赖最少。
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

import requests

from .config import Config
from .logger import get_logger

log = get_logger("llm")

# 值得自动重试的 HTTP 状态码（服务端临时性故障）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LLMClient:
    """单个 OpenAI 兼容端点客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: int = 40,
        retries: int = 2,
        backoff_base: float = 2.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff_base = max(0.5, float(backoff_base))

    def _post(self, url: str, headers: dict, payload: dict) -> requests.Response:
        """单次请求；网络连接错误抛 LLMError(可重试)。"""
        try:
            return requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        except requests.ConnectionError as e:
            raise LLMError(f"llm 连接失败: {e}", status=None, retryable=True) from e

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.8,
        max_tokens: int = 2000,
        return_finish_reason: bool = False,
        allow_reasoning_fallback: bool = True,
    ) -> str | tuple[str, str]:
        """普通对话补全，返回文本。

        对服务端临时故障（5xx/429/连接失败）做指数退避重试：
        LLM 服务一抖就走到规则兜底（“牛头不对马嘴”）的问题，靠这里兜住。

        return_finish_reason=True 时返回 (文本, finish_reason)：
        调用方可据此判断是否被 max_tokens 截断（finish_reason == "length"），
        从而重试而不是把半句话发出去。

        allow_reasoning_fallback=False 时，content 为空不会用 reasoning_content（思考过程）
        兜底，而是返回空——用于碎片提炼等场景，避免把模型的思考过程存进记忆。
        """
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self._post(url, headers, payload)
                if resp.status_code != 200:
                    err = LLMError(
                        f"llm http {resp.status_code}: {resp.text[:500]}",
                        status=resp.status_code,
                    )
                    if resp.status_code not in RETRYABLE_STATUS or attempt >= attempts:
                        raise err
                    log.warning("llm http %d (第%d/%d次)，%.1fs 后重试: %s",
                                resp.status_code, attempt, attempts, self._delay(attempt), resp.text[:120])
                    time.sleep(self._delay(attempt))
                    continue
                data = resp.json()
                try:
                    choice = data["choices"][0]
                    msg = choice["message"]
                    content = (msg.get("content") or "").strip()
                    if not content and allow_reasoning_fallback:
                        # 推理型模型可能把最终回答放在 reasoning_content，且 content 为空
                        content = (msg.get("reasoning_content") or "").strip()
                    finish_reason = str(choice.get("finish_reason") or "")
                    if return_finish_reason:
                        return content, finish_reason
                    return content
                except (KeyError, IndexError, TypeError) as e:
                    raise LLMError(f"llm bad response: {e}") from e
            except requests.Timeout:
                # 读取超时：服务端已长时间无响应，重试大概率更慢，直接抛给上层兜底
                raise LLMError(f"llm 请求超时({self.timeout}s)", status=None) from None
            except LLMError as e:
                if (e.status not in RETRYABLE_STATUS and not e.retryable) or attempt >= attempts:
                    raise
                log.warning("llm 连接失败 (第%d/%d次)，%.1fs 后重试: %s",
                            attempt, attempts, self._delay(attempt), e)
                time.sleep(self._delay(attempt))

        raise LLMError(f"llm 请求失败（已重试 {self.retries} 次）", status=None)

    def _delay(self, attempt: int) -> float:
        """指数退避 + 随机抖动：2s→4s（attempt 从 1 起）。"""
        return self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.5)

    def chat_json(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
        """要求 JSON 输出（骨架阶段用指令约束 + 容错解析）。"""
        text = self.chat(messages, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                raise LLMError(f"llm json parse failed: {e}") from e


class LLMFarm:
    """按档位路由的客户端集合。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.main = self._build("main")
        self.cheap = self._build("cheap")
        self.vision = self._build("vision")

    def _build(self, section: str) -> LLMClient | None:
        conf = self.cfg.llm.get(section)
        if not conf:
            return None
        retry_cfg = conf.get("retry", {}) or {}
        return LLMClient(
            base_url=conf.get("base_url", ""),
            api_key=self.cfg.resolve_api_key(section),
            model=conf.get("model", section),
            retries=int(retry_cfg.get("max_retries", 2)),
            backoff_base=float(retry_cfg.get("backoff_base", 2.0)),
        )

    def main_client(self) -> LLMClient:
        if self.main is None:
            raise LLMError("llm.main 未配置")
        return self.main

    def cheap_client(self) -> LLMClient:
        if self.cheap is None:
            raise LLMError("llm.cheap 未配置")
        return self.cheap

    def vision_client(self) -> LLMClient:
        if self.vision is None:
            raise LLMError("llm.vision 未配置")
        return self.vision
