"""自拍适配器：复用 ComfyUI，用自拍工作流。
形象锚点固定（黑长发低马尾、深棕眼、白背心），场景/情绪每次随机，镜头半身适中距离。
"""
from __future__ import annotations

import random

from core.logger import get_logger
from layers.adapter import AdapterResult
from adapters.comfyui import ComfyUIAdapter

log = get_logger("adapter.selftie")

# 通用形象锚点（默认占位；使用者可在 config.adapters.selftie.anchor 里写自己角色的形象，覆盖这里）
_ANCHOR = (
    "1girl, selfie photo, taken with the front camera of her own phone, "
    "first person POV, looking directly into the lens, "
    "medium shot, half body, moderate distance, upper body visible, "
    "modern anime style, soft lighting, high quality"
)

# 场景池（每次随机选一个，替换 %SCENE%）
_SCENES = [
    # 健身房（运动后）
    ("indoor gym, exercise equipment behind her, mirrors, sweaty after workout", "slightly out of breath, flushed cheeks, a few sweat drops"),
    # 天台/楼顶看风景
    ("rooftop terrace at dusk, city skyline behind, warm evening light, wind in hair", "breeze blowing loose strands, relaxed happy smile"),
    # 晨跑后的公园/街道
    ("morning park path after a jog, trees and soft morning sunlight behind", "light jogging glow, cheerful, holding a water bottle"),
    # 家里/房间日常
    ("cozy bedroom interior, soft daylight from window, casual at home", "relaxed lazy smile, just woke up, slightly sleepy eyes"),
    # 教室/自习室
    ("quiet classroom, desk with books behind, school setting", "studious but playful, pen in hand"),
    # 咖啡店/小店
    ("cozy cafe interior, warm lighting, counter and menu board behind", "casual chatty mood, holding a cup"),
]

# 情绪/神态微调池（随机追加）
_MOODS = [
    "playful wink, slight grin",
    "slightly shy smile, face a little red",
    "bright laughing, carefree",
    "cool calm look, one eyebrow raised",
    "genuine warm smile, soft eyes",
]

class SelftieAdapter:
    """包装 ComfyUIAdapter，用自拍工作流 + 随机场景。"""

    def __init__(self, comfy: ComfyUIAdapter, workflow_path: str | None = None):
        self.comfy = comfy
        self.workflow_path = workflow_path

    def name(self) -> str:
        return "selftie"

    def daily_limit(self) -> int:
        return self.comfy.daily_selftie_limit

    def execute(self, ctx: dict[str, Any]) -> AdapterResult:
        wf = self.workflow_path or ctx.get("workflow")
        if not wf:
            return AdapterResult(
                capability=self.name(),
                output=None,
                summary="（桩）还没配好自拍工作流",
                shareable=False,
            )

        # 场景随机：默认从池里挑，调用方可传 scene 指定
        scene, scene_mood = random.choice(_SCENES)
        mood = random.choice(_MOODS)
        prompt = (
            f"{_ANCHOR}, {scene}, {scene_mood}, {mood}, bokeh"
        )
        caption = ctx.get("caption") or "自拍一张"
        seed = ctx.get("seed") or random.randint(1, 2**31)

        log.info("[selftie] scene=%s mood=%s seed=%s", scene[:40], mood, seed)
        return self.comfy.execute(
            {
                "workflow": wf,
                "caption": caption,
                "prompt": prompt,
                "seed": seed,
            }
        )
