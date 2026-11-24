# -*- coding: utf-8 -*-
"""
FM8 遥测协议：偏移表 + 解析 + 校验。

**这是全项目唯一的协议真源。** 偏移不写死在代码里，而是按官方字段顺序
自动累加得出——字段顺序对了，偏移就不可能算错。

实测依据见 docs/01-协议实测记录.md：
    FM8 = 331 字节；Sled 0..231；Dash 232..310；扩展 311..330
    speed@244  power@248  torque@252  accel@303  brake@304  clutch@305  gear@307
    验证方式：3560 帧速度交叉校验 0 不一致
"""

from __future__ import annotations

import math
import struct

FM8_PACKET_SIZE = 331
DASH_OFF = 232

# 挡位编码的上限。10 = 最高的前进挡，11 = 换挡中（实测每次 70–153ms）。
# 超出这个范围说明不是"挡位很怪"，而是这一包数据坏了（错位或别的程序发的）。
MAX_GEAR_CODE = 11

# (字段名, 格式符) —— 顺序即官方文档顺序
SLED_FIELDS = [
    ("is_race_on", "i"), ("timestamp_ms", "I"),
    ("engine_max_rpm", "f"), ("engine_idle_rpm", "f"), ("current_engine_rpm", "f"),
    ("accel_x", "f"), ("accel_y", "f"), ("accel_z", "f"),
    ("vel_x", "f"), ("vel_y", "f"), ("vel_z", "f"),
    ("ang_vel_x", "f"), ("ang_vel_y", "f"), ("ang_vel_z", "f"),
    ("yaw", "f"), ("pitch", "f"), ("roll", "f"),
    ("susp_fl", "f"), ("susp_fr", "f"), ("susp_rl", "f"), ("susp_rr", "f"),
    ("slip_fl", "f"), ("slip_fr", "f"), ("slip_rl", "f"), ("slip_rr", "f"),
    ("wheelspeed_fl", "f"), ("wheelspeed_fr", "f"),
    ("wheelspeed_rl", "f"), ("wheelspeed_rr", "f"),
    ("rumble_fl", "i"), ("rumble_fr", "i"), ("rumble_rl", "i"), ("rumble_rr", "i"),
    ("puddle_fl", "f"), ("puddle_fr", "f"), ("puddle_rl", "f"), ("puddle_rr", "f"),
    ("surf_rumble_fl", "f"), ("surf_rumble_fr", "f"),
    ("surf_rumble_rl", "f"), ("surf_rumble_rr", "f"),
    ("slipangle_fl", "f"), ("slipangle_fr", "f"),
    ("slipangle_rl", "f"), ("slipangle_rr", "f"),
    ("combslip_fl", "f"), ("combslip_fr", "f"), ("combslip_rl", "f"), ("combslip_rr", "f"),
    ("susp_m_fl", "f"), ("susp_m_fr", "f"), ("susp_m_rl", "f"), ("susp_m_rr", "f"),
    ("car_ordinal", "i"), ("car_class", "i"), ("car_pi", "i"),
    ("drivetrain", "i"), ("num_cylinders", "i"),
]

DASH_FIELDS = [
    ("pos_x", "f"), ("pos_y", "f"), ("pos_z", "f"),
    ("speed", "f"), ("power", "f"), ("torque", "f"),
    ("tire_temp_fl", "f"), ("tire_temp_fr", "f"),
    ("tire_temp_rl", "f"), ("tire_temp_rr", "f"),
    ("boost", "f"), ("fuel", "f"), ("distance", "f"),
    ("best_lap", "f"), ("last_lap", "f"), ("current_lap", "f"), ("current_race_time", "f"),
    ("lap_number", "H"), ("race_position", "B"),
    ("accel", "B"), ("brake", "B"), ("clutch", "B"), ("handbrake", "B"), ("gear", "B"),
    ("steer", "b"), ("norm_line", "b"), ("norm_ai_brake_diff", "b"),
]

FM8_EXT_FIELDS = [
    ("tire_wear_fl", "f"), ("tire_wear_fr", "f"),
    ("tire_wear_rl", "f"), ("tire_wear_rr", "f"),
    ("track_ordinal", "i"),
]


def _build_offsets() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    off = 0
    for name, fmt in SLED_FIELDS:
        out.append((name, fmt, off))
        off += struct.calcsize("<" + fmt)
    assert off == DASH_OFF, f"Sled 段应为 {DASH_OFF} 字节，实际 {off}"

    for name, fmt in DASH_FIELDS:
        out.append((name, fmt, off))
        off += struct.calcsize("<" + fmt)
    assert off == 311, f"Dash 段应结束于 311，实际 {off}"

    for name, fmt in FM8_EXT_FIELDS:
        out.append((name, fmt, off))
        off += struct.calcsize("<" + fmt)
    assert off == FM8_PACKET_SIZE, f"总长应为 {FM8_PACKET_SIZE}，实际 {off}"
    return out


OFFSETS = _build_offsets()
_GEAR_OFF = next(o for n, _, o in OFFSETS if n == "gear")
_FLOAT_FIELDS = tuple(n for n, fmt, _ in OFFSETS if fmt == "f")


class Telemetry:
    """一帧解析后的遥测。构造时已经过校验，所以下游可以信任它的值。"""

    __slots__ = ("values", "raw_len")

    def __init__(self, values: dict, raw_len: int):
        self.values = values
        self.raw_len = raw_len

    # -- 直接字段 ----------------------------------------------------------
    def __getitem__(self, k):
        return self.values[k]

    def get(self, k, default=0.0):
        return self.values.get(k, default)

    @property
    def gear(self) -> int:
        return int(self.values.get("gear", 0))

    @property
    def rpm(self) -> float:
        return float(self.values.get("current_engine_rpm", 0.0))

    @property
    def max_rpm(self) -> float:
        return float(self.values.get("engine_max_rpm", 0.0))

    @property
    def torque(self) -> float:
        """发动机扭矩（单位跟随游戏设置，我们只用曲线形状）。"""
        return float(self.values.get("torque", 0.0))

    @property
    def car_ordinal(self) -> int:
        """车辆编号。Forza 用它区分车型，是学习档案的主键。"""
        return int(self.values.get("car_ordinal", 0))

    @property
    def timestamp_s(self) -> float:
        """游戏自己的时间戳（秒）。

        学习模块用它而不是 wall clock：这样离线按 6 倍速回放录制文件时，
        「爬升持续了多久」的判断和实时跑完全一致。
        """
        return int(self.values.get("timestamp_ms", 0)) / 1000.0

    def __str__(self) -> str:
        return (f"gear={self.gear} rpm={self.rpm:.0f}/{self.max_rpm:.0f} "
                f"spd={self.speed_kmh:.1f} thr={self.throttle:.2f} brk={self.brake:.2f}")

    # -- 派生量 ------------------------------------------------------------
    @property
    def rpm_pct(self) -> float:
        return self.rpm / self.max_rpm if self.max_rpm > 0 else 0.0

    @property
    def throttle(self) -> float:
        return self.values.get("accel", 0) / 255.0

    @property
    def brake(self) -> float:
        return self.values.get("brake", 0) / 255.0

    @property
    def speed_kmh(self) -> float:
        # Dash 的 Speed 偶发异常，用速度矢量兜底（参考项目的经验）
        s = self.values.get("speed", 0.0)
        if 0.0 <= s < 200.0:
            return s * 3.6
        vx = self.values.get("vel_x", 0.0)
        vy = self.values.get("vel_y", 0.0)
        vz = self.values.get("vel_z", 0.0)
        return math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6

    @property
    def vel_z(self) -> float:
        """车体坐标 Z = 车头方向。小于 0 才是真的在后退。"""
        return float(self.values.get("vel_z", 0.0))

    @property
    def is_shifting(self) -> bool:
        """换挡中 = 档位为 11。实测 15 次换挡全部经过 11，每次 70–153ms。"""
        return self.gear == 11

    @property
    def is_reversing(self) -> bool:
        """倒车判定必须用速度方向，不能用档位——0 同时表示菜单/空挡/倒挡。"""
        return self.vel_z < -1.5 and abs(self.speed_kmh) > 3.0

    @property
    def engine_running(self) -> bool:
        """菜单/加载时游戏仍发包，但红线为 0。这类帧一律不可用。"""
        return self.max_rpm > 0

    @property
    def race_on(self) -> bool:
        return int(self.values.get("is_race_on", 0)) != 0

    @property
    def usable(self) -> bool:
        return self.engine_running and self.race_on


def _plausible(values: dict) -> bool:
    """这一包数据像不像真的遥测。

    为什么要这一层：`struct.unpack` 只保证"字节数够、类型对"，它会把
    0x7FC00000 原样读成 NaN，也会把 200 当成合法挡位。这些值一路传下去，
    到 `int(rpm // 100)` 那一步才炸——那时候已经进了学习器，异常会顺着
    收包循环往上冒，整个程序退出。**一帧数据不该有这个权力。**

    所以判定放在最外层：不合法就整包丢掉，下游永远拿不到坏值。
    """
    for name in _FLOAT_FIELDS:
        if not math.isfinite(values[name]):
            return False
    gear = values["gear"]
    if gear > MAX_GEAR_CODE:
        return False
    # 转速是物理量，负数只可能来自解析错位
    if values["current_engine_rpm"] < 0.0:
        return False
    return True


def parse(buf: bytes) -> Telemetry | None:
    """把一段 UDP 数据翻译成 Telemetry。

    返回 None 的情况：长度不对、字段读不出来、或者数值明显不可能是真的
    （NaN/Inf、挡位越界、负转速）。**绝不猜**——错一帧的代价比丢一帧大得多。
    """
    if len(buf) != FM8_PACKET_SIZE:
        return None
    values: dict[str, float | int] = {}
    try:
        for name, fmt, off in OFFSETS:
            values[name] = struct.unpack_from("<" + fmt, buf, off)[0]
    except struct.error:
        return None
    if not _plausible(values):
        return None
    return Telemetry(values, len(buf))


def gear_label(g: int) -> str:
    if g == 0:
        return "R"
    if 1 <= g <= 10:
        return str(g)
    if g == 11:
        return "N"
    return f"?{g}"
