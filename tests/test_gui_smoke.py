# -*- coding: utf-8 -*-
"""
图形界面的冒烟测试：窗口能建起来，标签会跟着遥测变。

不测"好不好看"——那只有眼睛能判断。这里测的是**接线对不对**：
数据进来之后，挡位、转速、决策、学习这几行是不是真的更新了，
按钮和滑块是不是真的改到了引擎参数上。

用 py -3 运行（工作区自带的 Python 3.13 是精简版，没有 tkinter）：
    py -3 tests/test_gui_smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from math import exp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

try:
    import tkinter as tk
except Exception:                                    # 精简版 Python 没有 tkinter
    # 退出码 77 = "跳过"（automake 的惯例）。不能返回 0——
    # 那会让"没测"和"测过了"在脚本眼里一模一样，等于假装通过。
    print("跳过：这个 Python 没有 tkinter（用 py -3 运行可测）")
    raise SystemExit(77)

from fm8_tcu.gui import App                          # noqa: E402
from fm8_tcu.outputs import Hotkeys                  # noqa: E402
from fm8_tcu.service import ShiftService             # noqa: E402
from fake_fm8 import build_frame                     # noqa: E402

PASS = 0
FAIL = 0
PORT = 5392


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


class FakeSink:
    """记录调用，不真的按键。用来验证"演练 → 实发"切回来确实换了对象。"""

    def __init__(self) -> None:
        self.calls = []
        self.last_error = ""
        self.skipped = 0
        self.clutch_scan = 0x2A          # 假装配了 Shift 当离合
        self.use_clutch = False

    def shift(self, action: str) -> bool:
        self.calls.append(action)
        return True

    def set_clutch(self, on: bool) -> bool:
        self.use_clutch = bool(on)
        return True

    def release_all(self) -> None:
        pass


class RecordingHud:
    """不接 Windows/Tk 的 HUD 探针，用来检查快速通道的调用契约。"""

    def __init__(self) -> None:
        self.calls = []
        self.closed = False

    def set_state(self, s: dict, now: float | None = None) -> None:
        self.calls.append(("set_state", s, now))

    def tick(self, now: float | None = None) -> None:
        self.calls.append(("tick", now))

    def close(self) -> None:
        self.closed = True


def check_fast_hud_path() -> None:
    """不用 sleep 测帧率，只验证调度周期和同一时间戳传递。"""
    class FakeService:
        def __init__(self) -> None:
            self.snapshot_calls = 0

        def snapshot(self) -> dict:
            self.snapshot_calls += 1
            return {"sample": self.snapshot_calls}

    class FakeRoot:
        def __init__(self) -> None:
            self.after_calls = []

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))
            return len(self.after_calls)

    class FakeApp:
        _update_hud = App._update_hud
        _hud_tick = App._hud_tick

        def __init__(self) -> None:
            self.hud = RecordingHud()
            self.svc = FakeService()
            self.root = FakeRoot()

    app = FakeApp()
    app._update_hud(123.25)
    check("HUD 快速通道只取一次快照", app.svc.snapshot_calls == 1,
          str(app.svc.snapshot_calls))
    check("set_state 和 tick 共用时间戳",
          len(app.hud.calls) == 2
          and app.hud.calls[0] == ("set_state", {"sample": 1}, 123.25)
          and app.hud.calls[1] == ("tick", 123.25), str(app.hud.calls))

    app._hud_tick()
    check("HUD 每次只续订一个 16ms 定时器",
          len(app.root.after_calls) == 1 and app.root.after_calls[0][0] == 16,
          str(app.root.after_calls))
    app.hud = None
    snapshots = app.svc.snapshot_calls
    app._update_hud(200.0)
    check("HUD 关闭后快速通道直接返回", app.svc.snapshot_calls == snapshots,
          str(app.svc.snapshot_calls))


def main() -> None:
    print("图形界面冒烟测试")
    print("-" * 60)
    tmp = tempfile.mkdtemp(prefix="fm8gui_")
    svc = ShiftService(sink=FakeSink(), learn=True,
                       profile_dir=os.path.join(tmp, "profiles"),
                       log_path=os.path.join(tmp, "d.csv"))
    svc.open("127.0.0.1", PORT)

    root = tk.Tk()
    app = App(root, svc, Hotkeys(), "", prefs_path=os.path.join(tmp, "ui.json"))
    root.update()

    check("窗口已显示", bool(root.winfo_ismapped()), root.winfo_geometry())
    check("初始提示等待遥测", "等待" in app.status.cget("text"), app.status.cget("text"))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(build_frame(gear=3, rpm=7600, speed_ms=42.0, throttle=255,
                            timestamp_ms=1000), ("127.0.0.1", PORT))
    time.sleep(0.1)
    svc.poll()
    app._update_ui()
    root.update()

    check("挡位显示 3", app.gear.cget("text").strip() == "3", app.gear.cget("text"))
    check("转速有数字", "7600" in app.rpm.cget("text"), app.rpm.cget("text"))
    check("车速有数字", "151" in app.speed.cget("text"), app.speed.cget("text"))
    check("状态变在线", "在线" in app.status.cget("text"), app.status.cget("text"))
    check("决策里有 UP", "UP" in app.decision.cget("text"), app.decision.cget("text"))
    check("识别到车号", "car" in app.car.cget("text"), app.car.cget("text"))

    app.toggle()
    check("按钮切到停用", "停用" in app.btn_toggle.cget("text"),
          app.btn_toggle.cget("text"))
    check("服务已启用", svc.enabled)

    # ---- 手动离合开关 ----
    check("离合复选框可用", str(app.chk_clutch.cget("state")) == "normal",
          str(app.chk_clutch.cget("state")))
    app.clutch_var.set(True)
    app._on_clutch()
    check("勾选后输出层真的开了", svc.snapshot()["clutch"] and svc.sink.use_clutch)

    # 勾完就该落盘。否则被任务管理器结束/断电时，下次启动会丢掉这个开关。
    import json as _json
    prefs_file = os.path.join(tmp, "ui.json")
    saved = (_json.load(open(prefs_file, encoding="utf-8"))
             if os.path.exists(prefs_file) else {})
    check("勾选离合会立刻存偏好", saved.get("clutch") is True, str(saved))
    check("提示说明前提", "手动带离合" in app.hint.cget("text"), app.hint.cget("text"))
    app.clutch_var.set(False)
    app._on_clutch()
    check("取消后关掉", not svc.snapshot()["clutch"])

    # ---- F10 热键 ----
    from fm8_tcu.outputs import VK_F10, VK_F11

    class FakeKeys:
        def __init__(self, hit):
            self.hit = hit

        def pressed(self, vk):
            return vk == self.hit

    app.keys = FakeKeys(VK_F10)
    before = svc.snapshot()["clutch"]
    check("F10 不退出主循环", app._handle_hotkeys() is True)
    check("F10 切换手动离合", svc.snapshot()["clutch"] != before,
          str(svc.snapshot()["clutch"]))
    app.keys = FakeKeys(VK_F11)
    enabled_before = svc.enabled
    app._handle_hotkeys()
    check("F11 仍然管用", svc.enabled != enabled_before, str(svc.enabled))
    if svc.enabled != enabled_before:      # 恢复原状，后面的用例依赖它
        app.toggle()
    app.keys = FakeKeys(-1)                # 别让定时器把 F11 当成一直按着

    # ---- 高级参数默认收起 ----
    check("高级参数默认收起", not app._adv_open and not app.adv.winfo_ismapped())
    check("收起时仍显示当前值",
          "89%" in app.btn_adv.cget("text") and "68%" in app.btn_adv.cget("text"),
          app.btn_adv.cget("text"))
    collapsed_h = root.winfo_height()
    app._toggle_advanced()
    root.update()
    check("点开后能看到两个滑块",
          app._adv_open and app.adv.winfo_ismapped()
          and app.up_scale.winfo_ismapped() and app.down_scale.winfo_ismapped())
    check("展开后窗口变高", root.winfo_height() > collapsed_h,
          f"{collapsed_h} → {root.winfo_height()}")

    # 滑块只在展开时可拖（Tk 的 Scale.set 在不映射时不会触发 command），
    # 这也正是用户的真实路径：看不见就拖不到。
    app.up_scale.set(0.85)
    root.update()
    check("滑块改到引擎参数", abs(svc.policy.up_pct_high - 0.85) < 1e-6,
          str(svc.policy.up_pct_high))

    app._toggle_advanced()
    root.update()
    check("再点一次收起", not app._adv_open and not app.adv.winfo_ismapped())
    check("收起后标签显示新值", "85%" in app.btn_adv.cget("text"),
          app.btn_adv.cget("text"))
    check("收起后恢复原高度", root.winfo_height() == collapsed_h,
          f"{root.winfo_height()} vs {collapsed_h}")

    # ---- 停用：只显示判断结果，不按键也不等回执 ----
    app.toggle()                       # 从启用切回停用
    n_calls = len(svc.sink.calls)
    sock.sendto(build_frame(gear=3, rpm=7600, speed_ms=42.0, throttle=255,
                            timestamp_ms=1900), ("127.0.0.1", PORT))
    time.sleep(0.08)
    svc.poll()
    check("停用后不按键", len(svc.sink.calls) == n_calls, str(svc.sink.calls))
    check("停用后不等回执", svc.engine.pending is None, str(svc.engine.pending))
    app.toggle()                       # 恢复启用，后面 HUD 要用
    check("重新启用成功", svc.enabled, str(svc.enabled))

    # ---- 参数保存与回读 ----
    # "保存成功但下次不读"是配置类功能最常见的坑：
    # 路径必须和启动时加载的路径是同一个。
    import fm8_tcu.gui as gui_mod
    old_default = gui_mod.DEFAULT_PARAMS
    gui_mod.DEFAULT_PARAMS = os.path.join(tmp, "params.json")
    try:
        app.params_path = ""
        app.save_params()
        check("保存到 exe 旁的 params.json", os.path.exists(gui_mod.DEFAULT_PARAMS),
              gui_mod.DEFAULT_PARAMS)
        from fm8_tcu.config import load_policy
        saved, _notes = load_policy(gui_mod.DEFAULT_PARAMS)
        check("存进去的参数能读回来", abs(saved.up_pct_high - 0.85) < 1e-6,
              str(saved.up_pct_high))
    finally:
        gui_mod.DEFAULT_PARAMS = old_default

    # ---- HUD ----
    app.hud_var.set(True)
    app._toggle_hud()
    root.update()
    check("HUD 已弹出", app.hud is not None)
    if app.hud is not None:
        h = app.hud
        app._update_hud(time.monotonic())
        root.update()
        check("HUD 挡位跟着变", h.gear.cget("text").strip() == "3",
              h.gear.cget("text"))
        check("HUD 有转速", "7600" in h.rpm.cget("text"), h.rpm.cget("text"))
        check("HUD 有决策", "UP" in h.decision.cget("text"), h.decision.cget("text"))
        check("HUD 有学习概况", "齿比" in h.learning.cget("text"),
              h.learning.cget("text"))

        # 降挡帧也必须能进 HUD。v0.9.0 漏定义了 YELLOW，DOWN 一出现就
        # NameError，HUD 被当成"出错"关掉；而测试只喂过 UP 帧。
        h.set_state(dict(svc.snapshot(), gear=3, decision="DOWN 刹车 100%，转速仅 55%",
                         throttle=0.0, brake=1.0, usable=True, has_data=True,
                         stale=False), now=time.monotonic())
        check("HUD 能显示降挡决策", "DOWN" in h.decision.cget("text"),
              h.decision.cget("text"))
        check("降挡决策是黄色", h.decision.cget("fg") == "#e3b341",
              h.decision.cget("fg"))

        # ---- 分段转速条 ----
        from fm8_tcu.hud import FLASH_HZ, LED_COUNT, LED_FLASH, LED_OFF

        def fills() -> list:
            return [h.rpm_bar.itemcget(i, "fill") for i in h._leds]

        def lit() -> int:
            return sum(1 for f in fills() if f != LED_OFF)

        check("转速条有 15 个灯", len(h._leds) == LED_COUNT, str(len(h._leds)))
        check("转速条宽度没溢出", h.rpm_bar.winfo_width() <= 356 - 20,
              str(h.rpm_bar.winfo_width()))
        check("灯色分三段（绿黄红）",
              len({h._led_colors[0], h._led_colors[7], h._led_colors[11]}) == 3,
              str(h._led_colors))

        base = dict(svc.snapshot(), gear=3, rpm=7600.0, has_data=True,
                    usable=True, stale=False, enabled=True)
        lim = 0.919

        # 灯条满格 = min(断油点, 满油门升挡点)。测试按"相对满格"给比例，
        # 这样断言的是灯条自身的几何关系，不受标定基准变化影响。
        up_max = base["up_max"]
        span = min(lim, up_max) if up_max > 0.3 else lim

        def rpm_state(frac: float, limiter: float = lim, **extra) -> dict:
            state = dict(base, rpm_pct=frac * span, limiter_pct=limiter,
                         up_pct=up_max, up_max=up_max)
            state.update(extra)
            return state

        # 低于 50% 全灭：低速看数字就够了，灯条亮着反而是噪声
        h.set_state(rpm_state(0.30), now=100.0)
        check("低转速不亮灯", lit() == 0, str(lit()))

        h.set_state(rpm_state(0.60), now=100.0)
        n_low = lit()
        h.set_state(rpm_state(0.85), now=100.0)
        n_high = lit()
        check("转速越高亮的灯越多", 0 < n_low < n_high <= LED_COUNT,
              f"{n_low} → {n_high}")

        # 0.75 是绿黄交界：前 7 个全绿，第 8 个还没亮
        h.set_state(rpm_state(0.75), now=100.0)
        row = fills()
        check("0.75 时 7 个绿灯正好全亮",
              row[:7] == [h._led_colors[0]] * 7 and row[7] == LED_OFF,
              str(row[:9]))

        # 0.90 是黄红交界：前 11 个亮（7 绿 + 4 黄），第 12 个还没亮
        h.set_state(rpm_state(0.90), now=100.0)
        row = fills()
        check("0.90 时黄灯也满了",
              row[6] == h._led_colors[6] and row[10] == h._led_colors[7]
              and row[11] == LED_OFF, str(row[6:12]))

        h.set_state(rpm_state(0.965), now=100.0)
        check("0.965 时全红且尚未闪烁", fills() == h._led_colors and h._bar[0] == "level",
              str(fills()))
        h.set_state(rpm_state(0.98 - 1e-6), now=100.0)
        check("0.98 前仍保持全红", fills() == h._led_colors and h._bar[0] == "level",
              h._bar[0])

        # ---- 真实使用路径：升挡点前必须看得见红区 ----
        # 按断油点标定的话，红区（frac 0.90）落在升挡点之后，而自动换挡
        # 在升挡点就切走转速——灯条永远到不了红色。这里按真实参数验证：
        # limiter 0.919、满油门升挡点 0.89。
        real_lim, real_up = 0.919, 0.89
        h.set_state(dict(base, rpm_pct=real_up - 0.045, limiter_pct=real_lim,
                         up_pct=real_up, up_max=real_up), now=100.0)
        check("升挡前已亮红灯",
              any(f == h._led_colors[11] for f in fills()), str(fills()[11:]))
        check("升挡前还没闪", h._bar[0] == "level", h._bar[0])

        h.set_state(dict(base, rpm_pct=real_up, limiter_pct=real_lim,
                         up_pct=real_up, up_max=real_up), now=100.0)
        check("到升挡点整条闪", h._bar[0] == "flash", h._bar[0])

        # 断油比升挡点还早的改装车：满格按断油算，不能反过来拉伸
        h.set_state(dict(base, rpm_pct=0.80, limiter_pct=0.80,
                         up_pct=0.89, up_max=0.89), now=100.0)
        check("断油早于升挡点时按断油满格", h._bar[0] == "flash", h._bar[0])

        # 8Hz：100.000 亮、100.0625 灭、100.125 再亮，所有灯始终同步。
        check("闪烁频率配置为 8Hz", FLASH_HZ == 8, str(FLASH_HZ))
        h.set_state(rpm_state(0.98), now=100.0)
        a = fills()
        h._draw_bar(now=100.0625)
        b = fills()
        h._draw_bar(now=100.125)
        c = fills()
        check("8Hz 半周期整条同步亮灭",
              all(f == LED_FLASH for f in a) and all(f == LED_OFF for f in b)
              and all(f == LED_FLASH for f in c),
              f"a={a[:3]} b={b[:3]} c={c[:3]}")

        # 没有可靠断油点时回退到名义红线百分比。
        h.set_state(rpm_state(0.75, limiter=0.0), now=100.0)
        row = fills()
        check("无断油估计时按名义百分比点亮",
              row[:7] == [h._led_colors[0]] * 7 and row[7] == LED_OFF, str(row[:9]))
        h.set_state(rpm_state(0.98, limiter=0.5), now=100.0)
        check("断油估计无效时仍能进入闪烁",
              h._bar[0] == "flash" and all(f == LED_FLASH for f in fills()), h._bar[0])

        # ---- RPM 数字：首帧直显、40ms 时间常数、同时间不重复积分 ----
        h.set_state(rpm_state(0.60, rpm=6000.0), now=200.0)
        check("首个 RPM 样本直接显示", h.rpm.cget("text") == "6000 RPM",
              h.rpm.cget("text"))
        h.set_state(rpm_state(0.99, rpm=8000.0), now=200.0)
        check("数字未追上时灯条已即时闪烁",
              h.rpm.cget("text") == "6000 RPM" and h._bar[0] == "flash"
              and all(f == LED_FLASH for f in fills()), h.rpm.cget("text"))
        h._advance_rpm(200.040)
        expected = 6000.0 + 2000.0 * (1.0 - exp(-1.0))
        check("RPM 指数平滑按 40ms 时间常数推进",
              h._rpm_value is not None and abs(h._rpm_value - expected) < 1e-6
              and h.rpm.cget("text") == f"{expected:.0f} RPM",
              f"value={h._rpm_value} text={h.rpm.cget('text')}")
        smoothed = h._rpm_value
        h._advance_rpm(200.040)
        check("同一时间戳不会重复平滑", h._rpm_value == smoothed,
              f"{smoothed} -> {h._rpm_value}")

        invalid_states = (
            ("无数据", {"has_data": False}),
            ("菜单帧", {"usable": False}),
            ("遥测断流", {"stale": True}),
            ("换挡中", {"gear": 11}),
        )
        for name, extra in invalid_states:
            h.set_state(rpm_state(0.99, rpm=8100.0, **extra), now=200.080)
            check(f"{name}清空 RPM 并灭灯",
                  h.rpm.cget("text") == "—" and h._rpm_target is None
                  and h._rpm_value is None and h._rpm_at is None and lit() == 0,
                  f"text={h.rpm.cget('text')} lit={lit()}")
        h.set_state(rpm_state(0.80, rpm=8100.0), now=200.090)
        check("失效恢复后首帧直接显示新 RPM",
              h.rpm.cget("text") == "8100 RPM" and h._rpm_value == 8100.0,
              h.rpm.cget("text"))

        # 透明度：要的是「底板可调、文字始终不透明」。
        # 只调 -alpha 会把字一起变淡，所以 HUD 拆成两层：
        #   底板 panel = LWA_ALPHA（可调）
        #   文字 text  = LWA_COLORKEY（抠掉 #000000，alpha 永远 255）
        app.hud_scale.set(0.6)
        root.update()
        check("底板透明度可调", abs(h.alpha - 0.6) < 1e-6, str(h.alpha))

        from ctypes import byref, wintypes
        from fm8_tcu.overlay import user32

        def layered(hwnd):
            color, al, fl = wintypes.DWORD(), wintypes.BYTE(), wintypes.DWORD()
            user32.GetLayeredWindowAttributes(hwnd, byref(color), byref(al),
                                              byref(fl))
            return color.value, (al.value & 0xFF), fl.value

        _, panel_a, panel_f = layered(h._hwnd_panel)
        _, text_a, text_f = layered(h._hwnd_text)
        check("底板用 alpha 调透明", abs(panel_a - 153) <= 2 and (panel_f & 0x2),
              f"alpha={panel_a} flags={hex(panel_f)}")
        check("文字层不跟着变淡", text_a == 255, str(text_a))
        check("文字层用色键抠背景", bool(text_f & 0x1), hex(text_f))
        check("文字窗口抠的是纯黑", h.text.attributes("-transparentcolor") == "#000000",
              str(h.text.attributes("-transparentcolor")))

        app.hud_scale.set(0.1)              # 低于下限要被夹住
        root.update()
        check("透明度过低被夹到 0.30", abs(h.alpha - 0.30) < 1e-6, str(h.alpha))

        # ---- 鼠标拖动 ----
        app._toggle_move()
        check("进入移动模式",
              app.move_mode and app.btn_move.cget("text").startswith("结束移动"),
              app.btn_move.cget("text"))
        sx, sy = app.hud_pos
        app._update_drag(sx + 20, sy + 20, True)        # 在 HUD 内按下
        app._update_drag(sx + 120, sy + 80, True)       # 拖到别处
        root.update()
        check("拖动后位置正确", app.hud_pos == (sx + 100, sy + 60), str(app.hud_pos))
        check("两层一起动",
              app.hud.panel.winfo_geometry() == app.hud.text.winfo_geometry(),
              f"{app.hud.panel.winfo_geometry()} / {app.hud.text.winfo_geometry()}")
        check("底板真的挪了",
              f"+{sx + 100}+{sy + 60}" in app.hud.panel.winfo_geometry(),
              app.hud.panel.winfo_geometry())

        app._update_drag(sx + 120, sy + 80, False)      # 松手
        saved = json.load(open(os.path.join(tmp, "ui.json"), encoding="utf-8"))
        check("松手后位置写盘", saved.get("hud_pos") == list(app.hud_pos), str(saved))

        app._update_drag(app.hud_pos[0] + 10, app.hud_pos[1] + 10, True)
        app._update_drag(-9999, -9999, True)
        root.update()
        check("拖出屏幕会被夹住", app.hud_pos == (0, 0), str(app.hud_pos))
        app._update_drag(0, 0, False)

        app._toggle_move()
        check("退出移动模式", not app.move_mode
              and app.btn_move.cget("text").startswith("移动 HUD"))
        before = app.hud_pos
        app._update_drag(before[0] + 10, before[1] + 10, True)
        app._update_drag(before[0] + 200, before[1] + 200, True)
        check("非移动模式下拖不动", app.hud_pos == before, str(app.hud_pos))

        # 连续刷新 20 次，确认 HUD 快速通道不会因一次更新出错就被悄悄丢掉。
        for _ in range(20):
            app._update_hud(time.monotonic())
            root.update()
        check("HUD 连续刷新不消失", app.hud is not None, app.last_error)
        check("HUD 更新无错误", not app.last_error, app.last_error)

        from fm8_tcu.overlay import (GWL_EXSTYLE, WS_EX_TOPMOST,
                                     WS_EX_TRANSPARENT, user32)
        style = user32.GetWindowLongW(h._hwnd, GWL_EXSTYLE)
        check("HUD 鼠标穿透", bool(style & WS_EX_TRANSPARENT))
        check("HUD 置顶", bool(style & WS_EX_TOPMOST))
        check("HUD 不进任务栏", bool(style & 0x80))

        app.hud_var.set(False)
        app._toggle_hud()
        root.update()
        check("HUD 可以关掉", app.hud is None)

        probe = RecordingHud()
        app.hud = probe
        app._update_ui()
        check("主窗口 20Hz 更新不重复驱动 HUD", probe.calls == [], str(probe.calls))
        app.hud = None

    check_fast_hud_path()
    svc.close()
    root.destroy()
    sock.close()

    print("-" * 60)
    print(f"通过 {PASS}   失败 {FAIL}")
    raise SystemExit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
