# -*- coding: utf-8 -*-
"""
车辆档案存取：一台车一个 JSON，放在 profiles/ 下面。

几个刻意的决定：

- **原子写**。先写 `.tmp` 再 `os.replace`。否则程序在写一半时被 Ctrl+C 杀掉，
  档案就变成半个 JSON，下次启动直接解析失败——而这时候用户刚刚跑完两圈。
- **读失败不抛异常**。文件坏了就当没有，重新学。学得到的东西不值得让程序崩。
- **文件名清洗**。车号是数字，但万一以后加了别的键，不能让它带路径分隔符。
"""

from __future__ import annotations

import json
import os
import re

_SAFE = re.compile(r"[^0-9A-Za-z_.-]")


class ProfileStore:
    def __init__(self, directory: str = "profiles") -> None:
        self.dir = directory
        self.last_error = ""

    def path_for(self, key: str) -> str:
        return os.path.join(self.dir, _SAFE.sub("_", key) + ".json")

    def load(self, key: str) -> dict | None:
        path = self.path_for(key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            self.last_error = "档案格式不是对象"
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
        return None

    def save(self, key: str, data: dict) -> bool:
        path = self.path_for(key)
        tmp = path + ".tmp"
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except OSError as e:
            self.last_error = f"{type(e).__name__}: {e}"
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def keys(self) -> list[str]:
        try:
            return sorted(n[:-5] for n in os.listdir(self.dir) if n.endswith(".json"))
        except OSError:
            return []


def car_key(ordinal: int) -> str:
    """档案主键。用车的编号，不用车名——车名会随语言变，编号不会。"""
    return f"car{int(ordinal):05d}" if ordinal and ordinal > 0 else "unknown"
