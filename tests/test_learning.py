# -*- coding: utf-8 -*-
"""
阶段 5 学习模块的测试。

全部用合成数据：先定义一台"理想车"（已知齿比 + 已知扭矩曲线 + 已知断油点），
再把它的遥测喂给学习器，看能不能把参数反推回来。

这样做的价值：真车上你永远不知道"学到的 62.1"到底对不对，
但在这里正确答案是已知的，误差可以直接算出来。

运行：
    python tests/test_learning.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from fm8_tcu.config import dump_policy, load_policy, write_default            # noqa: E402
from fm8_tcu.engine import AutoShiftEngine, Policy              # noqa: E402
from fm8_tcu.learning.gear_ratio import GearRatioTable          # noqa: E402
from fm8_tcu.learning.rev_limiter import RevLimiter             # noqa: E402
from fm8_tcu.learning.vehicle import VehicleModel               # noqa: E402
from fm8_tcu.profile import ProfileStore, car_key               # noqa: E402
from fm8_tcu.protocol import Telemetry                          # noqa: E402

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


# ══════════════════════════════════════════════════════════════════════════
# 一台"理想车"
# ══════════════════════════════════════════════════════════════════════════

MAX_RPM = 8000.0
RATIOS = {1: 120.0, 2: 84.0, 3: 62.0, 4: 48.0, 5: 38.0, 6: 31.0}
LIMITER = 7400.0


def torque_curve(rpm: float) -> float:
    """峰值 420 Nm @ 4200 转 的抛物线。"""
    return 420.0 - 180.0 * ((rpm - 4200.0) / 3000.0) ** 2


def frame(gear: int, rpm: float, speed_kmh: float,
          throttle: float = 0.5, brake: float = 0.0,
          max_rpm: float = MAX_RPM, torque: float | None = None,
          race: int = 1, ordinal: int = 4242) -> Telemetry:
    v = {
        "is_race_on": race, "timestamp_ms": 0,
        "engine_max_rpm": max_rpm, "engine_idle_rpm": 900.0,
        "current_engine_rpm": rpm,
        "vel_x": 0.0, "vel_y": 0.0, "vel_z": speed_kmh / 3.6,
        "speed": speed_kmh / 3.6,
        "power": 0.0,
        "torque": torque_curve(rpm) if torque is None else torque,
        "accel": int(max(0.0, min(1.0, throttle)) * 255),
        "brake": int(max(0.0, min(1.0, brake)) * 255),
        "clutch": 0, "gear": gear,
        "car_ordinal": ordinal,
    }
    return Telemetry(v, 331)


# ══════════════════════════════════════════════════════════════════════════
# 齿比
# ══════════════════════════════════════════════════════════════════════════

def feed_ratios(table: GearRatioTable, gears=(1, 2, 3, 4, 5, 6),
                n: int = 45, rpm: float = 4800.0, wobble: float = 0.0005) -> None:
    for g in gears:
        speed = rpm / RATIOS[g]
        for i in range(n):
            table.learn(speed * (1 + (wobble if i % 2 else -wobble)),
                        g, rpm, MAX_RPM)


def test_gear_ratio_converges() -> None:
    t = GearRatioTable()
    feed_ratios(t)
    errs = []
    for g in (1, 2, 3, 4, 5, 6):
        med = t.median(g) or 0.0
        errs.append(abs(med / RATIOS[g] - 1.0))
        check(f"{g} 挡齿比误差 < 0.5%", abs(med / RATIOS[g] - 1.0) < 0.005,
              f"学到 {med:.2f}，真实 {RATIOS[g]}")
    check("六个挡位全部可信", all(t.known(g) for g in range(1, 7)))
    check("平均误差 < 0.2%", sum(errs) / len(errs) < 0.002,
          f"{sum(errs) / len(errs) * 100:.3f}%")


def test_gear_ratio_rejects_outliers() -> None:
    t = GearRatioTable()
    feed_ratios(t, gears=(2,), n=60)
    before = t.median(2)
    # 车轮打滑：转速虚高 45%
    for _ in range(40):
        t.learn(4800 / RATIOS[2], 2, 4800 * 1.45, MAX_RPM)
    after = t.median(2)
    check("打滑样本被拒绝", abs(after / RATIOS[2] - 1.0) < 0.01,
          f"污染前 {before:.2f} → 污染后 {after:.2f}，真实 {RATIOS[2]}")


def test_gear_ratio_monotonic_guard() -> None:
    t = GearRatioTable()
    # 2 挡齿比被污染成比 1 挡还大 → 必须判为不可信
    for _ in range(40):
        t.learn(4800 / 100.0, 1, 4800, MAX_RPM)
        t.learn(4800 / 130.0, 2, 4800, MAX_RPM)
    check("齿比不单调时判为不可信", not t.known(2),
          f"1 挡 {t.median(1):.1f} / 2 挡 {t.median(2):.1f}")


def test_gear_ratio_ignores_bad_frames() -> None:
    t = GearRatioTable()
    before = t.samples(3)
    t.learn(0.5, 3, 3000, MAX_RPM)          # 车速太低
    t.learn(50, 3, 300, MAX_RPM)            # 转速太低
    t.learn(50, 11, 3000, MAX_RPM)          # 换挡中
    t.learn(50, 3, 3000, 0.0)               # 红线为 0（菜单）
    check("无效帧不产生样本", t.samples(3) == before, f"{t.samples(3)}")


# ══════════════════════════════════════════════════════════════════════════
# 扭矩曲线
# ══════════════════════════════════════════════════════════════════════════

def feed_curve(vehicle: VehicleModel, rpm_lo: float = 2500.0,
               rpm_hi: float = 7400.0, step: float = 100.0,
               per_point: int = 4) -> None:
    for rpm in range(int(rpm_lo), int(rpm_hi) + 1, int(step)):
        # 用一个"刚好能用齿比解释"的车速，顺便让齿比也学一遍
        gear = 4 if rpm < 6000 else 5
        speed = rpm / RATIOS[gear]
        for _ in range(per_point):
            vehicle.learn(frame(gear, float(rpm), speed, throttle=1.0), 0.0)


def test_torque_curve_recovers_shape() -> None:
    v = VehicleModel(4242)
    feed_curve(v)
    check("曲线数据足够", v.curve.ready, f"{v.curve.summary()}")
    peak = v.curve.peak_torque_rpm() or 0.0
    check("峰值扭矩转速误差 < 250 转", abs(peak - 4200) < 250, f"学到 {peak:.0f}")
    got = v.curve.torque_at(4200.0) or 0.0
    check("峰值扭矩误差 < 15 Nm", abs(got - 420.0) < 15, f"学到 {got:.1f}")
    low = v.curve.torque_at(7000.0) or 0.0
    check("高转扭矩明显低于峰值", low < 300, f"7000 转 {low:.0f} Nm")


def test_torque_curve_needs_wot() -> None:
    v = VehicleModel(4242)
    for rpm in range(3000, 7000, 100):
        v.learn(frame(3, float(rpm), rpm / RATIOS[3], throttle=0.4), 0.0)
    check("部分油门样本不算数", not v.curve.ready, v.curve.summary())


def test_optimal_shift_point() -> None:
    v = VehicleModel(4242)
    feed_curve(v)
    pct = v.optimal_up_pct(4)
    check("算得出最优升挡点", pct is not None, str(pct))
    if pct:
        check("升挡点在峰值扭矩之后", pct > 4200 / MAX_RPM, f"{pct * 100:.1f}%")
        check("升挡点不超过红线", pct <= 1.0, f"{pct * 100:.1f}%")
        check("升挡点距红线还有余量", pct < 0.99, f"{pct * 100:.1f}%")


# ══════════════════════════════════════════════════════════════════════════
# 断油
# ══════════════════════════════════════════════════════════════════════════

def pull(lim: RevLimiter, gear: int, top: float, t0: float,
         duration: float = 0.6, steps: int = 12) -> float:
    """一次全油门拉转，返回下一次的起始时间。"""
    base = top - 600.0
    for i in range(steps + 1):
        rpm = base + (top - base) * i / steps
        lim.learn(rpm, gear, 1.0, MAX_RPM, True, t0 + duration * i / steps)
    return t0 + duration + 0.02


def test_rev_limiter_detects_fuel_cut() -> None:
    lim = RevLimiter()
    t = 0.0
    t = pull(lim, 4, LIMITER, t)
    lim.learn(LIMITER - 350, 4, 1.0, MAX_RPM, True, t)     # 断油，转速掉下来
    check("第一次不采信", lim.rpm is None, lim.summary())

    t = pull(lim, 4, LIMITER, t + 0.1)
    lim.learn(LIMITER - 350, 4, 1.0, MAX_RPM, True, t + 0.05)
    check("第二次采信", lim.rpm is not None, lim.summary())
    check("断油转速误差 < 60 转", abs((lim.rpm or 0) - LIMITER) < 60,
          f"学到 {lim.rpm:.0f}，真实 {LIMITER}")
    check("断油比例约 92.5%", abs((lim.pct or 0) - LIMITER / MAX_RPM) < 0.01,
          f"{lim.pct * 100:.1f}%")


def test_rev_limiter_ignores_upshift() -> None:
    lim = RevLimiter()
    t = pull(lim, 4, LIMITER, 0.0)
    # 升挡：先经过 11，再掉到低转速
    lim.learn(0, 11, 1.0, MAX_RPM, True, t)
    lim.learn(LIMITER * 0.78, 5, 1.0, MAX_RPM, True, t + 0.02)
    check("升挡掉速不算断油", lim.episodes == 0, f"episodes={lim.episodes}")


def test_rev_limiter_ignores_lift_off() -> None:
    lim = RevLimiter()
    t = pull(lim, 4, LIMITER, 0.0)
    lim.learn(LIMITER - 350, 4, 0.2, MAX_RPM, True, t)     # 收油，不是断油
    check("收油掉速不算断油", lim.episodes == 0, f"episodes={lim.episodes}")


# ══════════════════════════════════════════════════════════════════════════
# 车辆模型 / 档案
# ══════════════════════════════════════════════════════════════════════════

def build_full_vehicle() -> VehicleModel:
    v = VehicleModel(4242)
    t = 0.0
    # 先把齿比喂饱
    for g in (1, 2, 3, 4, 5, 6):
        speed = 4800 / RATIOS[g]
        for _ in range(45):
            v.learn(frame(g, 4800.0, speed, throttle=0.5), t)
            t += 0.01
    feed_curve(v)
    # 再拉两次断油
    t += 1.0
    for _ in range(2):
        for i in range(13):
            rpm = LIMITER - 600 + 600 * i / 12
            v.learn(frame(4, rpm, rpm / RATIOS[4], throttle=1.0), t)
            t += 0.05
        v.learn(frame(4, LIMITER - 350, (LIMITER - 350) / RATIOS[4],
                      throttle=1.0), t)
        t += 0.5
    return v


def test_vehicle_roundtrip() -> None:
    v = build_full_vehicle()
    check("车辆识别到 6 个挡", v.max_gear == 6, f"max_gear={v.max_gear}")
    check("断油已学到", v.limiter_pct is not None, v.limiter.summary())

    data = v.to_dict()
    text = json.dumps(data)
    v2 = VehicleModel.from_dict(json.loads(text))
    check("存档往返：齿比一致",
          abs((v2.ratios.median(3) or 0) - (v.ratios.median(3) or 0)) < 0.01)
    check("存档往返：齿比仍然可信", v2.ratios.known(3), str(v2.ratios.samples(3)))
    check("存档往返：断油一致",
          abs((v2.limiter_pct or 0) - (v.limiter_pct or 0)) < 1e-6)
    check("存档往返：曲线仍可用", v2.curve.ready, v2.curve.summary())
    check("存档往返：升挡点一致",
          abs((v2.optimal_up_pct(4) or 0) - (v.optimal_up_pct(4) or 0)) < 0.01)
    check("存档体积可控", len(text) < 20000, f"{len(text)} 字节")


def test_profile_keeps_spread() -> None:
    """存档往返不能把"不可信"洗成"可信"。

    踩过的坑：还原样本时用 ±s 而不是 ±2s，MAD 只剩一半，
    一个离散度 10% 的挡位读回来变成 5%，刚好混过 6% 的门槛。
    """
    t = GearRatioTable()
    for i in range(60):
        t.learn(4800 / 85.0, 2, 4800 * (0.90 if i % 2 else 1.10), MAX_RPM)
    before = t.spread(2)
    check("原始离散度超过门槛", before > 0.06, f"{before:.3f}")

    t2 = GearRatioTable.from_dict(t.to_dict())
    after = t2.spread(2)
    check("还原后离散度不变", abs(after - before) < 0.01,
          f"{before:.3f} → {after:.3f}")
    check("还原后仍不可信", not t2.known(2), f"spread={after:.3f}")


def test_profile_store() -> None:
    d = tempfile.mkdtemp(prefix="fm8prof_")
    try:
        store = ProfileStore(d)
        v = build_full_vehicle()
        key = car_key(v.car_ordinal)
        check("主键格式正确", key == "car04242", key)
        check("保存成功", store.save(key, v.to_dict()))
        check("文件存在", os.path.exists(store.path_for(key)))
        loaded = store.load(key)
        check("读回来是字典", isinstance(loaded, dict))
        v2 = VehicleModel.from_dict(loaded)
        check("读回来齿比可用", v2.ratios.known(6), str(v2.ratios.samples(6)))
        check("目录列表包含该车", key in store.keys(), str(store.keys()))

        # 坏文件不能炸
        with open(store.path_for(key), "w", encoding="utf-8") as f:
            f.write("{这不是 JSON")
        check("坏档案返回 None 而不是抛异常", store.load(key) is None)
        check("坏档案有错误记录", bool(store.last_error), store.last_error)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 引擎接入
# ══════════════════════════════════════════════════════════════════════════

def test_engine_uses_learned_ratio_to_block_over_rev() -> None:
    v = VehicleModel(4242)
    # 只学 2、3 挡：3 挡 50 rpm/kmh，2 挡 90 rpm/kmh
    for _ in range(40):
        v.learn(frame(3, 5000.0, 100.0, throttle=0.2), 0.0)
        v.learn(frame(2, 5400.0, 60.0, throttle=0.2), 0.0)
    check("2、3 挡齿比都可信", v.ratios.known(2) and v.ratios.known(3),
          f"2 挡 {v.ratios.samples(2)} / 3 挡 {v.ratios.samples(3)}")
    est = v.rpm_at(100.0, 2)
    check("降挡落点用实测齿比算", est is not None and abs(est - 9000) < 100,
          f"{est}")

    tele = frame(3, 5000.0, 100.0, throttle=0.0, brake=1.0)
    naive = AutoShiftEngine(Policy())
    d0 = naive.process(tele, 0.0)
    check("没有学习数据时会误降挡", d0.action == "DOWN", d0.describe())

    learned = AutoShiftEngine(Policy(), vehicle=v)
    d1 = learned.process(tele, 0.0)
    check("有实测齿比时拦住超转", d1.action == "" and "超转" in d1.blocked_by,
          d1.describe())


def test_uncertain_ratio_uses_stricter_guard() -> None:
    """齿比样本不足时，落点估计本身有 5%–10% 误差。

    实测教训：在 12000 转的车上，估 95% 觉得安全，但真实落点已经超红线，
    游戏直接拒绝降挡，连吃两次 800ms 超时。所以数据不可信时要更保守。
    """
    v = VehicleModel(4242)
    for _ in range(40):                      # 3 挡：干净，可信
        v.learn(frame(3, 5660.0, 100.0, throttle=0.2, max_rpm=9000), 0.0)
    # 2 挡：中位数 85，但样本一半 78、一半 92 → 离散度超限，不可信
    for i in range(41):
        ratio = 78.0 if i < 20 else (85.0 if i == 20 else 92.0)
        v.learn(frame(2, ratio * 100.0, 100.0, throttle=0.2, max_rpm=9000), 0.0)
    check("3 挡齿比可信", v.ratios.known(3), str(v.ratios.spread(3)))
    check("2 挡齿比不可信", not v.ratios.known(2), f"spread={v.ratios.spread(2):.3f}")

    tele = frame(3, 5660.0, 100.0, throttle=0.0, brake=1.0, max_rpm=9000)
    plain = AutoShiftEngine(Policy())
    check("没学习数据时按 97% 放行", plain.process(tele, 0.0).action == "DOWN")

    eng = AutoShiftEngine(Policy(), vehicle=v)
    d = eng.process(tele, 0.0)
    check("齿比不可信时按 92% 拦住", d.action == "" and "超转" in d.blocked_by,
          d.describe())
    check("日志标出样本不足", "样本不足" in d.detail, d.detail)

    slow = frame(3, 4528.0, 80.0, throttle=0.0, brake=1.0, max_rpm=9000)
    check("车速降下来后允许降挡", eng.process(slow, 1.0).action == "DOWN")


def test_engine_respects_learned_limiter() -> None:
    v = VehicleModel(4242)
    v.limiter.estimate = 7000.0
    v.limiter.episodes = 2
    v.limiter._max_rpm = MAX_RPM
    check("断油比例 87.5%", abs((v.limiter_pct or 0) - 0.875) < 1e-6)

    plain = AutoShiftEngine(Policy())
    learned = AutoShiftEngine(Policy(), vehicle=v)
    check("默认阈值 89%", abs(plain.upshift_pct(1.0) - 0.89) < 1e-6,
          f"{plain.upshift_pct(1.0):.3f}")
    check("学习后阈值降到 86%", abs(learned.upshift_pct(1.0) - 0.86) < 1e-6,
          f"{learned.upshift_pct(1.0):.3f}")

    tele = frame(3, 6960.0, 112.0, throttle=1.0)     # 87%
    check("默认不升挡", plain.process(tele, 0.0).action == "")
    check("学习后提前升挡", learned.process(tele, 0.0).action == "UP")


def test_engine_never_raises_shift_point() -> None:
    v = build_full_vehicle()
    eng = AutoShiftEngine(Policy(), vehicle=v)
    base = Policy().up_pct_high
    pct = eng.upshift_pct(1.0, 4)
    check("学习值不会把升挡点往上推", pct <= base + 1e-9,
          f"学习后 {pct:.3f}，配置 {base:.3f}")
    check("学习值最多下拉 8%", pct >= base - 0.08 - 1e-9, f"{pct:.3f}")


def test_engine_gear_ceiling_from_learning() -> None:
    v = build_full_vehicle()
    eng = AutoShiftEngine(Policy(), vehicle=v)
    check("挡位上限 = 学到的最高挡 + 1", eng.gear_ceiling() == 7,
          str(eng.gear_ceiling()))
    tele = frame(7, 6000.0, 6000.0 / RATIOS[6], throttle=1.0)
    check("到上限不再升挡", eng.process(tele, 0.0).action == "")


def test_top_gear_learned_from_failed_upshifts() -> None:
    """只能学到开过的挡位。如果把学到的最高挡当硬上限就永远进不了下一挡，
    所以上限先给一挡余量，再用连续两次升挡失败确认顶挡。"""
    v = VehicleModel(4242)
    for g in (1, 2, 3, 4, 5):
        for _ in range(40):
            v.learn(frame(g, 4800.0, 4800.0 / RATIOS[g], throttle=0.2), 0.0)
    eng = AutoShiftEngine(Policy(), vehicle=v)
    check("上限先留一挡余量", eng.gear_ceiling() == 6, str(eng.gear_ceiling()))

    tele = frame(5, 7800.0, 7800.0 / RATIOS[5], throttle=1.0)
    d = eng.process(tele, 0.0)
    check("5 挡仍然尝试升挡", d.action == "UP", d.describe())
    eng.process(tele, 1.0)                       # 第一次超时，再试一次
    d2 = eng.process(tele, 2.0)                  # 第二次超时 → 确认顶挡
    check("两次失败后确认顶挡", eng.gear_ceiling() == 5, str(eng.gear_ceiling()))
    check("之后不再尝试升挡", d2.action == "", d2.describe())


# ══════════════════════════════════════════════════════════════════════════
# 参数文件
# ══════════════════════════════════════════════════════════════════════════

def test_dump_policy_is_atomic() -> None:
    """保存参数不能留半个文件：写一半断电，下次启动会读失败并静默回默认值。"""
    d = tempfile.mkdtemp(prefix="fm8cfg_")
    try:
        path = os.path.join(d, "params.json")
        ok = dump_policy(Policy(), path)
        check("参数写盘成功", ok)
        check("没有残留 .tmp", not os.path.exists(path + ".tmp"),
              str(os.listdir(d)))
        back, _notes = load_policy(path)
        check("写进去的能读回来", abs(back.up_pct_high - Policy().up_pct_high) < 1e-9,
              str(back.up_pct_high))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_config_file() -> None:
    d = tempfile.mkdtemp(prefix="fm8cfg_")
    try:
        path = os.path.join(d, "params.json")
        check("能导出默认参数", write_default(path))
        policy, notes = load_policy(path)
        check("默认参数无损", abs(policy.up_pct_high - Policy().up_pct_high) < 1e-9,
              str(notes))

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cooldown_s": 0.0, "up_pct_high": 0.95,
                       "max_gear": 99, "没这个参数": 1}, f)
        policy, notes = load_policy(path)
        check("正常值被采用", abs(policy.up_pct_high - 0.95) < 1e-9)
        check("过小值被夹住", policy.cooldown_s >= 0.20, str(policy.cooldown_s))
        check("过大值被夹住", policy.max_gear <= 12, str(policy.max_gear))
        check("未知参数有提示", any("没这个参数" in n for n in notes), str(notes))

        # 字符串 "0" 不等于 0。bool("0") 是 True，会让关闭开关反而打开。
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"use_learned_shift": "0"}, f)
        policy, _ = load_policy(path)
        check('use_learned_shift "0" 解析为 False', policy.use_learned_shift is False,
              str(policy.use_learned_shift))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"use_learned_shift": "1"}, f)
        policy, _ = load_policy(path)
        check('use_learned_shift "1" 解析为 True', policy.use_learned_shift is True)

        # NaN 会让冷却判断 `since < nan` 恒为假，等于把冷却关掉。
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cooldown_s": float("nan"), "down_brake_pct": float("inf")}, f)
        policy, notes = load_policy(path)
        check("NaN 被拒绝而不是写进 Policy",
              policy.cooldown_s == Policy().cooldown_s, str(policy.cooldown_s))
        check("Inf 被拒绝", policy.down_brake_pct == Policy().down_brake_pct)
        check("NaN 有提示", any("不是有限数" in n for n in notes), str(notes))

        check("文件不存在不崩", load_policy(os.path.join(d, "无.json"))[0] is not None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_profile_schema_guard() -> None:
    """档案版本不认识就重学，不能猜着读。"""
    good = VehicleModel(4242)
    good.max_rpm = 9000.0
    data = good.to_dict()
    check("档案往返正常", VehicleModel.from_dict(data).car_ordinal == 4242)

    data["schema"] = 999
    v = VehicleModel.from_dict(data)
    check("版本不符时忽略档案", v.car_ordinal == 0 and bool(v.load_error), v.load_error)
    check("忽略后齿比是空的", not v.ratios.gears())


def main() -> None:
    print("阶段 5 学习模块测试")
    print("-" * 62)
    for fn in (
        test_gear_ratio_converges,
        test_gear_ratio_rejects_outliers,
        test_gear_ratio_monotonic_guard,
        test_gear_ratio_ignores_bad_frames,
        test_torque_curve_recovers_shape,
        test_torque_curve_needs_wot,
        test_optimal_shift_point,
        test_rev_limiter_detects_fuel_cut,
        test_rev_limiter_ignores_upshift,
        test_rev_limiter_ignores_lift_off,
        test_vehicle_roundtrip,
        test_profile_keeps_spread,
        test_profile_store,
        test_engine_uses_learned_ratio_to_block_over_rev,
        test_uncertain_ratio_uses_stricter_guard,
        test_engine_respects_learned_limiter,
        test_engine_never_raises_shift_point,
        test_engine_gear_ceiling_from_learning,
        test_top_gear_learned_from_failed_upshifts,
        test_config_file,
        test_dump_policy_is_atomic,
        test_profile_schema_guard,
    ):
        fn()
    print("-" * 62)
    print(f"通过 {PASS}   失败 {FAIL}")
    raise SystemExit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
