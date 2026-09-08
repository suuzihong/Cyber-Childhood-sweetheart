"""对话层：行动产出 → 主动话题（Part D-5）。

不自主动作，只把行动层/适配层的产出转成"今天想跟 {{user}} 说的话"。
触发点固定(18:30/20:00/21:30)，有频率护栏(一天≤4次主动)，21:45 后不主动。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from core.logger import get_logger
from core.memory import MemoryStore
from core import persona

log = get_logger("dialogue")


class DialogueLayer:
    def __init__(self, memory: MemoryStore, cfg: Any, llm_farm: Any = None):
        self.memory = memory
        self.cfg = cfg
        self.llm = llm_farm

    def _llm_topic(self, day_events: list[str], snap: dict[str, Any], has_media: bool = False, already_said: str = "", urgency: str = "", recent_context: str = "") -> str | None:
        """用主脑 LLM 生成今天想说的话（多角色：system 人设 + user 指令）。失败返回 None。"""
        if self.llm is None:
            return None
        try:
            client = self.llm.main_client()
            # system: 完整人设（卡级强度）+ 记忆回灌；user: 本次主动聊天指令
            system_msg = persona.build_system_prompt(snap, self.cfg)
            user_prompt = persona.build_topic_prompt(snap, day_events, has_media, already_said, urgency, recent_context)
            text, finish_reason = client.chat(
                system_msg + [{"role": "user", "content": user_prompt}],
                temperature=0.9,
                max_tokens=256,
                return_finish_reason=True,
            )
            # 清理可能残留的引号
            text = (text or "").strip().strip('"\'“”‘’')
            if not text or len(text) > 140 or finish_reason == "length":
                # 空/超长/被 max_tokens 截断（finish_reason=length）都视为失败，让调用方走规则回退
                return None
            return text
        except Exception as e:  # noqa: BLE001
            log.warning("对话层 LLM 生成失败，回退规则版: %s", e)
            return None

    def build_topic(self, day_events: list[str], touchpoint_index: int, has_media: bool = False, already_said: str = "", urgency: str = "", recent_context: str = "") -> str:
        """把今天的经历转成一句"想跟 {{user}} 说的话"。

        LLM 优先（有主脑时挑最有味道的点）；失败/无 LLM 回退规则版。
        has_media: 今天是否真的产出了可分享的图/照片（没有时提示词禁止她口嗨“画了图/发你图”）。
        urgency: 冷场情绪等级（idle/concern/probe/pout/panic），闲补主动开口时按冷场时长注入，空串=不追加。
        recent_context: 最近相处简述（用于情绪升级时贴合实际地判断该不该道歉）。
        """
        if not day_events:
            # 冷场越久，即使今天没素材也有情绪语气兜底；抄用 urgency 让"闲着"也有黏人感
            if urgency:
                base = "欸，今天也没啥特别的，就是想跟你说句话。"
                extra = persona.build_escalation_prompt(urgency, recent_context)
                return f"{base}\n（{extra}）" if extra else base
            return "欸，今天也没啥特别的，就是想跟你说句话。"

        snap = self.memory.snapshot()
        llm_topic = self._llm_topic(day_events, snap, has_media, already_said, urgency, recent_context)
        if llm_topic:
            log.info("对话层 LLM 生成话题: %s (touchpoint %d)", llm_topic, touchpoint_index)
            return llm_topic

        # 规则回退：取今天里最"值得分享"的一条
        pick = day_events[0]
        log.info("对话层挑中话题(规则): %s (touchpoint %d)", pick, touchpoint_index)
        return pick

    def build_reply(self, user_msg: str, pending_msgs: list[str] | None = None, last_outgoing: dict | None = None, link_content: str | None = None) -> str:
        """被动回复：{{user}}私聊找她，生成她要回的话（含连发合并节流）。

        回复不计入每日主动上限（那是主动开口的护栏）。
        last_outgoing: 她最近一次主动说过的话（含媒体/链接），注入上下文让她接得住话茬。
        link_content: {{user}}发来的链接已解析出的真实内容；为空则忽略。
        """
        msgs = pending_msgs if pending_msgs else [user_msg]
        snap = self.memory.snapshot()
        # 把共同记忆原文注入回复指令，提到这些时她必须照实说
        story = snap.get("user_history", [])
        story_lines = "\n".join(f"- {s}" for s in story[:10]) or "(暂无)"
        if self.llm is not None:
            try:
                client = self.llm.main_client()
                system_msg = persona.build_system_prompt(snap, self.cfg)
                user_prompt = persona.build_reply_prompt(user_msg, pending_msgs, story_lines, last_outgoing, link_content)
                text = ""
                # 生成回复：若被 max_tokens 截断（finish_reason=length）则重试一次；
                # 仍截断就视为失败走规则兜底，绝不把半句话发出去。
                for _attempt in range(2):
                    text, finish_reason = client.chat(
                        system_msg + [{"role": "user", "content": user_prompt}],
                        temperature=0.9,
                        max_tokens=256,
                        return_finish_reason=True,
                    )
                    text = (text or "").strip().strip('"\'“”‘’')
                    if text and finish_reason != "length":
                        break
                    log.warning("回复被截断(finish_reason=%s)，重试一次", finish_reason)
                    text = ""
                if text and len(text) <= 300:
                    # 记进即时记忆仓（当天对话原文，做梦时才沉淀；同时落盘防丢失）
                    today = datetime.now().strftime("%Y-%m-%d")
                    self.memory.add_immediate(today, f"{{{{user}}}}: {' / '.join(msgs)}")
                    self.memory.add_immediate(today, f"{self.cfg.agent_name}: {text}")
                    # 碎片提炼：从这轮聊天里抽一条“有分量的观察”进碎片仓（失败/无料不打扰主流程）
                    self._harvest_fragment(user_msg, text)
                    self.memory.save()
                    log.info("对话层生成回复: %s", text)
                    return text
            except Exception as e:  # noqa: BLE001
                log.warning("回复 LLM 生成失败，回退规则: %s", e)
        # 规则回退：口语化接话
        fallback = self._rule_reply(user_msg)
        today = datetime.now().strftime("%Y-%m-%d")
        self.memory.add_immediate(today, f"{{{{user}}}}: {user_msg}")
        self.memory.add_immediate(today, f"{self.cfg.agent_name}: {fallback}")
        self.memory.save()
        return fallback

    def _harvest_fragment(self, user_msg: str, reply_text: str) -> None:
        """从一轮对话里提炼一条“值得记住的观察”写入碎片仓（source=chat）。

        用便宜模型，失败或无可提炼时静默跳过，绝不影响主回复流程。
        """
        if self.llm is None:
            return
        try:
            client = self.llm.cheap_client()
            prompt = (
                "你是本角色（人设见你的身份设定）。刚才和你很熟的{{user}}聊了几句。\n"
                f"他说：{user_msg[:200]}\n"
                f"你回：{reply_text[:200]}\n\n"
                "如果这轮聊天里有值得你记住的‘观察’（比如{{user}}的近况/心事/习惯、你俩之间的小变化、"
                "值得以后提起的事），用一句话写出来（第一人称，像她自己记的）。\n"
                "如果没有值得记的（纯寒暄、打闹），只输出：无\n"
                "只输出这一句，不要解释不要引号。"
            )
            out = client.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=80,
                allow_reasoning_fallback=False,  # 碎片提炼绝不用思考过程当结果
            ).strip().strip('"\'“”‘’')
            # 过滤 LLM 思考过程泄漏：模型可能把 reasoning_content（思考过程）当结果返回，
            # 特征是含“判断/值得/这算是/是否值得”等元思考词，或明显超长。宁可漏记，不可存垃圾。
            noise_markers = ("我们只需要", "判断这轮", "值得记住", "值得记", "这算是", "可能值得", "是否值得")
            if (
                not out
                or out in ("无", "没有", "NONE", "None")
                or len(out) > 70
                or any(m in out for m in noise_markers)
            ):
                if out:
                    log.info("碎片提炼输出异常，丢弃: %s", out[:50])
                return
            self.memory.add_fragment(out, source="chat")
            log.info("对话碎片: %s", out[:50])
        except Exception as e:  # noqa: BLE001
            log.warning("碎片提炼失败(忽略): %s", e)

    @staticmethod
    def _rule_reply(user_msg: str) -> str:
        """无 LLM 时的兜底回复：简单口语化承接。

        LLM 故障（503/超时）时会走到这里，所以规则要尽量自然：
        先认寒暄/时间词，再认问句，最后才是通用接话，别再出现“牛头不对马嘴”。
        """
        msg = (user_msg or "").strip()
        if not msg:
            return "嗯？你咋了，说呗。"
        # 寒暄/睡觉/时间相关，先拦下来给合理回应
        if any(w in msg for w in ("睡了吗", "睡了没", "还没睡", "去睡", "晚安", "在吗", "在不", "在不在", "忙吗", "忙不")):
            if "睡" in msg:
                return "还没呢，怎么，大半夜找我，想我了？还是睡不着想找人唠嗑？"
            if "在" in msg:
                return "在呢在呢，刚看完个视频，咋了？"
            return "还行吧，你呢？"
        if msg.endswith(("?", "？", "吗", "么", "嘛", "呢")):
            return "嗯……这个我得想想，你等会儿哈，别催。"
        if len(msg) <= 4:
            return "嗯，我听着呢，继续说。"
        return f"哦，这样啊。行，我听着呢，你接着说。"

    def can_initiate(self, active_today: int, is_sleep_window: bool) -> bool:
        """护栏：不超过每日上限，且不在睡眠窗口。"""
        max_active = self.cfg.channel.get("max_active_per_day", 4)
        if active_today >= max_active:
            return False
        if is_sleep_window:
            return False
        return True
