"""刷贴吧适配器（Part E-2 · tieba_browse）真实实现。

贴吧网页/API 对本机反爬强（403/签名），走搜狗搜索拿真实帖子标题+摘要，
LLM 以角色视角生成观感 → 碎片仓「今日印象」（不上浮性格层）。
"""
from __future__ import annotations

from typing import Any

from core.logger import get_logger
from core.sogou import resolve_link, sogou_search
from layers.adapter import AdapterResult

log = get_logger("adapter.tieba")


class TiebaAdapter:
    def __init__(self, llm: Any = None):
        self.llm = llm

    def name(self) -> str:
        return "tieba_browse"

    def daily_limit(self) -> int:
        return 1

    def _pick(self, topic: str) -> list[dict[str, str]]:
        """搜贴吧帖子，优先含'贴吧'的搜索结果。"""
        q = f"贴吧 {topic}" if topic else "贴吧 沙雕 日常"
        results = sogou_search(q)
        # 过滤出像帖子的（有摘要内容）
        posts = [r for r in results if r.get("snippet") and r.get("title")]
        return posts[:4]

    def _summarize(self, posts: list[dict[str, str]], topic: str) -> str:
        """LLM 生成角色视角的观感；失败回退。"""
        if self.llm and posts:
            try:
                client = self.llm.cheap_client()
                lines = "\n".join(
                    f"- 《{p['title'][:60]}》 {p['snippet'][:120]}" for p in posts
                )
                prompt = (
                    "你是本角色（人设见你的身份设定），跟{{user}}很熟。"
                    "你刚在贴吧刷到几个帖子，用一句你的风格的话说说感想（像刷到瓜随手发消息），"
                    "可以吐槽可以吃瓜可以来兴趣，60字内，不要分析过程，不要英文。\n\n"
                    f"刷到的帖子：\n{lines}"
                )
                text = client.chat(
                    [{"role": "user", "content": prompt}], temperature=0.9, max_tokens=200
                ).strip().strip('"\'“”‘’')
                if text and len(text) <= 140:
                    return text
            except Exception as e:  # noqa: BLE001
                log.warning("贴吧 LLM 观感失败，回退: %s", e)
        if posts:
            return f"刷贴吧看到「{posts[0]['title'][:30]}」，有点意思"
        return "刷了会儿贴吧，没啥有意思的"

    def execute(self, ctx: dict[str, Any]) -> AdapterResult:
        topic = ctx.get("topic", "") or ""
        posts = self._pick(topic)
        summary = self._summarize(posts, topic)
        link = resolve_link(posts[0]["url"]) if posts and posts[0].get("url") else ""
        log.info("[贴吧] topic=%s 帖子=%d -> %s", topic, len(posts), summary)
        return AdapterResult(
            capability=self.name(),
            output=posts,
            summary=summary,
            to_world_view=False,
            shareable=True,
            link=link,
        )
