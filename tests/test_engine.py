# -*- coding: utf-8 -*-
"""
自动换挡引擎的规则测试。

为什么值得写：换挡规则里有大量"边界情况"——刹车时不许升挡、换挡中不许发、
超转不许降、冷却中不许动。这些在真车上很难复现，但用合成帧可以精确构造。

运行：
    python tests/test_engine.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from fm8_tcu.engine import AutoShiftEngine, Policy  # noqa: E402
from fm8_tcu.protocol import Telemetry  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {detail}")


def frame(**kw) -> Telemetry:
    v = {
        "is_race_on": 1, "timestamp_ms": 0,
        "engine_max_rpm": 9000.0, "engine_idle_rpm": 900.0,
        "current_engine_rpm": 6000.0,
        "vel_z": 50.0, "vel_x": 0.0, "vel_y": 0.0,
        "speed": 42.0,
        "accel": 255, "brake": 0, "clutch": 0, "gear": 3,
    }
    v.update(kw)
    return Telemetry(v, 331)


def test_upshift_at_threshold() -> None:
    eng = AutoShiftEngine()
    # 满油门阈值 89%
    d = eng.process(frame(current_engine_rpm=8100, accel=255), 0.0)   # 90%
    check("满油门 90% 触发升挡", d.action == "UP", d.describe())
    d2 = eng.process(frame(current_engine_rpm=7800, accel=255), 0.0)  # 86.7%
    check("满油门 86.7% 不触发", d2.action == "", d2.describe())


def test_no_upshift_while_braking() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(current_engine_rpm=8300, accel=255, brake=200), 0.0)
    check("刹车时即使转速到顶也不升挡", d.action != "UP", d.describe())


def test_brake_downshift() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(gear=5, current_engine_rpm=5400, speed=42.0,
                          accel=0, brake=255), 0.0)   # 60%
    check("刹车且转速 60% → 降挡", d.action == "DOWN", d.describe())


def test_over_rev_guard() -> None:
    eng = AutoShiftEngine()
    # 在 2 挡、转速 60% 时想降 1 挡：估算 60%×2 = 120% → 必须拦住
    d = eng.process(frame(gear=2, current_engine_rpm=5400, speed=42.0,
                          accel=0, brake=255), 0.0)
    check("降挡会超转时拒绝", d.action != "DOWN" and "超转" in d.blocked_by, d.describe())


def test_standstill_downshift() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(gear=3, speed=0.5, current_engine_rpm=900), 0.0)
    check("静止且不在 1 挡 → 降挡", d.action == "DOWN", d.describe())


def test_shifting_blocks() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(gear=11, current_engine_rpm=8000), 0.0)
    check("换挡中(11) 一律不动", d.action == "" and "换挡" in d.reason, d.describe())


def test_engine_off_blocks() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(engine_max_rpm=0.0, current_engine_rpm=0.0), 0.0)
    check("发动机未启动 → 不动", d.action == "", d.describe())


def test_reverse_blocks() -> None:
    eng = AutoShiftEngine()
    d = eng.process(frame(gear=0, vel_z=-5.0, speed=3.0), 0.0)
    check("倒车（vel_z<0）→ 不动", d.action == "", d.describe())


def test_cooldown() -> None:
    eng = AutoShiftEngine(Policy(cooldown_s=0.25))
    eng.process(frame(current_engine_rpm=8100, accel=255), 0.0)   # 发一条
    eng.pending = None                                           # 假装已确认
    d = eng.process(frame(current_engine_rpm=8100, accel=255), 0.1)
    check("冷却 0.25s 内不再换挡", d.action == "" and d.blocked_by == "cooldown", d.describe())


def test_pending_timeout() -> None:
    eng = AutoShiftEngine(Policy(pending_timeout_s=0.8))
    eng.process(frame(gear=3, current_engine_rpm=8100, accel=255), 0.0)
    check("发出后进入等待", eng.pending is not None)
    # 档位一直不变，1 秒后应判超时
    d = eng.process(frame(gear=3, current_engine_rpm=8100, accel=255), 1.0)
    check("超时计一次失败", eng.timeouts == 1, f"timeouts={eng.timeouts}")
    check("超时后决策里有等待/重试语义", d.action in ("UP", ""), d.describe())


def test_pending_resolved_by_gear() -> None:
    eng = AutoShiftEngine()
    eng.process(frame(gear=3, current_engine_rpm=8100, accel=255), 0.0)
    eng.process(frame(gear=4, current_engine_rpm=6500, accel=255), 0.1)
    check("档位变成 4 → 记为成功", len(eng.history) == 1 and eng.history[0].ok,
          str(eng.history))
    check("延迟约 100ms", 90 <= eng.history[0].latency_ms <= 110,
          str(eng.history[0].latency_ms))


def test_no_upshift_while_decelerating() -> None:
    """降挡后车速还在掉，就不该升回去。

    真实日志里出现过：163 km/h 降到 2 挡，0.65 秒后车速 146 又被升回 3 挡，
    白换两次。判据用「车速是不是在下降」，不是拍一个等待时间。
    """
    eng = AutoShiftEngine()
    # 先喂一段减速历史：163 → 145（每 0.1 秒一帧）
    for i in range(10):
        eng.process(frame(gear=2, current_engine_rpm=6000,
                          speed=(163 - i * 2) / 3.6, accel=200, brake=0), i * 0.1)

    d = eng.process(frame(gear=2, current_engine_rpm=8200,
                          speed=145 / 3.6, accel=150, brake=0), 1.0)
    check("轻油门 + 减速中 → 不许升挡",
          d.action == "" and d.blocked_by == "decelerating", d.describe())

    # 全油门时不再看减速趋势：上坡顶着断油跑不能被误拦
    d2 = eng.process(frame(gear=2, current_engine_rpm=8200,
                           speed=145 / 3.6, accel=255, brake=0), 1.1)
    check("全油门时不被减速判据拦住", d2.action == "UP", d2.describe())


def test_upshift_allowed_at_limiter_in_lower_gear() -> None:
    """反过来的坑：用「车速绝对值」当判据会死锁。

    3 挡 163 降到 2 挡后，2 挡跑到断油只有 159 km/h，车速永远回不到 163，
    结果顶着断油跑。用「趋势」判据就不会——到断油时车速是平的。
    """
    eng = AutoShiftEngine()
    d0 = eng.process(frame(gear=3, current_engine_rpm=5800,
                           speed=163 / 3.6, brake=255, accel=0), 0.0)
    check("先降挡", d0.action == "DOWN", d0.describe())

    # 2 挡里车速稳定在 159（已经到顶，不再上升）
    ups = 0
    for i in range(30):                       # 1.5 秒，每 50ms 一帧
        d = eng.process(frame(gear=2, current_engine_rpm=8100,
                              speed=159 / 3.6, accel=255, brake=0), 0.7 + i * 0.05)
        ups += 1 if d.action == "UP" else 0
    check("到断油时一定升挡（不会一直卡住）", ups >= 1, f"1.5 秒内升挡 {ups} 次")


def test_preview_does_not_mutate() -> None:
    eng = AutoShiftEngine()
    eng.preview(frame(current_engine_rpm=8100, accel=255))
    check("preview 不产生指令", eng.pending is None and not eng.history)


def test_ceiling_does_not_block_downshift() -> None:
    """到顶挡只该拦升挡。

    曾经把"已到档位上限"写在升降挡之前，于是 6 挡一旦被钉成顶挡，
    重刹也降不下去——车会永久卡在挡里，而且没有任何恢复手段。
    """
    eng = AutoShiftEngine()
    eng._top_guess = (3, 0.0)
    d = eng.process(frame(gear=3, current_engine_rpm=5000, speed=40.0, brake=255), 10.0)
    check("顶挡仍允许降挡", d.action == "DOWN", d.describe())


def test_ceiling_expires() -> None:
    """顶挡猜测要能过期。丢两次按键不等于车只有 5 挡。"""
    eng = AutoShiftEngine()
    eng._top_guess = (5, 100.0)
    check("有效期内按 5 挡算", eng.gear_ceiling(120.0) == 5, str(eng.gear_ceiling(120.0)))
    check("过期后恢复", eng.gear_ceiling(100.0 + 91.0) == 10,
          str(eng.gear_ceiling(100.0 + 91.0)))


def test_menu_frame_does_not_resolve() -> None:
    """菜单帧（gear=0, race=0）不能用来结算换挡。

    否则一次进菜单就会被记成"换挡成功/失败"，成功率里混进从未发生的换挡。
    """
    eng = AutoShiftEngine()
    eng.process(frame(current_engine_rpm=8200, accel=255), 0.0)
    check("已发出升挡", eng.pending is not None)
    eng.process(frame(is_race_on=0, engine_max_rpm=0.0, current_engine_rpm=0.0,
                      speed=0.0, vel_z=0.0, gear=0), 0.1)
    check("菜单帧不结算", not eng.history, str(eng.history))
    check("菜单帧作废该指令", eng.pending is None)


def test_standstill_respects_cooldown() -> None:
    """静止回 1 挡也要等冷却，不能连发。"""
    eng = AutoShiftEngine()
    d1 = eng.process(frame(gear=5, speed=0.5, current_engine_rpm=900, accel=0), 0.0)
    check("静止从 5 挡降挡", d1.action == "DOWN", d1.describe())
    d2 = eng.process(frame(gear=4, speed=0.5, current_engine_rpm=900, accel=0), 0.01)
    check("10ms 后再发被冷却挡住",
          d2.action == "" and d2.blocked_by == "cooldown", d2.describe())


def test_preview_ignores_history() -> None:
    """preview 必须只看这一帧。否则"拿录制做离线校验"得到的是上次运行的重放。"""
    eng = AutoShiftEngine()
    eng.last_action = "UP"
    eng.last_shift_t = 0.0
    d = eng.preview(frame(current_engine_rpm=8200, accel=255))
    check("preview 不受上一条升挡影响", d.action == "UP", d.describe())


def test_shift_through_gear11_resolves() -> None:
    """换挡一定经过 11，11 是**等回执**的状态，不是无效帧。

    曾经把 `gear <= 0` 和 `gear == 11` 归成一类作废 pending，结果每次换挡
    都在过渡帧被清掉：真实录制 15 次换挡 15 次经过 11，等于回执系统整体
    失效——统计永远"未换挡"、延迟中位数失效、顶挡推断永不触发。
    """
    eng = AutoShiftEngine()
    d = eng.process(frame(gear=3, current_engine_rpm=8200, accel=255), 0.0)
    check("发出升挡", d.action == "UP", d.describe())
    check("进入等待", eng.pending is not None)

    eng.process(frame(gear=11, current_engine_rpm=7600), 0.030)
    check("换挡中保持等待", eng.pending is not None, str(eng.pending))
    check("换挡中不结算", not eng.history, str(eng.history))

    eng.process(frame(gear=4, current_engine_rpm=6500), 0.060)
    check("到目标挡后结算成功",
          len(eng.history) == 1 and eng.history[0].ok, str(eng.history))
    check("延迟被记录", eng.history and eng.history[0].latency_ms == 60,
          str(eng.history[0].latency_ms if eng.history else None))


def test_unsent_command_rolls_back() -> None:
    """发不出去就回滚：没发键却留 pending，0.8 秒后会凭空多一次超时。"""
    eng = AutoShiftEngine()
    eng.process(frame(current_engine_rpm=8200, accel=255), 0.0)
    check("决策时进入等待", eng.pending is not None)
    eng.rollback_command()
    check("回滚后没有 pending", eng.pending is None)
    check("回滚后 last_action 复原", eng.last_action == "", eng.last_action)

    # 回滚后下一帧应该能重新判断，而不是卡在"等待上一条确认"
    d = eng.process(frame(current_engine_rpm=8200, accel=255), 0.016)
    check("回滚后下一帧照常判断", d.action == "UP", d.describe())


def test_rollback_does_not_revive_resolved_pending() -> None:
    """同一帧里先结算上一条、再发新指令，回滚只能撤掉新的那条。

    如果快照取在 process() 之前，回滚会把已经结算掉的 pending 复活，
    下一帧按旧挡位再记一次成功——统计里凭空多一次换挡。
    """
    p = Policy()
    p.cooldown_s = 0.20
    eng = AutoShiftEngine(p)
    eng.process(frame(gear=3, current_engine_rpm=8200, accel=255), 0.0)
    check("第 1 条指令已发出", eng.pending is not None)

    # 300ms 后到 4 挡：先结算成功，冷却也已过，同帧又发一条
    d = eng.process(frame(gear=4, current_engine_rpm=8200, accel=255), 0.30)
    check("同帧结算了一笔", len(eng.history) == 1 and eng.history[0].ok,
          str(eng.history))
    check("同帧又发出新指令", d.action == "UP", d.describe())

    # 假设这条没发出去（切出游戏）：回滚
    eng.rollback_command()
    check("回滚后不残留 pending", eng.pending is None, str(eng.pending))

    n = len(eng.history)
    eng.process(frame(gear=4, current_engine_rpm=6000, accel=0), 0.32)
    check("回滚没有复活已结算的指令", len(eng.history) == n,
          f"{n} → {len(eng.history)}")


def test_jump_shift_is_flagged() -> None:
    """玩家同时拨了一下导致跨两级：方向对就算成功，但记录里要标出来。"""
    eng = AutoShiftEngine()
    eng.process(frame(gear=3, current_engine_rpm=8200, accel=255), 0.0)
    check("先发出一条升挡", eng.pending is not None)
    eng.process(frame(gear=5, current_engine_rpm=7000, accel=0), 0.05)
    rec = eng.history[-1]
    check("跨两级仍算方向正确", rec.ok and rec.from_gear == 3,
          f"ok={rec.ok} from={rec.from_gear}")
    check("记录里标注跳挡", "跳2挡" in rec.reason, rec.reason)


def test_no_ack_keeps_decisions_flowing() -> None:
    """不等待回执时，决策要能一帧一帧连续给出来。

    这个状态就是"程序没启用"：没人按键，如果照样设 pending，下一帧会判
    "等待上一条确认"，一直等到 0.8 秒超时才动一次。界面上的决策看起来
    就像卡住了——这不只是显示问题，也说明引擎在等一个永远不会来的回执。
    """
    eng = AutoShiftEngine(expect_ack=False)
    d1 = eng.process(frame(current_engine_rpm=8200, accel=255), 0.0)
    check("不等待回执时照常判断", d1.action == "UP", d1.describe())
    check("也不设 pending", eng.pending is None)

    d2 = eng.process(frame(current_engine_rpm=6300, speed=48.0, accel=255), 0.05)
    check("下一帧不被 pending 挡住", d2.blocked_by != "pending", d2.describe())
    check("不产生假的换挡记录", not eng.history, str(eng.history))
    check("统计不冒充真实换挡", "0ms" not in eng.stats(), eng.stats())


def main() -> None:
    print("自动换挡规则测试")
    print("-" * 60)
    for fn in (
        test_upshift_at_threshold,
        test_no_upshift_while_braking,
        test_brake_downshift,
        test_over_rev_guard,
        test_standstill_downshift,
        test_shifting_blocks,
        test_engine_off_blocks,
        test_reverse_blocks,
        test_cooldown,
        test_pending_timeout,
        test_pending_resolved_by_gear,
        test_no_upshift_while_decelerating,
        test_upshift_allowed_at_limiter_in_lower_gear,
        test_preview_does_not_mutate,
        test_ceiling_does_not_block_downshift,
        test_ceiling_expires,
        test_menu_frame_does_not_resolve,
        test_standstill_respects_cooldown,
        test_preview_ignores_history,
        test_shift_through_gear11_resolves,
        test_unsent_command_rolls_back,
        test_rollback_does_not_revive_resolved_pending,
        test_jump_shift_is_flagged,
        test_no_ack_keeps_decisions_flowing,
    ):
        fn()
    print("-" * 60)
    print(f"通过 {PASS}   失败 {FAIL}")
    raise SystemExit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
