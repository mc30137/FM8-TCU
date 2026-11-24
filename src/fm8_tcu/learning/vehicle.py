# -*- coding: utf-8 -*-
"""
车辆模型：把三个学习器拼成一台"认识过的车"。

它能回答三个问题，换挡引擎只依赖这三个答案：

1. `rpm_at(speed, gear)` —— 这个车速挂进这个挡，转速会是多少？（靠齿比）
2. `optimal_up_pct(gear)` —— 这个挡位最晚该在百分之几红线升挡？（靠扭矩曲线 + 齿比）
3. `limiter_pct` —— 这台车真实断油在百分之几？（靠断油检测）

三者互相独立：缺哪个就退回哪个的默认值，不会因为一个没学好就整台车不可用。
"""

from __future__ import annotations

from .gear_ratio import GearRatioTable
from .power_curve import TorqueCurve
from .rev_limiter import RevLimiter

SCHEMA = 1


class VehicleModel:
    def __init__(self, car_ordinal: int = 0) -> None:
        self.car_ordinal = car_ordinal
        self.ratios = GearRatioTable()
        self.curve = TorqueCurve()
        self.limiter = RevLimiter()
        self.max_rpm = 0.0
        self.samples = 0
        self.dirty = 0                  # 未存盘的改动计数
        self.load_error = ""            # 档案读进来时出的问题（给界面显示）
        self._opt_cache: dict[int, float | None] = {}
        self._opt_sig: dict[int, tuple] = {}
        self._prev: tuple[float, float, float, int] | None = None

    # -- 学习 --------------------------------------------------------------
    def _is_steady(self, tele, now: float) -> bool:
        """这一帧的"转速/车速"是否同步。

        换挡、起步、重刹时，发动机转速和车速并不同步（离合半联动、驱动轮打滑、
        游戏对车速做了平滑），此时算出来的齿比会偏 5% 以上。所以只把
        "比值在 0.25 秒内变化不超过 25%" 的帧当作稳态样本。
        """
        prev = self._prev
        self._prev = (now, tele.rpm, tele.speed_kmh, tele.gear)
        if prev is None or prev[3] != tele.gear:
            return False
        dt = now - prev[0]
        if dt <= 0.001 or dt > 0.25 or prev[2] <= 1.0 or tele.speed_kmh <= 1.0:
            return False
        r0 = prev[1] / prev[2]
        r1 = tele.rpm / tele.speed_kmh
        if r0 <= 0 or r1 <= 0:
            return False
        return abs(r1 - r0) / r0 / dt < 0.25

    def learn(self, tele, now: float) -> None:
        """喂一帧遥测。无效帧会被三个学习器各自挡掉。"""
        if not tele.usable:
            self._prev = None
            return

        self.max_rpm = tele.max_rpm or self.max_rpm
        if tele.car_ordinal:
            self.car_ordinal = tele.car_ordinal

        # 用游戏时间戳，不用 wall clock：加速回放、卡顿时判断才一致
        t_now = tele.timestamp_s if tele.get("timestamp_ms", 0) else now

        # 换挡过程中转速和车速的比值不可信（离合断开，转速在追车速）
        if tele.is_shifting:
            self._prev = None
            self.limiter.learn(tele.rpm, tele.gear, tele.throttle,
                               tele.max_rpm, True, t_now)
            return

        steady = self._is_steady(tele, t_now)
        got_ratio = self.ratios.learn(tele.speed_kmh, tele.gear,
                                      tele.rpm, tele.max_rpm, steady=steady)
        got_curve = self.curve.learn(tele.rpm, tele.torque,
                                     tele.max_rpm, tele.throttle)
        if got_ratio or got_curve:
            self.samples += 1
            self.dirty += 1

        self.limiter.learn(tele.rpm, tele.gear, tele.throttle,
                           tele.max_rpm, True, t_now)

    def mark_saved(self) -> None:
        self.dirty = 0

    # -- 查询 --------------------------------------------------------------
    def rpm_at(self, speed_kmh: float, gear: int) -> float | None:
        """按实测齿比推算转速。

        注意：**不要求「可信」**，只要样本够 10 个就用。因为就算这挡数据有点散，
        它的中位数也比「按挡位号反比」的公式准得多；不确定性由调用方用更严的
        安全余量来兜（见 engine 的 over_rev_uncertain_pct）。
        """
        if speed_kmh < 5.0 or self.ratios.samples(gear) < 10:
            return None
        ratio = self.ratios.median(gear)
        if not ratio:
            return None
        est = ratio * speed_kmh
        # 估出来的转速不可能超过红线太多，明显越界说明这一挡没学好
        if self.max_rpm > 0 and est > self.max_rpm * 1.25:
            return None
        return est

    @property
    def limiter_pct(self) -> float | None:
        return self.limiter.pct

    @property
    def max_gear(self) -> int:
        """学到 3 个以上挡位才敢用学习值当上限，否则可能把车卡在 2 挡。"""
        known = [g for g in self.ratios.gears() if self.ratios.known(g)]
        return max(known) if len(known) >= 3 else 0

    def optimal_up_pct(self, gear: int) -> float | None:
        """当前挡位的最晚升挡点（百分比）。算不出来返回 None。

        物理依据：轮上推力 ∝ 扭矩 × 总传动比，而总传动比 ∝ 转速/车速。
        所以「换到高一挡后推力还更大」的那一刻，就是该升挡的点。
        """
        sig = (self.ratios.version, self.curve.version,
               self.limiter.episodes, round(self.max_rpm))
        if self._opt_sig.get(gear) == sig:
            return self._opt_cache.get(gear)
        val = self._compute_optimal(gear)
        self._opt_cache[gear] = val
        self._opt_sig[gear] = sig
        return val

    def _compute_optimal(self, gear: int) -> float | None:
        if self.max_rpm <= 0 or not self.curve.ready:
            return None
        if not (self.ratios.known(gear) and self.ratios.known(gear + 1)):
            return None
        r1 = self.ratios.median(gear)
        r2 = self.ratios.median(gear + 1)
        if not r1 or not r2 or r2 >= r1:
            return None

        limit = self.limiter.rpm or self.max_rpm * 0.995
        lo_curve, hi_curve = self.curve.rpm_span
        start = max(lo_curve, 0.55 * self.max_rpm)
        stop = min(limit, hi_curve)
        if stop <= start:
            return None

        step = self.max_rpm / 200.0
        rpm = start
        while rpm <= stop:
            rpm_next = rpm * r2 / r1
            if rpm_next > limit:
                break
            t1 = self.curve.torque_at(rpm)
            t2 = self.curve.torque_at(rpm_next)
            if t1 is None or t2 is None:
                break
            if t2 * r2 >= t1 * r1:
                return rpm / self.max_rpm
            rpm += step

        return max(0.5, stop / self.max_rpm - 0.01)

    # -- 展示 / 持久化 -----------------------------------------------------
    @property
    def ready(self) -> bool:
        return any(self.ratios.known(g) for g in self.ratios.gears())

    def summary(self) -> str:
        """一行紧凑状态，给浮窗用（要短）。"""
        ratio = self.ratios.summary()
        curve = (f"扭矩 {self.curve.quality * 100:.0f}%"
                 if self.curve.ready else "扭矩 学习中")
        lim = (f"断油 {self.limiter_pct * 100:.1f}%"
               if self.limiter_pct else "断油 待确认")
        return f"{ratio} · {curve} · {lim}"

    def detail(self) -> str:
        """给控制台面板用，可以长一点。"""
        gears = [g for g in self.ratios.gears() if self.ratios.known(g)]
        ratio_txt = ("-".join(map(str, (min(gears), max(gears))))
                     if len(gears) >= 2 else "—")
        peak_t = self.curve.peak_torque_rpm()
        peak_p = self.curve.peak_power_rpm()
        curve_txt = (f"峰值扭矩 {peak_t:.0f} 转 / 峰值功率 {peak_p:.0f} 转"
                     if self.curve.ready else "数据不足")
        lim = self.limiter_pct
        lim_txt = f"{lim * 100:.1f}%" if lim else "待确认"
        return (f"车号 {self.car_ordinal}  齿比 {ratio_txt} 挡  "
                f"扭矩曲线 {curve_txt}  断油 {lim_txt}")

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "car_ordinal": self.car_ordinal,
            "max_rpm": round(self.max_rpm, 1),
            "samples": self.samples,
            "ratios": self.ratios.to_dict(),
            "curve": self.curve.to_dict(),
            "limiter": self.limiter.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "VehicleModel":
        if not isinstance(data, dict):
            return cls()
        try:
            ordinal = int(data.get("car_ordinal", 0) or 0)
        except (TypeError, ValueError):
            ordinal = 0

        # 版本校验。写档案时一直带着 schema，读的时候却从不看它——
        # 将来字段含义变了，旧档案会被当成新格式读进来，算出离谱的齿比，
        # 而且没有任何迹象。宁可当它没有、重新学。
        try:
            schema = int(data.get("schema", SCHEMA))
        except (TypeError, ValueError):
            schema = SCHEMA
        if schema != SCHEMA:
            v = cls()
            v.load_error = (f"档案版本 {schema} 与程序 {SCHEMA} 不一致，"
                            "已忽略，按新车重新学")
            return v

        v = cls(ordinal)
        try:
            v.max_rpm = float(data.get("max_rpm", 0.0) or 0.0)
            v.samples = int(data.get("samples", 0) or 0)
        except (TypeError, ValueError):
            v.max_rpm, v.samples = 0.0, 0
        v.ratios = GearRatioTable.from_dict(data.get("ratios"))
        v.curve = TorqueCurve.from_dict(data.get("curve"))
        v.limiter = RevLimiter.from_dict(data.get("limiter"))
        return v
