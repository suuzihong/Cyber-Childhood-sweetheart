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


def _sleep_window_minutes(cfg: Config) -> tuple[int, int]:
    """睡眠窗口 [start, end)，单位分钟，支持跨夜（start > end）。

    注意：配置里的 casual_poke.window 是【非睡眠（可主动）】时段，
    默认 ['08:30', '00:30'] 意为 08:30 到次日 00:30 可主动。
    睡眠窗口是它的补集，即从 window 的 end 到 start：
      非睡眠 ['08:30','00:30']  →  睡眠 [00:30, 08:30)
    """
    window = cfg.schedule.get("casual_poke", {}).get("window") or []
    if len(window) >= 2:
        active_start_hm, active_end_hm = str(window[0]), str(window[1])
    else:
        active_start_hm, active_end_hm = cfg.schedule.get("wake_time", "08:30"), "00:30"
    sh, sm = active_end_hm.split(":")
    eh, em = active_start_hm.split(":")
    return int(sh) * 60 + int(sm), int(eh) * 60 + int(em)


def is_active_window(dt: datetime | None, cfg: Config) -> bool:
    """当前是否处于“可以主动”的时段（非睡眠窗口）。用于主动开口护栏。

    睡眠窗口 = [casual_poke.window 结束（默认 00:30）起，到 casual_poke.window
    开始（默认 08:30）止]，整段跨夜都算睡眠，不主动开口。

    注意：这里**不用 memory_closure**。memory_closure 是“做梦/收拢”的时间点
    （当前配置 00:30，与做饭同刻），拿它当睡眠起点会得到 [00:30, 08:30) 之外
    全时段，在 closure=00:30 时等价于全天判睡 → 闲补永远不触发。
    """
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    d = dt.astimezone(tz) if dt and dt.tzinfo else (dt.replace(tzinfo=tz) if dt else datetime.now(tz))
    cur = d.time().hour * 60 + d.time().minute
    window = cfg.schedule.get("casual_poke", {}).get("window") or []
    if len(window) >= 2:
        start_hm, end_hm = str(window[0]), str(window[1])
    else:
        # 没配窗口就退回 wake_time（避免再依赖 memory_closure）
        start_hm, end_hm = cfg.schedule.get("wake_time", "08:30"), "00:30"
    sh, sm = start_hm.split(":")
    eh, em = end_hm.split(":")
    start = int(sh) * 60 + int(sm)
    end = int(eh) * 60 + int(em)

    def _in(a: int, b: int, c: int) -> bool:
        """c 是否在 [a, b) 内，支持 a>b（跨夜窗口）。"""
        if a <= b:
            return a <= c < b
        return c >= a or c < b

    return _in(start, end, cur)


def sleep_overlap_minutes(
    start: datetime, end: datetime, cfg: Config
) -> float:
    """统计 [start, end) 里落在睡眠窗口内的分钟数。

    用于冷场时长的“有效计时”：她睡着的那段不该算作“你没理她”。
    支持跨夜窗口，也支持时间跨度跨越多天（逐日切片累加）。
    """
    tz = zoneinfo.ZoneInfo(cfg.timezone)

    def _to_tz(x: datetime) -> datetime:
        return x.astimezone(tz) if x.tzinfo else x.replace(tzinfo=tz)

    start = _to_tz(start)
    end = _to_tz(end)
    if end <= start:
        return 0.0

    s_min, e_min = _sleep_window_minutes(cfg)
    total = 0.0

    # 逐日切片：把跨天区间拆成 [当天0点, 次日0点)，逐段与睡眠窗口求交。
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        nxt = day + timedelta(days=1)
        # 该日的睡眠窗口（可能是跨夜：s_min > e_min 时切成两段）
        if s_min <= e_min:
            segments = [(day + timedelta(minutes=s_min), day + timedelta(minutes=e_min))]
        else:
            segments = [
                (day + timedelta(minutes=s_min), nxt),
                (day, day + timedelta(minutes=e_min)),
            ]
        for seg_start, seg_end in segments:
            lo = max(seg_start, start)
            hi = min(seg_end, end)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60.0
        day = nxt

    return total


def effective_elapsed_minutes(
    last: datetime | None, now: datetime | None, cfg: Config
) -> float:
    """冷场“有效时长”：墙上经过时间 - 期间落在睡眠窗口的时长。

    她睡着的时候感知不到你在不在，所以那段不该给情绪档位加分。
    例：21:00 回她、次日 12:00 再回，墙上 15h，睡眠窗口占 8h
    → 有效 7h，落在 probe 档，而不是 panic。
    """
    if last is None:
        return 0.0
    tz = zoneinfo.ZoneInfo(cfg.timezone)
    now = now.astimezone(tz) if now and now.tzinfo else ((now.replace(tzinfo=tz) if now else datetime.now(tz)))
    last = last.astimezone(tz) if last.tzinfo else last.replace(tzinfo=tz)
    if now <= last:
        return 0.0
    raw = (now - last).total_seconds() / 60.0
    return max(0.0, raw - sleep_overlap_minutes(last, now, cfg))
