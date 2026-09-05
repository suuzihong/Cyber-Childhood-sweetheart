"""时间点判定：把冻结的每日时序变成可测试的时钟逻辑。"""
from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import Config


@dataclass
class TodaySlots:
    """一天的调度槽位。"""

    dream: bool = False          # 00:30 做梦层
    wake: bool = False           # 08:30 起床·行动层
    activity_morning: bool = False  # 09:00 自主活动①
    fragment_lunch: bool = False    # 12:30 午间碎片消化
    activity_afternoon: bool = False  # 14:00 自主活动②
    chat_touchpoints: list[bool] = None  # 18:30 / 20:00 / 21:30

    def __post_init__(self) -> None:
        if self.chat_touchpoints is None:
            self.chat_touchpoints = [False, False, False]

    @property
    def any_chat(self) -> bool:
        return any(self.chat_touchpoints)


class Clock:
    """根据当前时间 + 配置，判定该触发哪个槽位。"""

    def __init__(self, cfg: Config, now: datetime | None = None):
        self.cfg = cfg
        self._now = now
        self.tz = zoneinfo.ZoneInfo(cfg.timezone)

    def now(self) -> datetime:
        if self._now is not None:
            return self._now
        return datetime.now(self.tz)

    def _to_tz(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    @staticmethod
    def _parse(hm: str) -> time:
        h, m = hm.split(":")
        return time(int(h), int(m))

    def slots_at(self, dt: datetime | None = None) -> TodaySlots:
        """计算某一时刻（默认现在）应触发的槽位。"""
        dt = self._to_tz(dt or self.now())
        sched = self.cfg.schedule
        s = TodaySlots()
        cur = dt.time()

        # 做梦 00:30（窗口 00:30-01:00）
        s.dream = self._within(cur, time(0, 30), 60)
        s.wake = self._within(cur, self._parse(sched.get("wake_time", "08:30")), 30)
        s.activity_morning = self._within(cur, self._parse(sched.get("activity_morning", "09:00")), 30)
        s.fragment_lunch = self._within(cur, self._parse(sched.get("fragment_lunch", "12:30")), 30)
        s.activity_afternoon = self._within(cur, self._parse(sched.get("activity_afternoon", "14:00")), 30)

        for i, tp in enumerate(sched.get("chat_touchpoints", [])):
            if i < 3:
                s.chat_touchpoints[i] = self._within(cur, self._parse(tp), 30)
        return s

    @staticmethod
    def _within(cur: time, target: time, minutes: int) -> bool:
        """cur 是否落在 [target, target+minutes) 内（支持分钟进位）。"""
        t0 = target.minute + target.hour * 60
        c = cur.minute + cur.hour * 60
        return t0 <= c < t0 + minutes


def is_active_window(dt: datetime | None, cfg: Config) -> bool:
    """当前是否处于“可以主动”的时段（非睡眠窗口）。用于主动开口护栏。

    睡眠窗口 = [memory_closure(默认 23:30) 起，到 wake_time(默认 08:30) 止]，
    整段跨夜都算睡眠，不主动开口。
    """
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    d = dt.astimezone(tz) if dt and dt.tzinfo else (dt.replace(tzinfo=tz) if dt else datetime.now(tz))
    cur = d.time().hour * 60 + d.time().minute
    closure = cfg.schedule.get("memory_closure", "21:45")
    wake = cfg.schedule.get("wake_time", "08:30")
    ch, cm = closure.split(":")
    wh, wm = wake.split(":")
    close = int(ch) * 60 + int(cm)
    wake_min = int(wh) * 60 + int(wm)
    # 睡眠窗口：23:30(closure) 之后 到 次日 wake_time 之前，整段不主动
    in_sleep = cur >= close or cur < wake_min
    return not in_sleep
