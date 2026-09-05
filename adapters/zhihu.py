"""刷知乎适配器（Part E-2 · zhihu_browse）真实实现。

知乎网页/API 对本机反爬强（403/风控），走搜狗搜索拿真实问题+回答摘要，
LLM 提炼成一条"新看法/处事看法" → 允许上浮世界性格层（闸门2通道）。
"""
from __future__ import annotations

from typing import Any

from core.logger import get_logger
from core.sogou import resolve_link, sogou_search
from layers.adapter import AdapterResult

log = get_logger("adapter.zhihu")


class ZhihuAdapter:
    def __init__(self, llm: Any = None):
        self.llm = llm

    def name(self) -> str:
        return "zhihu_browse"

    def daily_limit(self) -> int:
        return 1

    def _pick(self, topic: str) -> list[dict[str, str]]:
        """搜知乎问题/回答，优先含'知乎'且像讨论的。"""
        q = f"知乎 {topic}" if topic else "知乎 人生 建议"
        results = sogou_search(q)
        posts = []
        for r in results:
            t = r.get("title", "")
            if ("知乎" in t or "?" in t or "？" in t) and r.get("snippet"):
                posts.append(r)
            if len(posts) >= 4:
                break
        return posts

    def _summarize(self, posts: list[dict[str, str]]) -> str:
        """LLM 提炼成一条角色认同/想讨论的'看法'。"""
        if self.llm and posts:
            try:
                client = self.llm.cheap_client()
                lines = "\n".join(
                    f"- 《{p['title'][:60]}》 摘要: {p['snippet'][:130]}" for p in posts
                )
                prompt = (
                    "你是本角色（人设见你的身份设定），有自己的主见。"
                    "你在知乎刷到几个问题/回答，从里面提炼一条你认同或想跟{{user}}争论的"
                    "'新看法/处世观点'（一句话，60字内，口语化，有你的态度，别像说教）。\n\n"
                    f"刷到的内容：\n{lines}"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=0.9, max_tokens=200
                ).strip().strip('"\'“”‘’')
                if text and len(text) <= 140:
                    return text
            except Exception as e:  # noqa: BLE001
                log.warning("知乎 LLM 提炼失败，回退: %s", e)
        if posts:
            return f"看知乎「{posts[0]['title'][:30]}」有点想法"
        return "刷了会儿知乎，没啥有营养的"

    def execute(self, ctx: dict[str, Any]) -> AdapterResult:
        topic = ctx.get("topic", "") or ""
        posts = self._pick(topic)
        summary = self._summarize(posts)
        link = resolve_link(posts[0]["url"]) if posts and posts[0].get("url") else ""
        log.info("[知乎] topic=%s 问答=%d -> %s", topic, len(posts), summary)
        return AdapterResult(
            capability=self.name(),
            output=posts,
            summary=summary,
            to_world_view=True,  # 知乎产出可上浮世界性格层（闸门2通道）
            shareable=True,
            link=link,
        )
