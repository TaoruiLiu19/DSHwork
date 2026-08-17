"""启动画面：环境检测（第 7.2 节）。

环境检测在后台 daemon 线程执行，主线程用 QTimer 轮询共享变量来更新 UI，
不依赖跨线程 Signal（PySide6 从非 QThread 线程 emit 信号不可靠）。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QProgressBar,
    QPlainTextEdit,
    QPushButton,
    QDialog,
    QHBoxLayout,
)

from ... import constants as C
from ...core.process_manager import ProcessManager, EnvironmentCheck
from ...utils.logger import get_logger

log = get_logger("ui.splash_screen")


class EnvironmentCheckWorker:
    """环境检测 worker（纯 Python，不继承 QObject）。

    在 daemon threading.Thread 中执行，结果写入共享变量。
    主线程通过 QTimer 轮询读取，更新 UI。
    不使用 Signal.emit 跨线程传递，避免 PySide6 信号投递问题。
    """

    def __init__(self, process_manager: ProcessManager, workspace: str = ""):
        self.process_manager = process_manager
        self.workspace = workspace
        self._aborted = False
        self._thread: threading.Thread | None = None

        # 共享数据（worker 写，主线程读）
        self.progress: int = 0
        self.status_msg: str = ""
        self.log_lines: list[str] = []
        self.result: EnvironmentCheck | None = None
        self.done: bool = False
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="env-check"
        )
        self._thread.start()

    def abort(self) -> None:
        self._aborted = True

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set_progress(self, value: int, msg: str) -> None:
        with self._lock:
            self.progress = value
            self.status_msg = msg

    def _add_log(self, line: str) -> None:
        with self._lock:
            self.log_lines.append(line)

    def _set_result(self, check: EnvironmentCheck) -> None:
        with self._lock:
            self.result = check
            self.done = True

    def snapshot(self) -> tuple[int, str, list[str], bool, EnvironmentCheck | None]:
        """主线程安全读取当前状态。"""
        with self._lock:
            return (
                self.progress,
                self.status_msg,
                list(self.log_lines),
                self.done,
                self.result,
            )

    def _run(self) -> None:
        check = EnvironmentCheck()
        try:
            self._add_log("开始环境检测...")
            self._set_progress(10, "检测 Node.js...")

            # 设置日志回调：ProcessManager 的 _emit_log → worker._add_log → 启动画面日志框
            self.process_manager.set_log_callback(self._add_log)

            step = 0
            total_steps = 5

            # 1. Node.js（subprocess.run timeout=10s）
            step += 1
            check.node_ok, check.node_version = self.process_manager._check_node()
            self._set_progress(
                int(step / total_steps * 100),
                f"Node.js: {check.node_version or '未检测到'}",
            )
            if not check.node_ok:
                check.errors.append("未检测到 Node.js 18+，请安装：https://nodejs.org/")

            if self._aborted:
                return self._set_result(check)

            # 2. DSH CLI
            step += 1
            self._set_progress(int(step / total_steps * 100), "检测 DSH CLI...")
            check.dsh_cli_ok, check.dsh_cli_version = (
                self.process_manager._check_dsh_cli()
            )
            self._set_progress(
                int(step / total_steps * 100),
                f"DSH CLI: {check.dsh_cli_version or '未检测到'}",
            )

            # DSH CLI 未安装 → 标记需要下载，由主线程弹出阻塞式下载面板
            # （避免后台下载时已进入离线模式，用户看不到进度）
            if not check.dsh_cli_ok and not self._aborted:
                check.need_download = True
                # 优先使用便携运行时（local_runtime），下载 Node 阶段有精确百分比进度，
                # 圆形进度条展示效果更好；npm_global 无精确进度只能用不确定旋转模式
                check.download_type = "local_runtime"
                self._add_log("DSH CLI 未安装，需要下载运行环境...")
                self._add_log(
                    f"下载类型: {check.download_type}"
                    + "（便携运行时：下载 Node.js + 本地安装 DSH，有精确进度）"
                )
                return self._set_result(check)

            if self._aborted:
                return self._set_result(check)

            # 3. DSH 运行中检测（TCP 连 127.0.0.1:3080，2s 超时）
            step += 1
            check.dsh_running = self.process_manager._check_dsh_running()
            self._set_progress(
                int(step / total_steps * 100),
                f"DSH 运行中: {'是' if check.dsh_running else '否'}",
            )

            # 若 DSH 未运行且有 Node + CLI，尝试启动（30s 超时，首次可能需下载依赖）
            if (
                not check.dsh_running
                and check.node_ok
                and check.dsh_cli_ok
                and not self._aborted
            ):
                self._add_log("DSH 未运行，尝试启动（30s 超时）...")
                try:
                    check.dsh_running = self.process_manager.start_dsh(
                        workspace=self.workspace, timeout=C.DSH_STARTUP_TIMEOUT_SEC
                    )
                except Exception as e:
                    self._add_log(f"启动 DSH 失败: {e}")
                    check.dsh_running = False

                if not check.dsh_running and not self._aborted:
                    self._add_log("DSH 未就绪，将以离线模式进入主界面")
                    check.errors.append(
                        "DSH 启动未就绪，进入离线模式。可在设置中手动配置。"
                    )

            if self._aborted:
                return self._set_result(check)

            # 4 & 5. API Key + 可用模型
            if check.dsh_running and not self._aborted:
                step += 1
                self._set_progress(
                    int(step / total_steps * 100), "检测 API Key 与模型..."
                )
                import queue

                result_q: queue.Queue = queue.Queue()

                def _run_comms():
                    try:
                        from ...api import DshService

                        dsh = DshService()
                        probe = dsh.initialize()
                        if probe.success:
                            creds = dsh.check_credentials()
                            models = dsh.get_models()
                            dsh.shutdown()
                            result_q.put(
                                (
                                    bool(creds.get("has_key") or creds.get("valid")),
                                    [m.id for m in models],
                                    None,
                                )
                            )
                        else:
                            result_q.put((False, [], probe.error or "探测失败"))
                    except Exception as e:
                        result_q.put((False, [], str(e)))

                t = threading.Thread(target=_run_comms, daemon=True, name="splash-comms")
                t.start()
                t.join(timeout=5)
                if t.is_alive() or result_q.empty():
                    check.errors.append("DSH 通信检测超时，跳过 API Key 与模型校验。")
                else:
                    key_ok, models, err = result_q.get()
                    check.api_key_ok = key_ok
                    check.models_available = models
                    if err:
                        check.errors.append(f"DSH 通信检测失败: {err}")
                    if not key_ok and not err:
                        check.errors.append("API Key 未配置或无效，请在设置中输入")
                    if not models and key_ok and not err:
                        check.errors.append("无可用模型，请检查 API Key 权限")
            else:
                step += 1

            step += 1
            self._set_progress(
                100,
                "检测完成" if check.dsh_running else "进入离线模式",
            )
        except Exception as e:
            log.exception("环境检测线程异常: %s", e)
            check.errors.append(f"环境检测发生异常: {e}")
        finally:
            self._set_result(check)


class SplashScreen(QDialog):
    """启动画面。

    用 QTimer 轮询 worker 共享变量来更新 UI，
    不依赖跨线程 Signal，避免 PySide6 信号投递问题。
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        workspace: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.process_manager = process_manager
        self.workspace = workspace
        self._check_result: EnvironmentCheck | None = None
        self._worker: EnvironmentCheckWorker | None = None
        self._poll_timer: QTimer | None = None
        self._elapsed_log_lines = 0
        # 防止下载失败后重复弹窗（只尝试一次，失败则进入离线模式）
        self._download_attempted = False
        self._setup_ui()
        self._start_check()

    def _setup_ui(self) -> None:
        self.setWindowTitle("DSH Work 启动中")
        self.setFixedSize(480, 360)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("DSH Work")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #32F08C;")
        layout.addWidget(title)

        subtitle = QLabel("AI 原生桌面工作台")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size: 12px; color: #9599A6;")
        layout.addWidget(subtitle)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        self._status_label = QLabel("初始化...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("font-size: 12px; color: #9599A6;")
        layout.addWidget(self._status_label)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFixedHeight(80)
        self._log_view.setStyleSheet(
            "background-color: #1A1B1D; color: #9599A6; "
            "font-family: monospace; font-size: 10px; "
            "border: 1px solid #B5BDC5; border-radius: 4px;"
        )
        layout.addWidget(self._log_view)

        # 错误面板（默认隐藏）
        self._error_widget = QWidget()
        error_layout = QVBoxLayout(self._error_widget)
        error_layout.setContentsMargins(0, 0, 0, 0)
        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: #F65A5A; font-size: 11px;")
        self._error_label.setWordWrap(True)
        error_layout.addWidget(self._error_label)

        self._skip_btn = QPushButton("跳过，进入离线模式")
        self._skip_btn.setStyleSheet(
            "color: #7BB8FF; border: none; font-size: 11px;"
        )
        self._skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_btn.clicked.connect(self._skip)

        btn_row = QHBoxLayout()
        self._export_btn = QPushButton("导出诊断日志")
        self._export_btn.clicked.connect(self._export_diagnostics)
        btn_row.addWidget(self._skip_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._export_btn)
        error_layout.addLayout(btn_row)
        self._error_widget.setVisible(False)
        layout.addWidget(self._error_widget)

    def _start_check(self) -> None:
        """启动 worker + QTimer 轮询。"""
        self._worker = EnvironmentCheckWorker(
            self.process_manager, self.workspace
        )
        self._worker.start()

        # QTimer 每 100ms 轮询 worker 状态，在主线程更新 UI
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_worker)
        self._poll_timer.start(100)

    def _poll_worker(self) -> None:
        """主线程轮询 worker 共享变量，更新 UI。"""
        try:
            self._do_poll_worker()
        except KeyboardInterrupt:
            log.warning("用户中断启动画面轮询（Ctrl+C），继续等待环境检测完成...")
            # KeyboardInterrupt 在 Qt 事件循环中可能被吞掉，但不会影响 _poll_timer 继续触发
            # 只需要确保 _poll_worker 不会因为一次中断而永久停止

    def _do_poll_worker(self) -> None:
        if self._worker is None:
            return

        progress, status_msg, log_lines, done, result = self._worker.snapshot()

        # 更新进度条和状态
        self._progress.setValue(progress)
        self._status_label.setText(status_msg)

        # 追加新日志行
        new_count = len(log_lines) - self._elapsed_log_lines
        if new_count > 0:
            for line in log_lines[self._elapsed_log_lines:]:
                self._log_view.appendPlainText(line)
            self._elapsed_log_lines = len(log_lines)

        # 检测完成
        if done and result is not None:
            self._poll_timer.stop()
            self._check_result = result

            # 首次启动需要下载 DSH → 弹出阻塞式下载面板
            # 下载完成后重新检测，避免进入离线模式时下载还在后台跑
            if result.need_download and not self._download_attempted:
                self._download_attempted = True
                self._status_label.setText("需要下载 DSH 运行环境...")
                self._handle_download(result.download_type)
                return

            if result.all_ok:
                self._status_label.setText("就绪")
                self._status_label.setStyleSheet("font-size: 12px; color: #33C192;")
                QTimer.singleShot(500, self.accept)
            else:
                self._status_label.setText("检测发现问题，2 秒后进入离线模式...")
                self._status_label.setStyleSheet("font-size: 12px; color: #D27E24;")
                if result.errors:
                    self._error_label.setText("\n".join(result.errors))
                self._error_widget.setVisible(True)
                QTimer.singleShot(2000, self.accept)

    def _handle_download(self, download_type: str) -> None:
        """弹出阻塞式下载面板，完成后重新检测环境。

        下载面板内部 daemon 线程执行下载，主线程 QTimer 轮询更新进度。
        exec() 阻塞直到下载完成（成功/失败），确保用户看到完整进度。
        """
        from ..widgets.download_progress_panel import DownloadProgressPanel

        panel = DownloadProgressPanel(self.process_manager, download_type, self)
        panel.exec()

        if panel.success:
            # 下载成功，重新检测环境（DSH CLI 应已就绪）
            self._restart_check()
        else:
            # 下载失败，accept 进入离线模式（用户可后续在设置中重试）
            self._status_label.setText("下载失败，进入离线模式...")
            QTimer.singleShot(1000, self.accept)

    def _restart_check(self) -> None:
        """下载完成后重新检测环境（DSH CLI 应已就绪）。"""
        self._status_label.setText("下载完成，重新检测环境...")
        self._status_label.setStyleSheet("font-size: 12px; color: #32F08C;")
        # 重置日志框
        self._log_view.clear()
        self._elapsed_log_lines = 0
        # 新建 worker 做第二次检测
        self._worker = EnvironmentCheckWorker(self.process_manager, self.workspace)
        self._worker.start()
        self._poll_timer.start(100)

    def _skip(self) -> None:
        """用户点击跳过。"""
        if self._worker:
            self._worker.abort()
        if self._poll_timer:
            self._poll_timer.stop()
        self.accept()

    def _export_diagnostics(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        from ...utils.logger import export_diagnostics_bundle
        from pathlib import Path

        target = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if target:
            bundle = export_diagnostics_bundle(Path(target))
            self._error_label.setText(f"诊断日志已导出: {bundle}")

    @property
    def check_result(self) -> EnvironmentCheck | None:
        return self._check_result

    @property
    def worker(self) -> EnvironmentCheckWorker | None:
        return self._worker
