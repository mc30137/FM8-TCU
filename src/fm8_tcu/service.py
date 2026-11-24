# -*- coding: utf-8 -*-
"""
把「收包 → 学习 → 决策 → 发送 → 记账」收成一个对象。

为什么要有这一层：命令行面板和图形界面需要**完全一样的行为**。
如果各写一遍，早晚会出现"界面上是好的、命令行里是坏的"这种问题——
而这类问题最难查，因为两边看起来都正常。

所以：`app.py`（命令行）和 `gui.py`（图形界面）都只是这层的外壳，
自己不碰 socket、不碰引擎。

安全上的三个决定（都写在这里，因为这里是唯一的收包入口）：

1. 默认只监听 127.0.0.1。这是本机游戏的数据，没有理由让局域网都能发。
2. 即使绑了 0.0.0.0，也只处理来自回环地址的包；要收别的机器必须
   显式给 IP 白名单（`--allow-from`），没有"一键放开"的开关。
3. 断流恢复的第一帧不发键。看门狗不是"没有包所以没有动作"这种巧合，
   而是显式判断：这条数据有多旧，旧到不能作为发键的依据就丢掉。
"""

from __future__ import annotations

import csv
import os
import socket
import sys
import time
from collections import deque

from .engine import AutoShiftEngine, Policy
from .learning.vehicle import VehicleModel
from .outputs import NullSink
from .profile import ProfileStore, car_key
from .protocol import parse

STALE_S = 0.30          # 遥测多久没更新就停手（包率 = 帧率，约 12ms 一帧）
SAVE_EVERY_S = 5.0      # 学习成果多久落一次盘
SAVE_EVERY_N = 1500     # 或者攒够这么多新样本也存一次
LOG_MAX_BYTES = 5 << 20  # 决策日志超过 5MB 轮转一份

if getattr(sys, "frozen", False):
    # 打包成 exe 后 __file__ 指向临时解压目录，档案写在那里会被删掉，
    # 所以这种情况下用 exe 自己所在的目录。
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PROFILE_DIR = os.path.join(ROOT, "profiles")
DEFAULT_LOG = os.path.join(ROOT, "decisions.csv")

LOG_HEADER = ["时间s", "动作", "起始挡", "结果", "延迟ms", "原因", "持续s"]


def from_loopback(addr) -> bool:
    """这条包是不是本机发的。"""
    ip = (addr[0] if addr else "") or ""
    return ip.startswith("127.") or ip in ("::1", "localhost")


def writable_dir(path: str) -> bool:
    """这个目录能不能写。用真的写一个文件来判断——权限位在 Windows 上经常骗人。"""
    probe = os.path.join(path, ".write_test")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def fallback_dir() -> str:
    """主目录写不了时的备选：%LOCALAPPDATA%\\FM8AutoShift\\profiles。

    单文件 exe 的卖点是"拷到哪都能跑"。放在 Program Files 里时，
    exe 同目录是只读的，档案会静默丢失——那就不是"能跑"了。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FM8AutoShift", "profiles")


class ShiftService:
    def __init__(self, policy: Policy | None = None, sink=None,
                 learn: bool = True, profile_dir: str = DEFAULT_PROFILE_DIR,
                 log_path: str = "") -> None:
        self.policy = policy or Policy()
        # sink 是唯一的输出。没有"演练/实发"两套：不启用时不发键，
        # 由下面 engine.expect_ack 保证"不等回执"，不需要第二个 sink。
        self.sink = sink if sink is not None else NullSink()
        self.learn = learn
        self.store = ProfileStore(profile_dir or DEFAULT_PROFILE_DIR)
        self.log_path = log_path

        self.vehicle = VehicleModel()
        # 初始没有启用，所以一开始就不该等回执
        self.engine = AutoShiftEngine(self.policy, expect_ack=False,
                                      vehicle=self.vehicle if learn else None)

        self.enabled = False
        self.frames = 0
        self.bad_packets = 0        # 解析失败/数值不合法而丢掉的包
        self.foreign_packets = 0    # 来源不在白名单而丢掉的包
        self.watchdog_trips = 0     # 断流恢复、作废 pending 的次数
        self.last_tele = None
        self.last_text = "（还没有决策）"
        self.last_frame_t = 0.0     # monotonic
        self.car_key = ""
        self.last_error = ""
        self.profile_error = ""
        self.recent: deque = deque(maxlen=8)

        self._sock: socket.socket | None = None
        self.allow_from: set[str] = set()   # 显式放行的来源 IP
        self.allow_any = False              # 只有测试/明确选择才会为真
        self._last_ts = -1
        self._logged_rec = None
        self._last_state_key = None
        self._state_since = 0.0
        self._last_save = time.monotonic()
        self._log_f = None
        self._log_w = None
        self.t0 = time.monotonic()

    # -- 生命周期 ----------------------------------------------------------
    def open(self, host: str = "127.0.0.1", port: int = 5300,
             allow_from=None, allow_any: bool = False) -> None:
        if self._log_w is None and self.log_path:
            try:
                self._open_log()
            except OSError as e:
                # 日志写不了不该导致启动失败，但必须让人知道
                self.last_error = f"日志写不了：{e}"

        # 档案目录不可写时（典型场景：exe 放在 Program Files），
        # 退到用户目录。不退的话，学习成果每 5 秒失败一次，用户毫无察觉。
        if not writable_dir(self.store.dir):
            alt = fallback_dir()
            if writable_dir(alt):
                self.profile_error = (f"{self.store.dir} 不可写，"
                                      f"档案改存 {alt}")
                self.store = ProfileStore(alt)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            sock.bind((host, port))
        except OSError as e:
            sock.close()
            raise RuntimeError(f"无法监听 {host}:{port} — {e}") from e
        sock.settimeout(0.0)          # 非阻塞：界面可以按自己的节奏来取
        self._sock = sock
        if isinstance(allow_from, str):
            self.allow_from = {x.strip() for x in
                               allow_from.replace(";", ",").split(",") if x.strip()}
        else:
            self.allow_from = set(allow_from or ())
        self.allow_any = bool(allow_any)
        self.t0 = time.monotonic()

    def close(self) -> None:
        if self.learn and self.car_key and self.vehicle.dirty:
            self._save_profile()
        # 退出前把可能按下的键抬起来。否则一旦在 sleep 中途退出，
        # 游戏里会留下一个"一直按住"的换挡键。
        try:
            self.sink.release_all()
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        if self._log_f is not None:
            try:
                self._log_f.close()
            except OSError:
                pass
            self._log_f = None
            self._log_w = None

    def _source_ok(self, addr) -> bool:
        """这条包能不能收。默认只收本机，别的一律拒绝并计数。

        白名单是"明确的 IP"，不是"打开一个开关"。原来的 --allow-remote
        一旦打开就完全不校验来源，等于把按键注入面又敞开——那不是配置，
        那是陷阱。
        """
        if self.allow_any:
            return True
        ip = (addr[0] if addr else "") or ""
        return ip in self.allow_from or from_loopback(addr)

    def _open_log(self) -> None:
        """打开日志。超过大小就轮转，表头变了也轮转。"""
        path = self.log_path
        try:
            if os.path.exists(path):
                size = os.path.getsize(path)
                header_ok = False
                if size:
                    with open(path, "r", encoding="utf-8", newline="") as f:
                        header_ok = (f.readline().strip().split(",") == LOG_HEADER)
                if size > LOG_MAX_BYTES or (size and not header_ok):
                    os.replace(path, path + ".1")
        except OSError:
            pass

        new = not os.path.exists(path)
        self._log_f = open(path, "a", newline="", encoding="utf-8")
        self._log_w = csv.writer(self._log_f)
        if new:
            self._log_w.writerow(LOG_HEADER)
            self._log_f.flush()

    # -- 主循环的一步 ------------------------------------------------------
    def poll(self, max_packets: int = 8) -> int:
        """收若干包并处理。返回这一轮处理了多少新帧。

        非阻塞 socket：没有数据时立刻返回 0，界面不会卡。
        """
        if self._sock is None:
            return 0
        got = 0
        for _ in range(max_packets):
            try:
                raw, addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                break
            except OSError:
                break

            if not self._source_ok(addr):
                self.foreign_packets += 1
                continue

            tele = parse(raw)
            if tele is None:
                self.bad_packets += 1
                continue
            now = time.monotonic()

            # 去重：实测 25% 的包时间戳与上一包相同
            ts = int(tele.get("timestamp_ms", 0))
            if ts == self._last_ts:
                continue
            self._last_ts = ts

            # ---- 看门狗 ----
            # 隔了多久才来这一帧。超过 STALE_S 说明中间断过：
            # 这条指令的回执已经不可信（游戏可能暂停过、换过场景），
            # 作废它，并且**这一帧不用于发键**。
            gap = (now - self.last_frame_t) if self.last_frame_t else 0.0
            fresh = gap <= STALE_S
            if not fresh and self.engine.pending is not None:
                self.engine.pending = None
                self.watchdog_trips += 1

            self.last_frame_t = now
            self.last_tele = tele
            self.frames += 1
            got += 1

            self._learn(tele, now)
            sent = self._decide(tele, now, allow_send=fresh)
            if sent:
                # shift() 同步按住 60ms，这一批里剩下的包全是"发键之前"的旧帧。
                # 继续在同一轮里喂给引擎，等于拿旧状态去结算刚发出的指令。
                # 留给下一轮：去重和看门狗会按它们自己的时间戳处理。
                break

        self._maybe_save()
        self._flush_resolved()
        return got

    # -- 内部 --------------------------------------------------------------
    def _learn(self, tele, now: float) -> None:
        if not self.learn:
            return
        ordinal = tele.car_ordinal
        if ordinal > 0:
            key = car_key(ordinal)
            if key != self.car_key:
                if self.car_key:
                    self._save_profile()
                self.car_key = key
                data = self.store.load(key)
                self.vehicle = (VehicleModel.from_dict(data) if data
                                else VehicleModel(ordinal))
                if not self.vehicle.car_ordinal:
                    self.vehicle.car_ordinal = ordinal
                self.engine.vehicle = self.vehicle
                # 换车要清运行状态：上一台车的顶挡猜测、防抖计时、成功率
                # 对这台车毫无意义，留着只会让新车上手就带着旧包袱。
                self.engine.reset_car()
                self._last_state_key = None
                if self.vehicle.load_error:
                    self.profile_error = self.vehicle.load_error
                elif self.store.last_error:
                    self.profile_error = f"档案读不了，重新学：{self.store.last_error}"
                else:
                    self.profile_error = ""
        self.vehicle.learn(tele, now)

    def _decide(self, tele, now: float, allow_send: bool = True) -> bool:
        """处理一帧。返回值 = 这一帧是否真的发出了一次按键（供 poll 决定是否收手）。"""
        # 只有真的会发键时才等回执。没启用的时候等下去只会得到一堆假超时，
        # 而且决策序列会卡在"等待上一条确认"里，0.8 秒才动一次。
        self.engine.expect_ack = self.enabled
        # 决策过程会写 pending / last_action / last_shift_t。发不出去就回滚，
        # 否则引擎以为自己发过键，0.8 秒后凭空多一次超时。
        decision = self.engine.process(tele, now)
        self.last_text = decision.describe()

        key = (decision.action, decision.blocked_by, decision.reason)
        if key != self._last_state_key:
            held = (now - self._state_since) if self._state_since else 0.0
            self._last_state_key = key
            self._state_since = now
            self.recent.append((now - self.t0, decision.describe()))
            if self._log_w and not decision.action:
                why = decision.reason + (f" / {decision.blocked_by}"
                                         if decision.blocked_by else "")
                # 带上当时的车速转速：事后才能回答"为什么顶着断油不升挡"
                why += f" @{tele.speed_kmh:.0f}km/h {tele.rpm:.0f}转"
                # 最后一列是**上一条状态**持续了多久。只记"状态变了"看不出
                # "卡了 3 秒"和"闪了一下"的区别——那正是最难查的那类问题。
                self._log_w.writerow([f"{now - self.t0:.3f}", "HOLD", tele.gear,
                                      "—", 0, why, f"{held:.2f}"])
                self._log_f.flush()

        if not (self.enabled and decision.is_shift):
            return False
        if not allow_send:
            # 断流恢复后的第一帧：数据不够新，不能拿它当发键依据。
            # 回滚掉刚才那笔"换挡意图"，否则会变成一次假超时。
            self.engine.rollback_command()
            self.last_text = f"HOLD 遥测断流，本帧不发  ({decision.reason})"
            return False
        if not self.sink.shift(decision.action):
            self.engine.rollback_command()
            self.last_error = self.sink.last_error
            self.last_text = f"发送失败：{self.sink.last_error}"
            return False
        return True

    def _flush_resolved(self) -> None:
        rec = self.engine.last_resolved
        if rec is None or rec is self._logged_rec or self._log_w is None:
            return
        self._logged_rec = rec
        self._log_w.writerow([f"{rec.t - self.t0:.3f}", rec.action, rec.from_gear,
                              "成功" if rec.ok else "失败", rec.latency_ms,
                              rec.reason, ""])
        self._log_f.flush()

    def _maybe_save(self) -> None:
        if not self.learn or not self.car_key or not self.vehicle.dirty:
            return
        now = time.monotonic()
        if now - self._last_save >= SAVE_EVERY_S or self.vehicle.dirty >= SAVE_EVERY_N:
            self._save_profile()

    def _save_profile(self) -> bool:
        if not self.car_key:
            return False
        if self.store.save(self.car_key, self.vehicle.to_dict()):
            self.vehicle.mark_saved()
            self._last_save = time.monotonic()
            self.profile_error = ""
            return True
        # 写不进去要说出来。只读目录（Program Files）、磁盘满、权限问题，
        # 原来都是静默失败——用户跑了两圈，成果什么都没留下。
        self.profile_error = (f"档案写不进去：{self.store.last_error or self.store.dir}")
        return False

    # -- 对外 --------------------------------------------------------------
    def toggle(self) -> bool:
        self.enabled = not self.enabled
        self.engine.pending = None
        self.engine.last_action = ""          # 不带上一段的状态进来
        self.engine.last_shift_t = -1e9      # 重新启用时不带旧冷却
        return self.enabled

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.toggle()

    def set_clutch(self, on: bool) -> bool:
        """切换"手动离合换挡"。返回值表示输出层有没有离合键可用。

        界面勾选、命令行开关都走这里——只有一个入口，才不会出现"界面显示开着、
        实际没踩离合"这种事。
        """
        setter = getattr(self.sink, "set_clutch", None)
        ok = bool(setter(on)) if callable(setter) else False
        if not ok and on:
            self.last_error = "手动离合需要 --clutch-key 指定离合键"
        return ok

    def save_now(self) -> bool:
        if not self.car_key:
            return False
        return self._save_profile()

    def reset_profile(self) -> bool:
        """删掉当前车的档案，回到一张白纸。

        为什么需要：学到的数据会直接参与换挡决策。如果某一轮被脏数据污染了，
        用户得有个恢复手段——不应该逼他去 dist/profiles/ 里手动删 JSON。
        """
        if not self.car_key:
            return False
        path = self.store.path_for(self.car_key)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            self.profile_error = f"删不掉 {path}：{e}"
            return False
        ordinal = self.vehicle.car_ordinal
        self.vehicle = VehicleModel(ordinal)
        self.engine.vehicle = self.vehicle
        self.engine.reset_car()
        self.profile_error = ""
        return True

    def profile_path(self) -> str:
        return self.store.path_for(self.car_key) if self.car_key else ""

    def stale(self) -> bool:
        if not self.last_frame_t:
            return True
        return (time.monotonic() - self.last_frame_t) > STALE_S

    def learning_text(self) -> str:
        if not self.learn:
            return "学习已关闭（--no-learn）"
        return self.vehicle.summary()

    def stats_text(self) -> str:
        return self.engine.stats()

    def snapshot(self) -> dict:
        t = self.last_tele
        return {
            "gear": t.gear if t else 0,
            "rpm": t.rpm if t else 0.0,
            "max_rpm": t.max_rpm if t else 0.0,
            "rpm_pct": t.rpm_pct if t else 0.0,
            # 灯条要用两个数：
            #   limiter_pct  满格基准（物理上限）
            #   up_pct       当前油门下的升挡点（控制回路的上限）
            # 灯条满格取两者较大值，闪烁在到达升挡点时触发——见 hud.py
            "limiter_pct": self.vehicle.limiter_pct or 0.0,
            "up_pct": (self.engine.upshift_pct(t.throttle, t.gear)
                       if (t is not None and t.usable and 1 <= t.gear <= 10)
                       else 0.0),
            # 满油门升挡点：灯条标定的兜底基准。学到断油之前用它，
            # 否则红区会落在升挡点之上，永远亮不起来。
            "up_max": float(self.policy.up_pct_high),
            "speed": t.speed_kmh if t else 0.0,
            "throttle": t.throttle if t else 0.0,
            "brake": t.brake if t else 0.0,
            "has_data": t is not None,
            # 这一帧是不是真的"在比赛里"。菜单/暂停时 gear 是 0，
            # 但 gear_label(0) 是 "R"——如果照着显示，菜单里会看到倒挡。
            "usable": bool(t.usable) if t is not None else False,
            "stale": self.stale(),
            "frames": self.frames,
            "bad_packets": self.bad_packets,
            "foreign_packets": self.foreign_packets,
            "watchdog_trips": self.watchdog_trips,
            "decision": self.last_text,
            "stats": self.stats_text(),
            "learning": self.learning_text(),
            "car_key": self.car_key,
            "profile": self.profile_path(),
            "profile_error": self.profile_error,
            "enabled": self.enabled,
            # 手动离合：clutch 是当前是否启用，clutch_key 是能不能启用
            "clutch": bool(getattr(self.sink, "use_clutch", False)),
            "clutch_key": bool(getattr(self.sink, "clutch_scan", 0)),
            "last_error": self.last_error,
            "recent": list(self.recent),
        }
