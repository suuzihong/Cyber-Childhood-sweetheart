"""刷 B 站真实适配器（Part E-2 · bilibili_browse）。

流程：挑视频 → 下载视频流(m4s, 带 cookie) → PyAV 抽 3 帧 → 视觉模型看图+简介 → 观后总结。
产出走碎片仓「今日印象」，不上浮性格层（防刷个视频就改三观）。
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from core.logger import get_logger
from layers.adapter import AdapterResult

log = get_logger("adapter.bilibili")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
API = "https://api.bilibili.com"


class BilibiliAdapter:
    def __init__(self, daily_deep_limit: int = 2, cookie: str | None = None, llm: Any = None):
        self.daily_deep_limit = daily_deep_limit
        self.cookie = cookie or ""
        self.llm = llm
        self.tmp_dir = Path(tempfile.gettempdir()) / "lcyx_bili"

    def name(self) -> str:
        return "bilibili_browse"

    def daily_limit(self) -> int:
        return self.daily_deep_limit

    # ---------- 底层 ----------
    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _get(self, url: str, timeout: int = 15) -> dict:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _pick_video(self, topic: str) -> dict | None:
        """挑一个视频：有主题用搜索，否则用热门榜。"""
        try:
            if topic:
                q = urllib.parse.quote(topic)
                r = self._get(f"{API}/x/web-interface/search/type?search_type=video&keyword={q}")
                res = r.get("data", {}).get("result") or []
                if res:
                    return {"bvid": res[0]["bvid"], "title": res[0]["title"], "description": res[0].get("description", "")}
            r = self._get(f"{API}/x/web-interface/ranking/v2?rid=0&type=all")
            lst = r.get("data", {}).get("list") or []
            if lst:
                it = lst[0]
                return {"bvid": it["bvid"], "title": it["title"], "description": it.get("desc", "")}
        except Exception as e:
            log.warning("bili 挑视频失败: %s", e)
        return None

    def _get_video_meta(self, bvid: str) -> dict | None:
        try:
            v = self._get(f"{API}/x/web-interface/view?bvid={bvid}")
            d = v.get("data")
            if not d:
                return None
            return {
                "bvid": bvid,
                "cid": d["cid"],
                "title": d["title"],
                "description": (d.get("desc") or "").strip(),
                "pic": d.get("pic", ""),
                "duration": d.get("duration", 0),
                "owner": (d.get("owner") or {}).get("name", ""),
                "tname": d.get("tname", ""),
            }
        except Exception as e:
            log.warning("bili 视频信息失败 %s: %s", bvid, e)
            return None

    def _get_stream(self, meta: dict) -> str | None:
        try:
            p = self._get(
                f"{API}/x/player/playurl?bvid={meta['bvid']}&cid={meta['cid']}&qn=32&fnval=16"
            )
            vids = (p.get("data", {}).get("dash") or {}).get("video") or []
            if not vids:
                return None
            vids.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            return vids[0].get("baseUrl") or vids[0].get("base_url")
        except Exception as e:
            log.warning("bili 拿流失败: %s", e)
            return None

    def _download_stream(self, url: str, bvid: str) -> str | None:
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            dst = self.tmp_dir / f"{bvid}.m4s"
            req = urllib.request.Request(url, headers=self._headers())
            with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            return str(dst)
        except Exception as e:
            log.warning("bili 下载失败: %s", e)
            return None

    def _extract_frames(self, path: str, bvid: str, n: int = 3) -> list[str]:
        """PyAV 抽 n 帧为 jpg（自带 ffmpeg 库，无需外部 ffmpeg）。"""
        try:
            import av

        except Exception:
            return []
        try:
            self.tmp_dir.mkdir(parents=True, exist_ok=True)
            container = av.open(path)
            stream = container.streams.video[0]
            total = stream.frames or 0
            targets = [int(total * f) for f in (0.15, 0.5, 0.85)][:n] if total > 0 else [10, 40, 80][:n]
            frames: list[str] = []
            i = 0
            for frame in container.decode(stream):
                if i in targets and len(frames) < n:
                    fp = self.tmp_dir / f"{bvid}_f{i}.jpg"
                    frame.to_image().save(fp, "JPEG", quality=85)
                    frames.append(str(fp))
                i += 1
                if len(frames) >= n:
                    break
            container.close()
            return frames
        except Exception as e:
            log.warning("bili 抽帧失败: %s", e)
            return []

    # ---------- 观后总结 ----------
    def _summarize(self, meta: dict, frames: list[str]) -> str:
        """视觉模型看帧 + 读简介，生成观后总结。失败回退文字版。"""
        if self.llm is None:
            return self._fallback_summary(meta)
        try:
            vision = self.llm.vision_client()
            content: list[dict] = [{
                "type": "text",
                "text": (
                    f"你是本角色，正在B站刷视频。看下面几帧画面和简介，直接输出一句你刷到这个视频后想跟{{{{user}}}}说的话。\n"
                    f"标题：{meta['title']}\nUP主：{meta.get('owner','')}\n分区：{meta.get('tname','')}\n"
                    f"简介：{meta.get('description','(无)') or '(无)'}\n\n"
                    "直接给一句中文（60字内），就像真刷到视频随手评价，不要复述任务、不要分析过程、不要英文。可以吐槽可以来兴趣。"
                ),
            }]
            for fp in frames:
                b64 = base64.b64encode(open(fp, "rb").read()).decode()
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            text = vision.chat([{"role": "user", "content": content}], max_tokens=400).strip().strip('"\'“”‘’')
            return text or self._fallback_summary(meta)
        except Exception as e:
            log.warning("bili 视觉总结失败，回退文字版: %s", e)
            return self._fallback_summary(meta)

    @staticmethod
    def _fallback_summary(meta: dict) -> str:
        return f"今天刷到B站一个视频《{meta['title']}》（{meta.get('owner','')}发的），看着挺有意思"

    # ---------- 入口 ----------
    def execute(self, ctx: dict[str, Any]) -> AdapterResult:
        topic = (ctx.get("topic") or "").strip()
        pick = self._pick_video(topic)
        if not pick:
            return AdapterResult(
                capability=self.name(), output=None, summary="（B站这会儿没刷到想看的）", shareable=False
            )
        meta = self._get_video_meta(pick["bvid"])
        if not meta:
            return AdapterResult(capability=self.name(), output=None, summary="（B站视频没加载出来）", shareable=False)

        t0 = time.time()
        stream_url = self._get_stream(meta)
        frames: list[str] = []
        if stream_url:
            local = self._download_stream(stream_url, meta["bvid"])
            if local:
                frames = self._extract_frames(local, meta["bvid"])
        summary = self._summarize(meta, frames)
        log.info("[bilibili] 看完《%s》 耗%.1fs 帧%d", meta["title"], time.time() - t0, len(frames))
        return AdapterResult(
            capability=self.name(),
            output=meta,
            summary=summary,
            to_world_view=False,
            shareable=True,
            media=frames[:1],
            link=f"https://www.bilibili.com/video/{meta['bvid']}",
        )