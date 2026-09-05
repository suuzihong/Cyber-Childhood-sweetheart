"""搜狗搜索工具：从本机直连搜狗网页版，提取搜索结果（标题/链接/摘要）。

贴吧/知乎官方网页与 API 对本机均有强反爬（403 / 签名 / 风控），
但搜狗网页搜索可从本机直连（200），且能返回具体帖子的标题 + 内容摘要，
是这两个平台适配器可用的真实内容通道。

用法:
    results = sogou_search("贴吧 猫咪 求助")
    # -> [{"title": ..., "url": ..., "snippet": ...}, ...]
"""
from __future__ import annotations

import html
import random
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from core.logger import get_logger

log = get_logger("core.sogou")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
]

MAX_RESULTS = 8


def _fetch(query: str, timeout: int = 15) -> str:
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    headers = {
        "User-Agent": random.choice(UA_POOL),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _extract_results(body: str) -> list[dict[str, str]]:
    """从搜狗结果页提取 (标题, 链接, 摘要)。"""
    results: list[dict[str, str]] = []
    # 搜狗结果块: <h3 ...><a href="...">标题</a></h3> ... <p>摘要</p>
    items = re.findall(
        r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?</h3>(.*?)(?=<h3|$)', body, re.S
    )
    for href, title_html, rest in items:
        title = _clean(title_html)
        if not title:
            continue
        # 摘要：优先 <p> 块，其次 class="text-layout" div
        snip = ""
        ps = re.findall(r"<p[^>]*>(.*?)</p>", rest, re.S)
        if ps:
            snip = _clean(ps[0])
        if not snip:
            tl = re.search(r'class="text-layout"[^>]*>(.*?)</div>', rest, re.S)
            if tl:
                snip = _clean(tl.group(1))
        url = html.unescape(href)
        results.append({"title": title, "url": url, "snippet": snip})
        if len(results) >= MAX_RESULTS:
            break
    return results


def sogou_search(query: str, timeout: int = 15) -> list[dict[str, str]]:
    """搜狗搜索，返回 [{title,url,snippet}]，失败返回 []。"""
    try:
        body = _fetch(query, timeout=timeout)
        results = _extract_results(body)
        log.info("sogou 搜索 %r -> %d 条", query, len(results))
        return results
    except Exception as e:  # noqa: BLE001
        log.warning("sogou 搜索失败 %r: %s", query, e)
        return []


def resolve_link(url: str) -> str:
    """跟随搜狗 /link 重定向到真实 URL（尽力而为，失败原样返回）。"""
    if not url.startswith("/link?") and "sogou.com/link" not in url:
        return url
    full = url if url.startswith("http") else "https://www.sogou.com" + url
    try:
        req = urllib.request.Request(full, headers={"User-Agent": UA_POOL[0]})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
        return html.unescape(loc) if loc else url
    except Exception:  # noqa: BLE001
        return url
