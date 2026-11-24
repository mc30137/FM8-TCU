# -*- coding: utf-8 -*-
"""
置顶小浮窗。

为什么需要它：游戏全屏时，要看见控制台就得切窗口，而切窗口会让 FM8 自动暂停。
所以状态得直接画在游戏画面上。

⚠️ 这个模块踩过的坑，写在这里防止重犯：

1. `overrideredirect(True)` 如果在窗口**第一次映射之前**调用，Windows 上
   可能出现"窗口对象存在、IsWindowVisible 也返回 True，但屏幕上一片空白"。
   正确顺序是：withdraw() → 设置样式 → deiconify() → update()。
2. `-alpha`（半透明）依赖分层窗口，某些机器上会让窗口整体不可见。
   所以默认 **alpha=1.0（不透明）**，只在显式要求时才启用。
3. 判断"有没有被盖住"不能用 WindowFromPoint —— 它**会忽略 WS_EX_TRANSPARENT
   窗口**，而浮窗正是鼠标穿透的，所以永远查不到自己。必须沿 z 序走
   GetWindow(GW_HWNDPREV)。

"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_APPWINDOW = 0x00040000

HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
GW_HWNDPREV = 3
GWL_STYLE = -16
WS_VISIBLE = 0x10000000

user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL


def make_click_through(root) -> int:
    """设成「鼠标穿透 + 不进任务栏」。HUD 和命令行浮窗共用这一份。"""
    try:
        wid = root.winfo_id()
        parent = user32.GetParent(wid)
        hwnd = parent if parent else wid
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE,
            (style | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW,
        )
        return hwnd
    except Exception:
        return 0


def keep_on_top(hwnd: int, root=None) -> None:
    """重新申明置顶。游戏窗口自己也带 TOPMOST，会跟浮窗抢层级。"""
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if root is not None:
            root.lift()
    except Exception:
        pass


class Overlay:
    """一个置顶小窗，默认四行等宽文字。默认不透明、置顶、鼠标穿透。"""

    def __init__(self, x: int = 24, y: int = 24, width: int = 380,
                 height: int | None = None, alpha: float = 1.0,
                 borderless: bool = True, rows: int = 4) -> None:
        import tkinter as tk

        self._tk = tk
        self._rows = max(1, rows)
        if height is None:
            height = 12 + self._rows * 23
        self._width, self._height = width, height
        self._origin = (x, y)

        root = tk.Tk()
        self.root = root
        root.withdraw()                      # 关键：先藏起来，配置完再显示

        if borderless:
            try:
                root.overrideredirect(True)
            except Exception:
                pass

        root.configure(bg="#0b0b0b")
        if borderless:
            root.geometry(f"{width}x{height}+{x}+{y}")
        else:
            root.title("FM8 TCU")
            root.geometry(f"{width}x{height}+{x}+{y}")

        self._labels = []
        for i in range(self._rows):
            lab = tk.Label(
                root, text="—", anchor="w", justify="left",
                bg="#0b0b0b", fg="#ffffff",
                font=("Consolas", 11 if i else 13, "bold" if i == 0 else "normal"),
            )
            lab.place(x=12, y=8 + i * 23, width=width - 24, height=22)
            self._labels.append(lab)

        try:
            root.attributes("-topmost", True)
            if alpha < 1.0:
                root.attributes("-alpha", alpha)
        except Exception:
            pass

        root.deiconify()                     # 现在才真正显示
        self._pump()

        self._hwnd = self._make_click_through()
        self._last_topmost = 0.0
        self._reassert_topmost()
        self._pump()

    # -- internals ---------------------------------------------------------
    def _pump(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            pass

    def _make_click_through(self) -> int:
        return make_click_through(self.root)

    def _reassert_topmost(self) -> None:
        keep_on_top(self._hwnd, self.root)

    def _window_covering_us(self) -> int:
        cx = self._origin[0] + self._width // 2
        cy = self._origin[1] + self._height // 2
        h = self._hwnd
        for _ in range(256):
            h = user32.GetWindow(h, GW_HWNDPREV)
            if not h:
                return 0
            if not user32.IsWindowVisible(h):
                continue
            r = wintypes.RECT()
            if not user32.GetWindowRect(h, ctypes.byref(r)):
                continue
            if not (r.left <= cx < r.right and r.top <= cy < r.bottom):
                continue
            if user32.GetWindowLongW(h, GWL_EXSTYLE) & WS_EX_TRANSPARENT:
                continue
            return h
        return 0

    # -- public ------------------------------------------------------------
    def diag(self) -> dict:
        out = {"hwnd": self._hwnd, "visible": False, "covered": None}
        try:
            out["visible"] = bool(user32.IsWindowVisible(self._hwnd))
            style = user32.GetWindowLongW(self._hwnd, GWL_STYLE)
            out["ws_visible"] = bool(style & WS_VISIBLE)
            r = wintypes.RECT()
            user32.GetWindowRect(self._hwnd, ctypes.byref(r))
            out["rect"] = (r.left, r.top, r.right - r.left, r.bottom - r.top)
            out["covered"] = bool(self._window_covering_us())
        except Exception as e:
            out["error"] = str(e)
        return out

    def set_lines(self, lines: list[str], colors: list[str] | None = None) -> None:
        for i, lab in enumerate(self._labels):
            try:
                lab.config(text=lines[i] if i < len(lines) else "")
                if colors and i < len(colors):
                    lab.config(fg=colors[i])
            except Exception:
                pass

    def tick(self, now: float) -> None:
        if now - self._last_topmost > 0.5:
            self._last_topmost = now
            self._reassert_topmost()
        self._pump()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
