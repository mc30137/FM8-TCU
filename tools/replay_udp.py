# -*- coding: utf-8 -*-
"""
把录制的 .fm8rec 按原始节奏通过 UDP 重放出去。

用途：不开游戏，也能让主程序走完整的实时链路——接收、解析、决策、日志，
连时间节奏都一致。这是"上车前"最有价值的一次检验。

用法：
    python tools/replay_udp.py fm8_20261104_145105.fm8rec --port 5300
    python tools/replay_udp.py xxx.fm8rec --speed 2      # 两倍速
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe import read_records  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="重放录制文件到 UDP")
    ap.add_argument("file")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--loop", action="store_true", help="循环播放")
    args = ap.parse_args()

    if not os.path.exists(args.file):
        raise SystemExit(f"文件不存在：{args.file}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Windows 默认定时器精度 15.6ms，做不到 85 帧/秒。申请 1ms。
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    while True:
        t0 = time.perf_counter()
        n = 0
        for rel_ms, payload in read_records(args.file):
            target = t0 + (rel_ms / 1000.0) / max(args.speed, 0.01)
            while True:
                delay = target - time.perf_counter()
                if delay <= 0:
                    break
                time.sleep(min(delay, 0.01))
            sock.sendto(payload, (args.host, args.port))
            n += 1
        print(f"重放完成：{n} 个包")
        if not args.loop:
            break

    try:
        import ctypes
        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:
        pass
    sock.close()


if __name__ == "__main__":
    main()
