"""配置加载与校验。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """配置错误。"""


@dataclass
class Config:
    """运行时配置。默认指向 config.example.json 的结构。"""

    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # ---- 便捷访问器 ----
    @property
    def agent_name(self) -> str:
        return self.raw.get("agent", {}).get("name", "角色")

    @property
    def timezone(self) -> str:
        return self.raw.get("agent", {}).get("timezone", "Asia/Shanghai")

    @property
    def schedule(self) -> dict[str, Any]:
        return self.raw.get("schedule", {})

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw.get("llm", {})

    @property
    def channel(self) -> dict[str, Any]:
        return self.raw.get("channel", {})

    @property
    def adapters(self) -> dict[str, Any]:
        return self.raw.get("adapters", {})

    @property
    def sillytavern(self) -> dict[str, Any]:
        return self.raw.get("sillytavern", {})

    # ---- 加载 ----
    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        cfg = cls(raw=raw, path=p)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """基本校验：必填项存在，值合理。"""
        sched = self.raw.get("schedule", {})
        if "dream_time" not in sched or "wake_time" not in sched:
            raise ConfigError("schedule 缺少 dream_time / wake_time")
        llm = self.raw.get("llm", {})
        if "main" not in llm:
            raise ConfigError("llm 缺少 main 配置")

    def resolve_api_key(self, section: str) -> str | None:
        """取 llm.<section> 的 API key。

        兼容两种写法:
        - api_key: 直接填 key（推荐, 本机私密配置文件）
        - api_key_env: 填环境变量名, 从 os.environ 取
        """
        conf = self.llm.get(section, {})
        direct = conf.get("api_key")
        if direct:
            return direct
        env_name = conf.get("api_key_env")
        if not env_name:
            return None
        return os.environ.get(env_name)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """加载配置；未指定时尝试常见默认路径。"""
    if path is None:
        here = Path(__file__).resolve().parent.parent
        candidates = [
            here / "config.json",
            here / "config.example.json",
        ]
        for c in candidates:
            if c.exists():
                return Config.load(c)
        raise ConfigError("未找到配置文件，请复制 config.example.json 为 config.json")
    return Config.load(path)
