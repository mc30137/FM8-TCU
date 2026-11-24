# -*- coding: utf-8 -*-
"""
游戏内 HUD：文字 100% 不透明，黑色底板的不透明度可调。

为什么是**两个窗口**
--------------------
Windows 的 `-alpha` 作用在整个窗口上，一调透明度连字一起变淡；
`-transparentcolor`（色键）则只能整块抠掉，没有"半透明"。

要同时做到「字不透明 + 底板半透明」，只能分层：

    底层 panel：纯黑矩形，`-alpha` 随滑杆变（0.30~1.00）
    上层 text ：背景色设成 #000000 并交给 `-transparentcolor` 抠掉，
                只剩文字像素可见，永远 100% 不透明

两层都置顶、都鼠标穿透，`text` 压在 `panel` 上面。

⚠️ 注意三个坑：
1. 两层的创建顺序和 z 序要固定，否则可能被对方盖住；
2. 文字窗口的每个控件背景必须正好是 #000000，否则那块会被画成不透明黑块；
3. 独占全屏下任何窗口都画不上去，游戏要改成无边框窗口。
"""

from __future__ import annotations

import time
from math import exp, isfinite

from .overlay import keep_on_top, make_click_through
from .protocol import gear_label

BG = "#0b0b0b"          # 底板颜色（可见的那个深灰）
KEY = "#000000"         # 文字窗口的抠色，必须与所有控件背景一致
FG = "#f0f0f0"
DIM = "#8b949e"
LINE = "#39414d"
GREEN = "#7ee787"
RED = "#ff7b72"
BLUE = "#6cb6ff"
YELLOW = "#e3b341"               # 降挡决策；和 GUI 的黄色保持一致

# -- 转速条（F1 方向盘那种分段 LED）---------------------------------------
LED_COUNT = 15

# 三个颜色区间，比例是**相对断油点**的，不是相对名义红线。
# 为什么用断油点做分母：名义红线 9000 但实际 8271 就断油，如果按 9000 算，
# 最后 8% 永远点不亮，灯条永远差一格。
#
#   0.50 以下    全灭（怠速/低速，看数字就够了）
#   0.50–0.75    绿
#   0.75–0.90    黄
#   0.90–<0.98   红
#   0.98 以上    整条高频闪烁（该升挡了）
LED_BANDS = (
    (0.50, 0.75, "#22e06b"),      # 绿
    (0.75, 0.90, "#ffc233"),      # 黄
    # 0.965 是最后一个红灯的点亮阈值，不是红区的结束位置。
    # 如果最后阈值直接放到 0.98，它会和闪烁同时出现，永远无法稳定亮红灯；
    # 留出 0.965–0.98 这一小段"全红但不闪"，眼睛能看到满格的样子。
    (0.90, 0.965, "#ff3b30"),     # 红
)
# 每段分几个灯。7/4/4 而不是按区间宽度等比——红区只有 0.10 宽，
# 给 4 个灯更醒目，因为它代表"马上就要断油"。
BAND_LAMPS = (7, 4, 4)

LED_OFF = "#1c2028"               # 熄灭的灯：比背景稍亮，能看出灯位
LED_FLASH = "#8b7bff"             # 闪烁色（蓝紫，F1 的方向盘顶灯）
FLASH_FROM = 0.98                 # 到这个比例开始整条闪
FLASH_HZ = 8                      # 每秒 8 个完整亮灭周期
RPM_SMOOTH_TAU_S = 0.040          # 仅数字用 40ms 时间常数轻平滑；灯条始终即时

W, H = 356, 204

# 数字字体优先级。Bahnschrift 是 Win10 自带的 DIN 风格字体，
# 赛车仪表那种窄体数字；没有就退回等宽，再没有就用系统默认。
FONT_THIN = ("Bahnschrift", "Bahnschrift SemiBold", "DIN Alternate",
             "Consolas", "Courier New")


def _bar(value: float, width: int = 12) -> str:
    v = max(0.0, min(1.0, value))
    filled = int(round(v * width))
    return "█" * filled + "░" * (width - filled)


def _led_table() -> list[tuple[float, str]]:
    """每个灯的点亮阈值和颜色。

    阈值按区间分配而不是均匀分：0.50–0.75 放 7 个灯、0.75–0.90 放 4 个、
    0.90–0.965 放 4 个。这样 0.75 时正好 7 个绿灯全亮，0.90 时黄灯也满了——
    颜色切换和数字对得上，不会出现"说好 75% 变黄，结果 73% 就黄了"。
    """
    out: list[tuple[float, str]] = []
    for (lo, hi, color), n in zip(LED_BANDS, BAND_LAMPS):
        for k in range(n):
            out.append((lo + (hi - lo) * (k + 1) / n, color))
    return out


LED_TABLE = _led_table()
assert len(LED_TABLE) == LED_COUNT, "灯数必须和分段表对得上"


class Hud:
    def __init__(self, master=None, x: int = 30, y: int = 30,
                 alpha: float = 0.85) -> None:
        import tkinter as tk

        self._tk = tk
        self._alpha = max(0.30, min(1.0, alpha))
        self._alive = True

        if master is None:
            self._owner = tk.Tk()
            self._owner.withdraw()
            master = self._owner
        else:
            self._owner = None

        # 先建底板，再建文字层：这样文字天然在上面
        self.panel, self._hwnd_panel = self._make_window(master, x, y, panel=True)
        self.text, self._hwnd_text = self._make_window(master, x, y, panel=False)
        self.root = self.text          # 控件都挂在文字层
        self._hwnd = self._hwnd_text   # 兼容旧名字
        self._build()
        self._raise_text()
        self._last_top = 0.0
        self.hint_text = ""            # 移动模式下显示提示，覆盖页脚
        self._rpm_target: float | None = None  # 最新原始 RPM
        self._rpm_value: float | None = None   # 未取整的平滑值
        self._rpm_at: float | None = None      # 上次滤波推进的单调时间

    # -- 窗口 --------------------------------------------------------------
    def _make_window(self, master, x: int, y: int, panel: bool):
        tk = self._tk
        w = tk.Toplevel(master)
        w.withdraw()                     # 先藏起来，配置完再显示
        w.overrideredirect(True)
        w.configure(bg=BG if panel else KEY)
        w.geometry(f"{W}x{H}+{x}+{y}")
        try:
            w.attributes("-topmost", True)
            if panel:
                self._apply_panel_alpha(w)
            else:
                # 把纯黑抠成全透明，只留下文字像素
                w.attributes("-transparentcolor", KEY)
        except Exception:
            # 极老的 Tk 不支持 -transparentcolor，退化成深色底（至少还能用）
            w.configure(bg=BG if panel else "#101010")
        w.deiconify()
        w.update_idletasks()
        w.update()
        hwnd = make_click_through(w)
        keep_on_top(hwnd, w)
        return w, hwnd

    def _apply_panel_alpha(self, w=None) -> None:
        w = w or self.panel
        try:
            w.attributes("-alpha", self._alpha)
        except Exception:
            pass

    def _raise_text(self) -> None:
        """把文字层压到底板上面。两个都是 TOPMOST，不指定就会互相盖。"""
        try:
            from .overlay import HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, user32
            user32.SetWindowPos(self._hwnd_text, self._hwnd_panel, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            user32.SetWindowPos(self._hwnd_text, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    # -- 排版 --------------------------------------------------------------
    def _pick_font(self) -> str:
        """挑一个本机有的窄体数字字体。挑不到就退回等宽。"""
        try:
            from tkinter import font as tkfont
            families = set(tkfont.families(self.root))
            for name in FONT_THIN:
                if name in families:
                    return name
        except Exception:
            pass
        return "Consolas"

    def _label(self, text: str, x: int, y: int, w: int, h: int,
               size: int, color: str = FG, bold: bool = False,
               family: str = "Consolas", wrap: int = 0, anchor: str = "w"):
        kw = dict(text=text, bg=KEY, fg=color, anchor=anchor, justify="left",
                  font=(family, size, "bold" if bold else "normal"))
        if wrap:
            kw["wraplength"] = wrap
        lab = self._tk.Label(self.root, **kw)
        lab.place(x=x, y=y, width=w, height=h)
        return lab

    def _build(self) -> None:
        # 分隔线画在底板上，跟着底板一起变透明
        line = self._tk.Frame(self.panel, bg=LINE)
        line.place(x=12, y=124, width=W - 24, height=1)

        # ---- 1. 顶部 15 段 F1 转速条 ----
        # Canvas 的背景也是 KEY，所以它会被色键整块抠掉，只留下 LED 的彩色矩形。
        self.rpm_bar = self._tk.Canvas(self.root, width=W - 24, height=14,
                                       bg=KEY, highlightthickness=0, bd=0)
        self.rpm_bar.place(x=12, y=6)
        self._leds: list = []
        self._led_colors = [c for _, c in LED_TABLE]
        led_w, gap, top, led_h = 19, 3, 1, 12
        total = LED_COUNT * led_w + (LED_COUNT - 1) * gap
        x0 = max(0, (W - 24 - total) // 2)
        for i in range(LED_COUNT):
            x = x0 + i * (led_w + gap)
            self._leds.append(self.rpm_bar.create_rectangle(
                x, top, x + led_w, top + led_h, fill=LED_OFF, width=0))
        self._bar = ("off",)          # 灯条状态，见 _update_rpm_bar

        num_font = self._pick_font()

        # ---- 2. 座舱区：左边超大挡位，右边转速/车速同排 + 踏板条 ----
        self.gear = self._label("—", 12, 26, 68, 86, 44, FG, True,
                                family=num_font, anchor="center")
        self._label("挡", 78, 84, 22, 18, 10, DIM, family="Microsoft YaHei UI")

        self.rpm = self._label("—", 106, 26, 132, 28, 17, FG, True,
                               family=num_font, anchor="w")
        self.speed = self._label("— km/h", 238, 28, 106, 26, 15, BLUE, True,
                                 family=num_font, anchor="e")

        self.thr = self._label("油门 " + _bar(0), 106, 64, 238, 22, 10, GREEN,
                               family="Consolas")
        self.brk = self._label("刹车 " + _bar(0), 106, 88, 238, 22, 10, DIM,
                               family="Consolas")

        # ---- 3. 下半区：决策、统计、学习、页脚 ----
        self.decision = self._label("（还没有决策）", 12, 129, W - 24, 20, 10,
                                    FG, True, family="Microsoft YaHei UI")
        self.stats = self._label("统计 —", 12, 151, W - 24, 15, 9, DIM,
                                 family="Microsoft YaHei UI")
        self.learning = self._label("学习 —", 12, 168, W - 24, 15, 9, BLUE,
                                    family="Microsoft YaHei UI")
        self.footer = self._label("自动：关   ·   F8 关闭", 12, 186, W - 24, 14,
                                  8, "#5c6370", family="Microsoft YaHei UI")

    # -- 更新 --------------------------------------------------------------
    def set_state(self, s: dict, now: float | None = None) -> None:
        """s 是 ShiftService.snapshot() 的结果；now 用于确定性计时测试。"""
        now = time.monotonic() if now is None else now
        self.gear.config(text=gear_label(s["gear"]) if s.get("usable") else "—",
                         fg=(FG if s["enabled"] else DIM) if s["has_data"] else DIM)
        self._update_rpm_number(s, now)
        self.speed.config(text=(f"{s['speed']:.0f} km/h"
                                if s["has_data"] else "— km/h"))
        self.thr.config(
            text=f"油门 {_bar(s['throttle'])} {s['throttle'] * 100:3.0f}%",
            fg=GREEN if s["throttle"] > 0.05 else DIM)
        self.brk.config(
            text=f"刹车 {_bar(s['brake'])} {s['brake'] * 100:3.0f}%",
            fg=RED if s["brake"] > 0.05 else DIM)
        self._update_rpm_bar(s, now)
        # 决策按动作着色：余光就能分出是升挡、降挡，还是数据断了。
        dec_txt = s["decision"][:40]
        if s["stale"]:
            dec_color = RED
        elif dec_txt.startswith("UP"):
            dec_color = GREEN
        elif dec_txt.startswith("DOWN"):
            dec_color = YELLOW
        else:
            dec_color = FG
        self.decision.config(text=dec_txt, fg=dec_color)
        self.stats.config(text=s["stats"][:46])
        self.learning.config(text=s["learning"][:46])

        status = "遥测断流" if s["stale"] else ("自动：开" if s["enabled"] else "自动：关")
        color = RED if s["stale"] else (GREEN if s["enabled"] else "#5c6370")
        car = s.get("car_key") or "车辆未识别"
        footer = f"{car}   ·   {status}   ·   F8 关闭"
        if self.hint_text:
            footer = self.hint_text
        self.footer.config(text=footer, fg=color)

    def _update_rpm_number(self, s: dict, now: float) -> None:
        """只给数字做轻平滑；首帧直显，失效时彻底清除旧状态。"""
        try:
            target = float(s.get("rpm", 0.0))
        except (TypeError, ValueError):
            target = float("nan")
        valid = (s.get("has_data") and s.get("usable") and not s.get("stale")
                 and s.get("gear", 0) != 11 and isfinite(target) and target >= 0.0)
        if not valid:
            self._rpm_target = None
            self._rpm_value = None
            self._rpm_at = None
            # 菜单、暂停或断流时别显示旧转速，也别显示像真实值的 0 RPM。
            self.rpm.config(text="—")
            return
        self._rpm_target = target
        self._advance_rpm(now)

    def _advance_rpm(self, now: float) -> None:
        """按真实经过时间推进指数滤波，同一时间点不会重复积分。"""
        if self._rpm_target is None:
            return
        if self._rpm_value is None or self._rpm_at is None or now < self._rpm_at:
            # 首个有效样本直接采用；异常倒拨时钟时也以最新样本重建状态。
            self._rpm_value = self._rpm_target
        else:
            dt = now - self._rpm_at
            if dt > 0.0:
                alpha = 1.0 - exp(-dt / RPM_SMOOTH_TAU_S)
                self._rpm_value += alpha * (self._rpm_target - self._rpm_value)
        self._rpm_at = now
        self.rpm.config(text=f"{self._rpm_value:.0f} RPM")

    def _update_rpm_bar(self, s: dict, now: float | None = None) -> None:
        """用即时转速计算灯条状态，再交给 `_draw_bar` 画。

        满格基准是 `min(断油点, 满油门升挡点)`：

        - 学到断油（0.919）时，只要断油早于升挡点就用断油，那是物理上限。
        - **没学到断油时用升挡点兜底**。否则 span 退化成名义红线（1.0），
          而红区从 0.90 开始、车在 0.89 就升挡了——一格红都亮不起来。
          未学断油是默认状态（要手动拉到断油才学得到），所以这条路径很常见。

        闪烁跟着**当前油门下的升挡点**走，不跟断油点：控制回路的本职就是在
        断油前把转速切走，按断油点标定等于承诺一个永远不会发生的事件。
        闪 = 该升挡了，这才是驾驶员需要看到的信号。
        """
        now = time.monotonic() if now is None else now
        gear = s.get("gear", 0)
        if (not s.get("has_data") or not s.get("usable") or s.get("stale")
                or gear == 11):
            self._bar = ("off",)
            self._draw_bar(now)
            return

        pct = float(s.get("rpm_pct") or 0.0)
        lim = float(s.get("limiter_pct") or 0.0)
        up = float(s.get("up_pct") or 0.0)
        up_max = float(s.get("up_max") or 0.0)

        # 满格 = min(断油点, 满油门升挡点)，谁先到用谁。
        #
        # 为什么不是只看断油点：控制回路的本职就是在断油前把转速切走。
        # 按断油点标定，红区（0.90）会落在升挡点（0.85）之上——灯条永远
        # 亮不到红色，更别说闪。真实录制 4265 帧里，红区只有 26 帧，
        # 那还是手动拉到断油才出现的。
        #
        # 用 min 而不是 max：改装车可能断油比升挡点还早，那时得按断油算。
        if lim > 0.5 and up_max > 0.3:
            span = min(lim, up_max)
        else:
            span = lim if lim > 0.5 else (up_max if up_max > 0.3 else 1.0)
        frac = pct / span if span > 0.1 else pct

        # 闪烁 = 到达当前油门下的升挡点，或已经顶到满格。
        # 前者才是驾驶员要看的信号："现在升挡，别等断油。"
        #
        # 满格那一项用 frac 而不是 `lim`：span 可能已经是升挡点了，
        # `frac >= 0.98` 相当于提前 2%（8000 转对 8010 的升挡点）开始闪，
        # 那点提前量正好当"准备"用；而没学到断油时也只有这一项能兜底。
        flash = (up > 0.0 and pct >= up) or frac >= FLASH_FROM
        self._bar = ("flash",) if flash else ("level", frac)
        self._draw_bar(now)

    def _draw_bar(self, now: float | None = None) -> None:
        """按当前状态重画 15 个灯。只有 15 次 itemconfig，几十 Hz 也没压力。"""
        kind = self._bar[0]
        if kind == "off":
            for item in self._leds:
                self.rpm_bar.itemconfig(item, fill=LED_OFF)
            return

        if kind == "flash":
            # 所有灯同色同时亮灭。相位由绝对单调时间决定，
            # 不跟着刷新次数走，所以卡顿也不会打乱节奏。
            now = time.monotonic() if now is None else now
            on = (int(now * FLASH_HZ * 2) % 2) == 0
            color = LED_FLASH if on else LED_OFF
            for item in self._leds:
                self.rpm_bar.itemconfig(item, fill=color)
            return

        frac = self._bar[1]
        for item, (threshold, color) in zip(self._leds, LED_TABLE):
            self.rpm_bar.itemconfig(item, fill=(color if frac >= threshold else LED_OFF))

    # -- 位置 --------------------------------------------------------------
    def move_to(self, x: int, y: int) -> None:
        """两层必须一起动，否则文字会和底板错位。"""
        for w in (self.panel, self.text):
            try:
                w.geometry(f"{W}x{H}+{int(x)}+{int(y)}")
            except Exception:
                pass

    # -- 透明度 ------------------------------------------------------------
    def set_alpha(self, value: float) -> None:
        """只改底板。文字层永远是 100% 不透明。"""
        self._alpha = max(0.30, min(1.0, float(value)))
        self._apply_panel_alpha()
        self._pump()

    @property
    def alpha(self) -> float:
        return self._alpha

    # -- 生命周期 ----------------------------------------------------------
    def _pump(self) -> None:
        for w in (self.panel, self.text):
            try:
                w.update_idletasks()
                w.update()
            except Exception:
                pass

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if now - self._last_top > 0.5:
            self._last_top = now
            keep_on_top(self._hwnd_panel, self.panel)
            keep_on_top(self._hwnd_text, self.text)
            self._raise_text()
        # HUD 独立定时链约 60Hz；数字按真实时间轻平滑，灯条仍用即时转速。
        self._advance_rpm(now)
        # 闪烁按绝对单调时间重算相位，卡顿后不依赖补帧也不会累计漂移。
        if self._bar[0] == "flash":
            self._draw_bar(now)
        self._pump()

    def close(self) -> None:
        if not self._alive:
            return
        self._alive = False
        for w in (self.text, self.panel):
            try:
                w.destroy()
            except Exception:
                pass
        if self._owner is not None:
            try:
                self._owner.destroy()
            except Exception:
                pass
