"""轻量日志。所有 logger 统一输出到 stdout（配置在 root logger，各 logger 继承）。"""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def get_logger(name: str = "linchengyuxi") -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        # 配置 root logger 一次：所有命名 logger 通过 propagate 继承同一个 handler。
        # 这样无论哪个模块先调用，任何 logger 都能输出（修掉原来"只给第一个 logger 加 handler
        # 导致 main 等 logger 静默"的 bug）。
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        _CONFIGURED = True
    logger = logging.getLogger(name)
    # 命名 logger 默认 propagate=True 到 root，无需各自加 handler。
    return logger