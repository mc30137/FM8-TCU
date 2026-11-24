# -*- coding: utf-8 -*-
"""
按键输出层的时序测试（手动离合）。

为什么单独测：离合换挡能不能用，全在**顺序和时长**上——先按挡位还是先踩离合、
按键在离合接合的那一刻是不是还按着、异常时会不会把键留在按下状态。
这些在真车上没法调试，只能在这里用记录器固定下来。

不碰真键盘：`KeyboardSink` 支持注入 send/sleep，测试里换成记录器。

运行：
    python tests/test_outputs.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from fm8_tcu.outputs import KeyboardSink  # noqa: E402

PASS = 0
FAIL = 0

UP, DOWN, CLUTCH = 0x49, 0x47, 0x2A


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


class Recorder:
    """记下每一次按键和每一次 sleep，用来还原真实时序。"""

    def __init__(self, fail_on=None):
        self.events: list[tuple[int, bool]] = []      # (扫描码, 是否抬起)
        self.sleeps: list[float] = []
        self.fail_on = fail_on or (-1, None)          # (扫描码, up)

    def send(self, scan: int, up: bool) -> bool:
        if (scan, up) == self.fail_on:
            return False
        self.events.append((scan, up))
        return True

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 4))

    def sink(self, **kw) -> KeyboardSink:
        kw.setdefault("hold_ms", 60)
        return KeyboardSink(self.up_scan, self.down_scan, require_game=False,
                            send=self.send, sleep=self.sleep, **kw)

    up_scan = UP
    down_scan = DOWN


def test_plain_shift() -> None:
    """不踩离合时保持原样：按一下挡位键，按住 hold，抬起。"""
    r = Recorder()
    sock = r.sink(clutch_scan=CLUTCH)
    ok = sock.shift("UP")
    check("普通模式发送成功", ok)
    check("普通模式只动挡位键", r.events == [(UP, False), (UP, True)], str(r.events))
    check("普通模式按住 hold", r.sleeps == [0.06], str(r.sleeps))
    check("普通模式不启用离合", not sock.use_clutch)
    check("下行也发对键",
          sock.shift("DOWN") and r.events[-2:] == [(DOWN, False), (DOWN, True)],
          str(r.events[-2:]))


def test_clutch_sequence() -> None:
    """手动离合：挡位→离合→松离合→松挡位，总时长仍是 60ms。"""
    r = Recorder()
    sock = r.sink(clutch_scan=CLUTCH, use_clutch=True)
    ok = sock.shift("UP")
    check("离合模式发送成功", ok)
    check("顺序：挡位按下 → 离合按下 → 离合抬起 → 挡位抬起",
          r.events == [(UP, False), (CLUTCH, False), (CLUTCH, True), (UP, True)],
          str(r.events))
    check("时序 10/40/10", r.sleeps == [0.01, 0.04, 0.01], str(r.sleeps))
    check("总按住时长没变（延迟不变）", abs(sum(r.sleeps) - 0.06) < 1e-9, str(sum(r.sleeps)))


def test_clutch_hold_too_long() -> None:
    """离合按住时长不能吃掉整个 hold，否则挡位键一按就松。"""
    r = Recorder()
    sock = r.sink(hold_ms=30, clutch_scan=CLUTCH, clutch_hold_ms=40, use_clutch=True)
    sock.shift("UP")
    check("离合时长被夹到 hold 以内", sum(r.sleeps) <= 0.03 + 1e-9, str(r.sleeps))
    check("仍有前后两段", len(r.sleeps) == 3 and all(s >= 0 for s in r.sleeps), str(r.sleeps))


def test_clutch_failure_releases_keys() -> None:
    """离合键发不出去时，挡位键必须被抬起来，不能留在按下状态。"""
    r = Recorder(fail_on=(CLUTCH, False))
    sock = r.sink(clutch_scan=CLUTCH, use_clutch=True)
    ok = sock.shift("UP")
    check("失败时返回 False", not ok)
    check("挡位键已抬起", r.events and r.events[-1] == (UP, True), str(r.events))
    check("给出了原因", "离合" in sock.last_error, sock.last_error)


def test_release_all_includes_clutch() -> None:
    """退出时离合也要抬——不然下次进游戏是踩着离合的状态。"""
    r = Recorder()
    sock = r.sink(clutch_scan=CLUTCH, use_clutch=True)
    sock.release_all()
    scans = [s for s, up in r.events if up]
    check("三个键都抬起", scans == [UP, DOWN, CLUTCH], str(r.events))
    check("抬起顺序先挡位后离合", [s for s, _ in r.events][-1] == CLUTCH, str(r.events))


def test_set_clutch() -> None:
    """运行时开关：没有离合键时拒绝，有键时生效。"""
    r = Recorder()
    no_key = r.sink()
    check("没有离合键时开不了", not no_key.set_clutch(True) and not no_key.use_clutch)

    sock = r.sink(clutch_scan=CLUTCH)
    check("有离合键时能开", sock.set_clutch(True) and sock.use_clutch)
    check("也能关", sock.set_clutch(False) and not sock.use_clutch)

    sock.set_clutch(True)
    sock.shift("UP")
    check("开关真的影响发键", (CLUTCH, False) in r.events, str(r.events))


def main() -> None:
    print("按键输出测试（手动离合）")
    print("-" * 60)
    for fn in (test_plain_shift,
               test_clutch_sequence,
               test_clutch_hold_too_long,
               test_clutch_failure_releases_keys,
               test_release_all_includes_clutch,
               test_set_clutch):
        fn()
    print("-" * 60)
    print(f"通过 {PASS}   失败 {FAIL}")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
