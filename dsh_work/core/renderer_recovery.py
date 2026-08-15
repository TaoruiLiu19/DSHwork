"""渲染进程/WebView 崩溃自恢复状态机（P1，对齐 dsh_desktop 稳定存活清零思路）。

核心机制（借鉴对方"正常运行一段时间后，累计崩溃次数清零"的设计）：

                        ┌──────────────────────────────────────┐
                        │  create() → register_alive_timer()   │
                        │  └─ 启动 stable_timer（30s）        │
                        │     └─ 到期 → crash_count = 0       │
                        └──────────────┬───────────────────────┘
                                       │
                                       ▼
                              ┌──────────────────┐
                              │ crash_count = 0  │◀────────── stable_window timeout
                              │ status: healthy  │                （清零计数，下次崩溃从 1 起）
                              └─────────┬────────┘
                                        │ report_crash()
                                        ▼
                              ┌───────────────────────┐
                              │ crash_count += 1      │
                              │ stable_timer.restart()│
                              └─────────┬─────────────┘
                                        │
                        ┌───────────────┴───────────────┐
                        │                               │
                  crash_count <= MAX                 crash_count > MAX
                        │                               │
                        ▼                               ▼
              ┌──────────────────┐             ┌────────────────────┐
              │ action: RECOVER  │             │ action: GIVE_UP    │
              │ 调用用户 rec_fn() │             │ 告知用户手动刷新    │
              └──────────────────┘             └────────────────────┘

这样做的理由：
  · 偶发崩溃（OOM、GPU 驱动 bug）可以无感恢复，用户体验好
  · 持续故障（插件/页面死循环）在 N 次重试后停止自动恢复，避免「狂重启烧 CPU + 反复弹窗闪屏」
  · 状态机不依赖 Qt 核心类型，可单独测试；Qt 侧做一个薄适配器，非 Qt 程序也能复用

典型用法（QWebEnginePage）：
    machine = RendererRecoveryMachine(
        max_retries=3,
        stable_alive_secs=30,
        recover_fn=self._recreate_webview,
        give_up_fn=self._show_failure_banner,
    )
    # 挂载 WebEngine 信号
    page.renderProcessTerminated.connect(
        lambda term, status: machine.report_crash()
    )
    machine.track_created(page)  # 启动存活计时
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..utils.logger import get_logger

log = get_logger("core.renderer_recovery")


# ============================================================================
# 公开常量 / 数据类
# ============================================================================
class RecoveryAction(str, Enum):
    """report_crash() 返回的下一步动作（供调用方断言/埋点）。"""
    RECOVER = "recover"     # 触发恢复回调（重建 WebView/重启子进程）
    GIVE_UP = "give_up"     # 达到上限后交给用户（弹提示/横幅）
    NO_OP   = "no_op"       # 已放弃或内部异常（不应走到，保险值）


@dataclass
class RecoveryStats:
    """外部观察当前状态（状态栏展示/埋点上报）。"""
    crash_count: int                    # 当前累计崩溃次数（稳定窗口到期后会清零）
    max_retries: int                    # 配置的最大重试次数
    stable_alive_secs: int              # 配置的稳定窗口（秒）
    alive_seconds: float                # 当前实例已稳定存活秒数
    gave_up: bool                       # 是否已进入「不再自动恢复」态
    give_up_count: int                  # 历史上总共触发过多少次 GIVE_UP（会话内累计）


# ============================================================================
# 状态机核心
# ============================================================================
class RendererRecoveryMachine:
    """可复用的崩溃恢复状态机。

    线程安全：crash 上报和存活时间更新可能来自不同线程，内部用 RLock。
    计时器：默认用 threading.Timer（Qt 侧可以替换为 QTimer Adapter 以省去线程切换）。
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        stable_alive_secs: int = 30,
        recover_fn: Optional[Callable[[int], None]] = None,
        give_up_fn: Optional[Callable[[int], None]] = None,
        timer_cls: Optional[type] = None,
    ) -> None:
        """
        Args:
            max_retries:         连续崩溃多少次后放弃自动恢复（默认 3 次，与 Electron 常见经验值一致）
            stable_alive_secs:   稳定存活多少秒后把崩溃计数清零（默认 30s，对齐 dsh_desktop 思路）
            recover_fn(cnt):     崩溃后应该「恢复」时调用；cnt 是本次 crash_count（1=第一次）
            give_up_fn(cnt):     达到 max_retries 后调用；cnt 是当前 crash_count（>max_retries）
            timer_cls:           定时器类，必须实现 Timer(seconds, callback).start()/.cancel()
                                 默认 threading.Timer；Qt 侧可传一个 QTimer 封装，便于信号回主线程
        """
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if stable_alive_secs < 1:
            raise ValueError("stable_alive_secs must be >= 1")

        self.max_retries = int(max_retries)
        self.stable_alive_secs = int(stable_alive_secs)
        self.recover_fn = recover_fn
        self.give_up_fn = give_up_fn
        self._timer_cls = timer_cls or threading.Timer

        self._lock = threading.RLock()
        self._crash_count = 0
        self._gave_up = False
        self._give_up_count = 0
        self._created_at: Optional[float] = None     # 最近一次 track_created() 的时间戳
        self._stable_timer: Optional[object] = None  # threading.Timer 或 QTimer adapter

    # ------------------------------ 对外 API ------------------------------

    def track_created(self, obj: object = None) -> None:
        """组件（WebView/子进程）「刚创建完成」时调用，启动稳定存活计时窗口。

        每次重建后必须调用一次，否则：
          · 30s 清零逻辑不生效
          · alive_seconds() 返回 0
        Args:
            obj: 可选，被追踪对象（用于日志标识，不参与状态）
        """
        with self._lock:
            self._cancel_stable_timer_locked()
            self._created_at = time.monotonic()
            self._gave_up = False
            # 启动稳定计时器：到期后把崩溃计数清零
            self._stable_timer = self._timer_cls(
                self.stable_alive_secs, self._on_stable_window_complete
            )
            try:
                self._stable_timer.start()  # type: ignore[attr-defined]
            except Exception as e:
                log.error("stable_timer.start 失败: %s", e)
                self._stable_timer = None
            log.debug(
                "track_created obj=%s crash_count=%d gave_up=%s stable_window=%ss",
                getattr(obj, "__class__", type(obj)).__name__ if obj is not None else None,
                self._crash_count, self._gave_up, self.stable_alive_secs,
            )

    def report_crash(self, reason: str = "") -> RecoveryAction:
        """上报一次崩溃，返回下一步动作。

        触发 recover_fn / give_up_fn 回调（回调在「调用线程」执行，Qt 调用方请确保
        从主线程调用 report_crash，或回调内部切回主线程）。
        """
        with self._lock:
            # 稳定计时器：崩溃时重启 —— 避免「距离上次崩溃 29s 又崩一次 → 马上被清零」误判
            self._cancel_stable_timer_locked()
            # 已放弃：直接 NO_OP + 日志
            if self._gave_up:
                log.warning(
                    "再次崩溃但已进入 give_up 态，忽略自动恢复 (reason=%s)",
                    reason or "unknown",
                )
                return RecoveryAction.NO_OP
            self._crash_count += 1
            cnt = self._crash_count
            log.warning(
                "检测到渲染崩溃 count=%d/%d (reason=%s)",
                cnt, self.max_retries, reason or "unknown",
            )

            action: RecoveryAction
            if cnt <= self.max_retries:
                action = RecoveryAction.RECOVER
                self._safe_invoke(self.recover_fn, cnt)
                # 恢复后重新挂稳定计时器（下一帧 track_created 也会再挂一次，这里是兜底）
                self._arm_stable_timer_locked()
            else:
                action = RecoveryAction.GIVE_UP
                self._gave_up = True
                self._give_up_count += 1
                self._safe_invoke(self.give_up_fn, cnt)
            return action

    def reset(self) -> None:
        """手动重置计数/状态（用户手动点"恢复"后调用）。"""
        with self._lock:
            self._cancel_stable_timer_locked()
            self._crash_count = 0
            self._gave_up = False
            self._created_at = None

    def stats(self) -> RecoveryStats:
        with self._lock:
            now = time.monotonic()
            alive = 0.0 if not self._created_at else max(0.0, now - self._created_at)
            return RecoveryStats(
                crash_count=self._crash_count,
                max_retries=self.max_retries,
                stable_alive_secs=self.stable_alive_secs,
                alive_seconds=round(alive, 2),
                gave_up=self._gave_up,
                give_up_count=self._give_up_count,
            )

    # ------------------------------ 内部 ------------------------------

    def _cancel_stable_timer_locked(self) -> None:
        t = self._stable_timer
        self._stable_timer = None
        if t is None:
            return
        try:
            t.cancel()  # type: ignore[attr-defined]
        except Exception as e:
            log.debug("取消 stable_timer 异常 (忽略): %s", e)

    def _arm_stable_timer_locked(self) -> None:
        # 兜底用：report_crash RECOVER 分支挂一次，30s 内没再崩就清零
        self._cancel_stable_timer_locked()
        self._stable_timer = self._timer_cls(
            self.stable_alive_secs, self._on_stable_window_complete
        )
        try:
            self._stable_timer.start()  # type: ignore[attr-defined]
        except Exception as e:
            log.error("stable_timer arm 失败: %s", e)
            self._stable_timer = None

    def _on_stable_window_complete(self) -> None:
        """稳定窗口到期：清零崩溃计数（线程安全）。"""
        with self._lock:
            if self._crash_count > 0:
                log.info(
                    "稳定存活 %ds，崩溃计数 %d → 0（已过稳定窗口）",
                    self.stable_alive_secs, self._crash_count,
                )
                self._crash_count = 0
            self._gave_up = False

    @staticmethod
    def _safe_invoke(fn: Optional[Callable[[int], None]], arg: int) -> None:
        if not callable(fn):
            return
        try:
            fn(arg)
        except Exception as e:
            log.exception("recovery 回调抛出异常: %s", e)
