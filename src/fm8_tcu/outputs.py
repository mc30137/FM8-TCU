# -*- coding: utf-8 -*-
"""
输入输出：把"换挡"这个动作真正送进游戏，以及读热键。

用 SendInput + **扫描码**。原因：游戏多半走 Raw Input / DirectInput，
它们认的是键盘硬件扫描码；只发虚拟键码有时会被忽略。

实测数据（docs/03）：20 次手动换挡 20/20 成功，中位延迟 104ms。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

VK_F5, VK_F6, VK_F8, VK_F9, VK_F10, VK_F11, VK_F12 = 0x74, 0x75, 0x77, 0x78, 0x79, 0x7A, 0x7B

SCANCODES = {
    "num0": 0x52, "num1": 0x4F, "num2": 0x50, "num3": 0x51, "num4": 0x4B,
    "num5": 0x4C, "num6": 0x4D, "num7": 0x47, "num8": 0x48, "num9": 0x49,
    "q": 0x10, "e": 0x12, "r": 0x13, "f": 0x21, "g": 0x22,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
    "[": 0x1A, "]": 0x1B, ",": 0x33, ".": 0x34, "/": 0x35,
    "shift": 0x2A, "ctrl": 0x1D, "space": 0x39,
}


def send_scan(scan: int, up: bool) -> bool:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki.wVk = 0
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = None
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_process() -> str:
    """前台窗口所属的进程名（小写，含 .exe）。拿不到就返回空串。

    为什么要它：`SendInput` 是往**当前焦点窗口**发键的。切出去回微信的时候，
    程序照样会把 num9 发进输入框。这不是理论风险——自动换挡在最前面的时候，
    一次 Alt-Tab 就够了。
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""
    return ""


def in_game_focus(game_hint: str = "forza"):
    """前台是不是游戏。拿不到进程名时返回 None（由调用方决定放不放行）。"""
    name = foreground_process()
    if not name:
        return None
    return name.startswith(game_hint)


class KeyboardSink:
    """按键输出。保持时间必须够长，否则会被游戏的输入轮询漏掉。

    两种换挡方式：

    - **普通（默认）**：只按一下挡位键。
    - **手动离合**：游戏里变速箱设成"手动带离合"时，光按挡位键不会进挡，
      必须同时踩离合。按键顺序参考 forza-horizon-6-tgt2-shifter 公开记录的
      `keyboard_clutch` 行为（代码为独立实现）：

          挡位键按下 ──10ms── 离合按下 ──40ms── 离合松开 ──10ms── 挡位键松开

      总时长仍然是 `hold_ms`（默认 60ms），所以换挡延迟不变。按下/抬起的
      顺序是反的：挡位键要在离合接合的那一瞬间还按着。
    """

    def __init__(self, up_scan: int, down_scan: int, hold_ms: int = 60,
                 require_game: bool = True, game_hint: str = "forza",
                 clutch_scan: int = 0, clutch_hold_ms: int = 40,
                 use_clutch: bool = False, send=None, sleep=None):
        self.up_scan = up_scan
        self.down_scan = down_scan
        self.hold_ms = max(30, int(hold_ms))
        self.require_game = require_game
        self.game_hint = game_hint
        self.clutch_scan = int(clutch_scan or 0)
        self.clutch_hold_ms = max(10, int(clutch_hold_ms))
        self.use_clutch = bool(use_clutch and self.clutch_scan)
        self.last_error = ""
        self.skipped = 0            # 因为前台不是游戏而没发的次数
        # 注入点：测试里换成记录器，就能在不碰键盘的情况下验证时序
        self._send = send or send_scan
        self._sleep = sleep or time.sleep

    def set_clutch(self, on: bool) -> bool:
        """运行时切换手动离合。没配离合键时开关无效，返回 False。"""
        if not self.clutch_scan:
            return False
        self.use_clutch = bool(on)
        return True

    def _split_hold(self) -> tuple[int, int, int]:
        """把总按住时长切成 前段/离合/后段（毫秒）。"""
        total = self.hold_ms
        c_hold = min(self.clutch_hold_ms, max(10, total - 20))
        lead = max(0, (total - c_hold) // 2)
        tail = max(0, total - c_hold - lead)
        return lead, c_hold, tail

    def shift(self, action: str) -> bool:
        # 前台不是游戏就不发。拿不到进程名（None）时放行——宁可偶尔发错一次，
        # 也不要因为一个读不到的信息把功能整体关掉。
        if self.require_game and in_game_focus(self.game_hint) is False:
            self.skipped += 1
            self.last_error = f"前台是 {foreground_process() or '?'}，不是游戏，未发送"
            return False

        scan = self.up_scan if action == "UP" else self.down_scan
        if self.use_clutch and self.clutch_scan:
            return self._shift_with_clutch(scan)
        return self._shift_plain(scan)

    def _shift_plain(self, scan: int) -> bool:
        pressed = False
        try:
            if not self._send(scan, up=False):
                self.last_error = "SendInput 拒绝（权限不足？）"
                return False
            pressed = True
            self._sleep(self.hold_ms / 1000.0)
            return True
        except Exception as e:
            self.last_error = str(e)
            return False
        finally:
            # 关键：抬键放在 finally 里。原来的写法一旦 sleep 中途抛异常
            # （比如解释器正在退出），这个键就一直处于按下状态——用户在游戏里
            # 会遇到"车自己一直升挡"，而且找不到原因。
            if pressed:
                self._release(scan)

    def _shift_with_clutch(self, scan: int) -> bool:
        lead, c_hold, tail = self._split_hold()
        shift_down = clutch_down = False
        try:
            if not self._send(scan, up=False):
                self.last_error = "SendInput 拒绝（权限不足？）"
                return False
            shift_down = True
            if lead:
                self._sleep(lead / 1000.0)
            if not self._send(self.clutch_scan, up=False):
                self.last_error = "离合键发不出去（扫描码对吗？）"
                return False
            clutch_down = True
            self._sleep(c_hold / 1000.0)
            if not self._send(self.clutch_scan, up=True):
                self.last_error = "离合抬起失败，可能一直踩着离合"
            clutch_down = False
            if tail:
                self._sleep(tail / 1000.0)
            return True
        except Exception as e:
            self.last_error = str(e)
            return False
        finally:
            # 先松离合再松挡位：和按下顺序相反。顺序反了等于"无离合松挡"，
            # 在手动带离合的车上会顿一下，甚至不进挡。
            if clutch_down:
                self._release(self.clutch_scan)
            if shift_down:
                self._release(scan)

    def _release(self, scan: int) -> None:
        try:
            if not self._send(scan, up=True):
                self.last_error = "抬键失败，键可能仍是按下状态"
        except Exception:
            pass

    def release_all(self) -> None:
        """把所有可能按下的键抬起来。退出时调用。

        注意：它发的是全局 KEYUP，会落到当时的焦点窗口上。退出时游戏通常
        还在前台，所以没问题；但这也说明"抬键"这件事本身没有窗口归属，
        只能在退出路径上尽量做一次。
        """
        scans = [self.up_scan, self.down_scan]
        if self.clutch_scan:
            scans.append(self.clutch_scan)
        for scan in scans:
            try:
                if not self._send(scan, up=True):
                    self.last_error = "退出时抬键失败，键可能仍是按下状态"
            except Exception:
                pass


class NullSink:
    """什么都不发。用于测试注入，以及 service 在没给输出时的兜底。"""

    def __init__(self):
        self.last_error = ""
        self.skipped = 0
        self.clutch_scan = 0
        self.use_clutch = False

    def shift(self, action: str) -> bool:
        return True

    def set_clutch(self, on: bool) -> bool:
        return False

    def release_all(self) -> None:
        pass


class Hotkeys:
    def __init__(self):
        self._prev: dict[int, bool] = {}

    @staticmethod
    def down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def pressed(self, vk: int) -> bool:
        now = self.down(vk)
        was = self._prev.get(vk, False)
        self._prev[vk] = now
        return now and not was


# -- 鼠标（HUD 拖动用）-----------------------------------------------------
class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
user32.GetCursorPos.restype = wintypes.BOOL


def cursor_pos() -> tuple[int, int]:
    p = _POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


def screen_size() -> tuple[int, int]:
    return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
