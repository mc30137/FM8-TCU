# -*- coding: utf-8 -*-
"""
自动换挡决策引擎（阶段 4：只做 RACE 模式）。

设计上的三条铁律，都来自前两个参考项目的教训：

1. **每帧都要能解释"为什么没换挡"。**
   参考项目定义了 blocked_by 字段却从不填充，导致出问题时无从下手。
   这里每个 Decision 都带 reason 和 blocked_by。

2. **不在不确定的时候动作。**
   遥测无效、正在换挡(11)、上一条指令没确认、倒车——一律不动。
   宁可少换一次，也不要在错误时刻换。

3. **换挡有代价。**
   实测一次换挡 70–150ms 无动力，所以冷却必须大于它，且不允许连发。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .protocol import Telemetry

# 顶挡猜测的有效期。超过这个时间就允许再试一次升挡。
# 为什么需要：猜错一次（比如游戏吞了两次按键）不该永久关掉最高挡。
TOP_GUESS_TTL_S = 90.0

# 车速趋势的观察窗口
TREND_WINDOW_S = 0.40
TREND_MIN_SPAN_S = 0.20

# ══════════════════════════════════════════════════════════════════════════
# 参数
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Policy:
    """RACE 模式的全部可调参数。集中在这里，调参不用改逻辑。

    默认值不是拍脑袋定的，是从用户真实驾驶的 15 次换挡里量出来的
    （docs/01-协议实测记录.md）：

        全油门升挡：转速 86.0%–90.8%，中位 88.9%（实测断油点约 91.9%）
        刹车降挡：  转速 56.8%–67.8%，刹车 100%、油门 0%
        滑行降挡：  转速 44.3%，油门 4%
    """

    # 升挡：油门 0 → 80%，油门 1 → 90%（对齐实测的 88.9% 中位）
    up_pct_low: float = 0.79
    up_pct_high: float = 0.89
    up_min_throttle: float = 0.20        # 油门低于此值不升挡

    # 刹车降挡（赛道驾驶的主力降挡方式）
    down_brake_min: float = 0.30         # 刹车超过此值视为"在制动"
    down_brake_pct: float = 0.68         # 制动中转速掉到此值以下就降一挡

    # 动力降挡（出弯给油但转速太低）
    down_pct: float = 0.42
    down_min_throttle: float = 0.25
    down_min_speed_kmh: float = 25.0

    # 滑行降挡（收油过弯，既不踩油也不踩刹）
    coast_pct: float = 0.45
    coast_max_throttle: float = 0.10
    coast_max_brake: float = 0.10
    coast_min_speed_kmh: float = 30.0

    # 静止回 1 挡
    standstill_kmh: float = 3.0

    # 时间
    cooldown_s: float = 0.25             # 一次换挡后最短间隔（实测换挡 70–150ms）
    pending_timeout_s: float = 0.80      # 发出后多久没确认算失败
    after_up_block_down_s: float = 1.20  # 升挡后这段时间内不许降（防抖）
    after_down_block_up_s: float = 0.60  # 降挡后这段时间内不许升

    # 降挡后如果还在减速，就不许升回去。
    # 判据用「车速趋势」而不是「车速绝对值」——用绝对值会死锁：
    # 3 挡 163 km/h 降到 2 挡，2 挡跑到断油只有 159，车速永远回不到 163，
    # 结果顶着断油跑 4 秒。趋势判据没有这个问题：到断油时车速是平的，允许升挡。
    upshift_max_decel_kmh_s: float = 3.0
    # 油门够深就认为你在加速意图里，不再看减速趋势。
    # 没有这条的话，上坡顶着断油跑（车速会被坡度拖慢一点点）会被误拦。
    upshift_trust_throttle: float = 0.80

    # 保护
    over_rev_pct: float = 0.97           # 降挡后预计转速超过此值 → 不降
    over_rev_uncertain_pct: float = 0.92  # 齿比样本不足时用这个（更严）
    max_gear: int = 10                   # 硬上限（学到挡位数量后自动接管）

    # 学习成果的使用方式（阶段 5）
    use_learned_shift: bool = True
    learned_up_drop_max: float = 0.08    # 学习值最多把升挡点往下拉 8%
    limiter_margin: float = 0.015        # 距断油点留 1.5% 余量


# ══════════════════════════════════════════════════════════════════════════
# 决策结果
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Decision:
    action: str = ""          # "UP" / "DOWN" / ""
    reason: str = ""          # 触发原因（或当前状态）
    blocked_by: str = ""      # 被哪条规则挡住
    detail: str = ""          # 具体数值，便于事后核对

    @property
    def is_shift(self) -> bool:
        return bool(self.action)

    def describe(self) -> str:
        if self.action:
            return f"{self.action:<4} {self.reason}  ({self.detail})"
        if self.blocked_by:
            return f"HOLD {self.blocked_by}  ({self.detail})"
        return f"HOLD {self.reason}  ({self.detail})"


@dataclass
class ShiftRecord:
    t: float
    action: str
    from_gear: int
    ok: bool
    latency_ms: int
    reason: str


# ══════════════════════════════════════════════════════════════════════════
# 引擎
# ══════════════════════════════════════════════════════════════════════════

class AutoShiftEngine:
    def __init__(self, policy: Policy | None = None, expect_ack: bool = True,
                 vehicle=None):
        self.p = policy or Policy()
        # expect_ack=False：不等待回执。
        # 什么时候会这样：程序没启用（没按键）的时候。如果照样设 pending，
        # 下一帧就会判"等待上一条确认"，然后等 0.8 秒超时——界面上的决策
        # 会变成每 0.8 秒跳一次，看起来像卡住了。
        # 这不是"演练功能"，是引擎的一条基本假设：**没人按键，就不该等回执**。
        self.expect_ack = expect_ack
        # vehicle 是 learning.VehicleModel。为 None 时全部退回配置值，
        # 所以「没有学习数据」也能跑，只是估得粗一点。
        self.vehicle = vehicle
        self.pending: tuple[float, int, str, str] | None = None   # (时间, 起始挡, 动作, 原因)
        self.last_shift_t = -1e9     # 初始不处于冷却中
        self.last_action = ""
        self._speed_hist: deque = deque(maxlen=80)   # (时间, 车速) 用于算加减速趋势
        self.history: list[ShiftRecord] = []
        self.last_resolved: ShiftRecord | None = None
        self.timeouts = 0
        self.rejected = 0        # 因保护规则被拒的次数
        # 顶挡推断：连续两次升挡失败（挡位没变）就认定当前挡是最高挡。
        # 为什么需要它：学到的挡位只可能来自"开过的挡"，如果程序把学到的最高挡
        # 当成硬上限，就永远进不了下一挡，也就永远学不到它——自我锁死。
        #
        # 但这个推断**只影响"还能不能往上试"**，绝不影响降挡，而且会过期。
        # 记成 (挡位, 时间) 而不是单独一个数字，就是为了让它能失效。
        self._top_guess: tuple[int, float] = (0, 0.0)
        self._up_fail_gear = 0
        self._up_fail_count = 0
        # 上一条指令发出前的状态快照，供 rollback_command() 用
        self._pre_command: tuple | None = None

    # -- 参数计算 ----------------------------------------------------------
    def upshift_pct(self, throttle: float, gear: int = 0) -> float:
        """当前该在百分之几红线升挡。

        三层叠加，每层都只能让升挡点更保守、不能更激进：

        1. 油门曲线（驾驶习惯）：油门越深，越接近红线。
        2. 扭矩曲线 + 齿比（物理最优）：学习值只能把升挡点往下拉，
           最多拉 8%。这样即使某次学到脏数据，也不会把车开成"2000 转就升挡"。
        3. 断油转速（硬保护）：永远留 1.5% 余量。
        """
        t = max(0.0, min(1.0, throttle))
        p = self.p
        base = p.up_pct_low + (p.up_pct_high - p.up_pct_low) * t

        v = self.vehicle
        if v is None or not p.use_learned_shift:
            return base

        learned = v.optimal_up_pct(gear) if gear > 0 else None
        if learned is not None:
            base = min(base, max(learned, base - p.learned_up_drop_max))

        lim = v.limiter_pct
        if lim:
            base = min(base, lim - p.limiter_margin)

        return max(0.30, base)

    def gear_ceiling(self, now: float = 0.0) -> int:
        """允许尝试的最高挡。

        规则：**学到的最高挡 + 1**。多给一挡的余量，才能发现"还没开过的下一挡"。
        等到连续两次升挡都失败，`_top_guess` 会把它钉住，暂时不再重试。

        两个关键约束：
        - 有效期 90 秒。过期后重新给一次机会——丢键是最常见原因，
          不能因为两次丢键就永久放弃最高挡。
        - **只用于判断升挡**，不参与降挡。曾经把它当成"当前挡位的上限"，
          结果 6 挡一旦被钉住，连重刹都降不下去，车就卡在挡里了。
        """
        if self._top_guess[0]:
            gear, t = self._top_guess
            if now <= 0 or (now - t) <= TOP_GUESS_TTL_S:
                return gear
            self._top_guess = (0, 0.0)       # 过期，允许再试
        return self._learned_ceiling()

    def _learned_ceiling(self) -> int:
        """只看学习结果和参数，不看运行状态。preview 也用它。"""
        v = self.vehicle
        if v is not None:
            learned = v.max_gear
            if learned >= 3:
                return min(learned + 1, self.p.max_gear)
        return self.p.max_gear

    def reset_car(self) -> None:
        """换车时清空与"这台车"绑定的运行状态。

        不清的话，上一台车的顶挡猜测、防抖计时、成功率会原样套到新车上，
        而这些数字对一个没见过的车毫无意义。
        """
        self.pending = None
        self.last_shift_t = -1e9
        self.last_action = ""
        self._speed_hist.clear()
        self.history.clear()
        self.last_resolved = None
        self.timeouts = 0
        self.rejected = 0
        self._top_guess = (0, 0.0)
        self._up_fail_gear = 0
        self._up_fail_count = 0
        self._pre_command = None

    def rpm_after(self, tele: Telemetry, target_gear: int) -> tuple[float, bool]:
        """换到目标挡之后的转速估计，返回 (转速, 是否来自实测齿比)。"""
        v = self.vehicle
        if v is not None:
            est = v.rpm_at(tele.speed_kmh, target_gear)
            if est is not None:
                return est, True
        # 兜底：假设相邻挡齿比按挡位号成反比（阶段 4 的老公式）
        return tele.rpm * tele.gear / max(target_gear, 1), False

    # -- 主入口 ------------------------------------------------------------
    def speed_trend(self, now: float) -> float:
        """最近约 0.4 秒的车速变化率，单位 km/h 每秒。

        正数在加速，负数在减速，接近 0 表示速度稳住了（比如顶着断油跑）。
        样本不够时返回 0——宁可当作"没在减速"，也不要拿两个孤立点去判断。

        `now` 是参数而不是从历史里取：调用方决定"以哪一刻为当前"，
        否则拿录制数据回放时，函数会用错误的时间基准。
        """
        if now <= 0 or len(self._speed_hist) < 5:
            return 0.0
        pts = [(t, s) for t, s in self._speed_hist
               if 0.0 <= now - t <= TREND_WINDOW_S]
        if len(pts) < 5:
            return 0.0
        t1, s1 = pts[-1]
        t0, s0 = pts[0]
        span = t1 - t0
        if span < TREND_MIN_SPAN_S:
            return 0.0
        return (s1 - s0) / span

    def process(self, tele: Telemetry, now: float) -> Decision:
        p = self.p

        # ---- 0. 先结算上一条指令 ----
        resolved = self._resolve(tele, now)

        # ---- 1. 数据可用性 ----
        if not tele.usable:
            why = "发动机未启动/不在比赛中" if not tele.engine_running else "不在比赛中"
            return Decision(reason="遥测不可用", blocked_by=why,
                            detail=f"max_rpm={tele.max_rpm:.0f} race={int(tele.race_on)}")

        # 记下车速，后面用来看"是不是在减速"
        self._speed_hist.append((now, tele.speed_kmh))

        # ---- 2. 换挡中 ----
        if tele.is_shifting:
            return Decision(reason="换挡进行中", blocked_by=f"gear={tele.gear}",
                            detail="实测换挡耗时 70–150ms")

        # ---- 3. 上一条没确认 ----
        if self.pending is not None:
            waited = (now - self.pending[0]) * 1000
            return Decision(reason="等待上一条确认", blocked_by="pending",
                            detail=f"已等 {waited:.0f}ms")

        # ---- 4. 倒车 ----
        if tele.is_reversing:
            return Decision(reason="倒车中，TCU 被动", blocked_by=f"vel_z={tele.vel_z:.1f}")

        gear = tele.gear

        # ---- 5. 档位 0 但不是倒车：可能是菜单残留，先要求确认车速 ----
        if gear <= 0:
            return Decision(reason="档位无效", blocked_by=f"gear={gear}",
                            detail=f"speed={tele.speed_kmh:.1f}")

        # ---- 6. 冷却 ----
        # 放在静止判断**之前**。曾经静止回 1 挡走的是捷径，能连着发 DOWN，
        # 与"换挡有代价、不允许连发"直接冲突。
        since = now - self.last_shift_t
        if since < p.cooldown_s:
            return Decision(reason="冷却中", blocked_by="cooldown",
                            detail=f"{since * 1000:.0f}/{p.cooldown_s * 1000:.0f}ms")

        # ---- 7. 静止 ----
        if tele.speed_kmh < p.standstill_kmh:
            if gear >= 2:
                # 刹停后回 1 挡不受"刚升挡不许降"的限制：那是防抖，防的是
                # 升完立刻降回去的来回折腾；车都停了，降回 1 挡是真实需求。
                return self._command("DOWN", now, tele, "静止回 1 挡",
                                     f"speed={tele.speed_kmh:.1f}")
            return Decision(reason="静止", detail=f"gear={gear}")

        # ---- 8. 规则求值 ----
        decision = self._rules(tele, now, since)
        if decision.is_shift:
            return self._command(decision.action, now, tele,
                                 decision.reason, decision.detail)
        return decision

    # -- 纯规则（不看 pending / 冷却 / 历史）--------------------------------
    def _rules(self, tele: Telemetry, now: float, since: float, *,
               last_action: str | None = None,
               trend: float | None = None,
               ceiling: int | None = None) -> Decision:
        """只回答"按当前数据，该不该换"。冷却和防抖在外面处理。

        三个运行状态（上一条动作、车速趋势、挡位上限）可以显式传入。
        不传时从自身取——`process()` 就是这么用的；`preview()` 传空值进去，
        于是它真的只看这一帧，不看历史。参数化之后，"离线回放能复现实车判断"
        这句话才成立。
        """
        p = self.p
        gear = tele.gear
        if last_action is None:
            last_action = self.last_action
        if trend is None:
            trend = self.speed_trend(now)
        if ceiling is None:
            ceiling = self.gear_ceiling(now)

        if gear <= 0:
            return Decision(reason="档位无效", blocked_by=f"gear={gear}",
                            detail=f"speed={tele.speed_kmh:.1f}")

        if tele.speed_kmh < p.standstill_kmh:
            if gear >= 2:
                return Decision(action="DOWN", reason="静止回 1 挡",
                                detail=f"speed={tele.speed_kmh:.1f}")
            return Decision(reason="静止", detail=f"gear={gear}")

        rpm_pct = tele.rpm_pct
        up_pct = self.upshift_pct(tele.throttle, gear)
        base_pct = p.up_pct_low + (p.up_pct_high - p.up_pct_low) * max(0.0, min(1.0, tele.throttle))
        tag = " 学习值" if up_pct < base_pct - 1e-6 else ""
        block_down = (last_action == "UP" and since < p.after_up_block_down_s)
        block_up = (last_action == "DOWN" and since < p.after_down_block_up_s)
        # 刚降过挡、而且车速还在掉 → 说明还在减速，不该升回去
        hold_speed = (tele.throttle < p.upshift_trust_throttle
                      and trend < -p.upshift_max_decel_kmh_s)

        # 到顶挡只拦**升挡**。降挡必须有路可走——车在 6 挡重刹时必须能降到 5 挡，
        # 否则一次丢键就能把车永久卡在挡里。
        at_ceiling = gear >= ceiling

        # ---- 升挡 ----
        if (not at_ceiling
                and not block_up
                and not hold_speed
                and tele.throttle >= p.up_min_throttle
                and tele.brake < 0.05
                and rpm_pct >= up_pct):
            return Decision(action="UP", reason="达到升挡转速",
                            detail=f"{rpm_pct * 100:.1f}% ≥ {up_pct * 100:.1f}%{tag}")

        # ---- 降挡（三种情形，优先级：刹车 > 动力 > 滑行）----
        if not block_down and gear >= 2 and tele.speed_kmh >= p.down_min_speed_kmh:
            why = ""
            if tele.brake >= p.down_brake_min and rpm_pct < p.down_brake_pct:
                why = f"刹车 {tele.brake * 100:.0f}%，转速仅 {rpm_pct * 100:.0f}%"
            elif (tele.throttle >= p.down_min_throttle
                  and rpm_pct < p.down_pct):
                why = f"给油但转速仅 {rpm_pct * 100:.0f}%"
            elif (tele.throttle <= p.coast_max_throttle
                  and tele.brake <= p.coast_max_brake
                  and rpm_pct < p.coast_pct):
                why = f"滑行转速 {rpm_pct * 100:.0f}%"

            if why:
                # 防超转：用实测齿比估落点（学不到才退回旧公式）
                est, learned = self.rpm_after(tele, gear - 1)
                trusted = (self.vehicle is not None
                           and self.vehicle.ratios.known(gear - 1))
                # 齿比不确定时用更严的门槛：估 95% 看着安全，但齿比本身有 10% 误差，
                # 真实落点可能已经超红线，游戏会直接拒绝降挡（实测里连吃两次 800ms 超时）。
                limit_pct = (p.over_rev_pct if (not learned or trusted)
                             else p.over_rev_uncertain_pct)
                if tele.max_rpm > 0 and est / tele.max_rpm > limit_pct:
                    self.rejected += 1
                    src = "实测齿比" if learned else "估算"
                    if learned and not trusted:
                        src += "，样本不足"
                    return Decision(reason="需要降挡", blocked_by="防超转",
                                    detail=f"降挡后约 {est / tele.max_rpm * 100:.0f}%（{src}）")
                return Decision(action="DOWN", reason=why, detail=f"{rpm_pct * 100:.1f}%")

        if block_up:
            return Decision(reason="刚降挡，防抖", blocked_by="after_down",
                            detail=f"{since * 1000:.0f}ms")
        if at_ceiling:
            return Decision(reason="已到档位上限", blocked_by=f"gear={gear}/{ceiling}",
                            detail=f"{rpm_pct * 100:.0f}%")
        if hold_speed:
            return Decision(reason="车速在下降，先不升挡", blocked_by="decelerating",
                            detail=f"{trend:+.1f} km/h/s")
        if block_down:
            return Decision(reason="刚升挡，防抖", blocked_by="after_up",
                            detail=f"{since * 1000:.0f}ms")
        return Decision(reason="在功率带内", detail=f"{rpm_pct * 100:.0f}%")

    def preview(self, tele: Telemetry) -> Decision:
        """顾问模式：只判断这一帧，完全不受运行状态影响。

        做法是显式喂进"空历史"：上一条动作为空、车速趋势为 0、上限用学习值。
        这样拿录制数据跑出来的结论，和实车第一次遇到这帧时的结论一致——
        如果它还去读 self.last_action，回放的结果就只是在复现上一次运行。
        """
        if not tele.usable:
            return Decision(reason="遥测不可用", blocked_by="engine_off")
        if tele.is_shifting:
            return Decision(reason="换挡进行中", blocked_by="shifting")
        if tele.is_reversing:
            return Decision(reason="倒车中", blocked_by="reverse")
        return self._rules(tele, 0.0, 999.0, last_action="", trend=0.0,
                           ceiling=self._learned_ceiling())

    # -- 内部 --------------------------------------------------------------
    # -- 事务：发不出去就回滚 ----------------------------------------------
    def rollback_command(self) -> None:
        """撤销上一条换挡指令留下的状态。只在"确认没发出去"时调用。

        `_command()` 在"引擎决定换挡"的那一刻就写了 pending / last_action /
        last_shift_t，但发不发得出去要到 `service._decide()` 才知道。焦点检查
        拒发、断流首帧不发、SendInput 失败——这三条路径如果不回滚，引擎会以为
        自己发过键，0.8 秒后凭空多一次超时，两次就把顶挡钉住 90 秒。

        快照是在 `_command` 里、**改状态的前一行**取的，不是 `process()` 之前：
        同一帧可能先用 `_resolve` 结算了上一条（pending 清空、记一笔成功），
        紧接着又发出新指令。若快照取自 process 之前，回滚会把已经结算掉的
        pending 复活，下一帧按旧挡位再记一次成功——统计里凭空多一条换挡。
        """
        if self._pre_command is None:
            return
        self.pending, self.last_action, self.last_shift_t = self._pre_command
        self._pre_command = None

    def _command(self, action: str, now: float, tele: Telemetry,
                 reason: str, detail: str) -> Decision:
        # 把当时的车速/转速一起记进原因里。日志多这两项，事后才能回答
        # "那次升挡失败时车到底在什么状态"——没有上下文只能靠猜。
        note = f"{reason} @{tele.speed_kmh:.0f}km/h {tele.rpm:.0f}转"
        self._pre_command = (self.pending, self.last_action, self.last_shift_t)
        self.last_shift_t = now
        self.last_action = action
        if self.expect_ack:
            self.pending = (now, tele.gear, action, note)
        return Decision(action=action, reason=reason, detail=detail)

    def _resolve(self, tele: Telemetry, now: float) -> ShiftRecord | None:
        """看档位有没有响应上一条指令。"""
        if self.pending is None:
            return None
        t0, from_gear, action, reason = self.pending

        # 不能用来结算的帧：菜单、暂停、倒挡/空挡。
        #
        # 这里**必须**放过 gear=11（换挡中）——它恰恰是等回执的那个状态。
        # 曾经写成 `1 <= gear <= 10` 把 11 和 0 归成一类，结果每次换挡都在
        # 过渡帧被静默作废：真实录制 15 次换挡 15 次经过 11，等于回执系统
        # 整体失效，统计永远显示"未换挡"。0 是"游戏不在跑"，11 是"正在换"，
        # 两者语义相反，不能共用一个判断。
        if not tele.usable or not (1 <= tele.gear <= 11):
            # 静默作废这条指令：既不算成功也不算失败。
            # 保留 pending 的话，回到赛道第一帧会立刻超时，白记一次失败。
            self.pending = None
            return None

        if tele.is_shifting or tele.gear == from_gear:
            if now - t0 > self.p.pending_timeout_s:
                self.pending = None
                self.timeouts += 1
                self._note_shift_failure(action, from_gear, now)
                rec = ShiftRecord(t0, action, from_gear, False,
                                  int((now - t0) * 1000), reason + "(超时)")
                self.history.append(rec)
                self.last_resolved = rec
                return rec
            return None

        # 档位变了，且不是 11
        moved = tele.gear - from_gear
        # 方向对就算成功：玩家可能在同一刻也拨了一下，挡位一次跳两级。
        # 那不是失败（车确实往我们要的方向走了），但要在记录里标出来——
        # 统计里突然出现一条"跨两级、延迟 120ms"的记录时，能一眼看出原因。
        ok = (moved > 0) if action == "UP" else (moved < 0)
        note = reason if abs(moved) <= 1 else f"{reason}·跳{abs(moved)}挡"
        rec = ShiftRecord(t0, action, from_gear, ok, int((now - t0) * 1000), note)
        self.history.append(rec)
        if ok:
            self._note_shift_success(tele.gear)
        if len(self.history) > 200:
            del self.history[0]
        self.last_resolved = rec
        self.pending = None
        return rec

    # -- 统计 --------------------------------------------------------------
    def _note_shift_failure(self, action: str, from_gear: int, now: float) -> None:
        """升挡失败了两次 → 认为当前挡就是最高挡。

        只在上限附近才做这个推断，否则一次按键丢失（在 3 挡）就会被当成"3 挡是顶挡"。
        同时记下时间：这个结论会过期，见 `gear_ceiling()`。
        """
        if action != "UP" or from_gear < 2:
            return
        if from_gear == self._up_fail_gear:
            self._up_fail_count += 1
        else:
            self._up_fail_gear = from_gear
            self._up_fail_count = 1
        if self._up_fail_count >= 2 and from_gear >= self._learned_ceiling() - 1:
            # 用只看学习值的上限，不用 gear_ceiling()——后者包含 _top_guess，
            # 会让"猜中 6 挡"之后 5 挡的两次丢键把猜测改成 5，一次比一次低。
            self._top_guess = (from_gear, now)

    def _note_shift_success(self, to_gear: int) -> None:
        self._up_fail_gear = 0
        self._up_fail_count = 0
        # 升上去了，说明刚才那个"顶挡"判断是错的
        if self._top_guess[0] and to_gear > self._top_guess[0]:
            self._top_guess = (0, 0.0)

    def stats(self) -> str:
        if not self.history:
            return f"未换挡（超时 {self.timeouts}）"
        ok = [r for r in self.history if r.ok]
        lat = sorted(r.latency_ms for r in ok)
        med = lat[len(lat) // 2] if lat else 0
        return (f"换挡 {len(ok)}/{len(self.history)} 成功  "
                f"中位 {med}ms  超时 {self.timeouts}  防超转拒绝 {self.rejected}")
