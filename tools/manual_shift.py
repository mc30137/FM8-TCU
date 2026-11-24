#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 3：手动换挡测试器
=====================

目的只有一个：**证明我们能把手动换挡指令送进 FM8，并测出它有多快多可靠。**

为什么先做手动而不是自动：自动换挡一旦出错，你很难判断是"判断错了"还是
"指令没发进去"。手动模式把这两个问题拆开——你按键，它发指令，它报告结果。
只要手动模式 20 次全中，就证明"通道"是通的，后面才谈"策略"。

三个关键设计：

1. **零依赖**：按键检测用 GetAsyncKeyState，注入用 SendInput，都是 Windows 自带。
2. **每条指令都有回执**：发出去后盯着遥测，等档位真的变了才记"成功"，
   并记下从按键到档位变化的毫秒数。这就是真实换挡延迟。
3. **遥测断了就不发**：超过 1 秒没有有效数据，直接拒绝发送并提示。
   盲目发指令是我们明确要避免的行为。

用法：
    python tools/manual_shift.py                  # 默认：小键盘 9 升 / 7 降
    python tools/manual_shift.py --keys eq        # 改成 E / Q
    python tools/manual_shift.py --dry-run        # 只显示不发送，先验证按键

按键：
    F6 = 升挡      F5 = 降挡
    F7 = 切换「演练 / 实发」
    F12 = 退出
"""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import sys
import threading
import time
from ctypes import wintypes
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import Screen, build_layouts, gear_short, parse_frame  # noqa: E402

try:
    import winsound
    WINSOUND_OK = True
except Exception:                       # 非 Windows 或精简环境
    WINSOUND_OK = False

# ══════════════════════════════════════════════════════════════════════════
# 第 1 部分：Windows 输入接口
#
# 两个函数：
#   GetAsyncKeyState —— 读某个键当前是否被按住（不管窗口有没有焦点）
#   SendInput        —— 把按键事件塞进系统输入流（游戏看到的就是"真的按了"）
#
# 注入用的是**扫描码**而不是虚拟键码。原因是游戏多半走 Raw Input / DirectInput，
# 它们认的是键盘硬件扫描码；只发虚拟键码有时会被忽略。
# ══════════════════════════════════════════════════════════════════════════

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

VK_F5, VK_F6, VK_F7, VK_F12 = 0x74, 0x75, 0x76, 0x7B
VK_NUMLOCK = 0x90

# 常用键的扫描码（Set 1）。名字 → 扫描码
SCANCODES = {
    "num0": 0x52, "num1": 0x4F, "num2": 0x50, "num3": 0x51, "num4": 0x4B,
    "num5": 0x4C, "num6": 0x4D, "num7": 0x47, "num8": 0x48, "num9": 0x49,
    "q": 0x10, "e": 0x12, "r": 0x13, "f": 0x21, "g": 0x22,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "[": 0x1A, "]": 0x1B, ",": 0x33, ".": 0x34, "/": 0x35,
    "shift": 0x2A, "ctrl": 0x1D, "space": 0x39,
}


def key_down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def numlock_on() -> bool:
    """NumLock 的**开关状态**。注意不能用 GetAsyncKeyState —— 那个只反映按键是否按住。"""
    return bool(user32.GetKeyState(VK_NUMLOCK) & 0x0001)


# ══════════════════════════════════════════════════════════════════════════
# 声音反馈
#
# 为什么要声音：看屏幕就得切窗口，切窗口游戏就暂停。
# 所以"成功/失败"必须用耳朵就能分辨，眼睛留在赛道上。
# ══════════════════════════════════════════════════════════════════════════

_TONES = {
    "up": (1320, 55),      # 升挡成功：高、短
    "down": (880, 55),     # 降挡成功：中、短
    "fail": (300, 170),    # 失败/超时：低、长
}


def beep(kind: str, enabled: bool = True) -> None:
    if not enabled or not WINSOUND_OK:
        return
    freq, ms = _TONES.get(kind, (600, 60))

    def _play() -> None:
        try:
            winsound.Beep(freq, ms)
        except Exception:
            try:
                winsound.MessageBeep()
            except Exception:
                pass

    threading.Thread(target=_play, daemon=True).start()


def send_scan(scan: int, up: bool) -> bool:
    """发送一个扫描码事件。返回是否成功。"""
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki.wVk = 0
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    sent = user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    return sent == 1


class KeyTap:
    """按下-保持-释放。保持时间是关键：太短会被游戏漏掉。

    参考项目给的是 40~60ms。我们取 60ms，并且把它做成参数，方便后面调。
    """

    def __init__(self, scan: int, hold_ms: int = 60) -> None:
        self.scan = scan
        self.hold_ms = hold_ms

    def fire(self) -> bool:
        if not send_scan(self.scan, up=False):
            return False
        time.sleep(self.hold_ms / 1000.0)
        send_scan(self.scan, up=True)
        return True


class EdgeDetector:
    """把"一直按着"变成"按下的那一瞬间触发一次"。"""

    def __init__(self) -> None:
        self._prev: dict[int, bool] = {}

    def pressed(self, vk: int) -> bool:
        now_down = key_down(vk)
        was = self._prev.get(vk, False)
        self._prev[vk] = now_down
        return now_down and not was


# ══════════════════════════════════════════════════════════════════════════
# 第 2 部分：指令与回执
#
# 这是本阶段最重要的数据结构。每发一条指令就建一个 Pending，
# 然后每帧检查"档位变了吗"，变了就结算延迟；超过 1.5 秒没变就算失败。
# ══════════════════════════════════════════════════════════════════════════

class Pending:
    __slots__ = ("t_send", "from_gear", "expect_dir", "label")

    def __init__(self, t_send: float, from_gear: int, expect_dir: int, label: str):
        self.t_send = t_send
        self.from_gear = from_gear
        self.expect_dir = expect_dir   # +1 升挡, -1 降挡
        self.label = label


class ShiftStats:
    def __init__(self) -> None:
        self.results: list[tuple[str, int, bool]] = []   # (说明, 毫秒, 成功)
        self.timeouts = 0
        self.dry = 0                 # 演练次数，不计入成功率
        self.last_dry = ""

    def add(self, desc: str, ms: int, ok: bool) -> None:
        self.results.append((desc, ms, ok))
        if len(self.results) > 40:
            del self.results[0]

    def add_dry(self, desc: str) -> None:
        self.dry += 1
        self.last_dry = desc

    def summary(self) -> str:
        ok = [ms for _, ms, good in self.results if good]
        total = len(self.results)
        if not total:
            if self.dry:
                return f"尚未实发（演练 {self.dry} 次，不计入统计）"
            return "尚无数据"
        lat = f"中位 {median(ok):.0f}ms  最快 {min(ok)}ms  最慢 {max(ok)}ms" if ok else "无成功样本"
        tail = f"  演练 {self.dry} 次" if self.dry else ""
        return f"成功 {len(ok)}/{total}  超时 {self.timeouts}   {lat}{tail}"

    def recent(self, n: int = 4) -> list[str]:
        out = []
        for desc, ms, ok in self.results[-n:]:
            out.append(f"{desc}  {ms}ms  {'✓' if ok else '✗'}")
        return out


class ShiftTracker:
    """把"发指令 → 等回执 → 结算延迟"这段逻辑独立出来。

    单独成类的原因很实在：它是这个阶段唯一有判断逻辑的部分，
    抽出来就能用假数据做自动化测试，不用真的按键、不用开游戏。
    """

    TIMEOUT_S = 1.5
    SHIFT_GEAR = 11        # 换挡中的占位值（实测结论，见 docs/01-协议实测记录.md）

    def __init__(self) -> None:
        self.pending: Pending | None = None
        self.stats = ShiftStats()

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def command(self, now: float, gear: int, direction: int, label: str) -> bool:
        """登记一条指令。已有未完成的指令时返回 False。"""
        if self.pending is not None:
            return False
        self.pending = Pending(now, gear, direction, label)
        return True

    def on_frame(self, now: float, gear: int) -> tuple[str, int, bool] | None:
        """收到一帧遥测。如果这条指令有了结果，返回 (说明, 毫秒, 是否成功)。"""
        p = self.pending
        if p is None:
            return None
        # 换挡中（11）不算结果；档位没变也还不算——可能是延迟，等 tick() 判超时
        if gear == self.SHIFT_GEAR or gear == p.from_gear:
            return None
        ms = int((now - p.t_send) * 1000)
        moved = gear - p.from_gear
        ok = moved * p.expect_dir > 0
        desc = f"{p.label} {p.from_gear}→{gear}"
        self.pending = None
        self.stats.add(desc, ms, ok)
        return desc, ms, ok

    def tick(self, now: float) -> tuple[str, int, bool] | None:
        """超时检查。1.5 秒内档位没动，判为失败。"""
        p = self.pending
        if p is None or (now - p.t_send) <= self.TIMEOUT_S:
            return None
        desc = f"{p.label} {p.from_gear}→?"
        self.pending = None
        self.stats.timeouts += 1
        self.stats.add(desc, int(self.TIMEOUT_S * 1000), False)
        return desc, int(self.TIMEOUT_S * 1000), False


# ══════════════════════════════════════════════════════════════════════════
# 第 3 部分：主程序
# ══════════════════════════════════════════════════════════════════════════

def build_panel(state: dict, stats: ShiftStats, lines_hint: list[str]) -> list[str]:
    W = 78
    out = ["═" * W, " 阶段 3：手动换挡测试（按 F6 升挡 / F5 降挡 / F12 退出）", "═" * W]

    v = state.get("values")
    age = state.get("age", 999.0)
    live = "● 在线" if age < 0.5 else ("○ 断续" if age < 1.5 else "× 无数据（已阻止发送）")
    mode = "实发" if state["live_output"] else "演练（只显示不发送）"
    out.append(f" 遥测    {state['rate']:.1f} 帧/秒   {live}")
    out.append(f" 输出    {state['key_names']}   模式: {mode}")

    if v is None:
        out.append("─" * W)
        out.append(" 等待遥测……（确认游戏在赛道上、Data Out 已开启）")
        out.extend(lines_hint[:3])
        out.append("═" * W)
        return out

    rpm = v["current_engine_rpm"]
    mx = v["engine_max_rpm"]
    pct = (rpm / mx * 100) if mx > 0 else 0.0
    out.append("─" * W)
    out.append(f" 当前    档位 {gear_short(int(v['gear'])):>2}      "
               f"转速 {rpm:6.0f}/{mx:.0f} ({pct:4.1f}%)      "
               f"车速 {v['speed'] * 3.6:6.1f} km/h")
    out.append(f"         油门 {v['accel'] / 2.55:3.0f}%   刹车 {v['brake'] / 2.55:3.0f}%   "
               f"离合 {v['clutch'] / 2.55:3.0f}%   "
               f"{'换挡中' if int(v['gear']) == 11 else ''}")

    out.append("─" * W)
    last = state.get("last_result")
    if last:
        out.append(f" 上次    {last}")
    else:
        out.append(" 上次    ——")
    out.append(f" 统计    {stats.summary()}")
    for ln in stats.recent():
        out.append(f"         {ln}")

    msg = state.get("message", "")
    if msg and (time.time() - state.get("message_at", 0.0)) < 4.0:
        out.append("─" * W)
        out.append(f" {msg}")
    out.append("═" * W)
    return out


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="阶段 3：手动换挡测试器")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--keys", default="num9:num7", metavar="升:降",
                    help="游戏里绑定的升/降挡键，默认 num9:num7（FM8 默认小键盘）")
    ap.add_argument("--hold", type=int, default=60, help="按键保持毫秒数（默认 60）")
    ap.add_argument("--dry-run", action="store_true", help="只显示不发送")
    ap.add_argument("--overlay", action="store_true",
                    help="在游戏画面上显示置顶浮窗（需游戏为无边框窗口模式）")
    ap.add_argument("--pos", default="24,24", help="浮窗位置 左上角坐标，如 24,24")
    ap.add_argument("--mute", action="store_true", help="关闭声音提示")
    args = ap.parse_args()

    try:
        up_name, down_name = [s.strip().lower() for s in args.keys.split(":")]
        up_scan, down_scan = SCANCODES[up_name], SCANCODES[down_name]
    except (ValueError, KeyError):
        print(f"按键名不认识：{args.keys}")
        print(f"可用：{', '.join(sorted(SCANCODES))}")
        raise SystemExit(1)

    layouts = build_layouts()
    lay = layouts[331]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        print(f"无法监听 {args.host}:{args.port} — {e}")
        print("是不是还有另一个探针/遥测软件占着端口？")
        raise SystemExit(1)
    sock.settimeout(0.01)

    if up_name.startswith("num") and not numlock_on():
        print("\n[注意] NumLock 当前是关闭的。"
              "\n       小键盘键在没有 NumLock 时会被识别成方向键，游戏可能收不到。"
              "\n       请打开 NumLock，或改绑其它键（例如 --keys '[:]'）。\n")

    screen = Screen()
    edge = EdgeDetector()
    tracker = ShiftTracker()
    up_tap = KeyTap(up_scan, args.hold)
    down_tap = KeyTap(down_scan, args.hold)
    sound_on = not args.mute

    state = {
        "values": None, "age": 999.0, "rate": 0.0,
        "live_output": not args.dry_run,
        "key_names": f"{up_name} / {down_name}",
        "last_result": "", "last_ok": None, "message": "", "message_at": 0.0,
    }

    overlay = None
    if args.overlay:
        try:
            from overlay import Overlay
            ox, oy = [int(v) for v in args.pos.split(",")]
            overlay = Overlay(x=ox, y=oy)
            overlay.set_lines([
                "等待遥测…", "F6 升挡   F5 降挡", "F7 演练/实发   F12 退出", "",
            ])
            print("浮窗已开启。游戏请用「无边框窗口」模式，全屏独占时浮窗不可见。")
        except Exception as e:
            print(f"[浮窗不可用] {e}（不影响换挡，仍可用声音和本窗口查看结果）")
            overlay = None

    def say(text: str, now: float) -> None:
        state["message"] = text
        state["message_at"] = now
    last_ts = -1
    stamps: list[float] = []
    last_frame_at = 0.0
    last_draw = 0.0
    started_at = time.time()
    diag_done = False
    hint = [
        "  检查：游戏里 Data Out 是否开启、端口是否一致、是否已在赛道上。",
    ]

    print(f"已监听 {args.host}:{args.port}    升挡键 {up_name} / 降挡键 {down_name}")
    print("F6 升挡   F5 降挡   F7 切换演练/实发   F12 退出\n")

    try:
        while True:
            now = time.time()

            if edge.pressed(VK_F12):
                break
            if edge.pressed(VK_F7):
                state["live_output"] = not state["live_output"]
                say("已切到实发模式" if state["live_output"]
                    else "已切到演练模式（不会真的按键）", now)

            # ---- 接收 ----
            got_frame = False
            try:
                raw, _ = sock.recvfrom(2048)
                frame = parse_frame(raw, lay)
                if frame is not None:
                    v = frame.values
                    ts = int(v["timestamp_ms"])
                    if ts == last_ts:
                        pass          # 同一帧的重复包，丢弃
                    else:
                        last_ts = ts
                        got_frame = True
                        state["values"] = v
                        state["age"] = 0.0
                        last_frame_at = now
                        stamps.append(now)
                        while stamps and now - stamps[0] > 1.0:
                            stamps.pop(0)

                        # ---- 结算回执 ----
                        resolved = tracker.on_frame(now, int(v["gear"]))
                        if resolved is not None:
                            desc, ms, ok = resolved
                            state["last_ok"] = ok
                            state["last_result"] = (
                                f"{desc}  {'成功' if ok else '档位变化异常'}  延迟 {ms} ms"
                            )
                            beep("fail" if not ok else
                                 ("up" if "升挡" in desc else "down"), sound_on)
            except socket.timeout:
                pass
            except OSError:
                break

            # ---- 超时检查 ----
            timed_out = tracker.tick(now)
            if timed_out is not None:
                desc, ms, _ = timed_out
                state["last_ok"] = False
                state["last_result"] = f"{desc}  {ms} ms 内档位未变化（指令可能没送进游戏）"
                beep("fail", sound_on)

            if not got_frame:
                state["age"] = (now - last_frame_at) if last_frame_at else 999.0

            state["rate"] = float(len(stamps))

            # ---- 按键 ----
            for vk, tap, direction, label in (
                (VK_F6, up_tap, +1, "升挡"),
                (VK_F5, down_tap, -1, "降挡"),
            ):
                if not edge.pressed(vk):
                    continue
                v = state["values"]
                if v is None or state["age"] > 1.0:
                    say("遥测中断，已阻止发送（等数据恢复再试）", now)
                    continue
                gear_now = int(v["gear"])
                if gear_now == 11:
                    say("车辆正在换挡中，等一下再按", now)
                    continue
                if tracker.busy:
                    say("上一条指令还没确认，请稍等", now)
                    continue

                tracker.command(now, gear_now, direction, label)
                if state["live_output"]:
                    if not tap.fire():
                        say("SendInput 失败（可能权限不足，试试以管理员运行）", now)
                        tracker.pending = None
                        continue
                    say("", now)
                else:
                    # 演练模式不真的按键：单独计数，绝不混进成功率里
                    tracker.pending = None
                    tracker.stats.add_dry(f"{label} {gear_now}→{gear_now + direction}")
                    state["last_result"] = (
                        f"{label}（演练，未发送）{gear_now}→{gear_now + direction}"
                    )
                    state["last_ok"] = None
                    beep("up" if direction > 0 else "down", sound_on)

            # ---- 画面 ----
            if now - last_draw >= 0.1:
                last_draw = now
                screen.draw(build_panel(state, tracker.stats, hint), now)
                if overlay is not None:
                    try:
                        v = state["values"]
                        if v is None:
                            overlay.set_lines([
                                "等待遥测…", "F6 升挡   F5 降挡",
                                "F7 演练/实发   F12 退出", "",
                            ])
                        else:
                            ok = state.get("last_ok")
                            color = "#7ee787" if ok else ("#ff7b72" if ok is False else "#d0d0d0")
                            mode = "" if state["live_output"] else "  [演练]"
                            overlay.set_lines([
                                f"{gear_short(int(v['gear'])):>2} 挡   "
                                f"{v['current_engine_rpm']:.0f} rpm   "
                                f"{v['speed'] * 3.6:.0f} km/h{mode}",
                                state.get("last_result") or "——",
                                tracker.stats.summary(),
                                "F6 升   F5 降   F7 演练/实发   F12 退出",
                            ], colors=["#ffffff", color, "#a0a0a0", "#707070"])
                        overlay.tick(now)
                    except Exception:
                        overlay = None

                # 浮窗自检：3 秒后问一次"到底显示出来没有、有没有被盖住"
                if overlay is not None and not diag_done and now - started_at > 3.0:
                    diag_done = True
                    d = overlay.diag()
                    if not d.get("visible"):
                        print("\n[浮窗] 窗口没有显示出来（可能是创建失败）。"
                              "\n       声音提示不受影响，仍可正常测试。\n")
                    elif d.get("covered"):
                        print("\n[浮窗] 窗口存在，但有别的窗口压在它上面 —— 说明游戏画面"
                              "\n       盖住了它。常见原因是 Windows 的「全屏优化」把无边框窗口"
                              "\n       当成独占全屏在渲染。两个办法："
                              "\n         1) 游戏设置改成「窗口化」（不是无边框），浮窗就会出现；"
                              "\n         2) 右键 FM8 的 exe → 属性 → 兼容性 → 勾选「禁用全屏优化」。"
                              "\n       不改也没关系：声音提示始终有效。\n")
                    else:
                        print("\n[浮窗] 窗口已创建，且没有窗口压在它上面。"
                              "\n       如果游戏里仍然看不到，说明游戏的渲染绕过了桌面合成"
                              "\n       （全屏优化的典型表现），改用「窗口化」即可。"
                              "\n       声音提示始终有效。\n")

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        if overlay is not None:
            overlay.close()
        screen.finish()
        print()
        print("─" * 78)
        print(" 本次结果")
        print("─" * 78)
        print(f" {tracker.stats.summary()}")
        for desc, ms, ok in tracker.stats.results:
            print(f"   {desc:<18} {ms:>5} ms  {'✓' if ok else '✗'}")
        print()
        print(" 判读标准：")
        print("   · 成功 20/20 且中位延迟 < 250ms  → 通道可靠，可以进阶段 4")
        print("   · 有失败或超时                    → 先换 --keys 或装 vJoy 再试")
        print("   · 全部超时（档位毫无变化）        → 指令没送进游戏，多半是键位/焦点问题")


if __name__ == "__main__":
    main()
