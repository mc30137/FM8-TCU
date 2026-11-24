# -*- coding: utf-8 -*-
"""
界面偏好的存放：HUD 位置、HUD 不透明度。

和 `params.json` 分开：那个是**驾驶参数**（会影响换挡行为），
这个是**界面偏好**（只影响看起来怎么样）。混在一起的话，
以后想恢复默认参数会把窗口位置一起清掉。

文件放在 exe/项目根目录的 `ui.json`，写法和档案一样是原子写。
"""

from __future__ import annotations

import json
import os

from .service import ROOT

DEFAULT_PATH = os.path.join(ROOT, "ui.json")


def load(path: str = DEFAULT_PATH) -> dict:
    """读偏好。读坏了就当没有，绝不让程序因为一个显示设置起不来。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict, path: str = DEFAULT_PATH) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
