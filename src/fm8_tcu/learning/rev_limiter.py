# -*- coding: utf-8 -*-
"""
断油转速检测：找出这台车**真实**的转速上限。

为什么不能直接用 EngineMaxRpm
------------------------------
FM8 报的 EngineMaxRpm 是仪表上的名义红线，不是断油点。实测用户这台车：
名义 9000，实际断油 8271（91.9%）。如果按 9000 去算升挡点，最后 8% 全是白转，
而且会被断油反复截断，车会一顿一顿的。

断油长什么样
------------
全油门、同一个挡位、转速一路爬升 → 到某个点突然掉下来（燃油切断）→ 又爬上去。
这个「掉头」就是签名。我们的判据：

    油门 ≥ 85%  且  没换挡  且  爬升持续 ≥ 0.35 秒
    且  峰值 ≥ 70% 红线
    且  从峰值掉下来 ≥ 250 转

普通升挡也会掉转速，但升挡一定先经过挡位 11，而且挡位会变——所以判据里
「挡位不变」这一条就能把它排除掉。

单次检测可能是压路肩、车轮打滑造成的误判，所以要**两次独立确认**才采信。
"""

from __future__ import annotations

import math

MIN_PCT = 0.70          # 峰值低于红线的 70% 不算断油
MIN_CLIMB_S = 0.35      # 必须连续爬升这么久
DROP_RPM = 250          # 从峰值掉这么多转才算断油
WOT_THROTTLE = 0.85
NEED_EPISODES = 2       # 需要几次独立确认


class RevLimiter:
    def __init__(self) -> None:
        self.estimate = 0.0     # 学到的断油转速（原始转速值）
        self.episodes = 0       # 确认次数
        self.max_seen = 0.0     # 全油门时见过的最高转速
        self._peak = 0.0
        self._climb_start = 0.0
        self._gear = 0
        self._max_rpm = 0.0
        self._last_t = 0.0

    # -- 学习 --------------------------------------------------------------
    def learn(self, rpm: float, gear: int, throttle: float, max_rpm: float,
              usable: bool, now: float) -> None:
        if not (math.isfinite(rpm) and math.isfinite(max_rpm) and math.isfinite(now)):
            self._peak = 0.0
            return
        if not usable or max_rpm <= 0:
            self._peak = 0.0
            return

        # 时间倒退（游戏重开一局）或长时间没包（暂停）→ 之前的爬升作废。
        # 不做这个检查，一次暂停就会被当成"转速掉头"，误判成断油。
        if now < self._last_t or (now - self._last_t) > 0.5:
            self._peak = 0.0
            self._climb_start = now
        self._last_t = now

        self._max_rpm = max_rpm

        # 换挡中、倒挡、档位无效：全部重置，避免把换挡掉速当成断油
        if gear <= 0 or gear == 11:
            self._peak = 0.0
            self._gear = gear
            return

        if gear != self._gear:
            self._gear = gear
            self._peak = 0.0
            return

        if throttle < WOT_THROTTLE:
            self._peak = 0.0
            return

        if rpm > self.max_seen:
            self.max_seen = rpm

        if rpm > self._peak:
            if self._peak <= 0:
                self._climb_start = now
            self._peak = rpm
            return

        # 转速掉头了
        if (self._peak >= MIN_PCT * max_rpm
                and (now - self._climb_start) >= MIN_CLIMB_S
                and (self._peak - rpm) >= DROP_RPM):
            self.episodes += 1
            self.estimate = max(self.estimate, self._peak)
            self._peak = 0.0
            self._climb_start = now

    # -- 查询 --------------------------------------------------------------
    @property
    def rpm(self) -> float | None:
        """学到的断油转速；证据不足时返回 None，让上层退回名义红线。"""
        if self.episodes < NEED_EPISODES or self._max_rpm <= 0 or self.estimate <= 0:
            return None
        if not (0.70 * self._max_rpm <= self.estimate <= 1.02 * self._max_rpm):
            return None
        return self.estimate

    @property
    def pct(self) -> float | None:
        r = self.rpm
        return (r / self._max_rpm) if r else None

    def summary(self) -> str:
        if self.episodes < NEED_EPISODES:
            extra = f"（已见 {self.max_seen:.0f} 转）" if self.max_seen else ""
            return f"断油 待确认{extra}"
        return f"断油 {self.pct * 100:.1f}%"

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"rpm": round(self.estimate, 1), "episodes": self.episodes,
                "max_seen": round(self.max_seen, 1), "max_rpm": round(self._max_rpm, 1)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "RevLimiter":
        r = cls()
        if not isinstance(data, dict):
            return r
        try:
            r.estimate = float(data.get("rpm", 0.0) or 0.0)
            r.episodes = int(data.get("episodes", 0) or 0)
            r.max_seen = float(data.get("max_seen", 0.0) or 0.0)
            r._max_rpm = float(data.get("max_rpm", 0.0) or 0.0)
        except (TypeError, ValueError):
            return cls()
        return r
