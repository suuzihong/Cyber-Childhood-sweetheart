# -*- coding: utf-8 -*-
"""把 user.md / history.md 导入记忆仓（user_prefs / user_history）。

用法（在 scheduler 目录下）:
    python import_memory.py            # 导入 data/user.md 和 data/history.md
    python import_memory.py --user data/user.md --history data/history.md

规则:
- user.md    每行「键：值」（支持中文冒号/英文冒号/等号），导入 user_prefs
- history.md 每行一条经历，导入 user_history（追加，不覆盖已有）
- 导入成功后会备份原 memory.json

设计初衷：别人拿到懒人包，填好三个 md 文件后跑一次即可，不用手改 JSON。
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEM_PATH = HERE / "data" / "memory.json"


def _parse_kv(line: str) -> tuple[str, str] | None:
    for sep in ("：", ":", "="):
        if sep in line:
            k, v = line.split(sep, 1)
            k, v = k.strip(), v.strip()
            if k:
                return k, v
    return None


def import_user(md_path: Path, mem: dict) -> int:
    if not md_path.exists():
        return 0
    prefs = mem.setdefault("user_prefs", {})
    n = 0
    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kv = _parse_kv(line)
        if kv:
            prefs[kv[0]] = kv[1]
            n += 1
    return n


def import_history(md_path: Path, mem: dict) -> int:
    if not md_path.exists():
        return 0
    hist = mem.setdefault("user_history", [])
    existing = set(hist)
    n = 0
    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in existing:
            hist.append(line)
            existing.add(line)
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="导入 user.md / history.md 到记忆仓")
    ap.add_argument("--user", default=str(HERE / "data" / "user.md"))
    ap.add_argument("--history", default=str(HERE / "data" / "history.md"))
    args = ap.parse_args()

    if not MEM_PATH.exists():
        from core.memory import MemoryStore  # noqa: PLC0415

        ms = MemoryStore(MEM_PATH)
        ms.save()
    mem = json.loads(MEM_PATH.read_text(encoding="utf-8"))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(MEM_PATH, str(MEM_PATH) + ".bak_%s" % stamp)

    n_u = import_user(Path(args.user), mem)
    n_h = import_history(Path(args.history), mem)
    if n_u or n_h:
        MEM_PATH.write_text(
            json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("导入完成: user_prefs +%d 条, user_history +%d 条" % (n_u, n_h))
        print("备份: %s.bak_%s" % (MEM_PATH, stamp))
    else:
        print("没有可导入的内容（检查 %s / %s 是否存在且有内容）" % (args.user, args.history))


if __name__ == "__main__":
    main()
