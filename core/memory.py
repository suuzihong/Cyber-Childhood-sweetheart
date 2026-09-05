"""记忆三仓：近期聊天 / 碎片 / 表格总结。

对应 Part C 设计：
- recent_chat  : 浅仓, 会话滚动摘要 + 本周要事 + 下次话题
- fragments    : 中浅仓, 未确认毛坯(标来源), TTL 后清
- 表格仓       : rel_user / world_views / user_prefs / user_schedule / user_history

持久化为一个 JSON 文件 (data/memory.json)。骨架阶段不依赖向量库。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class MemoryStore:
    def __init__(self, path: str | Path, fragment_ttl_days: int = 3, history_max: int = 20):
        self.path = Path(path)
        self.fragment_ttl_days = fragment_ttl_days
        self.history_max = history_max
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        self._migrate_legacy()

    # ---------- 迁移 ----------
    def _migrate_legacy(self) -> None:
        """历史档案迁移：旧版人设中她读体育大学，现改为数字媒体艺术（数媒/设计类）。

        运行中的旧进程可能把旧 notes 写回磁盘，这里每次启动都修正一次并落盘，保证一致。
        """
        changed = False
        # rel_user.notes："读体育大学/读体育相关的大学" → 数媒/设计
        notes = self._data.get("rel_user", {}).get("notes", "")
        if "体育大学" in notes or "体育相关" in notes:
            notes = notes.replace("读体育大学", "读数字媒体艺术（数媒/设计类）大学")
            notes = notes.replace("读体育相关的大学", "读数字媒体艺术（数媒/设计类）大学")
            notes = notes.replace("体育大学", "数字媒体艺术（数媒/设计类）大学")
            self._data["rel_user"]["notes"] = notes
            changed = True
        # user_history：防童年记忆里残留"体育"专业表述（只改专业相关，不动运动爱好）
        for i, h in enumerate(self._data.get("user_history", [])):
            if "体育大学" in h or "体育相关" in h:
                self._data["user_history"][i] = (
                    h.replace("体育大学", "数字媒体艺术（数媒/设计类）大学")
                    .replace("体育相关的大学", "数字媒体艺术（数媒/设计类）大学")
                )
                changed = True
        if changed:
            self.save()
            import logging

            logging.getLogger("memory").info("记忆迁移：角色专业设定已更新")

    # ---------- 底层 ----------
    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return self._empty()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "recent_chat": {"last_summary": "", "weekly_items": [], "next_topic": ""},
            "fragments": [],
            "rel_user": {"trust": 50, "notes": ""},
            "world_views": [],
            "user_prefs": {},
            "user_schedule": {},
            "user_history": [],
            # 即时记忆仓：按天存的当天对话原文（未沉淀，留给做梦层总结/第二天背景）
            "immediate": {},
            # 长期记忆仓：每日摘要（做梦层 LLM 总结当天即时记忆后写入）
            "daily_summaries": {},
            # 最近一次做梦完成的日期（醒来层据此决定是否可清即时记忆）
            "last_dream_date": "",
        }

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ---------- 近期聊天仓 ----------
    def set_recent_summary(self, summary: str) -> None:
        self._data["recent_chat"]["last_summary"] = summary

    def add_weekly_item(self, item: str, max_items: int = 3) -> None:
        items = self._data["recent_chat"]["weekly_items"]
        if item not in items:
            items.insert(0, item)
        self._data["recent_chat"]["weekly_items"] = items[:max_items]

    def set_next_topic(self, topic: str) -> None:
        self._data["recent_chat"]["next_topic"] = topic

    # ---------- 碎片仓 ----------
    def add_fragment(self, text: str, source: str, confirmed: bool = False) -> None:
        """加一条碎片。confirmed=True 表示已经过验证可直接当事实。

        碎片仓有容量上限（默认保留最近 30 条未确认），防爆炸；确认过的永不自动清。
        """
        now = datetime.now().isoformat(timespec="seconds")
        self._data["fragments"].append(
            {
                "text": text,
                "source": source,
                "created": now,
                "confirmed": confirmed,
            }
        )
        # 上限保护：未确认碎片超 30 条时，丢最旧的
        pending = [f for f in self._data["fragments"] if not f.get("confirmed")]
        if len(pending) > 30:
            over = len(pending) - 30
            removed = 0
            kept = []
            for f in self._data["fragments"]:
                if not f.get("confirmed") and removed < over:
                    removed += 1
                    continue
                kept.append(f)
            self._data["fragments"] = kept

    def list_fragments(self, pending_only: bool = True) -> list[dict[str, Any]]:
        frags = self._data["fragments"]
        if pending_only:
            frags = [f for f in frags if not f.get("confirmed")]
        return list(frags)

    def prune_fragments(self, now: datetime | None = None) -> int:
        """删除超过 TTL 且未确认的碎片。返回删除条数。"""
        now = now or datetime.now()
        kept = []
        removed = 0
        for f in self._data["fragments"]:
            try:
                created = datetime.fromisoformat(f.get("created") or "")
            except ValueError:
                continue  # 日期坏掉的碎片不处理，交给 TTL 外的逻辑
            if not f.get("confirmed") and (now - created) > timedelta(days=self.fragment_ttl_days):
                removed += 1
                continue
            kept.append(f)
        self._data["fragments"] = kept
        return removed

    def confirm_fragment(self, index: int) -> dict[str, Any] | None:
        """把碎片标记为已确认(可迁入表格仓)。按未确认列表序。"""
        pending = self.list_fragments(pending_only=True)
        if not (0 <= index < len(pending)):
            return None
        target = pending[index]
        return self.confirm_fragment_by_text(target.get("text", ""))

    def confirm_fragment_by_text(self, text: str) -> dict[str, Any] | None:
        """按原文把碎片标记为已确认。

        用 text 匹配而非索引：_migrate 里循环确认多条时，前面的确认会改变
        “未确认列表”的长度，索引会错位导致后面的漏确认。
        """
        if not text:
            return None
        for f in self._data["fragments"]:
            if f.get("text") == text and not f.get("confirmed"):
                f["confirmed"] = True
                return f
        return None

    # ---------- 表格仓 ----------
    def world_views(self) -> list[dict[str, Any]]:
        return list(self._data["world_views"])

    def append_world_view(self, text: str, source: str, date: str | None = None) -> None:
        """世界性格 append（Part B: 只加不改）。"""
        self._data["world_views"].append(
            {"text": text, "source": source, "date": date or datetime.now().strftime("%Y-%m-%d")}
        )

    def set_rel_note(self, note: str) -> None:
        self._data["rel_user"]["notes"] = note

    def set_pref(self, key: str, value: Any) -> None:
        self._data["user_prefs"][key] = value

    def add_history(self, entry: str) -> None:
        """黑历史/共同记忆表, 超上限顶掉最不珍贵的(末尾)。"""
        hist = self._data["user_history"]
        if entry not in hist:
            hist.insert(0, entry)
        if len(hist) > self.history_max:
            del hist[self.history_max:]

    # ---------- 即时记忆仓（当天对话原文，未沉淀）----------
    def add_immediate(self, date: str, entry: str) -> None:
        """把当天一句对话写进即时记忆仓（按天分组）。不落盘，由调用方决定何时 save。"""
        day = self._data.setdefault("immediate", {}).setdefault(date, [])
        day.append(entry)
        # 单日上限，防止聊天过密撑爆（默认保留最近 100 句）
        if len(day) > 100:
            del day[: len(day) - 100]

    def immediate_for(self, date: str) -> list[str]:
        """取某天的即时记忆原文。"""
        return list(self._data.get("immediate", {}).get(date, []))

    def recent_immediate(self, limit: int = 20) -> list[str]:
        """取最近几天（按日期倒序）的即时记忆，供第二天早上当背景。"""
        days = sorted(self._data.get("immediate", {}).keys(), reverse=True)
        out: list[str] = []
        for d in days:
            lines = self._data["immediate"][d]
            if len(out) + len(lines) > limit:
                out.extend(lines[: limit - len(out)])
                break
            out.extend(lines)
        return out

    def clear_immediate_before(self, date: str) -> int:
        """清除指定日期之前（不含当天）的即时记忆。返回清除的天数。"""
        im = self._data.setdefault("immediate", {})
        stale = [d for d in im if d < date]
        for d in stale:
            del im[d]
        return len(stale)

    # ---------- 长期记忆仓（每日摘要）----------
    def set_daily_summary(self, date: str, summary: str) -> None:
        """写入某天的长期摘要（做梦层总结产物）。"""
        self._data.setdefault("daily_summaries", {})[date] = summary

    def daily_summary_for(self, date: str) -> str:
        return self._data.get("daily_summaries", {}).get(date, "")

    def recent_summaries(self, days: int = 3) -> list[str]:
        """取最近 N 天的长期摘要（新→旧）。"""
        ds = self._data.get("daily_summaries", {})
        return [ds[d] for d in sorted(ds.keys(), reverse=True)[:days] if ds[d]]

    def set_last_dream_date(self, date: str) -> None:
        self._data["last_dream_date"] = date

    def last_dream_date(self) -> str:
        return self._data.get("last_dream_date", "")
