# -*- coding: utf-8 -*-
"""
用真实录制数据检验换挡规则（不需要开游戏）。

做两件事：

1. **统计你手动换挡时的转速点**——这是校准阈值的唯一依据。
   我们定 0.82~0.94 是拍的吗？不是，但对你这台车合不合适，只能看你实际怎么开。

2. **跑一遍"影子引擎"**——把录制数据喂给决策引擎，看它在每个时刻想做什么、
   被什么挡住。因为没有真的按键，档位始终来自录制，所以它只能"想"。

用法：
    python tools/replay_engine.py fm8_20261104_145105.fm8rec
"""

from __future__ import annotations

import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from fm8_tcu.engine import AutoShiftEngine  # noqa: E402
from fm8_tcu.protocol import gear_label, parse  # noqa: E402
from probe import read_records            # noqa: E402


def load(path: str):
    out = []
    last_ts = -1
    for rel_ms, raw in read_records(path):
        tele = parse(raw)
        if tele is None:
            continue
        ts = int(tele.get("timestamp_ms", 0))
        if ts == last_ts:          # 同帧重复包，丢掉（实测占 25%）
            continue
        last_ts = ts
        out.append((rel_ms / 1000.0, tele))
    return out


def analyse_manual_shifts(frames) -> None:
    """统计每次真实换挡发生时的转速与油门。"""
    print("=" * 74)
    print(" 一、你手动换挡时的转速点（校准阈值的依据）")
    print("=" * 74)

    events = []
    prev_gear = None
    prev = None
    for t, tele in frames:
        g = tele.gear
        if g == 11:
            if prev is not None:
                events.append((t, prev))       # 记录进入换挡前的状态
            prev_gear = g
            continue
        if prev_gear is not None and g != prev_gear and prev is not None:
            events.append((t, prev))
        if g != 11:
            prev_gear = g
        prev = tele

    ups = [(t, x) for t, x in events if x.gear < 10]
    print(f"\n 共捕捉到 {len(events)} 次换挡前一刻的状态\n")
    print("   时间(s)  前挡   转速占比   油门    车速")
    seen = set()
    for t, x in events:
        key = (round(t, 1), x.gear)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {t:7.1f}   {x.gear:>2}    {x.rpm_pct * 100:5.1f}%   "
              f"{x.throttle * 100:4.0f}%   {x.speed_kmh:6.1f}")

    high = [x for _, x in events if x.throttle > 0.8]
    if high:
        pts = sorted(x.rpm_pct for x in high)
        print(f"\n 全油门（>80%）换挡点：最低 {pts[0] * 100:.1f}%  "
              f"中位 {pts[len(pts) // 2] * 100:.1f}%  最高 {pts[-1] * 100:.1f}%")
        print(" → 我们的 RACE 曲线在满油门时定在 94%，"
              f"与你的习惯差 {94 - pts[len(pts) // 2] * 100:+.1f} 个百分点")


def human_shifts(frames):
    """人类实际换挡：进入 11 之前的最后一帧 + 换完后的新挡位。"""
    out = []
    i = 0
    n = len(frames)
    while i < n:
        if frames[i][1].gear == 11 and (i == 0 or frames[i - 1][1].gear != 11):
            j = i - 1
            while j >= 0 and frames[j][1].gear == 11:
                j -= 1
            k = i
            while k < n and frames[k][1].gear == 11:
                k += 1
            if j >= 0 and k < n:
                before = frames[j][1]
                after = frames[k][1]
                act = "UP" if after.gear > before.gear else "DOWN"
                out.append((frames[i][0], act, before, after.gear))
            i = k
        else:
            i += 1
    return out


def engine_intents(frames, gap_s: float = 0.6):
    """引擎想做什么。同向意图在 gap_s 内只记一次（否则每帧都会喊）。"""
    eng = AutoShiftEngine()
    out = []
    last_t, last_action = -99.0, ""
    for t, tele in frames:
        d = eng.preview(tele)
        if d.action and (d.action != last_action or t - last_t > gap_s):
            out.append((t, d.action, d.reason, tele))
            last_t, last_action = t, d.action
        elif not d.action:
            last_action = ""
    return out


def compare(frames) -> None:
    print()
    print("=" * 74)
    print(" 二、引擎 vs 你：同一时刻它会不会做出同样的决定")
    print("=" * 74)

    humans = human_shifts(frames)
    intents = engine_intents(frames)

    print(f"\n 你换了 {len(humans)} 次，引擎想换 {len(intents)} 次\n")
    print("   你的时刻   动作   引擎时刻   差值      引擎原因")
    matched = 0
    used = set()
    for t_h, act_h, before, after_gear in humans:
        best = None
        for idx, (t_e, act_e, reason, tele) in enumerate(intents):
            if idx in used or act_e != act_h:
                continue
            dt = t_h - t_e
            if -0.5 <= dt <= 1.5:
                if best is None or abs(dt) < abs(best[0]):
                    best = (dt, idx, t_e, reason)
        if best:
            used.add(best[1])
            matched += 1
            dt, _, t_e, reason = best
            print(f"   {t_h:7.2f}   {act_h:<5}  {t_e:7.2f}   {dt * 1000:+6.0f}ms   {reason}")
        else:
            print(f"   {t_h:7.2f}   {act_h:<5}      —         未匹配      "
                  f"（当时 {before.rpm_pct * 100:.0f}% / "
                  f"油门 {before.throttle * 100:.0f}% / 刹车 {before.brake * 100:.0f}%）")

    print(f"\n 匹配 {matched}/{len(humans)}")
    if intents:
        extra = len(intents) - len(used)
        print(f" 引擎多出来的意图：{extra} 次（这些是你没换、但它想换的时刻）")
        for idx, (t_e, act_e, reason, tele) in enumerate(intents):
            if idx in used:
                continue
            print(f"   {t_e:7.2f}   {act_e:<5}  {tele.gear:>2}挡  "
                  f"{tele.rpm_pct * 100:5.1f}%  thr={tele.throttle * 100:3.0f}%  "
                  f"brk={tele.brake * 100:3.0f}%   {reason}")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "fm8_20261104_145105.fm8rec"
    if not os.path.isabs(path) and not os.path.exists(path):
        path = os.path.join(ROOT, path)
    frames = load(path)
    if not frames:
        print("没有可用帧")
        return
    print(f"文件：{os.path.basename(path)}    {len(frames)} 帧   "
          f"{frames[-1][0]:.1f} 秒\n")
    analyse_manual_shifts(frames)
    compare(frames)


if __name__ == "__main__":
    main()
