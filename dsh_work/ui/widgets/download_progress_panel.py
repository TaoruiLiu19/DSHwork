"""DSH 下载进度面板（首次启动阻塞式下载）。

首次启动检测到电脑里没有 DSH 时，弹出此面板阻塞启动，
下载完成后再继续环境检测，避免进入离线模式时下载还在后台跑。

UI 组件：
- CircularProgress：圆形环状进度条，支持精确百分比 + 不确定旋转模式
- DownloadProgressPanel：模态对话框，内部 daemon 线程下载，QTimer 轮询更新
"""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QConicalGradient
from PySide6.QtWidgets import (
    QWidget,
    QDialog,
    QVBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QHBoxLayout,
)

from ...utils.logger import get_logger

log = get_logger("ui.download_progress_panel")

# 进度回调签名：(done_bytes, total_bytes)；total_bytes=0 表示未知大小
ProgressCb = Callable[[int, int], None]


class CircularProgress(QWidget):
    """圆形环状进度条。

    两种模式：
    - 精确百分比：set_progress(done, total) 当 total>0 时显示百分比
    - 不确定模式：total=0 时显示旋转弧线（npm install 无精确进度时用）
    """

    # 配色（与启动画面标题色一致，暗色背景下可读）
    _COLOR_TRACK = QColor(255, 255, 255, 25)      # 底环
    _COLOR_DONE = QColor(50, 240, 140)            # 进度环 #32F08C
    _COLOR_TEXT = QColor(255, 255, 255, 230)      # 中心文字
    _COLOR_SUB = QColor(255, 255, 255, 140)       # 副文字

    def __init__(self, parent: QWidget | None = None, diameter: int = 160):
        super().__init__(parent)
        self._done = 0
        self._total = 0
        self._angle = 0  # 不确定模式旋转角度
        self._diameter = diameter
        # 固定尺寸：让 QLayout 不会因 sizePolicy 覆盖而破坏高宽，
        # 同时提供 sizeHint/minimumSizeHint 作为兜底
        self.setFixedSize(diameter, diameter)
        # 不确定模式旋转动画
        self._spin_timer = QTimer(self)
        self._spin_timer.timeout.connect(self._on_spin)
        self._spin_timer.start(40)  # 25fps 旋转

    # ---- 尺寸元信息（防止外部 setSizePolicy 破坏固定几何） ----
    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(self._diameter, self._diameter)

    def minimumSizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(self._diameter, self._diameter)

    def set_progress(self, done: int, total: int) -> None:
        """设置进度。total=0 表示未知大小（不确定模式）。"""
        changed = (done != self._done) or (total != self._total)
        self._done = done
        self._total = total
        if changed:
            self.update()

    def reset(self) -> None:
        """重置进度。"""
        self._done = 0
        self._total = 0
        self.update()

    def _on_spin(self) -> None:
        """不确定模式旋转。"""
        if self._total == 0:
            self._angle = (self._angle + 6) % 360
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        margin = 10
        ring_width = 8
        rect = QRectF(margin, margin, w - 2 * margin, h - 2 * margin)

        # 底环
        pen = QPen(self._COLOR_TRACK)
        pen.setWidth(ring_width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, 0, 360 * 16)

        if self._total > 0 and self._done > 0:
            # 精确百分比模式
            pct = min(100, int(self._done * 100 / self._total))
            # 渐变进度环（从绿到青，现代感）
            gradient = QConicalGradient(
                rect.center(), 90 - 360 * pct / 100
            )
            gradient.setColorAt(0, QColor(50, 240, 140))
            gradient.setColorAt(1, QColor(80, 200, 255))
            pen.setColor(self._COLOR_DONE)
            painter.setPen(pen)
            # Qt arc：startAngle 单位 1/16°，0=3点方向，正值逆时针
            # 从顶部（90°）开始顺时针绘制
            start_angle = 90 * 16
            span_angle = -int(pct * 3.6) * 16  # 负值=顺时针
            painter.drawArc(rect, start_angle, span_angle)

            # 中心百分比文字
            painter.setPen(self._COLOR_TEXT)
            font = QFont()
            font.setPointSize(int(w / 8))
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{pct}%")
        else:
            # 不确定模式：旋转的弧线（约 110°）
            pen.setColor(QColor(50, 240, 140, 220))
            painter.setPen(pen)
            start = (self._angle + 90) * 16
            span = -110 * 16
            painter.drawArc(rect, start, span)

            # 中心省略号
            painter.setPen(self._COLOR_SUB)
            font = QFont()
            font.setPointSize(int(w / 10))
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "···")

        painter.end()


class DownloadProgressPanel(QDialog):
    """DSH 下载进度面板（阻塞式模态对话框）。

    首次启动检测到需要下载 DSH 时弹出，阻塞主线程直到下载完成。
    内部 daemon 线程执行下载，QTimer 每 100ms 轮询共享变量更新 UI。

    两种下载类型：
    - local_runtime：便携运行时（下载 Node + 本地 npm install dsh），有精确进度
    - npm_global：npm install -g @deepseek-ai/dsh，无精确进度（不确定模式）
    """

    def __init__(
        self,
        process_manager,
        download_type: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.process_manager = process_manager
        self.download_type = download_type
        self._success = False
        self._finished = False
        self._thread: threading.Thread | None = None
        self._poll_timer: QTimer | None = None

        # 共享状态（daemon 线程写，主线程 QTimer 读）
        self._progress_done = 0
        self._progress_total = 0
        self._stage_msg = ""
        self._log_lines: list[str] = []
        self._elapsed_logs = 0
        self._lock = threading.Lock()

        self._setup_ui()
        self._start_download()

    # ===== UI =====

    def _setup_ui(self) -> None:
        self.setWindowTitle("下载 DSH 运行时")
        # 高度需容纳：margin 44 + title 22 + subtitle 16 + progress 140 + spacer 8
        # + stage_label 17 + log_view 90 + btn_row 30 + 7 个 spacing×14=98 ≈ 465
        # 用 470 确保不压缩 fixed 控件导致重叠
        self.setFixedSize(440, 470)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        # 暗色半透明背景（模拟磨砂玻璃质感，避免抢眼）
        self.setStyleSheet(
            "DownloadProgressPanel {"
            "  background-color: rgba(26, 27, 29, 0.96);"
            "  border: 1px solid #B5BDC5;"
            "  border-radius: 12px;"
            "}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        # 标题
        title = QLabel("下载 DSH 运行时")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #32F08C;")
        layout.addWidget(title)

        subtitle = QLabel("首次使用需下载运行环境，完成后自动启动")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("font-size: 11px; color: #9599A6;")
        layout.addWidget(subtitle)

        # 圆形进度条（居中，不覆盖下方阶段文字）
        self._progress = CircularProgress(self, diameter=140)
        layout.addWidget(self._progress, alignment=Qt.AlignmentFlag.AlignCenter)

        # 圆环与阶段文字之间加 4px 空白，视觉上不贴边
        spacer = QLabel()
        spacer.setFixedHeight(4)
        layout.addWidget(spacer)

        # 阶段文字
        self._stage_label = QLabel("正在准备...")
        self._stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stage_label.setStyleSheet("font-size: 12px; color: #E0E2F2;")
        layout.addWidget(self._stage_label)

        # 日志框
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFixedHeight(90)
        self._log_view.setStyleSheet(
            "QPlainTextEdit {"
            "  background-color: rgba(0, 0, 0, 0.3);"
            "  color: #9599A6;"
            "  font-family: monospace; font-size: 10px;"
            "  border: 1px solid #B5BDC5;"
            "  border-radius: 6px;"
            "  padding: 6px;"
            "}"
        )
        layout.addWidget(self._log_view)

        # 底部按钮区
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._close_btn = QPushButton("关闭")
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(50, 240, 140, 0.15);"
            "  color: #32F08C; border: 1px solid rgba(50, 240, 140, 0.3);"
            "  border-radius: 6px; padding: 6px 18px; font-size: 12px;"
            "}"
            "QPushButton:hover { background-color: rgba(50, 240, 140, 0.25); }"
            "QPushButton:disabled { color: #555; border-color: #333; }"
        )
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    # ===== 下载线程 =====

    def _start_download(self) -> None:
        """启动 daemon 下载线程 + QTimer 轮询。"""
        self._thread = threading.Thread(
            target=self._run_download, daemon=True, name="dsh-download"
        )
        self._thread.start()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(100)

    def _run_download(self) -> None:
        """daemon 线程：执行下载，通过共享变量回传进度/日志。"""
        def _log_cb(line: str) -> None:
            with self._lock:
                self._log_lines.append(line)

        def _progress_cb(done: int, total: int) -> None:
            with self._lock:
                self._progress_done = done
                self._progress_total = total

        # 接管 ProcessManager 的日志回调（环境检测 worker 之前可能已设置）
        try:
            self.process_manager.set_log_callback(_log_cb)
        except Exception:
            pass

        try:
            if self.download_type == "local_runtime":
                # 便携运行时：下载 Node（有进度）+ npm install dsh（无精确进度）
                with self._lock:
                    self._stage_msg = "正在下载便携 Node.js..."
                ok = self.process_manager.ensure_local_runtime(_progress_cb)
            else:
                # npm install -g：无精确进度，用不确定模式
                with self._lock:
                    self._stage_msg = "正在通过 npm 安装 DSH CLI..."
                    self._progress_total = 0  # 触发不确定模式
                ok = self.process_manager.install_dsh_cli()

            with self._lock:
                self._success = ok
                self._finished = True
                if ok:
                    self._stage_msg = "下载完成"
                    self._progress_done = 1
                    self._progress_total = 1
                else:
                    self._stage_msg = "下载失败，请查看日志或手动安装"
        except Exception as e:
            log.exception("下载线程异常: %s", e)
            with self._lock:
                self._log_lines.append(f"⚠ 下载异常: {e}")
                self._success = False
                self._finished = True
                self._stage_msg = "下载异常，请查看日志"

    # ===== 主线程轮询 =====

    def _poll(self) -> None:
        """主线程 QTimer：读取共享变量更新 UI。"""
        with self._lock:
            done = self._progress_done
            total = self._progress_total
            stage = self._stage_msg
            finished = self._finished
            success = self._success
            new_logs = self._log_lines[self._elapsed_logs:]
            self._elapsed_logs = len(self._log_lines)

        # 更新进度条
        self._progress.set_progress(done, total)
        self._stage_label.setText(stage)

        # 追加新日志
        for line in new_logs:
            self._log_view.appendPlainText(line)

        # 完成
        if finished:
            self._poll_timer.stop()
            self._close_btn.setEnabled(True)
            if success:
                self._stage_label.setText("下载完成，即将启动...")
                self._progress.set_progress(1, 1)  # 100%
                # 0.8s 后自动关闭，让用户看到完成状态
                QTimer.singleShot(800, self.accept)
            else:
                self._stage_label.setStyleSheet("font-size: 12px; color: #F65A5A;")

    def _on_close(self) -> None:
        """用户手动关闭。"""
        self.accept()

    @property
    def success(self) -> bool:
        return self._success
