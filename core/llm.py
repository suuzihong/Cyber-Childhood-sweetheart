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


# 各模型的采样参数约束。键为模型名前缀（小写匹配），值为该模型允许的区间 [下限, 上限]。
# 背景（2026-09-10）：备用端点 kimi-k2.6 只接受 temperature=1，主端点惯例传 0.8~0.9，
# 直传会 400，导致故障转移形同虚设——主端点一挂，回复就退化成规则兜底。
_MODEL_PARAM_RULES: dict[str, dict[str, list[float]]] = {
    "kimi-k2": {"temperature": [1.0, 1.0]},
}


def _adapt_params_for_model(model: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """把调用参数夹到目标模型允许的区间内；无约束或无需修改时原样返回。

    只处理关键字参数（采样参数都走 kwargs，不涉及 args）。
    """
    m = (model or "").lower()
    rule: dict[str, list[float]] | None = None
    for prefix, r in _MODEL_PARAM_RULES.items():
        if m.startswith(prefix):
            rule = r
            break
    if not rule:
        return kwargs
    out = dict(kwargs)
    for key, bounds in rule.items():
        if key in out and isinstance(out[key], (int, float)) and not isinstance(out[key], bool):
            out[key] = min(max(float(out[key]), bounds[0]), bounds[1])
    return out


class FallbackClient:
    """主/备双端点客户端：主端点失败后自动切到备用端点“接代思考”。

    与 LLMClient 保持同名接口（chat / chat_json），因此上层
    （dialogue / dream / 碎片提炼）无需感知，替换即得故障转移。

    切换时机：主端点在自身重试耗尽后仍抛异常（连接失败、超时、
    5xx/429 重试用尽、鉴权或配额异常等）→ 记一条 WARNING 后转备用。
    备用也失败 → 抛出备用端异常（上层照常走规则兜底），日志两边原因都记。

    不自动“切回”主端点：下次调用仍先试主端点，避免主端点抖一下就永久降级。
    """

    def __init__(self, primary: "LLMClient", secondary: "LLMClient", section: str = ""):
        self.primary = primary
        self.secondary = secondary
        self.section = section
        self._last_used = "primary"
        self._switch_count = 0

    @property
    def model(self) -> str:
        return self.primary.model if self._last_used == "primary" else self.secondary.model

    @property
    def base_url(self) -> str:
        return self.primary.base_url if self._last_used == "primary" else self.secondary.base_url

    @property
    def api_key(self) -> str:
        return self.primary.api_key if self._last_used == "primary" else self.secondary.api_key

    @property
    def timeout(self) -> int:
        return self.primary.timeout if self._last_used == "primary" else self.secondary.timeout

    def _model_label(self) -> str:
        return f"{self.model}@{self.base_url} (llm.{self.section or '?'}, last={self._last_used})"

    def _try(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """先走主端点，失败则换备用端点执行同一个方法。"""
        try:
            result = getattr(self.primary, method)(*args, **kwargs)
            self._last_used = "primary"
            return result
        except Exception as e:  # noqa: BLE001 —— 主端点任何失败都要能接管
            self._switch_count += 1
            log.warning(
                "llm.%s 主端点失败，切换备用端点接管（第%d次切换）: %s: %s",
                self.section or "?", self._switch_count, type(e).__name__, e,
            )
        try:
            result = getattr(self.secondary, method)(*args, **kwargs)
            self._last_used = "secondary"
            log.info("llm.%s 备用端点接管成功: %s", self.section or "?", self.secondary.model)
            return result
        except LLMError as e2:
            # 备用端点模型可能不接受主端点的采样参数（如 kimi-k2.6 只允许 temperature=1），
            # 直传会 400。这不是“端点挂了”而是“参数不兼容”，修正后重试一次，
            # 否则故障转移等于摆设：主端点一挂，回复就退化成规则兜底。
            fixed = _adapt_params_for_model(self.secondary.model, kwargs)
            if fixed != kwargs:
                log.warning(
                    "llm.%s 备用端点拒绝采样参数，按 %s 的约束修正后重试: %s",
                    self.section or "?", self.secondary.model, e2,
                )
                try:
                    result = getattr(self.secondary, method)(*args, **fixed)
                    self._last_used = "secondary"
                    log.info("llm.%s 备用端点接管成功（已修正参数）: %s",
                             self.section or "?", self.secondary.model)
                    return result
                except Exception as e3:  # noqa: BLE001
                    log.error(
                        "llm.%s 备用端点修正参数后仍失败，交由上层兜底: %s: %s",
                        self.section or "?", type(e3).__name__, e3,
                    )
                    raise
            log.error(
                "llm.%s 备用端点也失败，交由上层兜底: %s: %s",
                self.section or "?", type(e2).__name__, e2,
            )
            raise

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        return self._try("chat", *args, **kwargs)

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        return self._try("chat_json", *args, **kwargs)


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
        primary = LLMClient(
            base_url=conf.get("base_url", ""),
            api_key=self.cfg.resolve_api_key(section),
            model=conf.get("model", section),
            retries=int(retry_cfg.get("max_retries", 2)),
            backoff_base=float(retry_cfg.get("backoff_base", 2.0)),
            timeout=int(conf.get("timeout", 40)),
        )
        # 备用端点：主端点整体失败（连接不通/超时/5xx/鉴权异常）后接管。
        # 写法 llm.<section>.fallback = {...} 即生效；不配就退化成单端点。
        fb_conf = conf.get("fallback") or {}
        if not fb_conf.get("base_url") or not fb_conf.get("model"):
            return primary
        fb_key = fb_conf.get("api_key") or self.cfg.resolve_api_key(f"{section}.fallback") or ""
        # 占位符未替换（如 YOUR_FALLBACK_API_KEY_HERE）→ 视为未配置备用：
        # 否则主端点一挂，会拿着无效 key 去备用端点撞 401，日志更难定位。
        if not fb_key or fb_key.startswith("YOUR_"):
            log.info("llm.%s 备用端点已填写但 api_key 仍是占位符，跳过启用", section)
            return primary
        fb_retry = fb_conf.get("retry", {}) or {}
        secondary = LLMClient(
            base_url=fb_conf.get("base_url", ""),
            api_key=fb_key,
            model=fb_conf.get("model", ""),
            retries=int(fb_retry.get("max_retries", retry_cfg.get("max_retries", 2))),
            backoff_base=float(fb_retry.get("backoff_base", retry_cfg.get("backoff_base", 2.0))),
            timeout=int(fb_conf.get("timeout", conf.get("timeout", 40))),
        )
        log.info(
            "llm.%s 已启用备用端点: %s (%s)",
            section, secondary.model, secondary.base_url,
        )
        return FallbackClient(primary, secondary, section=section)

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
