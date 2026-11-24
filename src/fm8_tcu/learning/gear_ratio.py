# -*- coding: utf-8 -*-
"""
齿比学习：从遥测里量出每个挡位的「转速 / 车速」比值。

为什么需要它
------------
换挡决策里最危险的估算是「降一挡之后转速会变成多少」。阶段 4 用的是

    rpm_after = rpm * gear / (gear - 1)

这个公式隐含了「相邻挡位的齿比按挡位号成反比」。真实变速箱完全不是这样：
6 挡箱的 5→6 可能只差 18%，而 1→2 能差 45%。估错的后果我们亲眼见过——
54.8 秒降挡时估成 90%，实际只有 78%，于是 0.9 秒后又升回去，白换一次挡。

量出来的比值是纯物理量，车型、改装、轮胎直径全部自动包含在内，不用查数据库。

怎么算才靠谱
------------
1. **取中位数，不取平均**。车速读数偶尔有毛刺，平均值会被拉偏，中位数不会。
2. **偏离中位数 25% 以上的样本直接丢弃**。车轮打滑时转速和车速对不上，
   这类样本必须挡在门外，否则会把整张表污染掉。
3. **优先用「稳态」样本**。油门全开起步、重刹降挡这些瞬间，发动机转速和
   车速并不同步（离合半联动、驱动轮打滑、游戏对车速做了平滑），
   比值会偏 5% 以上。实测：3 挡用全部样本离散度 4.34%，只用稳态样本 0.50%。
4. **样本够 30 个才算「已知」**。少于这个数就退回旧公式，不硬用半成品。
5. **做单调性检查**。挡位越高齿比必须越小；违反说明样本有问题，标记为不可信。
"""

from __future__ import annotations

import math
import time
from collections import deque

MIN_SAMPLE = 30          # 一个挡位至少要多少样本才算学到
MAX_SAMPLE = 300         # 每个挡位最多留多少样本（滚动窗口，会忘掉旧数据）
MAX_SPREAD = 0.06        # 相对中位数的离散度上限（用 MAD 衡量）
                         # 6% 看着松，但它换来的是低挡位能被用上：
                         # 实测 2 挡稳态离散度 5.26%，而按旧公式估算会差 20% 以上。
                         # 单调性检查仍然拦得住真正错得离谱的值。
OUTLIER_RATIO = 0.25     # 偏离中位数超过这个比例直接丢
MIN_SPEED_KMH = 15.0     # 低于这个车速不采样（离合半联动、蠕行会失真）
MIN_RPM_PCT = 0.15       # 低于这个转速不采样（怠速抖动）
MAX_RPM_PCT = 1.10       # 超过红线 10% 视为坏数据
PLAUSIBLE = (5.0, 500.0)  # rpm per km/h 的合理范围，超出视为解析错位


class GearRatioTable:
    """每个挡位一张样本表，输出中位数齿比和可信度。

    「稳态」样本单独存一份：只有它攒够 30 个才用它，否则用全部样本。
    这样低挡位（稳态时机少）不会因为挑剔而永远学不到。
    """

    def __init__(self) -> None:
        self._buf: dict[int, deque] = {}
        self._steady: dict[int, deque] = {}
        self._median: dict[int, float] = {}
        self._spread: dict[int, float] = {}
        self._used_steady: dict[int, bool] = {}
        self._added = 0              # 自上次重算以来新增的样本数
        self._last_calc = 0.0
        self.version = 0             # 每次重算 +1，供上层缓存失效

    # -- 学习 --------------------------------------------------------------
    def learn(self, speed_kmh: float, gear: int, rpm: float, max_rpm: float,
              steady: bool = True) -> bool:
        """喂一帧。steady 表示这一帧转速与车速是否同步（由上层判断）。"""
        # NaN 和任何数比较都返回 False，所以下面的区间判断挡不住它，必须先显式检查。
        # 这不是重复劳动：协议层已经拦过一道，但学习器可以被单独调用（测试、脚本）。
        if not (math.isfinite(speed_kmh) and math.isfinite(rpm)
                and math.isfinite(max_rpm)):
            return False
        if max_rpm <= 0 or gear < 1 or gear > 12 or gear == 11:
            return False
        if speed_kmh < MIN_SPEED_KMH:
            return False
        pct = rpm / max_rpm
        if pct < MIN_RPM_PCT or pct > MAX_RPM_PCT:
            return False

        ratio = rpm / speed_kmh
        if not (PLAUSIBLE[0] <= ratio <= PLAUSIBLE[1]):
            return False

        buf = self._buf.get(gear)
        if buf is None:
            buf = deque(maxlen=MAX_SAMPLE)
            self._buf[gear] = buf

        # 粗筛：偏离已有中位数太多的样本，多半是打滑/换挡残留
        if len(buf) >= 15:
            med = self._median.get(gear)
            if med and abs(ratio / med - 1.0) > OUTLIER_RATIO:
                return False

        buf.append(ratio)
        if steady:
            s = self._steady.get(gear)
            if s is None:
                s = deque(maxlen=MAX_SAMPLE)
                self._steady[gear] = s
            s.append(ratio)
        self._added += 1
        return True

    # -- 查询 --------------------------------------------------------------
    def _source(self, gear: int) -> deque | None:
        steady = self._steady.get(gear)
        if steady is not None and len(steady) >= MIN_SAMPLE:
            return steady
        return self._buf.get(gear)

    def _refresh(self) -> None:
        """重算中位数。不是每帧都算——攒够 8 个样本或过了 0.5 秒才重算。"""
        mono = time.monotonic()
        if self._added < 8 and (mono - self._last_calc) < 0.5 and self._median:
            return
        self._added = 0
        self._last_calc = mono
        for g in self._buf:
            buf = self._source(g)
            if buf is None or len(buf) < 5:
                continue
            vals = sorted(buf)
            med = vals[len(vals) // 2]
            if med <= 0:
                continue
            dev = sorted(abs(v - med) for v in vals)
            self._median[g] = med
            self._spread[g] = dev[len(dev) // 2] / med
            self._used_steady[g] = (buf is self._steady.get(g))
        self.version += 1

    def samples(self, gear: int) -> int:
        buf = self._source(gear)
        return len(buf) if buf is not None else 0

    def using_steady(self, gear: int) -> bool:
        self._refresh()
        return bool(self._used_steady.get(gear))

    def median(self, gear: int) -> float | None:
        self._refresh()
        return self._median.get(gear)

    def spread(self, gear: int) -> float:
        self._refresh()
        return self._spread.get(gear, 1.0)

    def sane(self, gear: int) -> bool:
        """齿比是否可信：样本够、离散度小、且比低挡小比高挡大。"""
        self._refresh()
        if gear not in self._median or self._spread.get(gear, 1.0) > MAX_SPREAD:
            return False
        lower = self._median.get(gear + 1)          # 更高挡，齿比更小
        higher = self._median.get(gear - 1)         # 更低挡，齿比更大
        if higher and self._median[gear] >= higher * 0.995:
            return False
        if lower and self._median[gear] <= lower * 1.005:
            return False
        return True

    def known(self, gear: int) -> bool:
        return self.samples(gear) >= MIN_SAMPLE and self.sane(gear)

    def gears(self) -> list[int]:
        """已经学到的挡位（按样本数排序，不代表可信）。"""
        self._refresh()
        return sorted(g for g in self._median if self.samples(g) >= 10)

    def confidence(self, gear: int) -> float:
        """0..1，用来在界面上显示「学到几成」。"""
        return min(1.0, self.samples(gear) / 120.0)

    def summary(self) -> str:
        self._refresh()
        good = [g for g in self.gears() if self.known(g)]
        if not good:
            return "齿比 学习中"
        lo, hi = min(good), max(good)
        gap = [g for g in range(lo, hi + 1) if g not in good]
        if gap:
            return f"齿比 {lo}-{hi} 挡（缺 {','.join(map(str, gap))}）"
        return f"齿比 {lo}-{hi} 挡"

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        self._refresh()
        out = {}
        for g in sorted(self._median):
            out[str(g)] = {"ratio": round(self._median[g], 4),
                           "spread": round(self._spread.get(g, 1.0), 4),
                           "n": self.samples(g)}
        return out

    @classmethod
    def from_dict(cls, data: dict | None) -> "GearRatioTable":
        t = cls()
        if not isinstance(data, dict):
            return t
        for key, item in data.items():
            try:
                g = int(key)
                ratio = float(item["ratio"])
                spread = float(item.get("spread", 0.0))
                n = int(item.get("n", MIN_SAMPLE))
            except (KeyError, TypeError, ValueError):
                continue
            if not (PLAUSIBLE[0] <= ratio <= PLAUSIBLE[1]) or g <= 0 or g > 12:
                continue
            # 存档只保存结论，不保存原始遥测，文件才不会越滚越大。
            # 但**离散度必须一起存**：如果还原成一堆相同的值，离散度会变成 0，
            # 一份学得不好的齿比在重启后会被误判成"可信"。
            #
            # 还原时用 ±2s 而不是 ±s：我们要还原的是"中位绝对偏差"（MAD），
            # 而 5 点集合 [-2s,-s,0,s,2s] 的 MAD 正好等于 s。用 ±s 会得到 s/2，
            # 离散度凭空减半，一个 10% 的坏数据读回来变成 5%，就混过门槛了。
            spread = max(0.0, min(spread, 0.5))
            offsets = (-2.0, -1.0, 0.0, 1.0, 2.0)
            vals = [ratio * (1.0 + spread * k) for k in offsets]
            buf = deque(vals * max(1, min(n, MAX_SAMPLE) // len(vals)),
                        maxlen=MAX_SAMPLE)
            for _ in range(max(0, min(n, MAX_SAMPLE) - len(buf))):
                buf.append(ratio)
            t._buf[g] = buf
        t._added = MAX_SAMPLE
        t._refresh()
        return t
