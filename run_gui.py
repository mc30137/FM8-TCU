# -*- coding: utf-8 -*-
"""
打包入口。

PyInstaller 需要一个**脚本文件**作为入口，不能直接指定 `-m fm8_tcu.gui`，
所以有了这个文件。用源码运行时它也能直接跑：

    python run_gui.py

这里额外做一件事：**把崩溃写进文件**。
`--windowed` 打包后没有控制台，未捕获的异常会静默消失——用户看到窗口一闪
就没了，而开发者什么线索都没有。出问题的时候，"能查"比"能跑"更重要。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _report(exc_type, exc_value, tb) -> None:
    import traceback

    text = "".join(traceback.format_exception(exc_type, exc_value, tb))
    path = os.path.join(_base_dir(), "error.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {exc_type.__name__} =====\n{text}\n")
        note = f"详情已写入：\n{path}"
    except OSError as e:
        note = f"（连错误日志都写不了：{e}）"

    if sys.stderr is not None:           # --windowed 打包后可能连 stderr 都没有
        try:
            sys.stderr.write(text)
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, f"程序遇到错误，已经停下。\n\n{exc_value}\n\n{note}",
                "FM8 自动换挡", 0x10)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        from fm8_tcu.gui import main
        main()
    except SystemExit:
        raise
    except BaseException:
        _report(*sys.exc_info())
        raise SystemExit(1)
