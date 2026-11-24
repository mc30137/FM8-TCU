# -*- coding: utf-8 -*-
"""
图形界面版自动换挡。

它和命令行版共用 `service.py`，所以行为完全一致——这一点很重要：
如果两边各写一套逻辑，迟早出现"界面上好好的、命令行里不换挡"的问题。

界面里的每一块：
    顶部    标题、版本徽标、遥测状态胶囊
    主仪表  左：超大挡位 | 中：转速、车速、踏板条 | 右：车辆与帧统计
    中部    实时决策（按动作着色）+ 换挡统计与学习进度
    下部    参数滑块（带单位读数，高级基准可折叠）
    底部    启用(F11)、HUD(F8)、手动离合(F10)、移动 HUD(F9)、保存、退出
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from tkinter import messagebox

from . import __version__
from .config import dump_policy, load_policy
from .engine import Policy
from .hud import Hud
from . import hud as hud_mod
from .outputs import (Hotkeys, KeyboardSink, SCANCODES,
                      VK_F8, VK_F9, VK_F10, VK_F11, VK_F12,
                      cursor_pos, left_button_down, screen_size)
from .protocol import gear_label
from .service import DEFAULT_PROFILE_DIR, ROOT, ShiftService
from .ui_prefs import DEFAULT_PATH as DEFAULT_PREFS
from .ui_prefs import load as load_prefs, save as save_prefs

# ── 主题色板（深色赛车遥测风格）────────────────────────────────────────────
BG = "#0d0f14"
CARD = "#151921"
CARD_ALT = "#11151c"
LINE = "#242b36"
FG = "#edf2f7"
DIM = "#8b949e"
MUTED = "#5c6370"
GREEN = "#7ee787"
RED = "#ff7b72"
BLUE = "#6cb6ff"
YELLOW = "#e3b341"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"                 # 数字用等宽，转速跳动时列宽不变
HUD_FRAME_MS = 16                 # 独立 HUD 定时链，名义约 60Hz

# 参数文件的默认位置：和 exe / 项目根目录放在一起。
# 界面上的"保存参数"写这里，启动时也从这里读——两边必须是同一个路径，
# 否则会出现"保存成功但重启就丢"的情况。
DEFAULT_PARAMS = os.path.join(ROOT, "params.json")


class App:
    def __init__(self, root: tk.Tk, svc: ShiftService, keys: Hotkeys,
                 params_path: str, hud_pos: tuple[int, int] = (30, 30),
                 hud_alpha: float = 0.85, prefs_path: str = "",
                 clutch_name: str = "shift") -> None:
        self.root = root
        self.svc = svc
        self.keys = keys
        self.params_path = params_path
        self.prefs_path = prefs_path or DEFAULT_PREFS
        self.hud_pos = hud_pos
        self.hud_alpha = max(0.30, min(1.0, hud_alpha))
        self.clutch_name = clutch_name
        self.hud = None
        self.last_error = ""
        self.move_mode = False
        self._drag = None
        self._last_ui = 0.0
        self._syncing = False

        root.title(f"FM8 自动换挡  {__version__}")
        root.configure(bg=BG)
        root.geometry("780x620")
        root.minsize(740, 480)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self._build()
        self._refresh_buttons()
        self.root.after(50, self._tick)
        self._hud_job = None          # HUD 定时链只在 HUD 开着时续订

    # -- 界面搭建 ----------------------------------------------------------
    def _card(self, parent, pady=4) -> tk.Frame:
        """带 1px 描边的暗色卡片。边框用 highlightbackground 画，不占内边距。"""
        frame = tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                         highlightcolor=LINE, highlightthickness=1, bd=0)
        frame.pack(fill="x", padx=14, pady=pady)
        return frame

    def _build(self) -> None:
        # ---- 1. 顶栏 ----
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=14, pady=(10, 4))

        title_box = tk.Frame(head, bg=BG)
        title_box.pack(side="left")
        tk.Label(title_box, text="FM8 自动换挡", bg=BG, fg=FG,
                 font=(FONT, 15, "bold")).pack(side="left")
        tk.Label(title_box, text=f"v{__version__}", bg=CARD, fg=DIM,
                 font=(MONO, 9), padx=6, pady=1).pack(side="left", padx=(8, 0),
                                                      pady=(2, 0))

        self.status = tk.Label(head, text="● 等待遥测", bg=CARD, fg=DIM,
                               font=(FONT, 10, "bold"), padx=10, pady=3,
                               highlightbackground=LINE, highlightthickness=1)
        self.status.pack(side="right")

        # ---- 2. 主仪表卡片（三栏网格：数字不抖、路径不撑高）----
        top = self._card(self.root, pady=4)
        top.grid_columnconfigure(0, weight=0, minsize=132)
        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(2, weight=0, minsize=214)

        left = tk.Frame(top, bg=CARD)
        left.grid(row=0, column=0, sticky="ns", padx=(14, 8), pady=10)
        self.gear = tk.Label(left, text="—", bg=CARD, fg=FG,
                             font=(MONO, 50, "bold"), width=2, anchor="e")
        self.gear.pack(side="left")
        tk.Label(left, text="挡", bg=CARD, fg=DIM,
                 font=(FONT, 12, "bold")).pack(side="left", anchor="s",
                                               padx=(4, 0), pady=(0, 10))

        tk.Frame(top, bg=LINE, width=1).grid(row=0, column=0, sticky="nse",
                                             pady=12)

        mid = tk.Frame(top, bg=CARD)
        mid.grid(row=0, column=1, sticky="nsew", padx=(10, 12), pady=10)

        rpm_row = tk.Frame(mid, bg=CARD)
        rpm_row.pack(fill="x")
        self.rpm = tk.Label(rpm_row, text="— 转", bg=CARD, fg=FG,
                            font=(MONO, 20, "bold"), anchor="w")
        self.rpm.pack(side="left")
        self.speed = tk.Label(rpm_row, text="— km/h", bg=CARD, fg=BLUE,
                              font=(MONO, 15, "bold"), anchor="e")
        self.speed.pack(side="right", pady=(4, 0))

        # 文本踏板条保留：等宽字体直读，也给自动化测试用
        # 踏板读数：数字一行，下面两条真正的进度条（Frame 的宽度占比）。
        # 不用 █░ 字符是因为它在不同字体回退下宽窄不一，看起来像一排灰块。
        self.bars = tk.Label(mid, text="油门   0%      刹车   0%", bg=CARD,
                             fg=DIM, font=(MONO, 10), anchor="w")
        self.bars.pack(anchor="w", pady=(6, 3), fill="x")

        self._thr_track = tk.Frame(mid, bg="#0b0e12", height=5)
        self._thr_track.pack(fill="x", pady=(0, 2))
        self._thr_fill = tk.Frame(self._thr_track, bg=GREEN)
        self._thr_fill.place(x=0, y=0, relwidth=0.0, relheight=1.0)

        self._brk_track = tk.Frame(mid, bg="#0b0e12", height=5)
        self._brk_track.pack(fill="x")
        self._brk_fill = tk.Frame(self._brk_track, bg=RED)
        self._brk_fill.place(x=0, y=0, relwidth=0.0, relheight=1.0)

        right = tk.Frame(top, bg=CARD)
        right.grid(row=0, column=2, sticky="ne", padx=(6, 14), pady=12)
        self.car = tk.Label(right, text="车号 —", bg=CARD, fg=FG,
                            font=(MONO, 10, "bold"), anchor="e")
        self.car.pack(fill="x")
        self.frames = tk.Label(right, text="0 帧", bg=CARD, fg=DIM,
                               font=(MONO, 9), anchor="e")
        self.frames.pack(fill="x", pady=(2, 0))
        # 固定两行高：档案路径长（退到 %LOCALAPPDATA% 时更甚），
        # 不固定的话换行会把整个顶部卡片撑高，窗口高度跟着跳。
        self.profile = tk.Label(right, text="档案 —", bg=CARD, fg=MUTED,
                                font=(FONT, 9), anchor="ne", height=2,
                                wraplength=210, justify="right")
        self.profile.pack(fill="x", pady=(6, 0))

        # ---- 3. 决策 / 统计 / 学习 ----
        dec = self._card(self.root, pady=4)

        dec_head = tk.Frame(dec, bg=CARD)
        dec_head.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(dec_head, text="实时决策", bg=CARD, fg=DIM,
                 font=(FONT, 9, "bold")).pack(side="left")

        self.decision = tk.Label(dec, text="（还没有决策）", bg=CARD, fg=FG,
                                 font=(FONT, 13, "bold"), anchor="w",
                                 justify="left")
        self.decision.pack(fill="x", padx=14, pady=(0, 8))

        tk.Frame(dec, bg=LINE, height=1).pack(fill="x", padx=14)

        info = tk.Frame(dec, bg=CARD_ALT)
        info.pack(fill="x")

        self.stats = tk.Label(info, text="统计 —", bg=CARD_ALT, fg=DIM,
                              font=(FONT, 10), anchor="w")
        self.stats.pack(fill="x", padx=14, pady=(6, 2))

        learn_row = tk.Frame(info, bg=CARD_ALT)
        learn_row.pack(fill="x", padx=14, pady=(0, 8))
        self.learning = tk.Label(learn_row, text="学习 —", bg=CARD_ALT, fg=BLUE,
                                 font=(FONT, 10), anchor="w")
        self.learning.pack(side="left")

        # 自我学习直接参与换挡决策，就必须有一个"恢复出厂"的出口。
        # 没有这个按钮的话，用户被一轮脏数据污染后只能自己去删 JSON。
        self.btn_reset = tk.Button(
            learn_row, text="重置本车学习", command=self.reset_learning,
            bg=CARD, fg=DIM, relief="flat", font=(FONT, 9),
            activebackground=LINE, activeforeground=RED,
            highlightbackground=LINE, highlightthickness=1,
            bd=0, padx=8, pady=2, cursor="hand2")
        self.btn_reset.pack(side="right")

        # ---- 4. 参数卡片 ----
        box = self._card(self.root, pady=4)
        box.grid_columnconfigure(1, weight=1)

        tk.Label(box, text="参数调节（拖动立即生效）", bg=CARD, fg=DIM,
                 font=(FONT, 9, "bold")).grid(row=0, column=0, columnspan=3,
                                              sticky="w", padx=14, pady=(8, 4))

        self.cool_scale = self._slider(
            box, 1, "换挡冷却", 0.20, 1.00, 0.01,
            lambda v: self._set("cooldown_s", v),
            fmt=lambda v: f"{v * 1000:.0f} ms")
        self.hud_scale = self._slider(
            box, 2, "HUD 不透明度", 0.30, 1.00, 0.01,
            self._set_hud_alpha,
            fmt=lambda v: f"{v * 100:.0f} %")

        self._adv_open = False
        self.btn_adv = tk.Button(
            box, text="", command=self._toggle_advanced,
            bg=CARD, fg=BLUE, relief="flat", bd=0,
            activebackground=CARD, activeforeground=FG,
            font=(FONT, 9, "bold"), anchor="w", padx=14, pady=4,
            cursor="hand2")
        self.btn_adv.grid(row=3, column=0, columnspan=3, sticky="ew",
                          pady=(4, 4))

        self.adv = tk.Frame(box, bg=CARD_ALT,
                            highlightbackground=LINE, highlightthickness=1)
        self.adv.grid(row=4, column=0, columnspan=3, sticky="ew",
                      padx=1, pady=(0, 1))
        self.adv.grid_columnconfigure(1, weight=1)

        self.up_scale = self._slider(
            self.adv, 0, "满油门升挡", 0.60, 1.00, 0.005,
            lambda v: self._set("up_pct_high", v),
            fmt=lambda v: f"{v * 100:.1f} %", bg=CARD_ALT)
        self.down_scale = self._slider(
            self.adv, 1, "刹车降挡", 0.40, 0.90, 0.005,
            lambda v: self._set("down_brake_pct", v),
            fmt=lambda v: f"{v * 100:.1f} %", bg=CARD_ALT)
        tk.Label(
            self.adv,
            text="升挡点会按学到的扭矩/齿比自动下压（最多 8%，受断油点限制）；"
                 "降挡点是实测基准值。修改此处 = 更换新基准。",
            bg=CARD_ALT, fg=MUTED, font=(FONT, 8), justify="left",
            anchor="w").grid(row=2, column=0, columnspan=3, sticky="w",
                             padx=14, pady=(2, 8))

        self.adv.grid_remove()
        self._refresh_adv_label()
        self._sync_sliders()

        # ---- 5. 底部控制栏 ----
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=14, pady=(6, 6))

        self.btn_toggle = tk.Button(
            btns, text="启用（F11）", command=self.toggle,
            bg="#1f6feb", fg="white", relief="flat",
            activebackground="#388bfd", activeforeground="white",
            font=(FONT, 11, "bold"), padx=16, pady=6, cursor="hand2")
        self.btn_toggle.pack(side="left")

        self.hud_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            btns, text="HUD（F8）", variable=self.hud_var,
            command=self._toggle_hud, bg=BG, fg=FG, selectcolor=CARD,
            activebackground=BG, activeforeground=FG,
            font=(FONT, 10), cursor="hand2").pack(side="left", padx=(14, 4))

        # 手动带离合：游戏里变速箱是"手动带离合"时必须踩离合才能进挡。
        # 没配离合键（--clutch-key）时这个勾选是灰的，免得点了没反应。
        self.clutch_var = tk.BooleanVar(value=self.svc.snapshot()["clutch"])
        self.chk_clutch = tk.Checkbutton(
            btns, text=f"手动离合（{self.clutch_name}/F10）",
            variable=self.clutch_var, command=self._on_clutch,
            bg=BG, fg=FG, selectcolor=CARD,
            activebackground=BG, activeforeground=FG, font=(FONT, 10),
            state=("normal" if self.svc.snapshot()["clutch_key"] else "disabled"),
            cursor="hand2")
        self.chk_clutch.pack(side="left", padx=4)

        self.btn_move = tk.Button(
            btns, text="移动 HUD（F9）", command=self._toggle_move,
            bg=CARD, fg=FG, relief="flat",
            activebackground=LINE, activeforeground=FG,
            font=(FONT, 9), padx=10, pady=6, cursor="hand2")
        self.btn_move.pack(side="left", padx=6)

        tk.Button(
            btns, text="退出", command=self.quit, bg=CARD, fg=DIM,
            activebackground=RED, activeforeground="white",
            relief="flat", font=(FONT, 10), padx=12, pady=6,
            cursor="hand2").pack(side="right", padx=(6, 0))
        tk.Button(
            btns, text="保存参数", command=self.save_params, bg=CARD, fg=FG,
            activebackground=LINE, activeforeground=FG,
            relief="flat", font=(FONT, 10), padx=12, pady=6,
            cursor="hand2").pack(side="right")

        # ---- 6. 状态底栏 ----
        footer_bar = tk.Frame(self.root, bg=CARD_ALT,
                              highlightbackground=LINE, highlightthickness=1)
        footer_bar.pack(fill="x", padx=14, pady=(2, 10))
        self.hint = tk.Label(
            footer_bar,
            text=f"v{__version__}  默认关闭，按 F11 启用；F8 开关 HUD；"
                 f"F9 移动 HUD；F10 手动离合",
            bg=CARD_ALT, fg=DIM, font=(FONT, 9), anchor="w",
            padx=10, pady=4)
        self.hint.pack(fill="x")
        self._fit_height()

    def _fit_height(self) -> None:
        """窗口高度跟着内容走——折叠高级参数后不该留一块空白。

        必须把当前位置一起写回去：只给尺寸的话，Tk 会顺手把窗口挪到默认位置，
        点一下"收起"窗口就跳走了。
        """
        self.root.update_idletasks()
        w = max(self.root.winfo_width(), 780)
        h = max(self.root.winfo_reqheight(), 480)
        self.root.geometry(f"{w}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def _toggle_advanced(self) -> None:
        self._adv_open = not self._adv_open
        if self._adv_open:
            self.adv.grid()
        else:
            self.adv.grid_remove()
        self._refresh_adv_label()
        self._fit_height()

    def _refresh_adv_label(self) -> None:
        """收起时也要能看见"现在用的是多少"——藏起来不等于看不见。"""
        p = self.svc.policy
        if self._adv_open:
            self.btn_adv.config(text="▾ 收起基准参数（满油门升挡 / 刹车降挡）")
        else:
            self.btn_adv.config(
                text=f"▸ 学习推算：满油门升挡 {p.up_pct_high * 100:.0f}% · "
                     f"刹车降挡 {p.down_brake_pct * 100:.0f}%  （点击展开微调）")

    def _slider(self, parent, row: int, text: str, lo: float, hi: float,
                step: float, apply, fmt=None, bg: str = CARD) -> tk.Scale:
        """一行滑块：左边名字、中间滑轨、右边带单位的读数。

        `showvalue=False` 是因为原生数值只会显示 `0.25` 这种裸数字，
        用户得自己换算成 250ms / 89%；单位写出来才不用心算。
        """
        tk.Label(parent, text=text, bg=bg, fg=FG, font=(FONT, 10), width=11,
                 anchor="w").grid(row=row, column=0, sticky="w",
                                  padx=(14, 4), pady=4)
        var = tk.DoubleVar()
        val_label = tk.Label(parent, text="", bg=bg, fg=BLUE,
                             font=(MONO, 10, "bold"), width=9, anchor="e")
        val_label.grid(row=row, column=2, sticky="e", padx=(6, 14), pady=4)

        def _on_change(v: str) -> None:
            fv = float(v)
            val_label.config(text=fmt(fv) if fmt else f"{fv:.2f}")
            apply(fv)

        sc = tk.Scale(
            parent, from_=lo, to=hi, resolution=step,
            orient="horizontal", variable=var, showvalue=False,
            length=420, sliderlength=16, width=12, sliderrelief="raised",
            bg="#1e2836", fg=FG, troughcolor="#0b0e12",
            activebackground="#2b3a50", highlightthickness=0, bd=1,
            font=(FONT, 9), command=_on_change)
        sc.grid(row=row, column=1, sticky="ew", pady=4)
        sc._apply = apply            # 记住回调，同步数值时临时屏蔽
        sc._val_label = val_label
        sc._fmt = fmt
        return sc

    # -- 参数 --------------------------------------------------------------
    def _set(self, name: str, value: float) -> None:
        if self._syncing:
            return
        setattr(self.svc.policy, name, float(value))
        # 收起状态下按钮上写着当前值，拖完要跟着变
        self._refresh_adv_label()

    def _sync_sliders(self) -> None:
        self._syncing = True
        p = self.svc.policy
        for sc, val in ((self.up_scale, p.up_pct_high),
                        (self.down_scale, p.down_brake_pct),
                        (self.cool_scale, p.cooldown_s),
                        (self.hud_scale, self.hud_alpha)):
            sc.set(val)
            if getattr(sc, "_fmt", None):
                sc._val_label.config(text=sc._fmt(float(val)))
        self._syncing = False
        self._refresh_adv_label()

    def _set_hud_alpha(self, value: float) -> None:
        """HUD 不透明度，拖动立即生效；HUD 没开时先记下来。"""
        self.hud_alpha = max(0.30, min(1.0, float(value)))
        if self.hud is not None:
            try:
                self.hud.set_alpha(self.hud_alpha)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"

    def save_params(self) -> None:
        path = self.params_path or DEFAULT_PARAMS
        ok = dump_policy(self.svc.policy, path)
        self.hint.config(text=f"参数已保存到 {path}（下次启动自动读取）" if ok
                         else f"保存失败：{path} 写不进去，检查权限",
                         fg=GREEN if ok else RED)

    # -- 交互 --------------------------------------------------------------
    def _on_clutch(self) -> None:
        want = bool(self.clutch_var.get())
        if self.svc.set_clutch(want):
            self._save_prefs()          # 勾完就落盘，别等正常退出
            self.hint.config(
                text=(f"手动离合：换挡时踩 {self.clutch_name}"
                      "（前提：游戏里变速箱是「手动带离合」）"
                      if want else "手动离合已关闭"),
                fg=BLUE if want else DIM)
        else:
            self.clutch_var.set(False)
            self.hint.config(text="没有可用的离合键：用 --clutch-key 指定（默认 shift）",
                             fg=RED)

    def toggle(self) -> None:
        self.svc.toggle()
        self._refresh_buttons()
        self.hint.config(
            text=("已启用：会真的往游戏发按键" if self.svc.enabled
                  else "已停用：只显示判断结果，不按键"),
            fg=YELLOW if self.svc.enabled else DIM)

    def reset_learning(self) -> None:
        """删掉当前车的档案。学到的数据会直接影响换挡，必须能一键清空。"""
        key = self.svc.car_key
        if not key:
            self.hint.config(text="还没识别到车辆，暂时没有可重置的档案", fg=DIM)
            return
        if not messagebox.askyesno(
                "重置本车学习",
                f"删除 {key} 的档案？\n\n"
                "齿比、扭矩曲线、断油转速都会清空，需要重新跑一两圈。\n"
                "这个操作不能撤销。"):
            return
        if self.svc.reset_profile():
            self.hint.config(text=f"已重置 {key}，从现在开始重新学", fg=GREEN)
        else:
            self.hint.config(text=self.svc.profile_error or "重置失败", fg=RED)

    def _toggle_hud(self) -> None:
        """勾上就弹 HUD，取消就关掉。F8 也能切。"""
        want = bool(self.hud_var.get())
        if want and self.hud is None:
            try:
                x, y = self.hud_pos
                self.hud = Hud(master=self.root, x=x, y=y, alpha=self.hud_alpha)
                self.hud.set_state(self.svc.snapshot(), now=time.monotonic())
                self._start_hud_tick()
                self.hint.config(
                    text="HUD 已开启（鼠标穿透）。按 F9 可进入拖动模式调整位置",
                    fg=GREEN)
            except Exception as e:
                self.hud = None
                self.hud_var.set(False)
                self._stop_hud_tick()
                self.hint.config(text=f"HUD 打不开：{e}", fg=RED)
        elif not want and self.hud is not None:
            self._stop_hud_tick()
            self.hud.close()
            self.hud = None
            self._end_move()
            self.hint.config(text="HUD 已关闭", fg=DIM)

    # -- 移动 HUD ----------------------------------------------------------
    def _toggle_move(self) -> None:
        """HUD 平时是鼠标穿透的（点得到它就等于切出游戏），
        所以拖动要先进入"移动模式"，松开鼠标也不会误拖。"""
        if self.hud is None:
            self.hud_var.set(True)
            self._toggle_hud()
            if self.hud is None:
                return

        self.move_mode = not self.move_mode
        self._drag = None
        if self.move_mode:
            self.btn_move.config(text="结束移动（F9）", bg="#a03030")
            self.hud.hint_text = "按住左键拖动   ·   F9 结束"
            self.hint.config(
                text="移动模式：在 HUD 上按住左键拖动，再按 F9 结束。此时点击会同时传给游戏。",
                fg=YELLOW)
        else:
            self._end_move()

    def _end_move(self) -> None:
        self.move_mode = False
        self._drag = None
        if self.hud is not None:
            self.hud.hint_text = ""
        try:
            self.btn_move.config(text="移动 HUD（F9）", bg=CARD)
        except Exception:
            pass

    def _poll_drag(self) -> None:
        if self.hud is None or not self.move_mode:
            return
        cx, cy = cursor_pos()
        self._update_drag(cx, cy, left_button_down())

    def _update_drag(self, cx: int, cy: int, pressed: bool) -> None:
        """把光标坐标和按键状态交给它就行，方便单测（不用真的动鼠标）。"""
        if self.hud is None or not self.move_mode:
            self._drag = None
            return

        hx, hy = self.hud_pos
        if pressed:
            if self._drag is None:
                inside = (hx <= cx <= hx + hud_mod.W
                          and hy <= cy <= hy + hud_mod.H)
                if inside:
                    self._drag = (cx - hx, cy - hy)
            else:
                ox, oy = self._drag
                self._set_hud_pos(cx - ox, cy - oy)
        elif self._drag is not None:
            self._drag = None
            self._save_prefs()

    def _set_hud_pos(self, x: int, y: int) -> None:
        """夹在屏幕内，免得拖到看不见的地方找不回来。"""
        sw, sh = screen_size()
        x = max(0, min(int(x), max(0, sw - hud_mod.W)))
        y = max(0, min(int(y), max(0, sh - hud_mod.H)))
        self.hud_pos = (x, y)
        if self.hud is not None:
            self.hud.move_to(x, y)

    def _save_prefs(self) -> None:
        save_prefs({"hud_pos": [int(self.hud_pos[0]), int(self.hud_pos[1])],
                    "hud_alpha": round(float(self.hud_alpha), 2),
                    "clutch": bool(self.svc.snapshot()["clutch"])},
                   self.prefs_path)

    def _refresh_buttons(self) -> None:
        if self.svc.enabled:
            self.btn_toggle.config(text="停用（F11）", bg="#a03030",
                                   activebackground="#c93c3c")
        else:
            self.btn_toggle.config(text="启用（F11）", bg="#1f6feb",
                                   activebackground="#388bfd")

    def quit(self) -> None:
        try:
            self._stop_hud_tick()
            if self.hud is not None:
                self.hud.close()
                self.hud = None
            self._save_prefs()
            self.svc.close()
        finally:
            self.root.destroy()

    # -- 主循环 ------------------------------------------------------------
    def _handle_hotkeys(self) -> bool:
        """处理热键。返回 False 表示已经请求退出，主循环应停下。"""
        if self.keys.pressed(VK_F12):
            self.quit()
            return False
        if self.keys.pressed(VK_F11):
            self.toggle()
        if self.keys.pressed(VK_F10) and self.svc.snapshot()["clutch_key"]:
            self.clutch_var.set(not self.clutch_var.get())
            self._on_clutch()
        if self.keys.pressed(VK_F8):
            self.hud_var.set(not self.hud_var.get())
            self._toggle_hud()
        if self.keys.pressed(VK_F9):
            self._toggle_move()
        return True

    def _tick(self) -> None:
        if not self._handle_hotkeys():
            return

        self._poll_drag()

        try:
            self.svc.poll()
        except Exception as e:
            self.hint.config(text=f"出错：{e}", fg=RED)

        now = time.monotonic()
        if now - self._last_ui >= 0.05:
            self._last_ui = now
            self._update_ui()
        self.root.after(15, self._tick)

    def _update_ui(self) -> None:
        s = self.svc.snapshot()
        online = s["has_data"] and not s["stale"]

        if not s["has_data"]:
            self.status.config(text="● 等待遥测", fg=DIM)
        elif s["stale"]:
            self.status.config(text="● 遥测断流（已停手）", fg=RED)
        else:
            mode_tag = " · 自动开" if s["enabled"] else " · 只读"
            self.status.config(text=f"● 遥测在线{mode_tag}",
                               fg=GREEN if s["enabled"] else BLUE)

        self.gear.config(
            text=(f"{gear_label(s['gear']) if s['usable'] else '—':>2}"),
            fg=(GREEN if s["enabled"] else FG) if online else DIM)
        self.rpm.config(text=f"{s['rpm']:.0f} 转  ({s['rpm_pct'] * 100:.0f}%)")
        self.speed.config(text=f"{s['speed']:.0f} km/h")
        self.bars.config(text=f"油门 {s['throttle'] * 100:3.0f}%"
                              f"      刹车 {s['brake'] * 100:3.0f}%")
        self._thr_fill.place_configure(
            relwidth=max(0.0, min(1.0, s["throttle"])))
        self._brk_fill.place_configure(
            relwidth=max(0.0, min(1.0, s["brake"])))

        dec_txt = s["decision"]
        self.decision.config(text=dec_txt)
        if s["stale"]:
            self.decision.config(fg=RED)
        elif dec_txt.startswith("UP"):
            self.decision.config(fg=GREEN)
        elif dec_txt.startswith("DOWN"):
            self.decision.config(fg=YELLOW)
        else:
            self.decision.config(fg=FG)

        self.stats.config(text=s["stats"])
        self.learning.config(text=s["learning"])
        self.car.config(text=f"车号 {s['car_key'] or '—'}")

        extra = ""
        if s["bad_packets"]:
            extra += f" · 坏包 {s['bad_packets']}"
        if s["foreign_packets"]:
            extra += f" · 来源拒绝 {s['foreign_packets']}"
        self.frames.config(text=f"{s['frames']} 帧{extra}")

        if s["profile_error"]:
            self.profile.config(text=s["profile_error"], fg=RED)
        else:
            path = s["profile"] or "（还没识别到车辆）"
            # 只留尾部：完整路径对用户没用，前面全是同一个目录
            short = path if len(path) <= 34 else "…" + path[-33:]
            self.profile.config(text=f"档案 {short}", fg=DIM)

    def _update_hud(self, now: float) -> None:
        if self.hud is None:
            return
        try:
            s = self.svc.snapshot()
            self.hud.set_state(s, now=now)
            self.hud.tick(now=now)
        except Exception as e:
            self._close_hud_error(e)

    def _close_hud_error(self, e: Exception) -> None:
        """出错时真正销毁两层窗口，避免下次 F8 再叠出一套。"""
        self.last_error = f"{type(e).__name__}: {e}"
        self._stop_hud_tick()          # 先停定时链，否则回调会打到已销毁的窗口
        hud = self.hud
        if hud is not None:
            try:
                hud.close()
            except Exception:
                pass
        self.hud = None
        self.hud_var.set(False)
        self.hint.config(text=f"HUD 出错已关闭：{self.last_error}", fg=RED)

    def _hud_tick(self) -> None:
        """HUD 的 60Hz 快速通道。

        只在 HUD 开着时续订：关掉之后还每秒唤醒 60 次，纯属白烧 CPU，
        而且 `quit()` 之后回调打到一个已销毁的窗口上会抛 TclError。
        """
        if self.hud is None:
            self._hud_job = None
            return
        self._update_hud(time.monotonic())
        self._hud_job = self.root.after(HUD_FRAME_MS, self._hud_tick)

    def _stop_hud_tick(self) -> None:
        if self._hud_job is not None:
            try:
                self.root.after_cancel(self._hud_job)
            except Exception:
                pass
            self._hud_job = None

    def _start_hud_tick(self) -> None:
        self._stop_hud_tick()
        self._hud_job = self.root.after(HUD_FRAME_MS, self._hud_tick)


def _log_tk_error(exc_type, exc_value, tb) -> None:
    """Tk 回调（after / command / 事件）里的异常不会冒泡到 main()。

    `run_gui.py` 的 try/except 只能接住启动阶段的错误；定时链里出错会被 Tk
    自己吞掉——`--windowed` 下连 stderr 都没有，等于彻底静默。挂在这里才留得下痕迹。
    """
    import traceback

    text = "".join(traceback.format_exception(exc_type, exc_value, tb))
    try:
        with open(os.path.join(ROOT, "error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== Tk 回调异常 =====\n{text}\n")
    except OSError:
        pass
    if sys.stderr is not None:
        try:
            sys.stderr.write(text)
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="FM8 自动换挡（图形界面）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址。默认只收本机；要收局域网遥测用 0.0.0.0 + --allow-from")
    ap.add_argument("--allow-from", default="", metavar="IP[,IP]",
                    help="放行的来源 IP，逗号分隔。默认只收本机回环")
    ap.add_argument("--port", type=int, default=5300)
    ap.add_argument("--keys", default="num9:num7")
    ap.add_argument("--hold", type=int, default=60)
    ap.add_argument("--no-focus-check", action="store_true",
                    help="不检查前台窗口，任何窗口下都发键（默认只往游戏发）")
    ap.add_argument("--log", default=os.path.join(ROOT, "decisions.csv"),
                    help="决策日志 CSV")
    ap.add_argument("--config", default="", help="参数文件 JSON（默认读 exe 旁的 params.json）")
    ap.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    ap.add_argument("--no-learn", action="store_true")
    ap.add_argument("--autostart", action="store_true")
    ap.add_argument("--hud-pos", default="", help="HUD 位置 x,y（默认用上次保存的）")
    ap.add_argument("--hud-alpha", type=float, default=None,
                    help="HUD 底板不透明度 0.30~1.00（默认用上次保存的）")
    ap.add_argument("--clutch", action="store_true",
                    help="启动即开手动离合换挡（游戏里变速箱是「手动带离合」时用）")
    ap.add_argument("--clutch-key", default="shift", metavar="KEY",
                    help="离合键名，默认 shift；要和游戏里的绑定一致")
    ap.add_argument("--clutch-hold", type=int, default=40, metavar="MS",
                    help="换挡时离合按住的毫秒数，默认 40")
    args = ap.parse_args()

    try:
        up_name, down_name = [s.strip().lower() for s in args.keys.split(":")]
        up_scan, down_scan = SCANCODES[up_name], SCANCODES[down_name]
        clutch_scan = SCANCODES[args.clutch_key.strip().lower()]
    except (ValueError, KeyError):
        raise SystemExit(f"不认识的键名：{args.keys} / {args.clutch_key}")

    # 参数文件的优先级：--config 指定 > exe 旁边的 params.json > 默认值。
    # 少了中间这一层的话，界面上点"保存参数"写进 params.json，下次启动
    # 根本不会读——用户调好的参数静默变回默认值，而且没有任何提示。
    policy = Policy()
    notes: list[str] = []
    config_path = args.config or DEFAULT_PARAMS
    if args.config or os.path.exists(DEFAULT_PARAMS):
        policy, notes = load_policy(config_path)

    sink = KeyboardSink(up_scan, down_scan, args.hold,
                        require_game=not args.no_focus_check,
                        clutch_scan=clutch_scan, clutch_hold_ms=args.clutch_hold,
                        use_clutch=args.clutch)
    svc = ShiftService(policy, sink=sink, learn=not args.no_learn,
                       profile_dir=args.profile_dir, log_path=args.log)

    root = tk.Tk()
    root.report_callback_exception = _log_tk_error
    try:
        svc.open(args.host, args.port, allow_from=args.allow_from)
    except RuntimeError as e:
        root.withdraw()
        messagebox.showerror("无法监听端口",
                             f"{e}\n\n是不是还有另一个探针或遥测软件占着 {args.port}？")
        raise SystemExit(1)

    if args.autostart:
        svc.set_enabled(True)

    prefs = load_prefs()
    hx, hy = 30, 30
    saved = args.hud_pos or prefs.get("hud_pos")
    try:
        if isinstance(saved, str):
            hx, hy = [int(v) for v in saved.split(",")]
        elif isinstance(saved, (list, tuple)) and len(saved) == 2:
            hx, hy = int(saved[0]), int(saved[1])
    except (ValueError, TypeError):
        hx, hy = 30, 30

    alpha = args.hud_alpha
    if alpha is None:
        try:
            alpha = float(prefs.get("hud_alpha", 0.85))
        except (TypeError, ValueError):
            alpha = 0.85

    # 上次开着手动离合就继续开着；--clutch 优先级更高。
    if args.clutch or prefs.get("clutch"):
        if not svc.set_clutch(True):
            svc.last_error = "手动离合不可用"

    app = App(root, svc, Hotkeys(), args.config, hud_pos=(hx, hy),
              hud_alpha=alpha, clutch_name=args.clutch_key)
    # 双击启动时 --config 是空的，但 params.json 照样会被读；告警也要显示出来，
    # 否则"参数被夹住/写坏了"这件事只有加了命令行参数的人能看见。
    if notes:
        app.hint.config(text="；".join(notes[:3]), fg=YELLOW)

    try:
        root.mainloop()
    finally:
        try:
            svc.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
