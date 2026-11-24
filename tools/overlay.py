# -*- coding: utf-8 -*-
"""
浮窗自检（不需要游戏、不需要遥测）。

真正的实现现在在 src/fm8_tcu/overlay.py，这里只负责"显示 8 秒看看有没有"。

    python tools/overlay.py --test
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from fm8_tcu.overlay import Overlay  # noqa: E402


def _selftest() -> None:
    print("=" * 66)
    print(" 浮窗自检 —— 请看着屏幕左上角")
    print("=" * 66)
    print()
    print(" 第 1 段（8 秒）：无边框黑框，四行白字")
    print(" 第 2 段（8 秒）：带标题栏的窗口，标题是 FM8 TCU")
    print()

    def run(borderless: bool, title: str, seconds: float) -> None:
        print(f"--- 正在显示：{title} ---")
        try:
            ov = Overlay(x=60, y=60, borderless=borderless)
        except Exception as e:
            print(f"    创建失败：{e}")
            return
        ov.set_lines([title, "如果看到这个窗口，请记下它的样子",
                      "8 秒后自动切换 / 结束", f"无边框模式 = {borderless}"])
        t0 = time.time()
        nxt = 0.0
        while time.time() - t0 < seconds:
            now = time.time()
            ov.tick(now)
            if now - nxt > 2.0:
                nxt = now
                d = ov.diag()
                print(f"    状态: 可见={d.get('visible')} "
                      f"位置尺寸={d.get('rect')} 被遮挡={d.get('covered')}")
            time.sleep(0.03)
        ov.close()
        print()

    run(True, "测试 A：无边框浮窗", 8)
    run(False, "测试 B：带标题栏窗口", 8)
    print("=" * 66)
    print(" 请告诉开发：A 看到了吗？B 看到了吗？")
    print("=" * 66)


if __name__ == "__main__":
    _selftest()
