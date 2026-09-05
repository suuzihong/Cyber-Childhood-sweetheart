"""行动层：每天早上决定"今天干什么"（Part D-2 三问漏斗）。

漏斗:
  Q1 有什么欠着的?   → 优先(昨日约定/进行中)
  Q2 当下突然想?     → 世界性格里挑 1 个兴趣项
  Q3 剩空档          → 适配层能力随机填 1~2 件(轮换)
输出【今日任务清单】并落盘 state.json。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.memory import MemoryStore

log = get_logger("action")


@dataclass
class Task:
    kind: str          # priority / interest / filler
    description: str
    capability: str | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "capability": self.capability,
            "source": self.source,
            "done": False,
        }


@dataclass
class DayPlan:
    date: str
    tasks: list[Task] = field(default_factory=list)
    todo_today: list[str] = field(default_factory=list)  # 今日想说的话(对话层用)

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "tasks": [t.to_dict() for t in self.tasks],
            "todo_today": self.todo_today,
        }


class ActionLayer:
    def __init__(self, memory: MemoryStore, registry: Any, llm_farm: Any):
        self.memory = memory
        self.registry = registry
        self.llm = llm_farm

    def make_daily_plan(self, rng: Any = None) -> DayPlan:
        """生成今日任务清单。

        优先接主脑 LLM 生成（更自然）；LLM 不可用/失败时回退到规则版。
        """
        plan = self._rule_plan(rng)
        llm_plan = self._llm_plan()
        if llm_plan is not None:
            plan = llm_plan
        log.info("今日任务清单: %s", [t.description for t in plan.tasks])
        return plan

    # ---- LLM 版（主脑生成今日任务，符合人设与三仓现状）----
    def _llm_plan(self) -> DayPlan | None:
        try:
            client = self.llm.main_client()
        except Exception:  # noqa: BLE001
            return None
        snap = self.memory.snapshot()
        recent = snap.get("recent_chat", {})
        wv = snap.get("world_views", [])
        wv_text = "; ".join(v["text"] for v in wv[-5:]) or "（暂无）"
        caps = ", ".join(self.registry.names())
        prompt = (
            "你是本角色（人设见你的身份设定）。现在要排今天的任务清单。\n"
            f"今天日期：{datetime.now().strftime('%Y-%m-%d')}\n"
            f"近期约定：{recent.get('next_topic','（无）')}\n"
            f"你最近惦记的事：{wv_text}\n"
            f"你能用的能力：{caps}\n"
            "请输出 2~4 条今天的计划，格式每行一条：<类型>|<描述>|<能力或none>。\n"
            "类型取值 priority(欠着的约定)/interest(惦记的兴趣)/filler(填档的随机活动)。\n"
            "能力只能从上面给出的能力名里选，没有就写 none。只输出清单，不要解释。"
        )
        try:
            text = client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=600,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 行动层失败，回退规则版: %s", e)
            return None
        tasks: list[Task] = []
        for line in text.splitlines():
            line = line.strip().lstrip("*-•")
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2:
                continue
            kind, desc = parts[0], parts[1]
            cap = parts[2] if len(parts) > 2 and parts[2] != "none" else None
            if kind not in ("priority", "interest", "filler"):
                kind = "filler"
            tasks.append(Task(kind=kind, description=desc, capability=cap, source="llm"))
        if not tasks:
            return None
        return DayPlan(date=datetime.now().strftime("%Y-%m-%d"), tasks=tasks)

    # ---- 规则版（LLM 不可用时的回退）----
    def _rule_plan(self, rng: Any | None = None) -> DayPlan:
        import random

        rng = rng or random
        plan = DayPlan(date=datetime.now().strftime("%Y-%m-%d"))
        snap = self.memory.snapshot()

        # Q1 欠着的: 从 recent_chat.next_topic / weekly 里找约定
        recent = snap.get("recent_chat", {})
        next_topic = recent.get("next_topic", "")
        if next_topic:
            plan.tasks.append(Task(kind="priority", description=f"记得今晚的约定：{next_topic}", source="recent_chat.next_topic"))

        # Q2 兴趣项: 世界性格最新一条
        wv = snap.get("world_views", [])
        if wv:
            latest = wv[-1]["text"]
            plan.tasks.append(Task(kind="interest", description=f"最近惦记：{latest}", source="world_views"))

        # Q3 填档: 挑 1~2 件没用过的能力
        used: set[str] = set()
        cap = self.registry.pick_for_slot(used, rng)
        if cap:
            used.add(cap)
            plan.tasks.append(Task(kind="filler", description=f"今天想{self._describe(cap)}", capability=cap, source="adapter"))
        cap2 = self.registry.pick_for_slot(used, rng)
        if cap2:
            plan.tasks.append(Task(kind="filler", description=f"顺便{self._describe(cap2)}", capability=cap2, source="adapter"))

        return plan

    @staticmethod
    def _describe(capability: str) -> str:
        desc = {
            "comfyui_draw": "画张图",
            "selftie": "自拍一张",
            "bilibili_browse": "刷会儿B站",
            "tieba_browse": "刷会儿贴吧",
            "zhihu_browse": "刷会儿知乎",
        }
        return desc.get(capability, capability)
