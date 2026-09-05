"""链接解析：{{user}} 发来 B站/知乎/贴吧链接时，提取可聊的真实内容。

设计目标：她跟 {{user}} 聊链接内容时必须有真实依据——
- B站：官方 view API 拿标题/UP主/分区/简介（稳定，1~2s）
- 知乎/贴吧：官方接口反爬强，尽力抓 <title>；抓不到就明确返回 ok=False，
  让上层 prompt 引导她"承认打不开、请 {{user}} 讲讲"，而不是瞎编内容。

用法:
    from core.linkparser import find_link, parse_link
    url = find_link(user_msg)          # 从消息里抠出第一个链接
    info = parse_link(url)             # {platform, ok, title, summary, ...}
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from typing import Any

from core.logger import get_logger

log = get_logger("core.linkparser")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# ---- 链接识别 ----
BILI_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", re.I)
BILI_AV_RE = re.compile(r"bilibili\.com/video/av(\d+)", re.I)
BILI_SHORT_RE = re.compile(r"b23\.tv/([0-9A-Za-z]+)", re.I)
ZHIHU_RE = re.compile(r"zhihu\.com/(?:question|answer|p)/(\d+)", re.I)
TIEBA_RE = re.compile(r"tieba\.baidu\.com/p/(\d+)", re.I)

_URL_RE = re.compile(r"https?://[^\s，。；！？、\"'<>]+")


def find_link(text: str) -> str | None:
    """从消息文本里抠出第一个 http(s) 链接；没有返回 None。"""
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:!?)]}）】") if m else None


# ---- B站 ----
def _bili_view(bvid: str | None = None, aid: str | None = None) -> dict[str, str] | None:
    try:
        params = {"bvid": bvid} if bvid else {"aid": aid}
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"https://api.bilibili.com/x/web-interface/view?{q}",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "replace")).get("data") or {}
        if not data.get("title"):
            return None
        owner = (data.get("owner") or {}).get("name", "")
        return {
            "platform": "bilibili",
            "ok": True,
            "title": str(data.get("title", "")).strip(),
            "summary": f"UP主：{owner} · 分区：{data.get('tname', '')}\n简介：{(data.get('desc') or '(无)').strip()[:300]}",
            "url": f"https://www.bilibili.com/video/{data.get('bvid', bvid or '')}",
        }
    except Exception as e:  # noqa: BLE001
        log.warning("bili view 失败: %s", e)
        return None


def _resolve_short(url: str) -> str:
    """b23.tv 短链跟随重定向拿真实地址（尽力而为）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.geturl()
    except Exception:  # noqa: BLE001
        return url


def _parse_bili(url: str) -> dict[str, Any]:
    m = BILI_RE.search(url) or BILI_AV_RE.search(url)
    if m:
        g = m.group(1)
        info = _bili_view(bvid=g) if g.startswith("BV") else _bili_view(aid=g)
        if info:
            return info
    if BILI_SHORT_RE.search(url):
        real = _resolve_short(url)
        m2 = BILI_RE.search(real) or BILI_AV_RE.search(real)
        if m2:
            g2 = m2.group(1)
            info = _bili_view(bvid=g2) if g2.startswith("BV") else _bili_view(aid=g2)
            if info:
                return info
    return {"platform": "bilibili", "ok": False, "title": "", "summary": ""}


# ---- 知乎/贴吧：尽力抓 <title> ----
def _fetch_title(url: str) -> str:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S)
        if not m:
            return ""
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return html.unescape(t)[:120]
    except Exception as e:  # noqa: BLE001
        log.warning("抓 title 失败 %s: %s", url, e)
        return ""


def _parse_zhihu(url: str) -> dict[str, Any]:
    title = _fetch_title(url)
    if title and title != "知乎" and "知乎" in title:
        return {"platform": "zhihu", "ok": True, "title": title, "summary": "", "url": url}
    return {"platform": "zhihu", "ok": False, "title": "", "summary": ""}


def _parse_tieba(url: str) -> dict[str, Any]:
    title = _fetch_title(url)
    if title and "百度贴吧" in title or (title and "贴吧" in title):
        return {"platform": "tieba", "ok": True, "title": title, "summary": "", "url": url}
    return {"platform": "tieba", "ok": False, "title": "", "summary": ""}


# ---- 入口 ----
def parse_link(url: str) -> dict[str, Any]:
    """解析一个链接，返回 {platform, ok, title, summary, url, ...}。

    ok=True 时 title/summary 是真实抓到的内容，可直接注入回复上下文；
    ok=False 时上层应引导她"承认打不开、请 {{user}} 讲讲"，绝不编内容。
    """
    if not url:
        return {"platform": "unknown", "ok": False, "title": "", "summary": ""}
    try:
        if "bilibili.com" in url or "b23.tv" in url:
            return _parse_bili(url)
        if "zhihu.com" in url:
            return _parse_zhihu(url)
        if "tieba.baidu.com" in url:
            return _parse_tieba(url)
    except Exception as e:  # noqa: BLE001
        log.warning("链接解析失败 %s: %s", url, e)
    return {"platform": "unknown", "ok": False, "title": "", "summary": ""}
