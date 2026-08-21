"""系统托盘集成（第 3.7 节）。

关闭窗口时默认最小化到系统托盘而非退出。
托盘图标提供右键菜单：新建会话、切换到最近会话、切换 Work/Code 模式、暂停/恢复 DSH、退出。
托盘图标颜色反映连接状态（绿色=已连接，灰色=未连接）。
Agent 完成长任务时，托盘图标闪烁并弹出系统通知。

最小化任务进度追踪（第 9.3 节）三层可见性：
- tooltip：实时更新为"会话标题 · Step N · 步骤描述"
- 图标状态色 + 任务栏进度叠加（ITaskbarList3）
- 迷你浮窗（可切换，默认关）：始终置顶的磨砂玻璃小窗
- 步骤切换系统通知（默认关）
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.session_manager import AgentStatus
from ..utils.logger import get_logger

log = get_logger("ui.system_tray")


class SystemTray(QObject):
    """系统托盘管理。"""

    new_session_requested = Signal()
    toggle_mode_requested = Signal()
    pause_dsh_requested = Signal()
    resume_dsh_requested = Signal()
    quit_requested = Signal()
    restore_requested = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        self._connected = False
        self._agent_status = AgentStatus.IDLE
        self._step_text = ""
        self._session_title = ""
        self._mini_float_enabled = False
        self._mini_float = None

    def setup(self) -> None:
        """初始化系统托盘。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self._tray = QSystemTrayIcon(self._create_icon(connected=False), parent=self.parent())
        self._tray.setToolTip("DSH Work")
        self._tray.activated.connect(self._on_activated)
        self._build_menu()
        self._tray.show()

    def _build_menu(self) -> None:
        menu = QMenu()

        action_new = QAction("新建会话", menu)
        action_new.triggered.connect(self.new_session_requested)
        menu.addAction(action_new)

        menu.addSeparator()

        action_mode = QAction("切换 Work/Code 模式", menu)
        action_mode.triggered.connect(self.toggle_mode_requested)
        menu.addAction(action_mode)

        menu.addSeparator()

        self._action_pause = QAction("暂停 DSH", menu)
        self._action_pause.triggered.connect(self.pause_dsh_requested)
        menu.addAction(self._action_pause)

        self._action_resume = QAction("恢复 DSH", menu)
        self._action_resume.triggered.connect(self.resume_dsh_requested)
        self._action_resume.setVisible(False)
        menu.addAction(self._action_resume)

        menu.addSeparator()

        action_quit = QAction("退出", menu)
        action_quit.triggered.connect(self.quit_requested)
        menu.addAction(action_quit)

        if self._tray:
            self._tray.setContextMenu(menu)

    def _on_activated(self, reason) -> None:
        """托盘图标激活事件。"""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.restore_requested.emit()

    def _create_icon(self, connected: bool, running: bool = False) -> QIcon:
        """生成托盘图标（颜色反映连接状态）。"""
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 连接状态色（TRAE token）
        if running:
            color = QColor("#387BFF")  # 工作态蓝色
        elif connected:
            color = QColor("#33C192")  # 已连接绿色
        else:
            color = QColor("#666B75")  # 未连接灰色
        painter.setBrush(color)
        painter.setPen(QColor("#1A1B1D"))
        painter.drawEllipse(2, 2, 12, 12)
        painter.end()
        return QIcon(pixmap)

    def set_connection_status(self, connected: bool) -> None:
        """更新连接状态。"""
        self._connected = connected
        if self._tray:
            self._tray.setIcon(self._create_icon(connected, self._agent_status != AgentStatus.IDLE))

    def set_mini_float_enabled(self, enabled: bool) -> None:
        """启用/停用迷你浮窗（随 config.mini_float_window 切换）。"""
        self._mini_float_enabled = bool(enabled)
        try:
            if enabled:
                if self._mini_float is None:
                    from .widgets.mini_float import MiniFloatWindow
                    self._mini_float = MiniFloatWindow()
                    self._mini_float.restore_requested.connect(self.restore_requested)
                self._mini_float.set_agent_status(
                    self._agent_status, self._session_title, self._step_text
                )
                self._mini_float.show()
            elif self._mini_float is not None:
                self._mini_float.hide()
        except Exception as e:
            log.warning("迷你浮窗切换失败: %s", e)

    def set_agent_status(self, status: AgentStatus, session_title: str = "", step_text: str = "") -> None:
        """更新 Agent 状态（影响图标颜色与 tooltip）。"""
        self._agent_status = status
        self._session_title = session_title
        self._step_text = step_text
        if self._tray:
            self._tray.setIcon(self._create_icon(self._connected, status != AgentStatus.IDLE))
            # tooltip 实时更新为"会话标题 · Step N · 步骤描述"
            parts = []
            if session_title:
                parts.append(session_title)
            if status == AgentStatus.RUNNING and step_text:
                parts.append(step_text)
            elif status != AgentStatus.IDLE:
                parts.append(status.value)
            tooltip = " · ".join(parts) if parts else "DSH Work"
            self._tray.setToolTip(tooltip[:128])  # tooltip 长度限制
        # 同步迷你浮窗
        if self._mini_float and self._mini_float_enabled:
            self._mini_float.set_agent_status(status, session_title, step_text)

    def notify(self, title: str, message: str) -> None:
        """弹出系统通知（Agent 完成长任务时调用）。"""
        if self._tray:
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def show(self) -> None:
        if self._tray:
            self._tray.show()

    def hide(self) -> None:
        if self._tray:
            self._tray.hide()
        if self._mini_float is not None:
            try:
                self._mini_float.hide()
            except Exception as e:
                log.warning("隐藏迷你浮窗失败: %s", e)
