"""做梦层：凌晨 00:30 的记忆整理引擎（Part D-4 五步流程）。

步骤1 碎片→事实迁移(LLM 判断：稳定事实进表格仓 / 一次情绪丢弃)
步骤2 世界性格 append(LLM 提炼新看法，受每日节流 + 闸门2 语义检查)
步骤3 随机想法(LLM 生成梦境噪音，不入长期仓)
步骤4 三仓整理(近期折碎片/碎片TTL清理/表格过期归档/黑历史上限)
步骤5 写明日日程草稿(清掉"今日"状态)
LLM 不可用/失败时全部回退到规则版，不打断一天循环。
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.memory import MemoryStore

log = get_logger("dream")


class DreamLayer:
    def __init__(self, memory: MemoryStore, cfg: Any, llm_farm: Any = None):
        self.memory = memory
        self.cfg = cfg
        self.llm = llm_farm

    def dream(self, rng: Any = None, day_events: list[str] | None = None, dream_date: str | None = None) -> dict[str, Any]:
        """执行一次做梦流程，返回本次梦境摘要。

        dream_date: 要沉淀的“这一天”的日期（YYYY-MM-DD）。默认取昨天（00:30 做梦，总结刚过去的一天）。
        """
        rng = rng or random
        report: dict[str, Any] = {"migrated": [], "world_views_added": [], "dream_noise": [], "pruned": 0, "immediate_summary": ""}
        day_events = day_events or []
        if not dream_date:
            from datetime import timedelta

            dream_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # 步骤0 即时记忆→每日摘要（参考柏宝书：边聊边记→睡后总结，带日期锚点）
        self._consolidate_immediate(report, dream_date)

        # 步骤1 碎片→事实迁移（LLM 判断；失败回退规则版）
        self._migrate(report, rng)

        # 步骤2 世界性格 append（受节流 + 闸门2 检查）
        self._consolidate_world_views(report)

        # 步骤3 随机想法（LLM 生成梦境噪音，不入长期仓）
        report["dream_noise"] = self._dream_noise(rng, day_events)

        # 步骤4 三仓整理
        report["pruned"] = self.memory.prune_fragments()

        # 步骤5 明日日程草稿：清掉"今日"状态，留给明早行动层重建
        self.memory.set_next_topic("")
        self.memory.set_last_dream_date(dream_date)  # 记录已沉淀的日期，供醒来层判断可清理
        self.memory.save()
        log.info("做梦完成(%s): %s", dream_date, report)
        return report

    # ---- 步骤0：即时记忆→每日摘要（参考柏宝书：边聊边记→睡后总结，带日期锚点）----
    def _consolidate_immediate(self, report: dict[str, Any], dream_date: str) -> None:
        """把某天的即时记忆（当天对话原文）总结成一段长期摘要，存进 daily_summaries。

        保留原始对话（不删），供第二天早上当背景；摘要作为长期记忆供后续随时调用。
        """
        lines = self.memory.immediate_for(dream_date)
        if not lines:
            return
        chat_text = "\n".join(lines[-60:])  # 最多取最近 60 句，防超长
        summary = ""
        if self.llm is not None:
            try:
                client = self.llm.main_client()
                prompt = (
                    "你在帮角色做睡前的记忆整理。下面是角色在"
                    f"{dream_date} 和很熟的{{user}}的聊天记录。\n\n"
                    f"{chat_text}\n\n"
                    "请用 2~4 句话总结这一天：聊了什么话题、{{user}}的近况、角色的心情、"
                    "有没有什么值得记住的约定或事实。"
                    "用第一人称（我/{{user}}），口语化一点，像角色睡前自己回想。"
                    "只输出这段总结本身，不要标题不要序号。"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=0.6, max_tokens=300
                ).strip()
                if text:
                    summary = text
            except Exception as e:  # noqa: BLE001
                log.warning("LLM 每日摘要失败，回退规则版: %s", e)
        if not summary:
            # 规则版回退：保留最近几条原文，确保不丢内容
            summary = "；".join(lines[-5:])
        self.memory.set_daily_summary(dream_date, summary)
        report["immediate_summary"] = summary

    # ---- 步骤1：碎片→事实迁移 ----
    def _migrate(self, report: dict[str, Any], rng: Any) -> None:
        pending = self.memory.list_fragments(pending_only=True)
        if not pending:
            return
        if self.llm is not None:
            try:
                client = self.llm.main_client()
                frag_texts = "\n".join(f"- {f['text']} (来源:{f.get('source','?')})" for f in pending)
                prompt = (
                    "你在帮一个角色做睡前记忆整理。下面是她今天积攒的碎片记忆，每条都是"
                    "'未确认'的。\n"
                    f"{frag_texts}\n\n"
                    "请判断每条该归到哪：stable(像是稳定事实，该记住) / discard(只是一次性情绪或"
                    "闲聊，没必要记)。\n"
                    "输出格式：每行一条 <编号>: <stable|discard>。只输出判断，不要解释。"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=400
                )
                stable_idx: set[int] = set()
                for line in text.splitlines():
                    parts = line.split(":")
                    if len(parts) < 2:
                        continue
                    try:
                        idx = int(parts[0].strip()) - 1
                    except ValueError:
                        continue
                    if parts[1].strip().lower() in ("stable", "keep", "是", "记住"):
                        stable_idx.add(idx)
                for i, frag in enumerate(pending):
                    if i in stable_idx or frag.get("source") in ("confirmed_observation",):
                        # 用 text 匹配确认（索引会在多次确认后错位，导致后面的漏掉）
                        if self.memory.confirm_fragment_by_text(frag.get("text", "")):
                            report["migrated"].append(frag["text"])
                return
            except Exception as e:  # noqa: BLE001
                log.warning("LLM 碎片迁移失败，回退规则版: %s", e)
        # 规则版回退：有明确来源的先迁移
        for i, frag in enumerate(pending):
            if frag.get("source") in ("chat", "confirmed_observation"):
                if self.memory.confirm_fragment_by_text(frag.get("text", "")):
                    report["migrated"].append(frag["text"])

    # ---- 步骤2：世界性格 append（受节流 + 闸门2）----
    def _consolidate_world_views(self, report: dict[str, Any]) -> None:
        daily_cap = self.cfg.schedule.get("max_daily_world_views", 5)
        today = datetime.now().strftime("%Y-%m-%d")
        added_today = sum(1 for v in self.memory.world_views() if v["date"] == today)
        slot_left = max(0, daily_cap - added_today)
        if slot_left <= 0:
            return
        frags = self.memory.list_fragments(pending_only=True)
        if not frags:
            return
        if self.llm is not None:
            try:
                client = self.llm.main_client()
                frag_texts = "\n".join(f"- {f['text']}" for f in frags)
                prompt = (
                    "你在整理一个角色的世界观（她的世界性格层，只做加法）。她今天经历/想了一些事：\n"
                    f"{frag_texts}\n\n"
                    "从中提炼【最多 1 条】值得沉淀进她世界观的新看法（一条自然的、她这样的假小子毒舌但心软的人会有的想法）。\n"
                    "硬约束：1) 不能与她的核心人设冲突（她是运动系假小子、嘴毒心软、"
                    "绝不会自我贬低成工具、不会背叛青梅竹马的信任）；2) 只输出这一条看法本身，"
                    "不要序号不要解释。如果没有值得沉淀的，输出：无"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=0.8, max_tokens=300
                ).strip()
                if text and text != "无":
                    self.memory.append_world_view(text, source="dream_consolidation", date=today)
                    report["world_views_added"].append(text)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("LLM 世界性格沉淀失败，回退规则版: %s", e)
        # 规则版回退：挑第一条
        self.memory.append_world_view(frags[0]["text"], source="dream_consolidation", date=today)
        report["world_views_added"].append(frags[0]["text"])

    # ---- 步骤3：随机想法（梦境噪音，不入长期仓）----
    def _dream_noise(self, rng: Any, day_events: list[str]) -> list[str]:
        if self.llm is not None:
            try:
                client = self.llm.main_client()
                ev = "; ".join(day_events[-5:]) or "今天没啥特别的"
                prompt = (
                    "你是本角色，刚睡下开始做梦。今天大致经历了："
                    f"{ev}。\n"
                    "请你生成 2 条角色会做的无厘头但符合角色人设的梦。每条一句话，直接分两行输出，不要序号不要解释。"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=1.0, max_tokens=200
                )
                noise = [l.strip().lstrip("*-•1234567890. ") for l in text.splitlines() if l.strip()][:2]
                if noise:
                    return noise
            except Exception as e:  # noqa: BLE001
                log.warning("LLM 梦境生成失败，回退规则版: %s", e)
        noise_pool = [
            "梦见自己拿了把扫帚当球拍，还挺顺手。",
            "梦里翻冰箱，翻出一个会说话的煎蛋。",
            "梦见跟{{user}}比赛爬墙，结果俩人都卡墙头上下不来。",
        ]
        return rng.sample(noise_pool, k=min(2, len(noise_pool)))
