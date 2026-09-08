"""角色调度器入口。

用 APScheduler 按冻结的每日时序挂槽位任务:
  00:30 做梦层  08:30 行动层  09:00/14:00 自主活动
  12:30 午间碎片  18:30/20:00/21:30 主动对话

运行:
  python main.py --config config.json
骨架阶段为"能跑、能点验槽位、能落盘"；LLM/适配器/QQ 可按配置逐项启用。
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import load_config
from core.clock import is_active_window
from core.logger import get_logger
from core.memory import MemoryStore
from core.llm import LLMFarm

from layers.adapter import AdapterRegistry
from layers.action import ActionLayer
from layers.dream import DreamLayer
from layers.dialogue import DialogueLayer

from adapters.comfyui import ComfyUIAdapter
from adapters.selftie import SelftieAdapter
from adapters.bilibili import BilibiliAdapter
from adapters.tieba import TiebaAdapter
from adapters.zhihu import ZhihuAdapter

log = get_logger("main")


class LinchengyuxiApp:
    """把各层接起来的应用壳。"""

    def __init__(self, config_path: str):
        self.cfg = load_config(config_path)
        self.rng = random.Random()

        # 记忆
        data_dir = Path(__file__).resolve().parent / "data"
        self.memory = MemoryStore(
            data_dir / "memory.json",
            fragment_ttl_days=self.cfg.schedule.get("fragment_ttl_days", 3),
            history_max=self.cfg.schedule.get("history_max", 20),
        )

        # LLM
        self.llm = LLMFarm(self.cfg)

        # 适配层注册表
        self.registry = AdapterRegistry()
        self._setup_adapters()

        # 层
        self.action = ActionLayer(self.memory, self.registry, self.llm)
        self.dream = DreamLayer(self.memory, self.cfg, self.llm)
        self.dialogue = DialogueLayer(self.memory, self.cfg, self.llm)

        # 运行时状态
        self.state_path = data_dir / "state.json"
        self.state: dict = self._load_state()

        # 通道（可空）
        self.channel = self._build_channel()

        # 被动回复缓冲
        self._pending: dict[str, list[str]] = {}
        self._pending_ts: dict[str, float] = {}
        self._pending_event = threading.Event()

    # ---------- 组装 ----------
    def _setup_adapters(self) -> None:
        ad = self.cfg.adapters
        comfy_cfg = ad.get("comfyui", {})
        comfy = ComfyUIAdapter(
            base_url=comfy_cfg.get("base_url", "http://127.0.0.1:8188"),
            daily_draw_limit=comfy_cfg.get("daily_draw_limit", 3),
            daily_selftie_limit=comfy_cfg.get("daily_selftie_limit", 2),
            workflow_path=comfy_cfg.get("workflow_draw"),
        )
        self.registry.register(comfy)
        wf = comfy_cfg.get("workflow_selftie")
        self.registry.register(SelftieAdapter(comfy, wf))
        if ad.get("bilibili", {}).get("enabled"):
            self.registry.register(
                BilibiliAdapter(
                    daily_deep_limit=ad["bilibili"].get("daily_deep_limit", 2),
                    cookie=ad["bilibili"].get("cookie"),
                    llm=self.llm,
                )
            )
        if ad.get("tieba", {}).get("enabled"):
            self.registry.register(TiebaAdapter(llm=self.llm))
        if ad.get("zhihu", {}).get("enabled"):
            self.registry.register(ZhihuAdapter(llm=self.llm))
        log.info("适配层已注册: %s", self.registry.names())

    def _build_channel(self):
        ch = self.cfg.channel
        if ch.get("type") != "onebot11":
            return None
        try:
            from channel.qq_onebot import OneBot11Client

            return OneBot11Client(
                ch.get("ws_url", ""),
                ch.get("targets", {}),
                access_token=ch.get("access_token"),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("通道初始化失败(跳过, 仅本地跑): %s", e)
            return None

    # ---------- 被动回复（{{user}}私聊她）----------
    def start_reply_loop(self) -> None:
        """启动接收+回复循环：她监听 QQ 私聊，收到就回（计入记忆，不计入主动上限）。"""
        if not self.channel:
            log.warning("无 QQ 通道，跳过回复循环")
            return
        try:
            self.channel.start_listen(self._on_private_msg)
        except Exception as e:  # noqa: BLE001
            log.error("启动接收监听失败: %s", e)
            return
        t = threading.Thread(target=self._reply_loop, daemon=True, name="reply-loop")
        t.start()
        log.info("被动回复循环已启动（{{user}}私聊她会回）")

    def _on_private_msg(self, data: dict) -> None:
        """监听线程回调：把用户私聊放进待回复缓冲，带时间戳供合并窗口使用。"""
        try:
            uid = str(data.get("user_id", ""))
            if not uid:
                return
            msg = self._extract_message_text(data)
            if not msg or not msg.strip():
                return
            self._pending.setdefault(uid, []).append(msg)
            self._pending[uid][:] = self._pending[uid][-10:]
            # 记录该用户最后一条消息到达时间（用于 15s 合并窗口）
            self._pending_ts[uid] = time.time()
            # 用户发消息即一次聊天互动，更新“最后一次收到用户回复”时间戳（冷场情绪升级的锚点）
            # 注意必须存 ISO 字符串：job_casual_poke 用 dt.fromisoformat 解析，存 float 会让间隔检查崩掉
            from datetime import datetime as _dt

            self.state["last_user_reply"] = _dt.now().isoformat()
            self.state["last_chat_ts"] = _dt.now().isoformat()  # 兼容旧逻辑/其他用途
            self._save_state()  # 落盘：重启后冷场计时不丢
            self._pending_event.set()
            log.info("[收到] %s: %s", uid, msg[:40])
        except Exception as e:  # noqa: BLE001
            log.error("解析私聊消息失败: %s", e)

    @staticmethod
    def _extract_message_text(data: dict) -> str:
        """从 OneBot 私聊事件里取文本（拼接 CQ 消息段）。

        引用回复（reply/quote 段）会把被引用原话带进上下文，
        让她明白用户是在针对她之前说的哪句话提问（新版 OneBot11
        的 reply 段带 text；旧版只有 id 时忽略，不破坏现有功能）。
        """
        msg = data.get("message")
        if isinstance(msg, str):
            return msg
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type", "")
                sdata = seg.get("data", {}) or {}
                if stype == "text":
                    parts.append(sdata.get("text", ""))
                elif stype in ("reply", "quote"):
                    quoted = (sdata.get("text") or "").strip()
                    if quoted:
                        parts.append(f"（你引用了我发的消息：\"{quoted}\"）")
            return "".join(parts)
        return ""

    def _reply_loop(self) -> None:
        """处理线程：按 15s 合并窗口攒批应答（连发合并成一条回），不计入主动上限。

        收到消息后不立即回，而是等该用户最后一条消息静默 15 秒（期间新消息
        不断续窗并入同批），窗口到期才生成回复并发送——模拟真人看完再打的节奏。
        """
        merge_window = 15.0  # 秒：同一用户连发消息的合并窗口
        poll = 0.5  # 秒：轮询间隔（远小于窗口，保证准时发）
        while True:
            try:
                self._pending_event.wait(timeout=poll)
                self._pending_event.clear()
            except Exception:  # noqa: BLE001
                pass
            if not self._pending:
                continue
            now = time.time()
            ready = [
                uid
                for uid in list(self._pending.keys())
                if now - self._pending_ts.get(uid, 0) >= merge_window
            ]
            for uid in ready:
                msgs = self._pending.pop(uid, [])
                self._pending_ts.pop(uid, None)
                if not msgs:
                    continue
                try:
                    self._reply_one(uid, msgs)
                except Exception as e:  # noqa: BLE001
                    log.error("[回复] %s 失败: %s", uid, e)

    def _reply_one(self, uid: str, msgs: list[str]) -> None:
        """对单个用户生成并发送回复；不检查每日主动上限（被动不受限）。

        发送前模拟真人打字时间（消息越长聊得越久，随机抖动更自然），
        也借这个窗口让连发消息能合并成一条回应。
        """
        self._roll_date_if_needed()
        lo = self.state.get("last_outgoing") or {}
        # 用户发了链接（B站/知乎/贴吧）→ 解析真实内容注入上下文，让她聊得有依据
        link_content = self._resolve_user_link(msgs)
        text = self.dialogue.build_reply(msgs[-1], msgs if len(msgs) > 1 else None, lo, link_content)
        self._simulate_typing(text)
        if self.channel:
            self._send_reply(uid, msgs, text, lo)

    def _resolve_user_link(self, msgs: list[str]) -> str | None:
        """从用户消息里找链接并解析真实内容；返回注入 prompt 的文本，无链接返回 None。

        解析成功：给标题/简介等真实内容；解析失败：明确标注“打不开”，
        让她老实承认没看过、请用户讲讲，而不是瞎编内容。
        """
        try:
            from core.linkparser import find_link, parse_link

            for msg in reversed(msgs):
                url = find_link(msg)
                if not url:
                    continue
                # B站视频链接：先深度解析（下载+抽帧+视觉看画面，慢 20-30s），失败回退元数据
                bvid = ""
                if "bilibili.com/video/" in url:
                    bvid = url.rstrip("/").split("/")[-1]
                if bvid:
                    deep = self._deep_analyze_bili(bvid)
                    if deep:
                        return deep
                    log.info("[链接] B站深度解析失败，回退元数据: %s", url)
                info = parse_link(url)
                if not info.get("ok"):
                    log.info("[链接] 打不开: %s", url)
                    return f"（{url} 这个链接我这边打不开/没加载出来，没看过内容）"
                lines = [info.get("title", "")]
                if info.get("summary"):
                    lines.append(info["summary"])
                text = "\n".join(x for x in lines if x)
                log.info("[链接] 已解析: %s -> %s", url, text[:60])
                return text
        except Exception as e:  # noqa: BLE001
            log.warning("链接解析异常，忽略: %s", e)
        return None

    def _deep_analyze_bili(self, bvid: str) -> str | None:
        """B站视频深度解析：复用 bilibili 适配器（下载+抽帧+视觉），失败返回 None。"""
        try:
            adapter = self.registry.get("bilibili_browse")
            if adapter is None or not hasattr(adapter, "analyze_video"):
                return None
            return adapter.analyze_video(bvid)
        except Exception as e:  # noqa: BLE001
            log.warning("B站深度解析异常: %s", e)
            return None

    def _send_reply(self, uid: str, msgs: list[str], text: str, lo: dict) -> None:
        """发送回复；若用户在回应她刚主动发的图/链接（“看看/发我/图呢”），把媒体/链接一并带上。"""
        want = self._wants_outgoing_media(msgs[-1], lo)
        # 只补发她自己的图（comfyui/selftie）：视频帧不算“她的图”，要图时走现画兜底
        media = [m for m in (lo.get("media") or []) if m] if self.state.get("last_media_origin") == "own" else []
        link = lo.get("link") or ""
        if want and media:
            self.channel.send_private_image(uid, media[0], text)
            log.info("[回复] 带图发送: %s", media[0])
            self.state["last_outgoing"]["media"] = []  # 图已给，避免下次重复发
            self._mark_media_sent()
            return
        if want and link:
            self.channel.send_private(uid, f"{text}\n{link}")
            log.info("[回复] 带链接发送: %s", link)
            self.state["last_outgoing"]["link"] = ""
            self._save_state()
            return
        if want and not media:
            # 兜底：她刚才说要发图/链接但手上没成品（口嗨了），用户真在要 → 现场画一张补发
            self._draw_on_demand(uid, text)
            return
        self.channel.send_private(uid, text)

    def _draw_on_demand(self, uid: str, text: str) -> None:
        """兜底：用户真在要图，但她手上没有成品 → 后台现画一张，画好直接补发。

        不阻塞回复线程：先回文字（让她有回应），画图异步完成后补发图片。
        """
        adapter = self.registry.get("comfyui_draw") if self.registry else None
        if adapter is None:
            self.channel.send_private(uid, text)
            return

        def _worker() -> None:
            try:
                result = adapter.execute({"slot": "on_demand"})
                media = list(getattr(result, "media", None) or [])
                if media and self.channel:
                    self.channel.send_private_image(uid, media[0], text)
                    log.info("[兜底] 现画现发: %s", media[0])
                    self.state.setdefault("last_outgoing", {})
                    self.state["last_outgoing"]["media"] = []
                    self._save_state()
                else:
                    log.warning("[兜底] 画图无产出，仅文本")
            except Exception as e:  # noqa: BLE001
                log.warning("[兜底] 现画失败: %s", e)

        import threading

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _wants_outgoing_media(user_msg: str, lo: dict) -> bool:
        """粗略判断用户是否在回应她刚主动分享的图/链接。"""
        if not lo or not lo.get("text"):
            return False
        # 她刚才的话里得是“给了东西/要给你看”的语境，用户再用要东西的词才算
        offer_words = ("要不要", "给你看", "发你", "看看", "图", "链接", "视频", "照片", "发个")
        if not any(w in lo["text"] for w in offer_words):
            return False
        intent_words = ("看看", "发我", "发来", "发个", "发图", "给我看", "图呢", "照片", "链接", "视频", "发过来", "发一下", "整一个", "发了吗", "真发", "没看到", "没收到", "没看见", "图在哪", "怎么没图")
        return any(w in user_msg for w in intent_words)

    def _escalation_plan(self, elapsed_min: float) -> tuple[str, int]:
        """由冷场时长(分钟)推导 (情绪档, 最小发送间隔分钟)。

        冷场越久，档位越高、间隔越短（融合版节奏）：
        - <3h     不发（间隔=180，即不到180分钟不触发）
        - 3~6h    concern / 每3h
        - 6~10h   probe / 每2h
        - 10~12h  pout / 每0.5h
        - >=12h   panic / 每10min
        """
        if elapsed_min < 200:
            return "idle", 180
        if elapsed_min < 360:
            return "concern", 180
        if elapsed_min < 600:
            return "probe", 120
        if elapsed_min < 720:
            return "pout", 30
        return "panic", 10

    def _has_today_media(self) -> bool:
        """今天是否真的产出过可分享的图/照片。

        只认 comfyui_draw/selftie 自己产出的图（origin=own）；
        bilibili 的视频帧不算"她的图"，否则她会误以为今天画过图。
        """
        if self.state.get("last_media_origin") != "own":
            return False
        import time as _time
        from pathlib import Path

        media = [m for m in (self.state.get("last_media") or []) if m]
        if not media:
            return False
        try:
            p = Path(media[0])
            if not p.exists():
                return False
            return p.stat().st_mtime >= _time.time() - 24 * 3600
        except Exception:
            return bool(media)

    def _media_already_sent(self) -> bool:
        """上次产出的图是否已在之前的主动消息里附图发过（避免重复发同一张图）。"""
        sent_ts = self.state.get("last_media_sent_ts")
        if not sent_ts:
            return False
        media = [m for m in (self.state.get("last_media") or []) if m]
        if not media:
            return False
        try:
            from datetime import datetime as _dt
            sent = _dt.fromisoformat(sent_ts).timestamp() if isinstance(sent_ts, str) else float(sent_ts)
            from pathlib import Path
            return Path(media[0]).stat().st_mtime <= sent
        except Exception:
            return False

    def _mark_media_sent(self) -> None:
        """记录当前 last_media 已附图发送过，防止后续闲补/触点重复发同一张图。"""
        from datetime import datetime as _dt
        self.state["last_media_sent_ts"] = _dt.now().isoformat()
        self._save_state()

    def _simulate_typing(self, text: str) -> None:
        """模拟打字耗时：按回复长度估秒数，加随机抖动，范围 2~6 秒。"""
        import time as _time
        if not text:
            return
        # 按字数估打字时长（约每秒 12 字），夹在 2~6 秒，加 ±1 秒抖动
        est = max(2.0, min(6.0, len(text) / 12.0))
        jitter = random.uniform(-1.0, 1.0)
        delay = max(1.5, est + jitter)
        _time.sleep(delay)

    # ---------- 状态 ----------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                # 清洗历史 bug 残留：last_chat_ts/last_active 曾误存 float（epoch 秒），
                # job_casual_poke 用 dt.fromisoformat 解析会崩。非字符串一律清掉。
                for key in ("last_chat_ts", "last_active"):
                    if not isinstance(state.get(key), str):
                        state[key] = ""
                return state
            except json.JSONDecodeError:
                pass
        return {"active_today": 0, "last_date": "", "day_events": [], "last_chat_ts": "", "task_by_cap": {}, "spoken_today": []}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _roll_date_if_needed(self) -> None:
        """跨日重置主动计数。"""
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.get("last_date") != today:
            self.state["last_date"] = today
            self.state["active_today"] = 0
            self.state["day_events"] = []
            self.state["last_active"] = ""
            self.state["last_chat_ts"] = ""
            self.state["last_outgoing"] = {}  # 昨天的“刚说过”作废
            # 昨天的任务清单作废（今天 8:30 行动层会重建；避免 wake 没跑时 activity 用旧任务）
            self.state["tasks"] = []
            self.state["task_by_cap"] = {}  # 昨天的任务映射作废
            self.state["spoken_today"] = []  # 昨天说过的作废

    # ---------- 槽位任务 ----------
    def job_dream(self) -> None:
        self._roll_date_if_needed()
        report = self.dream.dream(self.rng)
        self._save_state()
        log.info("[做梦] %s", report)

    def job_wake(self) -> None:
        self._roll_date_if_needed()
        # 醒来先检查：昨夜（或之前）是否已做梦沉淀。确认沉淀完成后，清除更早的即时记忆
        # （保留昨天的原文作为今早背景；只清前天及更早）。
        from datetime import timedelta
        from datetime import datetime as _dt
        yesterday = (_dt.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if self.memory.last_dream_date() >= yesterday:
            cleared = self.memory.clear_immediate_before(yesterday)
            if cleared:
                self.memory.save()
                log.info("[起床] 已确认做梦沉淀，清理 %d 天前的即时记忆", cleared)
        else:
            log.info("[起床] 最近做梦日期 %s < %s，保留即时记忆（待沉淀）", self.memory.last_dream_date() or "(无)", yesterday)
        plan = self.action.make_daily_plan(self.rng)
        self.state["day_events"] = [t.description for t in plan.tasks if t.capability]
        self.state["task_by_cap"] = {t.capability: t.description for t in plan.tasks if t.capability}
        self.state["tasks"] = [t.to_dict() for t in plan.tasks]
        self._save_state()
        log.info("[起床] 今日任务: %s", [t.description for t in plan.tasks])
        # 早安问候（保留固定问候，计入每日护栏）
        if self.channel and self.dialogue.can_initiate(self.state["active_today"], not is_active_window(None, self.cfg)):
            try:
                # 早安用固定问候语（不依赖 LLM/当天活动，避免空事件时生成“没啥特别的”式呆话）
                topic = "早啊，醒了没？新的一天，今天也得劲儿点。"
                self.state["active_today"] += 1
                self._save_state()
                self.channel.broadcast_private(topic)
                log.info("[早安] %s", topic)
            except Exception as e:  # noqa: BLE001
                log.error("[早安] 发送失败: %s", e)

    def job_casual_poke(self) -> None:
        """闲时补位 / 冷场情绪升级（融合版）。

        冷场时长锚点 = 用户最后一次真正回复（last_user_reply），她主动发多少条都不降档；
        只有用户回她才清零冷场。冷场越久，追得越急、越频繁：
        - <3h    不发
        - 3~6h   每 3h 一句（concern）
        - 6~10h  每 2h 一句（probe）
        - 10~12h 每 0.5h 一句（pout）
        - >=12h  每 10min 一句（panic）
        睡觉时间暂停；次日早起打招呼后按当前冷场档位继续。
        """
        self._roll_date_if_needed()
        poke_cfg = self.cfg.schedule.get("casual_poke", {})
        if not poke_cfg.get("enabled", True):
            return
        sleep_window = not is_active_window(None, self.cfg)
        if not self.dialogue.can_initiate(self.state["active_today"], sleep_window):
            return
        from datetime import datetime as dt
        # 冷场锚点：用户最后一次真正回复；没有则退回历史聊天时间（极端冷启动）
        last = self.state.get("last_user_reply") or self.state.get("last_chat_ts") or self.state.get("last_active")
        elapsed_min = 0.0
        got_last = False
        if last:
            try:
                elapsed_min = (dt.now() - dt.fromisoformat(last)).total_seconds() / 60
                got_last = True
            except Exception:
                pass
        # 由冷场时长推导“该不该发 / 间隔多久 / 什么情绪档”
        urgency, min_gap = self._escalation_plan(elapsed_min)
        if got_last and elapsed_min < min_gap:
            log.info("[闲补] 距用户回复仅 %dm，跳过(el=%s)", int(elapsed_min), urgency)
            return
        try:
            topic = self.dialogue.build_topic(
                self.state.get("day_events", []), 100,
                has_media=self._has_today_media() and not self._media_already_sent(),
                already_said="\n".join(self.state.get("spoken_today", [])),
                urgency=urgency,
            ) or "欸，闲得慌，跟你说个事"
        except Exception:
            topic = "欸，闲得慌，跟你说个事"
        self.state["active_today"] += 1
        self.state["last_active"] = dt.now().isoformat()
        self.state["last_chat_ts"] = dt.now().isoformat()  # 被动/主动都更新最近活动，但冷场锚点仍是 last_user_reply
        self.state["last_urgency"] = urgency  # 记录本次情绪等级（预留路线B：是否该打电话）
        self._save_state()
        if self.channel:
            try:
                # 闲补只带她自己画的图（own），视频帧不随闲聊补发；已附图发过的图不重复发
                has_media = self._has_today_media() and not self._media_already_sent()
                media = list(self.state.get("last_media") or []) if has_media else []
                link = self.state.get("last_link") or "" if has_media else ""
                self.channel.broadcast_private(topic, media or None)
                if media:
                    self._mark_media_sent()
                log.info("[闲补] %s", topic)
                self.state.setdefault("spoken_today", []).append(topic)
                # 记录最近一次主动说的话（只记 own 图，避免补图发成视频帧）
                self.state["last_outgoing"] = {
                    "text": topic,
                    "media": media,
                    "link": link,
                    "ts": dt.now().isoformat(),
                }
                # 她主动说的话也写进即时记忆（做梦层一并沉淀）
                self.memory.add_immediate(dt.now().strftime("%Y-%m-%d"), f"{self.cfg.agent_name}: {topic}")
                self.memory.save()
                self._save_state()
            except Exception as e:  # noqa: BLE001
                log.error("[闲补] 发送失败: %s", e)
        else:
            log.info("[闲补] (未接QQ) %s", topic)

    def job_activity(self, slot: str) -> None:
        self._roll_date_if_needed()
        # 优先执行今天计划里查到但还没做的能力任务（如自拍/画图），
        # 没有明确计划任务时再从注册表挑一个（避免随机捡到没有工作流的 comfyui_draw）。
        cap_name = self._pick_pending_adapter()
        if not cap_name:
            cap_name = self.registry.pick_for_slot(set(), self.rng)
        if not cap_name:
            log.info("[活动 %s] 没有可执行能力", slot)
            return
        adapter = self.registry.get(cap_name)
        try:
            # 排除上次分享过的 B 站视频（防重复刷同一个）
            exclude_bvid = ""
            _lk = self.state.get("last_link") or ""
            if "bilibili.com/video/" in _lk:
                exclude_bvid = _lk.rstrip("/").split("/")[-1]
            result = adapter.execute({"slot": slot, "exclude_bvid": exclude_bvid})
        except Exception as e:  # noqa: BLE001
            log.error("[活动 %s] %s 执行失败: %s", slot, cap_name, e)
            return
        # 标记该能力对应的计划任务已完成（避免早晚两次活动重复做同一件）
        for t in self.state.get("tasks", []):
            if t.get("capability") == cap_name:
                t["done"] = True
        # 空 summary 防护：没有产出描述时给个兜底文本，避免空串进 day_events/碎片/主动开口
        summary = (getattr(result, "summary", "") or "").strip()
        if not summary:
            summary = "刚忙完一件小事，感觉还行"
        # 清洗适配器描述：LLM 思考过程泄漏（超长英文元思考）→ 简短兜底，避免垃圾进 day_events/碎片/主动开口
        summary = self._clean_event_text(summary, cap_name)
        shareable = bool(result.shareable and summary)
        if shareable:
            self.state["day_events"].append(summary)
        # 对应计划任务已完成：从话题素材里移除，防止整天反复提同一件事
        done_desc = self.state.get("task_by_cap", {}).pop(cap_name, None)
        if done_desc and done_desc in self.state["day_events"]:
            self.state["day_events"].remove(done_desc)
            log.info("[活动 %s] 任务已完成，移出话题素材: %s", cap_name, done_desc[:30])
        # 暂存最近一次产出的图/链接（供后续她要图时补发）
        # origin: own=她画的/拍的图, video=转发视频的帧(不算她的图), 其他=""
        self.state["last_media"] = list(getattr(result, "media", None) or [])
        self.state["last_link"] = getattr(result, "link", "") or ""
        self.state["last_media_origin"] = (
            "own" if cap_name in ("comfyui_draw", "selftie")
            else "video" if cap_name == "bilibili_browse"
            else ""
        )
        # 行动层碎片：刷B站/贴吧/知乎/画图的“感受”写进碎片仓（供做梦层沉淀世界性格）
        if shareable:
            try:
                self.memory.add_fragment(f"今天{cap_name}：{summary}", source=cap_name)
                self.memory.save()
                log.info("[活动 %s] 碎片已记: %s", slot, summary[:40])
            except Exception as e:  # noqa: BLE001
                log.warning("[活动 %s] 记碎片失败: %s", slot, e)
        self._save_state()
        log.info("[活动 %s] %s → %s", slot, cap_name, summary)
        # 事件驱动主动开口：做完这件事，当场跟 {{user}} 说一句（不等到固定整点）。
        # 图片（自拍/画图）带图配文；纯文字事件（贴吧/知乎/B站观感）直接吐槽。
        if shareable and self.channel:
            self._speak_about(result, cap_name, summary)
        elif result.media:  # 有图但不可分享的文字事件（如自拍）仍要发图
            try:
                self.channel.broadcast_private(result.summary or "（%s分享）" % self.cfg.agent_name, result.media)
                self._mark_media_sent()
                log.info("[活动 %s] 已发送媒体 %d 张", slot, len(result.media))
            except Exception as e:  # noqa: BLE001
                log.error("[活动 %s] 发送媒体失败: %s", slot, e)

    @staticmethod
    def _clean_event_text(text: str, cap_name: str = "") -> str:
        """清洗适配器产出的描述：思考过程泄漏（超长英文元思考）替换为简短兜底。"""
        noise = ("roleplay", "reasoning", "analysis", "task repetition", "I need to output", "Let me make it", "user wants me")
        if len(text) > 120 or any(n in text.lower() for n in noise):
            return "刚刷到点有意思的东西，回头跟你细说" if cap_name else "刚忙完一件小事，感觉还行"
        return text

    def _speak_about(self, result: Any, cap_name: str, summary: str = "") -> None:
        """事件驱动：针对刚发生的这件事主动开口（带频率护栏，计入每日 8 次）。"""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        sleep_window = not is_active_window(None, self.cfg)
        if not self.dialogue.can_initiate(self.state["active_today"], sleep_window):
            log.info("[主动] 护栏已满/睡眠窗口，事件不开口")
            return
        # 空 summary 兜底：绝不让空串进主动开口
        if not summary:
            summary = (getattr(result, "summary", "") or "").strip() or "刚干完件小事，跟你吱一声"
        try:
            topic = self.dialogue.build_topic(
                [summary], 0,
                has_media=self._has_today_media(),
                already_said="\n".join(self.state.get("spoken_today", [])),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[主动] 生成失败，用原文: %s", e)
            topic = summary
        # 兜底：build_topic 失败回退仍可能返回空串（极端情况），再保一层
        if not topic or not topic.strip():
            topic = summary
        # 分享链接：刷到的视频/帖子直接附上，让她“甩链接给你”
        link = getattr(result, "link", "") or ""
        msg = f"{topic}\n{link}" if link else topic
        self.state["active_today"] += 1
        self.state["last_active"] = datetime.now().isoformat()
        self._save_state()
        try:
            self.channel.broadcast_private(msg, result.media or None)
            if result.media:
                self._mark_media_sent()
            log.info("[主动] 刚%s→开口: %s%s", cap_name, topic, f" 链接:{link}" if link else "")
            self.state.setdefault("spoken_today", []).append(topic)
            # 记录最近一次主动说的话（含媒体/链接），供被动回复注入上下文并补发图/链接
            self.state["last_outgoing"] = {
                "text": topic,
                "media": list(result.media or []),
                "link": link,
                "ts": datetime.now().isoformat(),
            }
            # 她主动说的话也写进即时记忆（做梦层会一并沉淀，她自己“记得”说过）
            self.memory.add_immediate(today, f"{self.cfg.agent_name}: {msg}")
            self.memory.save()
            self._save_state()
        except Exception as e:  # noqa: BLE001
            log.error("[主动] 发送失败: %s", e)

    def _pick_pending_adapter(self) -> str | None:
        """从今天计划 tasks 里找一个还带 capability 且今天还没做过的能力。

        优先级固定：自拍 > 画图（偏好出图发给你）。已用过的能力跳过。
        """
        used = {t.get("capability") for t in self.state.get("tasks", []) if t.get("done")}
        tasks = self.state.get("tasks", [])
        order = ["selftie", "comfyui_draw", "bilibili_browse", "tieba_browse", "zhihu_browse"]
        pending = {t.get("capability") for t in tasks if t.get("capability") and not t.get("done")}
        for cap in order:
            if cap in pending and cap not in used:
                return cap
        return None

    def job_fragment_lunch(self) -> None:
        self._roll_date_if_needed()
        # 午间: 碎片初归位(骨架只打日志)
        n = len(self.memory.list_fragments(pending_only=True))
        log.info("[午间] 待处理碎片 %d 条", n)

    def job_chat(self, touchpoint: int) -> None:
        self._roll_date_if_needed()
        sleep_window = not is_active_window(None, self.cfg)
        if not self.dialogue.can_initiate(self.state["active_today"], sleep_window):
            log.info("[对话 %d] 跳过(护栏)", touchpoint)
            return
        topic = self.dialogue.build_topic(
            self.state.get("day_events", []), touchpoint,
            has_media=self._has_today_media() and not self._media_already_sent(),
            already_said="\n".join(self.state.get("spoken_today", [])),
        )
        # 兜底：LLM 失败回退 day_events[0] 仍可能为空串（极端），绝不让空消息发出去
        if not topic or not topic.strip():
            topic = "欸，今天也没啥特别的，就是想跟你说句话。"
        self.state["active_today"] += 1
        from datetime import datetime
        self.state["last_active"] = datetime.now().isoformat()
        self._save_state()
        if self.channel:
            try:
                # 她说"画了图给你看"时，图要真的附上（media_rule 承诺的）；只附 own 图，已发过的不重复发
                has_media = self._has_today_media() and not self._media_already_sent()
                media = list(self.state.get("last_media") or []) if has_media else []
                link = self.state.get("last_link") or "" if has_media else ""
                self.channel.broadcast_private(topic, media or None)
                if media:
                    self._mark_media_sent()
                log.info("[对话 %d] %s", touchpoint, topic)
                self.state.setdefault("spoken_today", []).append(topic)
                # 记录最近一次主动说的话 + 写即时记忆（她自己说过的话要记得）
                self.state["last_outgoing"] = {
                    "text": topic,
                    "media": media,
                    "link": link,
                    "ts": datetime.now().isoformat(),
                }
                self.memory.add_immediate(datetime.now().strftime("%Y-%m-%d"), f"{self.cfg.agent_name}: {topic}")
                self.memory.save()
                self._save_state()
            except Exception as e:  # noqa: BLE001
                log.error("[对话 %d] 发送失败: %s", touchpoint, e)
        else:
            log.info("[对话 %d] (未接QQ, 仅预览) %s", touchpoint, topic)


def build_scheduler(app: LinchengyuxiApp) -> BlockingScheduler:
    sched = BlockingScheduler(timezone=app.cfg.timezone)
    s = app.cfg.schedule
    # 错过触发后允许在宽限期内补跑（避免 LLM 偶发慢导致任务 miss 后直接放弃）
    MISFIRE = 300  # 秒，5 分钟宽限

    def hm(key: str) -> str:
        return s.get(key, "00:00")

    sched.add_job(app.job_dream, CronTrigger(hour=0, minute=30), id="dream", misfire_grace_time=MISFIRE)
    sched.add_job(app.job_wake, CronTrigger(hour=8, minute=30), id="wake", misfire_grace_time=MISFIRE)
    sched.add_job(app.job_activity, CronTrigger(hour=9, minute=0), args=["morning"], id="act_morning", misfire_grace_time=MISFIRE)
    sched.add_job(app.job_fragment_lunch, CronTrigger(hour=12, minute=30), id="frag_lunch", misfire_grace_time=MISFIRE)
    sched.add_job(app.job_activity, CronTrigger(hour=14, minute=0), args=["afternoon"], id="act_afternoon", misfire_grace_time=MISFIRE)
    # 固定晚间收尾（21:30，一天一句"今天过得怎样"）
    for i, tp in enumerate(s.get("chat_touchpoints", [])):
        h, m = tp.split(":")
        sched.add_job(app.job_chat, CronTrigger(hour=int(h), minute=int(m)), args=[i], id=f"chat_{i}", misfire_grace_time=MISFIRE)
    # 闲时补位：持续检查（每 15 分钟），距上次聊天≥min_gap 分钟且不在睡眠窗口时主动开口
    # 具体是否开口由 job_casual_poke 内部判断（睡眠窗口/每日上限/间隔），这里只负责高频轮询
    poke_cfg = s.get("casual_poke", {})
    if poke_cfg.get("enabled", True):
        poke_minutes = poke_cfg.get("check_interval_minutes", 15) or 15
        sched.add_job(
            app.job_casual_poke,
            IntervalTrigger(minutes=poke_minutes),
            id="poke_check",
            misfire_grace_time=MISFIRE,
            coalesce=True,
        )
    return sched


def main() -> None:
    ap = argparse.ArgumentParser(description="角色调度器")
    ap.add_argument("--config", default=None, help="config.json 路径")
    ap.add_argument("--run", action="store_true", help="真正运行调度器(默认 dry-run 自检)")
    args = ap.parse_args()

    app = LinchengyuxiApp(args.config) if args.config else None
    # 无 --config 时用默认加载（自动找 config.example.json）
    if app is None:
        cfg = load_config()
        app = LinchengyuxiApp(str(cfg.path))

    # 无 --run 时只做 dry-run 自检后退出
    if not args.run:
        log.info("dry-run 自检: 适配层=%s, 时区=%s, 槽位已挂载", app.registry.names(), app.cfg.timezone)
        for touchpoint_idx in range(3):
            topic = app.dialogue.build_topic(["试机: 今天天气不错"], touchpoint_idx)
            log.info("对话试样[%d]: %s", touchpoint_idx, topic)
        return

    sched = build_scheduler(app)
    log.info("调度器启动: %s", app.cfg.timezone)
    # 被动入口：启动监听/回复循环（不阻塞，后台 daemon 线程）
    app.start_reply_loop()
    sched.start()


if __name__ == "__main__":
    main()
