#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FM8 遥测探针（阶段 2）
=======================

这是一个"只读"工具：只监听 UDP 数据包并显示，绝不向游戏发送任何东西。
它的唯一任务是回答一个问题：**我们读到的 FM8 数据，准不准、实不实时。**

设计上的两个刻意的选择：

1. 单文件、零第三方依赖（只用 Python 自带的标准库）。
   排障时最怕"装了三个库还报错"，所以这个文件可以直接双击运行。

2. 偏移表由"官方字段顺序"自动累加算出，而不是手写死数字。
   这样只要字段顺序抄对了，偏移就不可能算错；而且 `--offsets` 可以把
   算出来的表打出来，方便和游戏实测对照。

用法：
    python tools/probe.py                  # 监听 5300 端口
    python tools/probe.py --port 5555      # 换端口
    python tools/probe.py --record run1.fm8rec
    python tools/probe.py --replay run1.fm8rec
    python tools/probe.py --offsets        # 只打印偏移表，不接收数据
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import sys
import time
from collections import Counter, deque

# ══════════════════════════════════════════════════════════════════════════
# 第 1 部分：协议定义
#
# Forza 的 Data Out 包就是一段固定顺序的二进制数据。字段顺序是官方文档给定
# 的，我们按顺序累加每个字段占的字节数，就得到了每个字段的偏移。
#
# 注意：Forza 的浮点数是小端序（little-endian），所以格式串前面都加 "<"。
#   i = 有符号 32 位整数（4 字节）   I = 无符号 32 位整数（4 字节）
#   f = 32 位浮点数（4 字节）        H = 无符号 16 位整数（2 字节）
#   B = 无符号 8 位整数（1 字节）    b = 有符号 8 位整数（1 字节）
# ══════════════════════════════════════════════════════════════════════════

# Sled 段：232 字节。Forza Motorsport 7 / 2023 与 Horizon 系列这一段完全相同。
SLED_FIELDS = [
    ("is_race_on", "i"),
    ("timestamp_ms", "I"),
    ("engine_max_rpm", "f"),
    ("engine_idle_rpm", "f"),
    ("current_engine_rpm", "f"),
    ("accel_x", "f"), ("accel_y", "f"), ("accel_z", "f"),
    ("vel_x", "f"), ("vel_y", "f"), ("vel_z", "f"),
    ("ang_vel_x", "f"), ("ang_vel_y", "f"), ("ang_vel_z", "f"),
    ("yaw", "f"), ("pitch", "f"), ("roll", "f"),
    ("susp_fl", "f"), ("susp_fr", "f"), ("susp_rl", "f"), ("susp_rr", "f"),
    ("slip_fl", "f"), ("slip_fr", "f"), ("slip_rl", "f"), ("slip_rr", "f"),
    ("wheelspeed_fl", "f"), ("wheelspeed_fr", "f"), ("wheelspeed_rl", "f"), ("wheelspeed_rr", "f"),
    ("rumble_fl", "i"), ("rumble_fr", "i"), ("rumble_rl", "i"), ("rumble_rr", "i"),
    ("puddle_fl", "f"), ("puddle_fr", "f"), ("puddle_rl", "f"), ("puddle_rr", "f"),
    ("surf_rumble_fl", "f"), ("surf_rumble_fr", "f"),
    ("surf_rumble_rl", "f"), ("surf_rumble_rr", "f"),
    ("slipangle_fl", "f"), ("slipangle_fr", "f"), ("slipangle_rl", "f"), ("slipangle_rr", "f"),
    ("combslip_fl", "f"), ("combslip_fr", "f"), ("combslip_rl", "f"), ("combslip_rr", "f"),
    ("susp_m_fl", "f"), ("susp_m_fr", "f"), ("susp_m_rl", "f"), ("susp_m_rr", "f"),
    ("car_ordinal", "i"), ("car_class", "i"), ("car_pi", "i"),
    ("drivetrain", "i"), ("num_cylinders", "i"),
]

# Dash 段：79 字节。这是我们要的"仪表数据"。
DASH_FIELDS = [
    ("pos_x", "f"), ("pos_y", "f"), ("pos_z", "f"),
    ("speed", "f"),            # 单位：米/秒
    ("power", "f"),            # 单位：瓦特
    ("torque", "f"),           # 单位：牛·米
    ("tire_temp_fl", "f"), ("tire_temp_fr", "f"),
    ("tire_temp_rl", "f"), ("tire_temp_rr", "f"),
    ("boost", "f"),
    ("fuel", "f"),
    ("distance", "f"),
    ("best_lap", "f"), ("last_lap", "f"),
    ("current_lap", "f"), ("current_race_time", "f"),
    ("lap_number", "H"),
    ("race_position", "B"),
    ("accel", "B"),            # 0..255
    ("brake", "B"),            # 0..255
    ("clutch", "B"),           # 0..255
    ("handbrake", "B"),
    ("gear", "B"),             # 语义待实测（0=R？11=N？）
    ("steer", "b"),
    ("norm_line", "b"),
    ("norm_ai_brake_diff", "b"),
]

# FM8（Forza Motorsport 2023）在 Dash 之后多出 20 字节：四轮磨损 + 赛道 ID。
FM8_EXT_FIELDS = [
    ("tire_wear_fl", "f"), ("tire_wear_fr", "f"),
    ("tire_wear_rl", "f"), ("tire_wear_rr", "f"),
    ("track_ordinal", "i"),
]


class Layout:
    """一种包的布局：各段从哪个字节开始、每个字段在哪个字节。"""

    def __init__(self, key: str, name: str, total: int, dash_off: int | None,
                 ext_off: int | None, note: str = ""):
        self.key = key
        self.name = name
        self.total = total
        self.dash_off = dash_off
        self.ext_off = ext_off
        self.note = note
        self.fields: list[tuple[str, str, int]] = []

        off = 0
        for name_, fmt in SLED_FIELDS:
            self.fields.append((name_, fmt, off))
            off += struct.calcsize("<" + fmt)
        self.sled_size = off

        if dash_off is not None:
            off = dash_off
            for name_, fmt in DASH_FIELDS:
                self.fields.append((name_, fmt, off))
                off += struct.calcsize("<" + fmt)
            self.dash_size = off - dash_off

        if ext_off is not None:
            off = ext_off
            for name_, fmt in FM8_EXT_FIELDS:
                self.fields.append((name_, fmt, off))
                off += struct.calcsize("<" + fmt)
            self.ext_size = off - ext_off

    def __repr__(self) -> str:
        return f"<Layout {self.key} total={self.total}>"


def build_layouts() -> dict[int, Layout]:
    """按包长度建立"长度 → 布局"的查找表。

    长度是唯一可靠的判别依据：FM8 是 331，FM7 是 311，Horizon 是 324。
    """
    layouts = {
        331: Layout("fm8", "FM8 (Forza Motorsport 2023) Car Dash", 331, 232, 311,
                    note="这正是我们要的格式"),
        311: Layout("fm7", "FM7 Car Dash（旧版，没有轮胎磨损）", 311, 232, None,
                    note="如果收到这个，说明游戏里选的是旧的 Dash 格式或游戏版本不同"),
        324: Layout("horizon", "Horizon 系列 Car Dash（FH4/FH5/FH6）", 324, 244, None,
                    note="如果收到这个，说明你在跑地平线而不是 FM8"),
        232: Layout("sled", "Sled（只有车辆物理数据，没有档位/踏板）", 232, None, None,
                    note="请在游戏里把 Packet Format 改成 Dash"),
    }
    return layouts


# ══════════════════════════════════════════════════════════════════════════
# 第 2 部分：解析与校验
# ══════════════════════════════════════════════════════════════════════════

class Frame:
    """一帧解析结果。用普通字典存字段值，够用且好打印。"""

    __slots__ = ("values", "raw", "layout", "warnings")

    def __init__(self, values: dict, raw: bytes, layout: Layout, warnings: list[str]):
        self.values = values
        self.raw = raw
        self.layout = layout
        self.warnings = warnings


def parse_frame(buf: bytes, layout: Layout) -> Frame | None:
    """把一段字节按布局翻译成字段字典。失败返回 None（不抛异常）。"""
    if len(buf) < layout.total:
        return None

    values: dict[str, float | int] = {}
    try:
        for name, fmt, off in layout.fields:
            values[name] = struct.unpack_from("<" + fmt, buf, off)[0]
    except struct.error:
        return None

    warnings = validate(values)
    return Frame(values, buf, layout, warnings)


def validate(v: dict) -> list[str]:
    """基本合理性检查。

    这一步很关键：如果偏移表错了，读出来的数字往往"看着像真的但很离谱"。
    有了这些检查，错误会自己喊出来，而不是悄悄拿去换挡。
    """
    out: list[str] = []

    for key in ("speed", "power", "torque", "current_engine_rpm", "engine_max_rpm"):
        x = v.get(key)
        if x is None:
            continue
        if not math.isfinite(x):
            out.append(f"{key} 不是有限数（NaN/Inf）")

    rpm = v.get("current_engine_rpm", 0.0)
    max_rpm = v.get("engine_max_rpm", 0.0)
    if max_rpm <= 0:
        out.append("engine_max_rpm <= 0（发动机没启动，或在菜单里）")
    elif rpm > max_rpm * 1.05:
        out.append(f"转速 {rpm:.0f} 超过红线 {max_rpm:.0f} 的 105%（偏移可能错了）")

    spd = v.get("speed", 0.0)
    if spd < -1 or spd > 150:
        out.append(f"车速 {spd:.1f} m/s 不合理（-1..150 之外）")

    # 交叉校验：Dash 的 Speed 应当和 Sled 的速度矢量长度一致。
    # 这条对不上，几乎一定是 Dash 区起点算错了。
    vx, vy, vz = v.get("vel_x", 0.0), v.get("vel_y", 0.0), v.get("vel_z", 0.0)
    vmag = math.sqrt(vx * vx + vy * vy + vz * vz)
    if spd > 3.0 and abs(spd - vmag) > 1.5:
        out.append(f"Speed({spd:.1f}) 与 |Velocity|({vmag:.1f}) 不一致，差 {abs(spd - vmag):.1f}")

    gear = v.get("gear", 0)
    if gear > 12:
        out.append(f"档位读到 {gear}（0..12 之外，很可能偏移错了）")

    trq = v.get("torque", 0.0)
    if trq < -2000 or trq > 4000:
        out.append(f"扭矩 {trq:.0f} Nm 不合理")

    pw = v.get("power", 0.0)
    if abs(pw) > 2_000_000:
        out.append(f"功率 {pw:.0f} W 不合理")

    return out


def gear_label(g: int) -> str:
    """档位显示。0 一定是倒挡；11 在 Horizon 里是空挡；FM8 待实测。"""
    if g == 0:
        return "倒挡"
    if 1 <= g <= 10:
        return ""
    if g == 11:
        return "空挡?"
    return f"异常值"


def gear_short(g: int) -> str:
    """日志里用的短标签。"""
    if g == 0:
        return "R"
    if 1 <= g <= 10:
        return str(g)
    if g == 11:
        return "N?"
    return f"?{g}"


# ══════════════════════════════════════════════════════════════════════════
# 第 3 部分：统计
# ══════════════════════════════════════════════════════════════════════════

class Stats:
    def __init__(self) -> None:
        self.total = 0
        self.parse_fail = 0
        self.other_sizes: Counter[int] = Counter()
        self.stamp_times: deque[float] = deque()          # 用于算包率
        self.intervals: deque[float] = deque(maxlen=600)  # 用于算间隔抖动
        self.last_recv = 0.0
        self.first_recv = 0.0
        self.ts_regressions = 0
        self._last_ts = 0
        self.warn_counts: Counter[str] = Counter()
        self.gear_counts: Counter[int] = Counter()
        self.gear_stats: dict[int, dict] = {}
        self.gear_changes: deque[str] = deque(maxlen=4)
        self.transitions: Counter[tuple[int, int]] = Counter()
        self.dup_ts = 0
        self.invalid_frames = 0

    def on_packet(self, now: float, frame: Frame | None, size: int) -> None:
        if frame is None:
            self.parse_fail += 1
            self.other_sizes[size] += 1
            return

        self.total += 1
        if self.first_recv == 0.0:
            self.first_recv = now
        if self.last_recv > 0:
            dt = now - self.last_recv
            if 0 < dt < 1.0:                 # 超过 1 秒的间隔视为"断流"，不计入抖动
                self.intervals.append(dt)
        self.last_recv = now
        self.stamp_times.append(now)
        while self.stamp_times and now - self.stamp_times[0] > 1.0:
            self.stamp_times.popleft()

        ts = int(frame.values.get("timestamp_ms", 0))
        if self._last_ts and ts and ts < self._last_ts - 5:
            self.ts_regressions += 1
        if ts and ts == self._last_ts:
            # 时间戳完全相同 = 同一帧发了多个包。这能解释"间隔忽大忽小"。
            self.dup_ts += 1
        if ts:
            self._last_ts = ts

        for w in frame.warnings:
            key = w.split("（")[0].split(" 不")[0][:40]
            self.warn_counts[key] += 1

        self._observe_gear(frame)

    def _observe_gear(self, frame: Frame) -> None:
        """按档位累加统计。

        除了计数，还记录该档位下的车速和转速——因为"这个值是什么意思"
        不能靠猜，要看它出现时的物理状态：
        速度为 0 的 11 和 80 km/h 的 11，含义完全不同。
        """
        v = frame.values
        g = int(v.get("gear", 0))
        self.gear_counts[g] += 1

        st = self.gear_stats.setdefault(
            g, {"n": 0, "invalid": 0, "valid": 0,
                "speed": 0.0, "rpm": 0.0, "max_speed": 0.0, "max_rpm": 0.0}
        )
        st["n"] += 1

        max_rpm = float(v.get("engine_max_rpm", 0.0))
        if max_rpm <= 0:
            # 发动机未启动：菜单、加载、暂停。这些帧不能用来判断档位语义。
            st["invalid"] += 1
            self.invalid_frames += 1
            return

        st["valid"] += 1
        st["speed"] += float(v.get("speed", 0.0)) * 3.6
        st["rpm"] += float(v.get("current_engine_rpm", 0.0))
        st["max_speed"] = max(st["max_speed"], float(v.get("speed", 0.0)) * 3.6)
        st["max_rpm"] = max(st["max_rpm"], float(v.get("current_engine_rpm", 0.0)))

    def pps(self) -> float:
        return float(len(self.stamp_times))

    def age(self, now: float) -> float:
        return now - self.last_recv if self.last_recv else 999.0

    def interval_ms(self) -> tuple[float, float, float]:
        if not self.intervals:
            return 0.0, 0.0, 0.0
        xs = sorted(self.intervals)
        avg = sum(xs) / len(xs) * 1000
        p99 = xs[min(len(xs) - 1, int(len(xs) * 0.99))] * 1000
        return avg, min(xs) * 1000, p99

    def note_gear_change(self, old: int, new: int, v: dict, now: float) -> None:
        self.transitions[(old, new)] += 1
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
        self.gear_changes.append(
            f"{stamp}   {gear_short(old)} → {gear_short(new)}"
            f"    rpm {v.get('current_engine_rpm', 0):.0f}"
            f"    {v.get('speed', 0) * 3.6:.1f} km/h"
            f"    thr {v.get('accel', 0) / 2.55:.0f}%"
        )


# ══════════════════════════════════════════════════════════════════════════
# 第 4 部分：录制 / 回放
#
# 录制的意义：以后改算法时，可以拿同一段真实数据反复回放对比，
# 不用每次都真的去开车。这是参考项目能稳定迭代的关键手段。
# ══════════════════════════════════════════════════════════════════════════

REC_MAGIC = b"FM8REC1\n"


def _stop_key_pressed() -> bool:
    """检查是否按下了 Q 或 Esc。

    为什么不只用 Ctrl+C：这个程序是在 .bat 里被调用的，Ctrl+C 会让 cmd
    弹出"终止批处理作业(Y/N)"，一旦选 Y，窗口直接关掉，统计信息就看不见了。
    读取按键用的是 Windows 自带模块，不引入第三方依赖。
    """
    if os.name != "nt":
        return False
    try:
        import msvcrt
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("q", "Q", "\x1b"):
                return True
    except Exception:
        pass
    return False


class Recorder:
    def __init__(self, path: str) -> None:
        self.path = path
        self.f = open(path, "wb")
        self.f.write(REC_MAGIC)
        self.t0 = time.perf_counter()
        self.count = 0

    def write(self, buf: bytes) -> None:
        rel_ms = int((time.perf_counter() - self.t0) * 1000)
        self.f.write(struct.pack("<IH", rel_ms, len(buf)))
        self.f.write(buf)
        self.count += 1

    def close(self) -> None:
        try:
            self.f.flush()
            self.f.close()
        except OSError:
            pass


def read_records(path: str):
    """生成 (相对毫秒, 原始字节)。"""
    with open(path, "rb") as f:
        magic = f.read(len(REC_MAGIC))
        if magic != REC_MAGIC:
            raise SystemExit(
                f"这不是本工具录制的文件（头部不匹配）。\n文件: {path}"
            )
        while True:
            head = f.read(6)
            if len(head) < 6:
                return
            rel_ms, length = struct.unpack("<IH", head)
            payload = f.read(length)
            if len(payload) < length:
                return
            yield rel_ms, payload


# ══════════════════════════════════════════════════════════════════════════
# 第 5 部分：显示
# ══════════════════════════════════════════════════════════════════════════

ANSI_ON = False


def enable_ansi() -> bool:
    """让 Windows 控制台支持光标定位。失败则退回"逐行打印"模式。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:
        return False


def bar(value: float, width: int = 22) -> str:
    value = max(0.0, min(1.0, value))
    n = int(round(value * width))
    return "█" * n + "░" * (width - n)


def fmt(v: float, spec: str) -> str:
    try:
        return format(v, spec)
    except (ValueError, TypeError):
        return str(v)


class Screen:
    """负责把面板画到固定位置。

    ANSI 可用时按"行号绝对定位"重画，而不是靠换行符往下走——
    靠换行的话，一旦面板行数接近窗口底部，控制台会整屏上滚，
    上一帧的内容就会漂移，看起来像花屏。
    """

    HIDE_CURSOR = "\x1b[?25l"
    SHOW_CURSOR = "\x1b[?25h"

    def __init__(self) -> None:
        global ANSI_ON
        ANSI_ON = enable_ansi() and sys.stdout.isatty()
        self.height = 30
        self.first = True
        self.last_plain = 0.0
        self.cursor_hidden = False

    def draw(self, lines: list[str], now: float) -> None:
        if ANSI_ON:
            out = []
            if self.first:
                out.append("\x1b[2J")          # 首次清屏
                out.append(self.HIDE_CURSOR)
                self.cursor_hidden = True
                self.first = False
            for i, ln in enumerate(lines, 1):
                out.append(f"\x1b[{i};1H\x1b[K{ln}")
            # 清掉上一帧多出来的行
            for i in range(len(lines) + 1, self.height + 1):
                out.append(f"\x1b[{i};1H\x1b[K")
            self.height = max(self.height, len(lines))
            out.append("\x1b[1;1H")
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        else:
            if now - self.last_plain < 1.0:
                return
            self.last_plain = now
            sys.stdout.write("\n".join(lines) + "\n" + "-" * 78 + "\n")
            sys.stdout.flush()

    def finish(self) -> None:
        if ANSI_ON:
            sys.stdout.write("\x1b[2J\x1b[H")
            if self.cursor_hidden:
                sys.stdout.write(self.SHOW_CURSOR)
            sys.stdout.flush()


def build_panel(frame: Frame | None, layout: Layout, stats: Stats, now: float,
                endpoint: str, extra_note: str = "") -> list[str]:
    W = 78
    out: list[str] = []
    out.append("═" * W)
    out.append(" FM8 遥测探针（只读） — 不会向游戏发送任何输入")
    out.append("═" * W)

    tag = f"[{layout.total}B] {layout.name}"
    out.append(f" 包格式  {tag}")

    age = stats.age(now)
    if stats.last_recv == 0:
        live = "× 尚未收到任何包"
    elif age < 0.5:
        live = "● 在线"
    elif age < 2.0:
        live = "○ 断续"
    else:
        live = "× 无数据"

    if stats.total == 0:
        out.append(f" 接收    {endpoint}   等待第一个数据包……   {live}")
    else:
        avg, mn, p99 = stats.interval_ms()
        out.append(
            f" 接收    {endpoint}   包率 {stats.pps():.1f}/s   间隔 {avg:.1f}ms"
            f"(最小 {mn:.1f} / P99 {p99:.1f})   {live}"
        )
        out.append(
            f" 累计    {stats.total} 包   解析失败 {stats.parse_fail}"
            f"   告警 {sum(stats.warn_counts.values())}   时间戳倒退 {stats.ts_regressions}"
            f"   最后收包 {age:.2f}s 前"
        )

    if frame is None:
        out.append("")
        out.append(" 等待游戏数据……")
        out.append("")
        if extra_note:
            for ln in extra_note.splitlines():
                out.append("   " + ln)
        out.append("═" * W)
        return out

    v = frame.values
    rpm = v.get("current_engine_rpm", 0.0)
    max_rpm = v.get("engine_max_rpm", 0.0)
    pct = (rpm / max_rpm) if max_rpm > 0 else 0.0
    spd_kmh = v.get("speed", 0.0) * 3.6
    accel = v.get("accel", 0)
    brake = v.get("brake", 0)
    clutch = v.get("clutch", 0)

    out.append("─" * W)
    out.append(f" 档位  Gear = {v.get('gear', 0):<3} {gear_label(int(v.get('gear', 0))):<4}"
               f"           车速  Speed = {spd_kmh:6.1f} km/h")
    out.append(f" 转速  RPM  = {rpm:6.0f} / {max_rpm:6.0f}  ({pct * 100:5.1f}%)"
               f"      油门  Accel = {accel:3d} ({accel / 2.55:3.0f}%)")
    out.append(f" 功率  Power = {v.get('power', 0) / 1000:6.1f} kW"
               f"             刹车  Brake = {brake:3d} ({brake / 2.55:3.0f}%)")
    out.append(f" 扭矩  Torque= {v.get('torque', 0):6.0f} Nm"
               f"             离合  Clutch= {clutch:3d} ({clutch / 2.55:3.0f}%)")
    dt = {0: "FWD", 1: "RWD", 2: "AWD"}.get(int(v.get("drivetrain", -1)), "?")
    out.append(f" 状态  IsRaceOn = {int(v.get('is_race_on', 0))}   {dt}   PI={v.get('car_pi', 0)}"
               f"   CarOrdinal={v.get('car_ordinal', 0)}   {v.get('num_cylinders', 0)}缸")

    out.append("─" * W)
    out.append(f" 转速 [{bar(pct)}] {pct * 100:3.0f}%")
    out.append(f" 油门 [{bar(accel / 255)}] {accel / 2.55:3.0f}%")
    out.append(f" 刹车 [{bar(brake / 255)}] {brake / 2.55:3.0f}%")

    if stats.gear_changes:
        out.append("─" * W)
        out.append(" 最近换挡（观察档位是否逐档变化、换挡耗时多久）")
        for ln in stats.gear_changes:
            out.append("   " + ln)

    warns = list(frame.warnings)
    if stats.warn_counts:
        extra = "；".join(f"{k}×{c}" for k, c in stats.warn_counts.most_common(3))
        if not warns:
            warns = [f"历史告警：{extra}"]

    out.append("─" * W)
    if warns:
        for i, w in enumerate(warns[:3]):
            out.append((" ⚠ " if i == 0 else "   ") + w)
    else:
        out.append(" ✓ 所有数值通过合理性检查（偏移表与实测一致）")
    if extra_note:
        out.append("   " + extra_note)
    out.append("═" * W)
    return out


HELP_NO_DATA = """收不到数据时，按顺序检查：
  1) 游戏里：设置 > 玩法与 HUD > UDP 竞速遥测 > Data Out 打开
  2) 同一页：IP 填 127.0.0.1，端口填 {port}（必须和这里一致）
  3) 同一页：Packet Format 选 Dash（不是 Sled）
  4) 必须已经"上赛道"——菜单、暂停、加载界面都不发数据
  5) 微软商店版若仍收不到：把游戏里的 IP 改成你电脑的局域网 IP（如 192.168.x.x），
     本工具的监听地址默认 0.0.0.0 可以直接收到；再不行才需要回环豁免
  6) 防火墙：首次运行允许"专用网络"访问"""


# ══════════════════════════════════════════════════════════════════════════
# 第 6 部分：主流程
# ══════════════════════════════════════════════════════════════════════════

def print_offsets(layouts: dict[int, Layout]) -> None:
    print("按官方字段顺序自动累加得到的偏移表")
    print("（偏移 = 该字段第一个字节在包里的位置，从 0 开始数）\n")
    for size, layout in sorted(layouts.items(), reverse=True):
        print(f"■ {size} 字节 — {layout.name}")
        if layout.dash_off is not None:
            print(f"  Sled: 0..{layout.sled_size - 1}   Dash: {layout.dash_off}.."
                  f"{layout.dash_off + layout.dash_size - 1}", end="")
            if layout.ext_off is not None:
                print(f"   扩展: {layout.ext_off}..{size - 1}")
            else:
                print()
        else:
            print(f"  Sled: 0..{layout.sled_size - 1}")
        for name, fmt, off in layout.fields:
            mark = ""
            if name in ("speed", "power", "torque", "accel", "brake", "clutch", "gear",
                        "current_engine_rpm", "engine_max_rpm", "is_race_on"):
                mark = "   ← 程序要用的字段"
            print(f"    {off:4d}  {name:22s} {fmt}{mark}")
        print()


def run_live(args, layouts: dict[int, Layout], stats: Stats, recorder: Recorder | None) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # 故意不设 SO_REUSEADDR：Windows 上它会让第二个实例静默抢同一端口，
    # 表现为"界面没数据但其实在跑"，很难排查。这里宁可启动就报错。
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        print(f"\n[启动失败] 无法监听 {args.host}:{args.port} — {e}")
        print("  常见原因：已经有别的遥测软件（或本工具的另一个窗口）占用了这个端口。")
        print("  处理办法：关掉那个程序，或换一个端口（游戏里也要同步改）。\n")
        raise SystemExit(1)

    sock.settimeout(0.2)
    endpoint = f"{args.host}:{args.port}"
    screen = Screen()

    layout: Layout | None = None
    last_frame: Frame | None = None
    last_gear: int | None = None
    started = time.time()
    last_draw = 0.0
    note = ""

    print(f"已开始监听 {endpoint} ……")
    if args.duration:
        print(f"将在 {args.duration:.0f} 秒后自动结束并打印统计。")
    print("运行中按 Q 键结束，会打印本次统计（档位分布、包率、告警）。")
    if not ANSI_ON:
        print("（当前控制台不支持原地刷新，将以每秒一屏的方式滚动输出）")

    try:
        while True:
            if _stop_key_pressed():
                break
            if args.duration and (time.time() - started) >= args.duration:
                break
            try:
                raw, _addr = sock.recvfrom(2048)
            except socket.timeout:
                raw = None
            except OSError:
                break

            now = time.time()

            if raw is not None:
                if recorder is not None:
                    recorder.write(raw)

                lay = layouts.get(len(raw))
                if lay is None:
                    if len(raw) >= 331:
                        lay = layouts[331]
                    else:
                        stats.on_packet(now, None, len(raw))
                        note = f"收到 {len(raw)} 字节的包，不是已知的 Forza 格式"
                        raw = None

                if raw is not None and lay is not None:
                    if layout is None or layout.key != lay.key:
                        layout = lay
                        note = lay.note
                        if lay.key != "fm8":
                            note = f"⚠ 这不是 FM8 Dash 格式。{lay.note}"
                    frame = parse_frame(raw, lay)
                    stats.on_packet(now, frame, len(raw))
                    if frame is not None:
                        g = int(frame.values.get("gear", 0))
                        if last_gear is not None and g != last_gear:
                            stats.note_gear_change(last_gear, g, frame.values, now)
                        last_gear = g
                        last_frame = frame

            if layout is None:
                layout = layouts[331]
                waiting = True
            else:
                waiting = False

            if now - last_draw >= 0.2:
                last_draw = now
                if waiting or last_frame is None:
                    show_note = HELP_NO_DATA.format(port=args.port) if stats.age(now) > 3.0 else note
                    screen.draw(build_panel(None, layout, stats, now, endpoint, show_note), now)
                else:
                    screen.draw(build_panel(last_frame, layout, stats, now, endpoint, note), now)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass
        elapsed = max(1e-6, time.time() - started)
        screen.finish()
        print_summary(stats, elapsed, args)


def run_replay(args, layouts: dict[int, Layout], stats: Stats) -> None:
    screen = Screen()
    endpoint = f"回放 {os.path.basename(args.replay)}"
    layout: Layout | None = None
    last_frame: Frame | None = None
    last_gear: int | None = None
    t0 = time.perf_counter()
    wall0 = time.time()
    speed = max(0.01, args.speed)
    last_draw = 0.0
    count = 0

    for rel_ms, raw in read_records(args.replay):
        # 用"录制时的时间戳"作为逻辑时钟，这样统计出来的是录制本身的节奏，
        # 而不是回放循环的抖动。
        sim_now = wall0 + (rel_ms / 1000.0) / speed
        while True:
            delay = (t0 + (rel_ms / 1000.0) / speed) - time.perf_counter()
            if delay <= 0:
                break
            time.sleep(min(delay, 0.02))

        lay = layouts.get(len(raw))
        if lay is None:
            stats.on_packet(sim_now, None, len(raw))
            continue
        layout = lay
        frame = parse_frame(raw, lay)
        stats.on_packet(sim_now, frame, len(raw))
        if frame is not None:
            g = int(frame.values.get("gear", 0))
            if last_gear is not None and g != last_gear:
                stats.note_gear_change(last_gear, g, frame.values, sim_now)
            last_gear = g
            last_frame = frame
        count += 1
        if sim_now - last_draw >= 0.2:
            last_draw = sim_now
            if last_frame is not None and layout is not None:
                screen.draw(build_panel(last_frame, layout, stats, sim_now, endpoint), sim_now)

    screen.finish()
    print(f"回放结束：{count} 个包")
    print_summary(stats, 0.0, args)


def print_summary(stats: Stats, elapsed: float, args) -> None:
    print()
    print("─" * 78)
    print(" 本次统计")
    print("─" * 78)
    print(f" 有效包: {stats.total}    解析失败: {stats.parse_fail}")
    if elapsed > 0:
        print(f" 本次运行时长: {elapsed:.1f}s")
    avg, mn, p99 = stats.interval_ms()
    if avg:
        # 用"首个包到最后一个包"的跨度算速率，避免把启动等待时间也算进去
        span = (stats.last_recv - stats.first_recv) if stats.first_recv else 0.0
        rate = (stats.total - 1) / span if span > 0.5 else stats.pps()
        print(f" 包间隔: 平均 {avg:.2f}ms  最小 {mn:.2f}ms  P99 {p99:.2f}ms")
        if rate > 65:
            print(f"         平均 {rate:.1f} 包/秒 —— 明显高于 60，说明 FM8 是"
                  f"跟着帧率发的，不是固定 60Hz")
        elif rate > 45:
            print(f"         平均 {rate:.1f} 包/秒 —— 接近官方文档说的 60 包/秒")
        else:
            print(f"         平均 {rate:.1f} 包/秒 —— 偏低，可能有丢包或端口被抢")

    if stats.dup_ts:
        print(f" 同时间戳重复包: {stats.dup_ts} 个（同一帧发了多个包，会带来突发式到达）")
    if stats.invalid_frames:
        print(f" 发动机未启动帧: {stats.invalid_frames} 个（菜单/加载/暂停；已从档位统计中剔除）")

    if stats.other_sizes:
        print(f" 其它长度: {dict(stats.other_sizes)}")

    if stats.gear_stats:
        print()
        print(" 档位原始值分布（车速/转速只统计发动机已启动的帧，这样才看得出语义）")
        print("   值  标签     总帧    无效    有效   平均车速    平均转速   最高车速")
        for g in sorted(stats.gear_stats):
            st = stats.gear_stats[g]
            if st["valid"]:
                a_s = st["speed"] / st["valid"]
                a_r = st["rpm"] / st["valid"]
            else:
                a_s = a_r = 0.0
            print(f"  {g:3d}  {gear_short(g):>4}  {st['n']:7d}  {st['invalid']:6d}  {st['valid']:6d}"
                  f"  {a_s:7.1f} km/h  {a_r:8.0f}  {st['max_speed']:7.1f}")

    if stats.warn_counts:
        print()
        print(" 告警统计")
        for k, c in stats.warn_counts.most_common(10):
            print(f"   {k}  ×{c}")

    if stats.transitions:
        print()
        print(" 档位跳变序列（回答：11 是不是总是夹在两个相邻挡之间）")
        print("   从 → 到    次数")
        for (a, b), c in stats.transitions.most_common(12):
            print(f"   {gear_short(a):>2} → {gear_short(b):<2}   {c:4d}")

    print()
    print(" 怎么读这张表：")
    print("   · 某个值的『平均车速』明显大于 0 → 它是行驶中的挡位")
    print("   · 平均车速 ≈ 0 且转速 ≈ 0 → 它是静止/未初始化状态，不能当行驶挡用")
    print("   · 『无效』列是发动机没启动的帧（菜单/加载），不计入车速转速")
    print()
    print(" 下一步：把这一屏截图发我，尤其是档位分布和包率两段。")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    p = argparse.ArgumentParser(
        description="FM8 遥测探针（只读，不发送任何输入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python tools/probe.py\n"
            "  python tools/probe.py --port 5555 --record 练习一圈.fm8rec\n"
            "  python tools/probe.py --replay 练习一圈.fm8rec\n"
            "  python tools/probe.py --offsets\n"
        ),
    )
    p.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，即所有网卡）")
    p.add_argument("--port", type=int, default=5300, help="监听端口（默认 5300）")
    p.add_argument("--record", nargs="?", const="auto", metavar="文件",
                   help="把收到的原始包录制成文件；只写 --record 时自动用时间戳命名")
    p.add_argument("--replay", metavar="文件", help="回放之前录制的文件，不监听网络")
    p.add_argument("--speed", type=float, default=1.0, help="回放倍速（默认 1.0）")
    p.add_argument("--duration", type=float, default=0.0,
                   help="监听多少秒后自动结束（默认 0 = 一直监听，按 Q 结束）")
    p.add_argument("--offsets", action="store_true", help="只打印偏移表然后退出")
    args = p.parse_args()

    layouts = build_layouts()

    if args.offsets:
        print_offsets(layouts)
        return

    stats = Stats()

    if args.replay:
        if not os.path.exists(args.replay):
            raise SystemExit(f"文件不存在: {args.replay}")
        run_replay(args, layouts, stats)
        return

    record_path = args.record
    if record_path == "auto":
        record_path = "fm8_" + time.strftime("%Y%m%d_%H%M%S") + ".fm8rec"
    recorder = Recorder(record_path) if record_path else None
    if recorder:
        print(f"开始录制到 {recorder.path}")
    try:
        run_live(args, layouts, stats, recorder)
    finally:
        if recorder:
            recorder.close()
            print(f"录制已保存：{recorder.path}（{recorder.count} 个包）")


if __name__ == "__main__":
    main()
