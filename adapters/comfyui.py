"""ComfyUI 画图/自拍适配器（本地 ComfyUI HTTP API）。

接法(Part E-3):
  POST {base}/prompt   提交工作流
  GET  {base}/history  轮询完成
  GET  {base}/view     取图
默认 base: http://127.0.0.1:8188
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from core.logger import get_logger
from layers.adapter import AdapterResult

log = get_logger("adapter.comfyui")


class ComfyUIAdapter:
    def __init__(
        self,
        base_url: str,
        daily_draw_limit: int = 3,
        daily_selftie_limit: int = 2,
        workflow_path: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.daily_draw_limit = daily_draw_limit
        self.daily_selftie_limit = daily_selftie_limit
        self.workflow_path = workflow_path
        self._default_prompt = "a beautiful detailed anime illustration, vibrant colors, masterpiece"

    def name(self) -> str:
        return "comfyui_draw"

    def daily_limit(self) -> int:
        return self.daily_draw_limit

    def _submit(self, workflow: dict[str, Any]) -> str:
        resp = requests.post(f"{self.base_url}/prompt", json={"prompt": workflow}, timeout=30)
        resp.raise_for_status()
        return resp.json()["prompt_id"]

    def _wait(self, prompt_id: str, timeout: float = 300) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = requests.get(f"{self.base_url}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            data = r.json()
            if prompt_id in data:
                return data[prompt_id]
            time.sleep(2)
        return None

    def _fetch_images(self, history: dict[str, Any]) -> list[str]:
        """从 history 里收集输出图片并保存到 data/media/。"""
        outputs = history.get("outputs", {})
        saved: list[str] = []
        media_dir = Path(__file__).resolve().parent.parent / "data" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        for node, out in outputs.items():
            for img in out.get("images", []):
                filename = img.get("filename")
                if not filename:
                    continue
                url = f"{self.base_url}/view?filename={filename}&subfolder={img.get('subfolder','')}&type={img.get('type','output')}"
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                dst = media_dir / filename
                dst.write_bytes(r.content)
                saved.append(str(dst))
                log.info("saved image: %s", dst)
        return saved

    def execute(self, ctx: dict[str, Any]) -> AdapterResult:
        """ctx: {workflow, caption, prompt, negative_prompt, seed, width, height, selftie, lora_name}。
        workflow 可以是预置文件路径或 dict；缺省用构造时传入的默认 workflow_path（普通画图）；
        lora_name 给定时向工作流注入 LoraLoaderModelOnly 节点。"""
        workflow = ctx.get("workflow") or self.workflow_path
        caption = ctx.get("caption", "")
        if isinstance(workflow, (str, Path)):
            wf = self._load_workflow(Path(workflow))
        else:
            wf = workflow
        if not wf:
            return AdapterResult(
                capability=self.name(), output=None, summary="没有可用的绘图工作流", shareable=False
            )
        # 填占位符：%prompt% / %negative_prompt% / %seed% / %width% / %height%
        import random

        seed = ctx.get("seed") or random.randint(1, 2**31)
        wf = self._fill_placeholders(
            wf,
            prompt=ctx.get("prompt", ""),
            negative_prompt=ctx.get(
                "negative_prompt", "lowres, bad anatomy, bad hands, text, watermark, deformed"
            ),
            seed=seed,
            width=ctx.get("width", 1024),
            height=ctx.get("height", 1024),
        )
        # 可选：注入 LoRA 节点（Anima 工作流的 UNETLoader 之后）
        lora = ctx.get("lora_name")
        if lora:
            wf = self._inject_lora(wf, lora, ctx.get("lora_strength", 0.9))
        prompt_id = self._submit(wf)
        hist = self._wait(prompt_id)
        images = self._fetch_images(hist) if hist else []
        if not images:
            return AdapterResult(
                capability=self.name(), output=None, summary="这次没画出来，画布空了", shareable=False
            )
        summary = caption or "画了张图"
        return AdapterResult(
            capability=self.name(),
            output=images,
            summary=summary,
            shareable=True,
            media=images,
        )

    @staticmethod
    def _load_workflow(p: Path) -> dict[str, Any]:
        import json

        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _fill_placeholders(wf: dict[str, Any], **values: Any) -> dict[str, Any]:
        """递归替换工作流里的 %name% 占位符。"""

        def walk(o: Any) -> Any:
            if isinstance(o, str):
                for k, v in values.items():
                    o = o.replace(f"%{k}%", str(v))
                return o
            if isinstance(o, dict):
                return {k: walk(v) for k, v in o.items()}
            if isinstance(o, list):
                return [walk(v) for v in o]
            return o

        return walk(wf)

    @staticmethod
    def _inject_lora(wf: dict[str, Any], lora_name: str, strength: float = 0.9) -> dict[str, Any]:
        """向 Anima 工作流注入 LoraLoaderModelOnly 节点：
        找到 UNETLoader 节点，在其后插 LoRA，把所有把 UNET 当 model 的引用改为经 LoRA。"""
        import copy

        wf = copy.deepcopy(wf)
        unet_id = None
        for nid, node in wf.items():
            if isinstance(node, dict) and node.get("class_type") == "UNETLoader":
                unet_id = nid
                break
        if unet_id is None:
            return wf  # 非 Anima 工作流，跳过
        lora_id = f"lora_{lora_name.replace('.', '_')}"
        wf[lora_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": lora_name,
                "strength_model": strength,
                "model": [unet_id, 0],
            },
            "_meta": {"title": "character Lora"},
        }
        # 把所有引用 [unet_id, 0] 作为 model 的地方改指 lora_id（保留 UNETLoader 自身输入）
        for nid, node in wf.items():
            if nid == lora_id:
                continue
            inputs = node.get("inputs", {}) if isinstance(node, dict) else {}
            for k, v in inputs.items():
                if v == [unet_id, 0]:
                    inputs[k] = [lora_id, 0]
        return wf
