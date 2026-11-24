# -*- coding: utf-8 -*-
"""
扭矩曲线学习：把「油门到底」时的扭矩样本拟合成一条抛物线。

为什么要学
----------
阶段 4 的升挡点是 89%，这个数字是量用户习惯得来的，不是这辆车的最优值。
换一台车、换一个改装，这个数字就废了。真正决定"该在多少转升挡"的是
扭矩曲线 + 相邻挡齿比：只要下一挡的轮上推力还比当前挡大，就不该升挡。

怎么学
------
1. **只用全油门样本**（油门 ≥ 85%）。部分油门下的扭矩不代表外特性曲线。
2. **按 100 rpm 分箱，每箱取中位数**。这样每个转速区间权重相同，
   不会被"某一段路转速停留久、样本多"带偏。
3. **用最小二乘法拟合二次曲线** T(r) = a + b·x + c·x²，x 归一化到 [-1, 1]，
   避免 9000² 这种数量级让方程病态。
4. **数据不够就返回 None**，让引擎退回配置值。绝不拿半成品去决策。

关于单位：Forza 的 Power/Torque 会跟随游戏内的单位设置（kW/Nm 或 HP/lb-ft）。
我们只用**曲线形状**和**不同挡位之间的比值**，单位一致就不会影响结论。
"""

from __future__ import annotations

import math
from collections import deque

BIN_WIDTH = 100           # rpm 分箱宽度
BIN_CAP = 40              # 每个箱子最多留多少样本
MIN_TOTAL = 120           # 总共至少多少样本才算学过
MIN_BINS = 8              # 至少覆盖多少个转速箱
MIN_COVERAGE = 0.35       # 转速跨度至少占红线的多少
WOT_THROTTLE = 0.85       # 低于这个油门不采样
MIN_RPM_PCT = 0.20        # 低于这个转速不采样（怠速区扭矩没意义）


def _solve3(m: list[list[float]], v: list[float]) -> list[float] | None:
    """3×3 线性方程组，高斯消元（带主元）。矩阵奇异时返回 None。"""
    a = [list(m[i]) + [v[i]] for i in range(3)]
    for i in range(3):
        pivot = max(range(i, 3), key=lambda r: abs(a[r][i]))
        if abs(a[pivot][i]) < 1e-12:
            return None
        a[i], a[pivot] = a[pivot], a[i]
        div = a[i][i]
        for j in range(i, 4):
            a[i][j] /= div
        for r in range(3):
            if r == i:
                continue
            factor = a[r][i]
            if factor:
                for j in range(i, 4):
                    a[r][j] -= factor * a[i][j]
    return [a[0][3], a[1][3], a[2][3]]


class TorqueCurve:
    """全油门扭矩样本 → 二次曲线 → 峰值扭矩/功率转速。"""

    def __init__(self) -> None:
        self._bins: dict[int, deque] = {}
        self._total = 0
        self._added = 0
        self._coef: tuple[float, float, float] | None = None
        self._center = 4500.0        # 归一化中心，取红线一半
        self._min_bin = 0
        self._max_bin = 0
        self._max_engine = 0.0
        self.version = 0

    # -- 学习 --------------------------------------------------------------
    def learn(self, rpm: float, torque: float, max_rpm: float, throttle: float) -> bool:
        # NaN 会让下面所有区间判断失效（NaN 与任何数比较都是 False），
        # 最后在 int(rpm // BIN_WIDTH) 处抛 ValueError。所以先挡住。
        if not (math.isfinite(rpm) and math.isfinite(torque)
                and math.isfinite(max_rpm)):
            return False
        if max_rpm <= 0 or throttle < WOT_THROTTLE:
            return False
        if torque <= 0 or rpm < MIN_RPM_PCT * max_rpm:
            return False
        if rpm > 1.05 * max_rpm:
            return False

        self._max_engine = max(self._max_engine, max_rpm)
        key = int(rpm // BIN_WIDTH)
        buf = self._bins.get(key)
        if buf is None:
            buf = deque(maxlen=BIN_CAP)
            self._bins[key] = buf
        buf.append(torque)
        self._total += 1
        self._added += 1
        if self._added >= 40:
            self._fit()
        return True

    # -- 拟合 --------------------------------------------------------------
    def _fit(self) -> None:
        if self._added < 40 and self._coef is not None:
            return
        if len(self._bins) < MIN_BINS:
            return
        self._added = 0
        self._center = max(1000.0, self._max_engine / 2.0)

        # 每个箱子取中位数 → 等权重样本点
        pts = []
        for key in sorted(self._bins):
            vals = sorted(self._bins[key])
            rpm = (key + 0.5) * BIN_WIDTH
            pts.append((rpm, vals[len(vals) // 2]))

        s = [0.0] * 5          # Σx^0 .. Σx^4
        t = [0.0] * 3          # Σx^i·y
        for rpm, torque in pts:
            x = (rpm - self._center) / self._center
            y = torque
            p = 1.0
            for i in range(5):
                s[i] += p
                if i < 3:
                    t[i] += p * y
                p *= x

        normal = [[s[0], s[1], s[2]],
                  [s[1], s[2], s[3]],
                  [s[2], s[3], s[4]]]
        coef = _solve3(normal, t)
        if coef is None:
            self._coef = None
            return
        a, b, c = coef
        # 抛物线必须开口向下，否则不是扭矩曲线，是拟合失败
        if c >= 0 or a <= 0:
            self._coef = None
            return
        self._coef = (a, b, c)
        self._min_bin = min(self._bins) * BIN_WIDTH
        self._max_bin = (max(self._bins) + 1) * BIN_WIDTH
        self.version += 1

    # -- 查询 --------------------------------------------------------------
    @property
    def rpm_span(self) -> tuple[float, float]:
        if not self._bins:
            return (0.0, 0.0)
        return (min(self._bins) * BIN_WIDTH, (max(self._bins) + 1) * BIN_WIDTH)

    @property
    def coverage(self) -> float:
        if self._max_engine <= 0:
            return 0.0
        lo, hi = self.rpm_span
        return max(0.0, (hi - lo) / self._max_engine)

    @property
    def quality(self) -> float:
        """0..1：样本数、覆盖宽度、转速跨度三者取最弱的那个。"""
        n = min(1.0, self._total / 400.0)
        bins = min(1.0, len(self._bins) / 20.0)
        return min(n, bins, min(1.0, self.coverage / 0.6))

    @property
    def ready(self) -> bool:
        return (self._coef is not None
                and self._total >= MIN_TOTAL
                and len(self._bins) >= MIN_BINS
                and self.coverage >= MIN_COVERAGE)

    def _eval(self, rpm: float) -> float:
        a, b, c = self._coef
        x = (rpm - self._center) / self._center
        return a + b * x + c * x * x

    def torque_at(self, rpm: float) -> float | None:
        """查表。超出已观测范围时按端点取值——外推没有依据，不如夹住。"""
        if not self.ready:
            return None
        lo, hi = self.rpm_span
        r = min(max(rpm, lo), hi)
        return max(0.0, self._eval(r))

    def peak_torque_rpm(self) -> float | None:
        if not self.ready:
            return None
        a, b, c = self._coef
        # dT/dx = b + 2c·x = 0
        x = -b / (2 * c)
        rpm = self._center * (1 + x)
        lo, hi = self.rpm_span
        return min(max(rpm, lo), hi)

    def peak_power_rpm(self) -> float | None:
        if not self.ready:
            return None
        lo, hi = self.rpm_span
        best_rpm, best_p = lo, -1.0
        step = max(25.0, (hi - lo) / 120.0)
        rpm = lo
        while rpm <= hi:
            p = self._eval(rpm) * rpm
            if p > best_p:
                best_p, best_rpm = p, rpm
            rpm += step
        return best_rpm

    def summary(self) -> str:
        if not self.ready:
            return f"扭矩 学习中（{self._total} 样本）"
        return (f"扭矩 {self.quality * 100:.0f}%"
                f"（峰值 {self.peak_torque_rpm():.0f} 转）")

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        if self._coef is None:
            return {}
        bins = {}
        for key in sorted(self._bins):
            vals = sorted(self._bins[key])
            bins[str(key)] = round(vals[len(vals) // 2], 1)
        return {"bins": bins, "total": self._total, "max_rpm": self._max_engine}

    @classmethod
    def from_dict(cls, data: dict | None) -> "TorqueCurve":
        """从档案还原。存的是**分箱中位数**而不是拟合系数——
        系数换个样本就会变，箱子才是原始证据，重新拟合更可靠。"""
        t = cls()
        if not isinstance(data, dict):
            return t
        bins = data.get("bins")
        if not isinstance(bins, dict) or not bins:
            return t
        try:
            for key, val in bins.items():
                k = int(key)
                v = float(val)
                if v <= 0:
                    continue
                t._bins[k] = deque([v] * 3, maxlen=BIN_CAP)
            t._total = int(data.get("total", len(t._bins) * 3))
            t._max_engine = float(data.get("max_rpm", 0.0) or 0.0)
        except (TypeError, ValueError):
            return cls()
        if len(t._bins) < MIN_BINS:
            return cls()
        if t._max_engine <= 0:
            t._max_engine = (max(t._bins) + 1) * BIN_WIDTH
        t._added = 40
        t._fit()
        t._added = 0
        return t
