# -*- coding: utf-8 -*-
"""
合成 FM8 遥测包 —— 用于在没有游戏的情况下测试解析器和决策逻辑。

这是自动化测试的地基：只要我们能自己造出"格式正确的包"，就能在
不开游戏的前提下验证每一个字节的读取是否正确。

用法（手动发一段假数据给探针看效果）：
    python tests/fake_fm8.py --port 5399 --seconds 5
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import time

PACKET_SIZE = 331
DASH = 232


def _pack(buf: bytearray, fmt: str, off: int, *values) -> None:
    struct.pack_into("<" + fmt, buf, off, *values)


def build_frame(
    *,
    gear: int = 3,
    rpm: float = 5200.0,
    max_rpm: float = 8000.0,
    idle_rpm: float = 900.0,
    speed_ms: float = 42.0,
    throttle: int = 200,
    brake: int = 0,
    clutch: int = 0,
    power_w: float = 180_000.0,
    torque_nm: float = 320.0,
    is_race_on: int = 1,
    timestamp_ms: int = 0,
    car_ordinal: int = 3271,
    car_class: int = 5,
    car_pi: int = 745,
    drivetrain: int = 1,
    num_cylinders: int = 6,
    tire_wear: float = 0.02,
    track_ordinal: int = 12,
) -> bytes:
    """造一个 331 字节的 FM8 Dash 包。字段含义与官方文档一一对应。"""
    b = bytearray(PACKET_SIZE)

    # ---- Sled（0..231）----
    _pack(b, "i", 0, is_race_on)
    _pack(b, "I", 4, timestamp_ms)
    _pack(b, "f", 8, max_rpm)
    _pack(b, "f", 12, idle_rpm)
    _pack(b, "f", 16, rpm)
    # 纵向加速度用 -9.8 表示"水平路面"，会触发探针的腾空判断，这里给 0
    _pack(b, "fff", 20, 0.0, 0.0, 0.0)
    # 速度矢量与 speed 必须一致，否则探针会告警
    _pack(b, "fff", 32, 0.0, 0.0, speed_ms)
    _pack(b, "fff", 44, 0.0, 0.0, 0.0)
    _pack(b, "fff", 56, 0.0, 0.0, 0.0)
    _pack(b, "ffff", 68, 0.5, 0.5, 0.5, 0.5)          # 悬挂行程
    _pack(b, "ffff", 84, 0.0, 0.0, 0.0, 0.0)          # 滑移率
    _pack(b, "ffff", 100, 0.0, 0.0, speed_ms / 0.33, speed_ms / 0.33)
    _pack(b, "iiii", 116, 0, 0, 0, 0)                 # 路肩
    _pack(b, "ffff", 132, 0.0, 0.0, 0.0, 0.0)         # 积水
    _pack(b, "ffff", 148, 0.0, 0.0, 0.0, 0.0)         # 路面震动
    _pack(b, "ffff", 164, 0.0, 0.0, 0.0, 0.0)         # 滑移角
    _pack(b, "ffff", 180, 0.0, 0.0, 0.0, 0.0)         # 合成滑移
    _pack(b, "ffff", 196, 0.08, 0.08, 0.08, 0.08)     # 悬挂行程(米)
    _pack(b, "i", 212, car_ordinal)
    _pack(b, "i", 216, car_class)
    _pack(b, "i", 220, car_pi)
    _pack(b, "i", 224, drivetrain)
    _pack(b, "i", 228, num_cylinders)

    # ---- Dash（232..310）----
    _pack(b, "fff", DASH + 0, 100.0, 2.5, -420.0)     # 位置
    _pack(b, "f", DASH + 12, speed_ms)
    _pack(b, "f", DASH + 16, power_w)
    _pack(b, "f", DASH + 20, torque_nm)
    _pack(b, "ffff", DASH + 24, 82.0, 83.0, 79.0, 80.0)   # 胎温
    _pack(b, "f", DASH + 40, 0.8)                      # 涡轮
    _pack(b, "f", DASH + 44, 0.62)                     # 油量
    _pack(b, "f", DASH + 48, 1543.0)                   # 里程
    _pack(b, "f", DASH + 52, 95.2)                     # 最快圈
    _pack(b, "f", DASH + 56, 96.8)                     # 上一圈
    _pack(b, "f", DASH + 60, 41.7)                     # 当前圈
    _pack(b, "f", DASH + 64, 512.3)                    # 总时间
    _pack(b, "H", DASH + 68, 3)                        # 圈数
    _pack(b, "B", DASH + 70, 4)                        # 名次
    _pack(b, "B", DASH + 71, throttle)
    _pack(b, "B", DASH + 72, brake)
    _pack(b, "B", DASH + 73, clutch)
    _pack(b, "B", DASH + 74, 0)
    _pack(b, "B", DASH + 75, gear)
    _pack(b, "b", DASH + 76, 0)
    _pack(b, "b", DASH + 77, 0)
    _pack(b, "b", DASH + 78, 0)

    # ---- FM8 扩展（311..330）----
    _pack(b, "ffff", 311, tire_wear, tire_wear, tire_wear, tire_wear)
    _pack(b, "i", 327, track_ordinal)

    return bytes(b)


def main() -> None:
    ap = argparse.ArgumentParser(description="向本机发送合成的 FM8 遥测包")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5399)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--hz", type=float, default=60.0)
    args = ap.parse_args()

    # Windows 默认的定时器精度约 15.6ms，做不到 60Hz。申请 1ms 精度。
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    period = 1.0 / args.hz
    t0 = time.perf_counter()
    n = 0
    ts = 0

    print(f"向 {args.host}:{args.port} 发送合成包，{args.seconds:.0f} 秒……")
    while True:
        elapsed = time.perf_counter() - t0
        if elapsed > args.seconds:
            break
        ts += int(period * 1000)

        # 造一段"逐渐加速 + 每秒升一挡"的假数据，方便肉眼检查
        gear = min(6, 1 + int(elapsed))
        rpm = 3000 + (elapsed % 1.0) * 4000
        speed = 12 + elapsed * 9
        sock.sendto(
            build_frame(gear=gear, rpm=rpm, speed_ms=speed, throttle=255,
                        power_w=150_000 + rpm * 20, torque_nm=280 + rpm / 40,
                        timestamp_ms=ts),
            (args.host, args.port),
        )
        n += 1
        sleep = period - ((time.perf_counter() - t0) - elapsed)
        if sleep > 0:
            time.sleep(sleep)

    sock.close()
    try:
        import ctypes
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass
    print(f"完成，共发送 {n} 个包。")


if __name__ == "__main__":
    main()
