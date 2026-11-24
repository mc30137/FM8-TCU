# -*- coding: utf-8 -*-
"""
参数文件：把 Policy 从代码里搬到 JSON。

为什么要这个：阶段 4 的阈值写死在 Policy 的默认值里，改一个数就得改代码。
现在允许用户放一个 params.json 在旁边，只覆盖想改的那几项。

**所有值都要过范围检查。** 一个手滑把 cooldown_s 写成 0，程序会在换挡还没
完成时再发一次指令——这种错误不该靠人去发现。
"""

from __future__ import annotations

import json
import math
import os

from .engine import Policy

# 字段 → (最小, 最大, 类型)
RULES: dict[str, tuple[float, float, type]] = {
    "up_pct_low": (0.30, 1.00, float),
    "up_pct_high": (0.30, 1.00, float),
    "up_min_throttle": (0.0, 1.0, float),
    "down_brake_min": (0.0, 1.0, float),
    "down_brake_pct": (0.20, 1.00, float),
    "down_pct": (0.20, 1.00, float),
    "down_min_throttle": (0.0, 1.0, float),
    "down_min_speed_kmh": (0.0, 200.0, float),
    "coast_pct": (0.20, 1.00, float),
    "coast_max_throttle": (0.0, 1.0, float),
    "coast_max_brake": (0.0, 1.0, float),
    "coast_min_speed_kmh": (0.0, 200.0, float),
    "standstill_kmh": (0.0, 20.0, float),
    # 下限 0.20 秒：实测一次换挡耗时 70–153ms，冷却必须明显大于它。
    # 更小的值会让程序在换挡还没完成时再发一次指令。界面滑杆也画到这个下限。
    "cooldown_s": (0.20, 2.0, float),
    "pending_timeout_s": (0.20, 3.0, float),
    "after_up_block_down_s": (0.0, 3.0, float),
    "after_down_block_up_s": (0.0, 3.0, float),
    "upshift_max_decel_kmh_s": (0.0, 30.0, float),
    "upshift_trust_throttle": (0.0, 1.0, float),
    "over_rev_pct": (0.80, 1.10, float),
    "over_rev_uncertain_pct": (0.80, 1.10, float),
    "max_gear": (2, 12, int),
    "learned_up_drop_max": (0.0, 0.30, float),
    "limiter_margin": (0.002, 0.10, float),
    "use_learned_shift": (0, 1, bool),
}

_TRUE_WORDS = {"1", "true", "yes", "on", "是", "开"}
_FALSE_WORDS = {"0", "false", "no", "off", "否", "关"}


def _to_bool(value) -> bool:
    """JSON 里写 "0" 的人拿到的必须是 False。

    直接 `bool(value)` 会让字符串 "0" 变成 True——非空字符串永远为真，
    于是 use_learned_shift: "0" 反而打开了学习开关。这种错误没有任何报错，
    只会让行为跟预期相反。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
    raise ValueError(f"不是布尔值：{value!r}")


def load_policy(path: str) -> tuple[Policy, list[str]]:
    """读参数文件。返回 (Policy, 提示列表)。文件有问题不影响启动。"""
    notes: list[str] = []
    policy = Policy()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return policy, [f"参数文件不存在：{path}（用默认值）"]
    except (OSError, ValueError) as e:
        return policy, [f"参数文件读不了：{e}（用默认值）"]

    if not isinstance(raw, dict):
        return policy, ["参数文件顶层必须是对象（用默认值）"]

    for key, value in raw.items():
        if key.startswith("_"):
            continue                      # _说明 之类的注释字段，不是参数
        rule = RULES.get(key)
        if rule is None:
            notes.append(f"忽略未知参数 {key}")
            continue
        lo, hi, typ = rule
        try:
            if typ is bool:
                val = _to_bool(value)
            elif typ is int:
                val = int(value)
            else:
                val = float(value)
        except (TypeError, ValueError):
            notes.append(f"{key}={value!r} 不是合法取值，已忽略")
            continue
        # NaN / Inf 必须单独挡：NaN 和任何数比较都是 False，
        # 于是 `lo <= nan <= hi` 为假、`min(max(nan, lo), hi)` 还是 nan，
        # 最后 NaN 会被写进 Policy。cooldown_s 变成 NaN 之后
        # `since < nan` 恒为假——冷却彻底失效，而且没有任何提示。
        if typ is not bool and not math.isfinite(float(val)):
            notes.append(f"{key}={val} 不是有限数，已忽略")
            continue
        if not (lo <= val <= hi):
            clamped = min(max(val, lo), hi)
            notes.append(f"{key}={val} 超出 {lo}~{hi}，已夹到 {clamped}")
            val = clamped
        setattr(policy, key, val)

    if policy.up_pct_low > policy.up_pct_high:
        notes.append("up_pct_low 大于 up_pct_high，已对调")
        policy.up_pct_low, policy.up_pct_high = policy.up_pct_high, policy.up_pct_low

    return policy, notes


def write_default(path: str) -> bool:
    """导出一份带注释的默认参数。JSON 不支持注释，所以用 _说明 字段。"""
    return dump_policy(Policy(), path)


def dump_policy(policy: Policy, path: str) -> bool:
    """把当前参数写到文件（界面上点"保存参数"就走这里）。"""
    data = {k: getattr(policy, k) for k in RULES}
    data["_说明"] = {
        "up_pct_low": "油门 0 时的升挡转速（占红线比例）",
        "up_pct_high": "油门 100% 时的升挡转速",
        "down_brake_pct": "刹车时转速低于此值就降挡",
        "cooldown_s": "两次换挡最短间隔（秒），不要低于 0.20",
        "use_learned_shift": "是否使用学习到的升挡点（1=用，0=只用上面的固定值）",
        "max_gear": "没有学习数据时的挡位硬上限",
    }
    # 原子写：和 profile.py / ui_prefs.py 保持一致。
    # 直接 open(path, "w") 会在写一半时被中断（关机、崩溃、U 盘拔出），
    # 留下一个语法不完整的 params.json——下次启动读失败，参数静默回默认值。
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
