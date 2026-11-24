# -*- coding: utf-8 -*-
"""
`service.py` 的测试：收包 → 去重 → 学习 → 决策 → 发送 → 记日志。

为什么单独测它：命令行版和图形界面版都只是这层的外壳。
这层错了，两个界面会一起错；这层对了，界面换十次也不影响行为。

用真的 UDP 回环发数据，不 mock socket —— 端口、去重、缓冲这些
只有真发包才能验证。

运行：
    python tests/test_service.py
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import socket
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fm8_tcu.engine import Policy                    # noqa: E402
from fm8_tcu.outputs import KeyboardSink             # noqa: E402
from fm8_tcu.engine import ShiftRecord              # noqa: E402
from fm8_tcu.service import ShiftService             # noqa: E402
from fake_fm8 import build_frame                     # noqa: E402

PASS = 0
FAIL = 0
PORT = 5391


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


class FakeSink:
    """记录被调用的次数，不真的按键。fail=True 用来模拟 SendInput 失败。"""

    def __init__(self) -> None:
        self.calls = []
        self.last_error = ""
        self.fail = False

    def shift(self, action: str) -> bool:
        self.calls.append(action)
        if self.fail:
            self.last_error = "模拟发送失败"
            return False
        return True

    def release_all(self) -> None:
        pass


def send(port: int, **kw) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(build_frame(**kw), ("127.0.0.1", port))
    finally:
        sock.close()


def main() -> None:
    print("服务层测试")
    print("-" * 60)
    tmp = tempfile.mkdtemp(prefix="fm8svc_")
    log_path = os.path.join(tmp, "decisions.csv")
    sink = FakeSink()
    svc = ShiftService(Policy(), sink=sink, learn=True,
                       profile_dir=os.path.join(tmp, "profiles"),
                       log_path=log_path)
    try:
        svc.open("127.0.0.1", PORT)

        # ---- 来源白名单：默认只收本机，放开必须点名 IP ----
        check("默认收本机", svc._source_ok(("127.0.0.1", 1234)))
        check("默认拒外机", not svc._source_ok(("192.168.1.9", 1234)))
        svc.allow_from = {"192.168.1.9"}
        check("白名单 IP 放行", svc._source_ok(("192.168.1.9", 1234)))
        check("白名单不影响本机", svc._source_ok(("127.0.0.1", 1234)))
        check("白名单之外仍拒绝", not svc._source_ok(("192.168.1.10", 1234)))
        svc.allow_from = set()

        # 命令行传进来的是字符串，逗号/分号都能分
        svc3 = ShiftService(Policy(), sink=FakeSink(), learn=False,
                            profile_dir=os.path.join(tmp, "profiles"))
        try:
            svc3.open("127.0.0.1", PORT + 2, allow_from="192.168.1.9; 10.0.0.7")
            check("白名单解析逗号/分号",
                  svc3.allow_from == {"192.168.1.9", "10.0.0.7"},
                  str(svc3.allow_from))
            check("没有一键放开", not svc3.allow_any)
        finally:
            svc3.close()

        # ---- 收包与去重 ----
        send(PORT, gear=3, rpm=5000, timestamp_ms=1000)
        send(PORT, gear=3, rpm=5000, timestamp_ms=1000)     # 同时间戳：重复帧
        send(PORT, gear=3, rpm=5000, timestamp_ms=1012)
        svc.poll()
        check("同一时间戳的重复包被丢掉", svc.frames == 2, str(svc.frames))
        check("快照有数据", svc.snapshot()["has_data"])

        # ---- 未启用时不发指令 ----
        send(PORT, gear=3, rpm=8100, speed_ms=42.0, throttle=255, timestamp_ms=1100)
        svc.poll()
        check("未启用时不按键", not sink.calls, str(sink.calls))
        check("但决策照样算", "UP" in svc.last_text, svc.last_text)

        # ---- 启用后按键 ----
        svc.toggle()
        send(PORT, gear=3, rpm=8100, speed_ms=42.0, throttle=255, timestamp_ms=1200)
        svc.poll()
        check("启用后发出升挡", sink.calls == ["UP"], str(sink.calls))
        check("进入等待确认", svc.engine.pending is not None)

        # ---- 档位变化 → 结算并写日志 ----
        time.sleep(0.05)
        send(PORT, gear=4, rpm=6500, speed_ms=48.0, throttle=255, timestamp_ms=1300)
        svc.poll()
        svc.poll()
        check("档位变化算成功", svc.engine.history and svc.engine.history[-1].ok,
              str(svc.engine.history[-1:]))
        rows = list(csv.reader(open(log_path, encoding="utf-8")))
        header = rows[0]
        body = rows[1:]
        check("日志有表头", header[:2] == ["时间s", "动作"], str(header))
        check("日志里有这次升挡",
              any(r[1] == "UP" and r[3] == "成功" for r in body), str(body[-2:]))
        check("日志原因带车速转速",
              any("km/h" in r[5] and "转" in r[5] for r in body if r[1] == "UP"),
              str([r[5] for r in body if r[1] == "UP"]))

        # ---- 换挡中(11) 一律不动 ----
        n_before = len(sink.calls)
        send(PORT, gear=11, rpm=8000, speed_ms=48.0, throttle=255, timestamp_ms=1400)
        svc.poll()
        check("换挡中不发指令", len(sink.calls) == n_before, str(sink.calls))

        # ---- 坏包：数值不合法 ----
        n_bad = svc.bad_packets
        send(PORT, gear=3, rpm=float("nan"), timestamp_ms=1450)
        svc.poll()
        check("NaN 帧被丢弃并计数", svc.bad_packets == n_bad + 1, str(svc.bad_packets))

        # ---- 遥测断流 ----
        # 只断言 stale() 这个显示标志是不够的——那正是阶段 1 批评参考项目
        # 的写法（is_live 只用于 UI）。真正要保证的是"断流时不发键"。
        svc.last_frame_t = time.monotonic() - 1.0
        check("断流被识别", svc.stale())

        svc.set_enabled(True)
        svc.engine.pending = (time.monotonic(), 4, "UP", "test")
        trips = svc.watchdog_trips
        sink.calls.clear()
        send(PORT, gear=3, rpm=8300, speed_ms=42.0, throttle=255, timestamp_ms=1460)
        svc.poll()
        check("断流时作废未确认指令", svc.engine.pending is None)
        check("看门狗计数 +1", svc.watchdog_trips == trips + 1, str(svc.watchdog_trips))
        check("断流恢复的第一帧不发键", not sink.calls, str(sink.calls))

        # ---- 停用状态下不按键、也不等回执 ----
        # 这是原来"演练模式"真正解决的问题：没人按键的话，等回执只会
        # 让决策卡在"等待上一条确认"里，0.8 秒才动一次。
        svc.set_enabled(False)
        n_before = len(sink.calls)
        n_hist = len(svc.engine.history)
        svc.engine.pending = None
        send(PORT, gear=4, rpm=8100, speed_ms=48.0, throttle=255, timestamp_ms=1500)
        svc.poll()
        check("停用时不碰真按键", len(sink.calls) == n_before, str(sink.calls))
        check("停用时不设 pending", svc.engine.pending is None,
              str(svc.engine.pending))
        # 比长度，不是比"空"——history 里还有前面那些真实换挡的记录
        check("停用时不产生新记录", len(svc.engine.history) == n_hist,
              str(svc.engine.history[-1:]))
        svc.set_enabled(True)

        # ---- 换挡经过 gear=11 仍能结算 ----
        # 这是真实换挡的必经路径（录制里 15/15 次都经过 11）。曾经把 11 当成
        # 无效帧作废 pending，导致回执、延迟、成功率、顶挡推断全部失效。
        n_hist = len(svc.engine.history)
        svc.engine.pending = None
        svc.engine.last_shift_t = -1e9
        send(PORT, gear=3, rpm=8200, speed_ms=42.0, throttle=255, timestamp_ms=2000)
        svc.poll()
        check("发出升挡并进入等待", svc.engine.pending is not None)

        send(PORT, gear=11, rpm=7600, speed_ms=43.0, throttle=255, timestamp_ms=2012)
        svc.poll()
        check("11 帧保持等待", svc.engine.pending is not None, str(svc.engine.pending))
        check("11 帧不结算", len(svc.engine.history) == n_hist,
              str(len(svc.engine.history)))

        send(PORT, gear=4, rpm=6400, speed_ms=48.0, throttle=255, timestamp_ms=2024)
        svc.poll()
        check("经过 11 后结算成功",
              len(svc.engine.history) == n_hist + 1 and svc.engine.history[-1].ok,
              str(svc.engine.history[-1:]))
        # 两帧在同一个 poll 里处理，wall clock 间隔接近 0，所以只验字段类型。
        # 真实使用中帧间隔 12ms，延迟自然非零。
        check("延迟字段是整数毫秒",
              isinstance(svc.engine.history[-1].latency_ms, int),
              str(svc.engine.history[-1].latency_ms))

        # ---- 断流恢复首帧：不发键，也不留 pending ----
        svc.engine.pending = None
        svc.engine.last_shift_t = -1e9
        n_hist = len(svc.engine.history)
        n_calls = len(sink.calls)
        svc.last_frame_t = time.monotonic() - 1.0
        send(PORT, gear=4, rpm=8300, speed_ms=48.0, throttle=255, timestamp_ms=2100)
        svc.poll()
        check("断流首帧不发键", len(sink.calls) == n_calls, str(sink.calls))
        check("断流首帧不留 pending", svc.engine.pending is None,
              str(svc.engine.pending))
        check("断流首帧不产生记录", len(svc.engine.history) == n_hist,
              str(svc.engine.history[-1:]))

        send(PORT, gear=4, rpm=8300, speed_ms=48.0, throttle=255, timestamp_ms=2112)
        svc.poll()
        check("断流恢复后下一帧恢复正常", "pending" not in svc.last_text,
              svc.last_text)

        # ---- 发送失败：回滚，不留 pending、不启动冷却 ----
        svc.engine.pending = None
        svc.engine.last_shift_t = -1e9
        n_hist = len(svc.engine.history)
        sink.fail = True
        send(PORT, gear=4, rpm=8300, speed_ms=48.0, throttle=255, timestamp_ms=2200)
        svc.poll()
        check("发送失败不留 pending", svc.engine.pending is None,
              str(svc.engine.pending))
        check("发送失败不产生记录", len(svc.engine.history) == n_hist,
              str(svc.engine.history[-1:]))
        check("发送失败会提示", "发送失败" in svc.last_text, svc.last_text)
        sink.fail = False

        # ---- 学习档案 ----
        # 多喂几帧让齿比攒够 5 个样本（少于 5 个不算中位数，会被跳过）
        for i in range(8):
            send(PORT, gear=3, rpm=3000 + i * 10, timestamp_ms=1600 + i * 12)
        svc.poll()
        check("识别到车辆", svc.car_key.startswith("car"), svc.car_key)
        check("保存档案成功", svc.save_now())
        profile = os.path.join(tmp, "profiles", svc.car_key + ".json")
        check("档案文件存在", os.path.exists(profile), profile)
        data = json.load(open(profile, encoding="utf-8"))
        check("档案里有齿比", bool(data.get("ratios")), str(data.get("ratios")))

        # ---- 换车：引擎状态要跟着换 ----
        svc.engine._top_guess = (6, time.monotonic())
        svc.engine.last_action = "UP"
        svc.engine.history.append(ShiftRecord(0.0, "UP", 6, True, 90, "旧车的记录"))
        send(PORT, gear=3, rpm=4000, timestamp_ms=1700, car_ordinal=8888)
        svc.poll()
        check("换车后档案键变了", svc.car_key == "car08888", svc.car_key)
        check("换车清掉顶挡猜测", not svc.engine._top_guess[0],
              str(svc.engine._top_guess))
        check("换车清空换挡历史", not svc.engine.history, str(svc.engine.history))
        check("换车清掉上一条动作", svc.engine.last_action == "", svc.engine.last_action)

        # ---- 手动离合：开关要真的落到输出层，并且改变发键 ----
        events = []
        csink = KeyboardSink(up_scan=0x49, down_scan=0x47, hold_ms=60,
                             require_game=False, clutch_scan=0x2A,
                             send=lambda sc, up: events.append((sc, up)) or True,
                             sleep=lambda _s: None)
        csvc = ShiftService(Policy(), sink=csink, learn=False,
                            profile_dir=os.path.join(tmp, "p3"), log_path="")
        try:
            csvc.open("127.0.0.1", PORT + 3)
            check("默认不开手动离合", csvc.snapshot()["clutch"] is False)
            check("识别到离合键", csvc.snapshot()["clutch_key"] is True)
            check("开关落到输出层", csvc.set_clutch(True) and csvc.snapshot()["clutch"])
            csvc.set_enabled(True)
            send(PORT + 3, gear=3, rpm=8200, speed_ms=42.0, throttle=255,
                 timestamp_ms=2000)
            csvc.poll()
            check("换挡序列里有离合按下",
                  events == [(0x49, False), (0x2A, False), (0x2A, True), (0x49, True)],
                  str(events))
        finally:
            csvc.close()

        # ---- 旧格式日志要轮转，不能把两种表头混在一起 ----
        old_log = os.path.join(tmp, "old.csv")
        with open(old_log, "w", encoding="utf-8") as f:
            f.write("旧表头\n")
        svc2 = ShiftService(Policy(), sink=FakeSink(), learn=False,
                            profile_dir=os.path.join(tmp, "p2"), log_path=old_log)
        try:
            svc2.open("127.0.0.1", PORT + 1)
        finally:
            svc2.close()
        check("旧格式日志被轮转", os.path.exists(old_log + ".1"), old_log)
        check("新日志有新的表头",
              open(old_log, encoding="utf-8").readline().startswith("时间s,动作"),
              open(old_log, encoding="utf-8").readline())
    finally:
        svc.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print(f"通过 {PASS}   失败 {FAIL}")
    raise SystemExit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
