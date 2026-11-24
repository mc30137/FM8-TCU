# -*- coding: utf-8 -*-
"""
命令行版自动换挡（RACE 模式）。

安全设计：
- **默认关闭**，必须按 F11 主动启用（fail-safe）。
- **遥测断流 300ms 立即停手**：断流恢复的第一帧不发键，且作废未确认指令。
- **F12 是硬急停**：退出并释放按键（`KeyboardSink.release_all`）。
- **换挡中(11) 不发、上一条没确认不发、冷却中不发**。
- **只监听 127.0.0.1、只接受本机来源**；发键前确认前台是游戏。
- **每帧决策都带理由**，并写进 CSV。

真正的逻辑全在 `service.py`，这里只负责画面板和读热键。
图形界面见 `gui.py`，两者行为完全一致。

用法：
    python -m fm8_tcu.app --port 5300
    python -m fm8_tcu.app --write-config ..\\params.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

from .config import load_policy, write_default
from .engine import Policy
from .outputs import (Hotkeys, KeyboardSink, SCANCODES,
                      VK_F10, VK_F11, VK_F12)
from .overlay import Overlay
from .protocol import gear_label
from .service import DEFAULT_PROFILE_DIR, ShiftService


def draw(lines: list[str], first: bool) -> None:
    out = ["\x1b[2J"] if first else ["\x1b[H"]
    for ln in lines:
        out.append(ln + "\x1b[K\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="FM8 自动换挡（RACE 模式）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。默认只收本机；要收局域网遥测用 0.0.0.0 + --allow-from")
    ap.add_argument("--allow-from", default="", metavar="IP[,IP]",
                    help="放行的来源 IP，逗号分隔。默认只收本机回环"
                         "——没有'一键全部放开'的开关")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--keys", default="num9:num7", help="升:降 键名，默认小键盘")
    ap.add_argument("--hold", type=int, default=60, help="按键保持毫秒")
    ap.add_argument("--clutch", action="store_true",
                    help="手动离合换挡（游戏里变速箱设成「手动带离合」时用）")
    ap.add_argument("--clutch-key", default="shift", metavar="KEY",
                    help="离合键名，默认 shift；要和游戏里的绑定一致")
    ap.add_argument("--clutch-hold", type=int, default=40, metavar="MS",
                    help="换挡时离合按住的毫秒数，默认 40")
    ap.add_argument("--no-focus-check", action="store_true",
                    help="不检查前台窗口，任何窗口下都发键（默认只往游戏发）")
    ap.add_argument("--log", default="", help="决策日志 CSV 路径")
    ap.add_argument("--autostart", action="store_true", help="启动即启用")
    ap.add_argument("--overlay", action="store_true", help="游戏内显示置顶浮窗")
    ap.add_argument("--pos", default="24,24", help="浮窗位置 x,y")
    ap.add_argument("--config", default="", help="参数文件 JSON")
    ap.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="车辆档案目录")
    ap.add_argument("--no-learn", action="store_true", help="关闭学习")
    ap.add_argument("--write-config", default="", help="导出默认参数后退出")
    args = ap.parse_args()

    if args.write_config:
        ok = write_default(args.write_config)
        print(f"已写入默认参数：{args.write_config}" if ok
              else f"写不了 {args.write_config}")
        return

    policy = Policy()
    if args.config:
        policy, notes = load_policy(args.config)
        for note in notes:
            print(f"[参数] {note}")

    try:
        up_name, down_name = [s.strip().lower() for s in args.keys.split(":")]
        up_scan, down_scan = SCANCODES[up_name], SCANCODES[down_name]
        clutch_scan = SCANCODES[args.clutch_key.strip().lower()]
    except (ValueError, KeyError):
        print(f"不认识的键名：{args.keys} / {args.clutch_key}")
        print("可用：" + ", ".join(sorted(SCANCODES)))
        raise SystemExit(1)

    sink = KeyboardSink(up_scan, down_scan, args.hold,
                        require_game=not args.no_focus_check,
                        clutch_scan=clutch_scan, clutch_hold_ms=args.clutch_hold,
                        use_clutch=args.clutch)
    svc = ShiftService(policy, sink=sink, learn=not args.no_learn,
                       profile_dir=args.profile_dir, log_path=args.log)
    try:
        svc.open(args.host, args.port, allow_from=args.allow_from)
    except RuntimeError as e:
        print(str(e))
        print("是不是还有另一个探针/遥测软件占着端口？")
        raise SystemExit(1)

    if args.autostart:
        svc.set_enabled(True)

    keys = Hotkeys()
    overlay = None
    if args.overlay:
        try:
            ox, oy = [int(v) for v in args.pos.split(",")]
            overlay = Overlay(x=ox, y=oy, rows=5)
            overlay.set_lines(["等待遥测…", "F11 启用/停用   F12 退出", "", "", ""])
        except Exception as e:
            print(f"[浮窗不可用] {e}")

    first_draw = True
    last_draw_at = 0.0
    errors = 0
    try:
        while True:
            now = time.monotonic()
            if keys.pressed(VK_F12):
                break
            if keys.pressed(VK_F11):
                svc.toggle()
            if keys.pressed(VK_F10):
                svc.set_clutch(not svc.snapshot()["clutch"])

            try:
                svc.poll()
            except Exception:
                # 一帧坏数据不该杀掉整个程序。第一次打完整栈，之后只计数。
                # 协议层已经拦了 NaN/越界，这里是最后一道网：
                # 万一还有没想到的路径，用户看到的是"出错 N 次"，不是闪退。
                errors += 1
                if errors == 1:
                    traceback.print_exc()

            if now - last_draw_at >= 0.1:
                last_draw_at = now
                snap = svc.snapshot()
                if snap["has_data"]:
                    gear_txt = (f"{gear_label(snap['gear']):>2}"
                                if snap["usable"] else " —")
                    status = (f"{gear_txt} 挡   "
                              f"{snap['rpm']:5.0f}/{snap['max_rpm']:.0f} "
                              f"({snap['rpm_pct'] * 100:4.1f}%)   "
                              f"{snap['speed']:6.1f} km/h   "
                              f"油门 {snap['throttle'] * 100:3.0f}%  "
                              f"刹车 {snap['brake'] * 100:3.0f}%")
                else:
                    status = "等待遥测…（确认游戏在赛道上、Data Out 已开）"

                draw([
                    "═" * 76,
                    " FM8 自动换挡（RACE 模式）",
                    "═" * 76,
                    f" 遥测   {'× 断流（已停手）' if snap['stale'] else '● 在线'}   "
                    f"{snap['frames']} 帧   {up_name}/{down_name}   "
                    f"自动: {'开' if snap['enabled'] else '关（只显示不按键）'}"
                    + (f"   坏包 {snap['bad_packets']}" if snap["bad_packets"] else "")
                    + (f"   来源拒绝 {snap['foreign_packets']}" if snap["foreign_packets"] else "")
                    + ("   手动离合" if snap["clutch"] else "")
                    + (f"   内部错误 {errors}" if errors else ""),
                    "─" * 76,
                    f" 状态   {status}",
                    f" 决策   {snap['decision']}",
                    f" 统计   {snap['stats']}",
                    f" 学习   {snap['learning']}",
                    "─" * 76,
                    " F11 启用/停用   F10 手动离合    F12 退出",
                    "═" * 76,
                ], first_draw)
                first_draw = False

                if overlay is not None:
                    try:
                        if not snap["has_data"]:
                            overlay.set_lines(["等待遥测…", "F11 启用/停用   F12 退出",
                                              "", "", ""])
                        else:
                            tag = "" if snap["enabled"] else "  [只显示]"
                            gear_txt = (f"{gear_label(snap['gear']):>2}"
                                        if snap["usable"] else " —")
                            overlay.set_lines([
                                f"{gear_txt} 挡   "
                                f"{snap['rpm']:5.0f} rpm   {snap['speed']:5.0f} km/h{tag}",
                                snap["decision"][:48],
                                snap["stats"][:48],
                                snap["learning"][:48],
                                "F11 启用/停用   F12 退出",
                            ], colors=["#ffffff",
                                       "#ff7b72" if snap["stale"] else "#7ee787",
                                       "#a0a0a0", "#6cb6ff", "#707070"])
                        overlay.tick(now)
                    except Exception:
                        overlay = None
    except KeyboardInterrupt:
        pass
    finally:
        if overlay is not None:
            overlay.close()
        svc.close()
        sys.stdout.write("\x1b[2J\x1b[H")
        print("─" * 76)
        print(" 本次结果")
        print("─" * 76)
        print(f" {svc.stats_text()}")
        for rec in svc.engine.history[-20:]:
            print(f"   {rec.action:<5} 从 {rec.from_gear} 挡  "
                  f"{'✓' if rec.ok else '✗'}  {rec.latency_ms:>4}ms  {rec.reason}")
        if svc.learn:
            print(f" {svc.vehicle.detail()}")
            if svc.profile_path():
                print(f" 档案：{svc.profile_path()}")
        if args.log:
            print(f"\n 日志：{os.path.abspath(args.log)}")


if __name__ == "__main__":
    main()
