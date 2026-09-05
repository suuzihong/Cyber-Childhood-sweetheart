"""适配层：能力注册表 + 分发（Part E）。

每个能力实现一个统一接口:
    def name(self) -> str
    def daily_limit(self) -> int
    def execute(self, ctx) -> AdapterResult

AdapterResult 是产出对象, 统一回灌闸门。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class AdapterResult:
    """一个能力执行后的产出。"""

    capability: str
    output: Any
    summary: str = ""            # 人话总结, 供碎片/对话层使用
    to_world_view: bool = False  # 是否允许上浮世界性格层(闸门2通道)
    shareable: bool = False      # 是否值得跟 {{user}} 分享
    media: list[str] = field(default_factory=list)  # 本地媒体文件路径
    link: str = ""               # 可分享的原始链接(视频/帖子), 供主动开口时带上


class Adapter(Protocol):
    def name(self) -> str: ...

    def daily_limit(self) -> int: ...

    def execute(self, ctx: dict[str, Any]) -> AdapterResult: ...


class AdapterRegistry:
    """能力注册表。新增能力 = 注册一个 Adapter。"""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name()] = adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def get(self, name: str) -> Adapter | None:
        return self._adapters.get(name)

    def pick_for_slot(self, used_today: set[str], rng: Any) -> str | None:
        """从没做腻/没用过的能力里挑一个（D-2 的 Q3 填档）。"""
        available = [n for n in self._adapters if n not in used_today]
        if not available:
            return None
        import random

        return rng.choice(available) if rng else random.choice(available)
