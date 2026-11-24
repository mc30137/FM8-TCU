# -*- coding: utf-8 -*-
"""生成 PyInstaller 用的 Windows 版本资源。

为什么需要：exe 是 --windowed 单文件，出问题时用户双击看不到任何版本信息。
写进版本资源后，右键 → 属性 → 详细信息 就能看到版本号和产品名，
不用我们的程序也能回答"你跑的是哪一版"。

用法（打包脚本会调用）：
    python tools/make_version_info.py build/version_info.txt
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from fm8_tcu import __version__  # noqa: E402

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'FM8 Auto Shift'),
        StringStruct('FileDescription', 'FM8 自动换挡（FM8 TCU）'),
        StringStruct('FileVersion', '{ver}'),
        StringStruct('InternalName', 'FM8AutoShift'),
        StringStruct('OriginalFilename', 'FM8自动换挡.exe'),
        StringStruct('ProductName', 'FM8 自动换挡'),
        StringStruct('ProductVersion', '{ver}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def quad(version: str) -> tuple[int, int, int, int]:
    """'0.7.0' → (0, 7, 0, 0)。版本号位数不够就补零，多了就截断。"""
    parts = []
    for piece in version.split(".")[:4]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join("build", "version_info.txt")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    text = TEMPLATE.format(quad=quad(__version__), ver=__version__)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"version info → {out}  (v{__version__})")


if __name__ == "__main__":
    main()
