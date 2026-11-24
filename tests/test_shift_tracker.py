# -*- coding: utf-8 -*-
"""
ShiftTracker 的自动化测试。

为什么这类测试值得写：
"发指令 → 等回执"这段逻辑有四个容易错的边界——换挡中(11)、档位没变、
超时、方向不符。这些边界在真实驾驶里都很难复现（总不能每次都刚好卡在
换挡瞬间按 F6），但用假数据可以精确构造。

运行：
    python tests/test_shift_tracker.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from manual_shift import ShiftTracker  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


def test_upshift_success() -> None:
    t = ShiftTracker()
    t.command(100.0, 3, +1, "升挡")
    check("升挡：换挡中(11)不算结果", t.on_frame(100.05, 11) is None)
    r = t.on_frame(100.072, 4)
    check("升挡：看到 4 挡判成功", r is not None and r[2] is True, str(r))
    check("升挡：延迟约 72ms", r is not None and 70 <= r[1] <= 74, str(r))
    check("升挡：结算后不再 busy", t.busy is False)


def test_wrong_direction() -> None:
    t = ShiftTracker()
    t.command(0.0, 3, +1, "升挡")
    r = t.on_frame(0.2, 2)          # 想让升挡，结果掉到 2 挡
    check("方向不符判失败", r is not None and r[2] is False, str(r))


def test_timeout() -> None:
    t = ShiftTracker()
    t.command(0.0, 3, +1, "升挡")
    check("未到超时不结算", t.tick(1.0) is None)
    r = t.tick(1.6)
    check("超过 1.5s 判超时", r is not None and r[2] is False, str(r))
    check("超时计入统计", t.stats.timeouts == 1)


def test_gear_unchanged_is_not_result() -> None:
    t = ShiftTracker()
    t.command(0.0, 3, +1, "升挡")
    check("档位没变不算结果", t.on_frame(0.3, 3) is None)
    check("仍然 busy", t.busy is True)


def test_busy_rejects_second_command() -> None:
    t = ShiftTracker()
    check("第一条指令登记成功", t.command(0.0, 3, +1, "升挡") is True)
    check("未完成时拒绝第二条", t.command(0.1, 3, +1, "升挡") is False)


def test_downshift_through_neutral() -> None:
    """实测里降挡会经过 11 且可能持续 150ms，中间帧必须被忽略。"""
    t = ShiftTracker()
    t.command(10.0, 4, -1, "降挡")
    for i, g in enumerate([11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11]):
        if t.on_frame(10.071 + i * 0.012, g) is not None:
            check("降挡：11 期间不应结算", False, f"第 {i} 帧误判")
            return
    r = t.on_frame(10.153, 3)
    check("降挡：11 期间不结算，看到 3 挡才结算",
          r is not None and r[2] is True and 140 <= r[1] <= 160, str(r))


def test_stats_summary() -> None:
    t = ShiftTracker()
    for i in range(3):
        t.command(i * 1.0, 3, +1, "升挡")
        t.on_frame(i * 1.0 + 0.07, 4)
    s = t.stats.summary()
    check("统计包含成功率", "3/3" in s, s)


def main() -> None:
    print("ShiftTracker 测试")
    print("-" * 60)
    for fn in (
        test_upshift_success,
        test_wrong_direction,
        test_timeout,
        test_gear_unchanged_is_not_result,
        test_busy_rejects_second_command,
        test_downshift_through_neutral,
        test_stats_summary,
    ):
        print(f"\n{fn.__doc__ or fn.__name__}")
        fn()
    print("-" * 60)
    print(f"通过 {PASS}   失败 {FAIL}")
    raise SystemExit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
