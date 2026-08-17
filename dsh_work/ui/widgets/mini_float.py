"""迷你浮窗（第 3.7 / 9.3 节）。

始终置顶的磨砂玻璃小窗，用于在窗口最小化到托盘时速览
Agent 运行进度（会话标题 · 状态 · 当前步骤描述）。

特性：
- 无边框 + 置顶 + 工具窗口，不抢占焦点
- 高斯模糊圆角卡片质感（磨砂玻璃近似）
- 可鼠标拖动，记忆上次位置（持久化到 UserConfig）
- 跟随主题：通过 ThemeManager 单例监听，主题切换自动刷新配色
- 双击浮窗恢复主窗口（通过 restore_requested 信号）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ...config import UserConfig
from ...core.session_manager import AgentStatus
from ...utils.logger import get_logger

log = get_logger("ui.mini_float")

# 浮窗默认尺寸
_FLOAT_WIDTH = 260
_FLOAT_HEIGHT = 72
# 默认停靠到屏幕右下角时的边距
_EDGE_MARGIN = 24


class MiniFloatWindow(QWidget):
    """始终置顶的磨砂玻璃迷你浮窗。"""

    restore_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_FLOAT_WIDTH, _FLOAT_HEIGHT)

        # 状态缓存
        self._agent_status = AgentStatus.IDLE
        self._session_title = ""
        self._step_text = ""
        self._theme = None
        self._drag_offset: QPoint | None = None

        self._build_ui()
        self._load_theme()
        self._apply_theme()
        self._restore_position()

        # 订阅主题切换（ThemeManager 为单例）
        self._wire_theme_listener()

    # ===== UI 构建 =====

    def _build_ui(self) -> None:
        self._card = QFrame(self)
        self._card.setObjectName("MiniFloatCard")
        self._card.setGeometry(0, 0, _FLOAT_WIDTH, _FLOAT_HEIGHT)

        # 阴影（磨砂玻璃感）
        shadow = QGraphicsDropShadowEffect(self._card)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 90))
        self._card.setGraphicsEffect(shadow)

        outer = QVBoxLayout(self._card)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(4)

        self._title_lbl = QLabel("DSH Work")
        self._title_lbl.setObjectName("FloatTitle")
        self._title_lbl.setStyleSheet("font-size: 12px; font-weight: 600;")

        self._status_lbl = QLabel("空闲")
        self._status_lbl.setObjectName("FloatStatus")
        self._status_lbl.setStyleSheet("font-size: 10px;")

        self._step_lbl = QLabel("")
        self._step_lbl.setObjectName("FloatStep")
        self._step_lbl.setWordWrap(True)
        self._step_lbl.hide()

        outer.addWidget(self._title_lbl)
        outer.addWidget(self._status_lbl)
        outer.addWidget(self._step_lbl)

    # ===== 主题 =====

    def _wire_theme_listener(self) -> None:
        try:
            from ..theme.theme_manager import ThemeManager
            tm = ThemeManager()
            tm.add_listener(self._on_theme_changed)
        except Exception as e:
            log.warning("订阅主题监听失败: %s", e)

    def _on_theme_changed(self, theme) -> None:
        self._theme = theme
        self._apply_theme()

    def _load_theme(self) -> None:
        try:
            from ..theme.theme_manager import ThemeManager
            tm = ThemeManager()
            if tm.current is None:
                tm.load_all()
            self._theme = tm.current
        except Exception as e:
            log.warning("加载主题失败: %s", e)

    def _apply_theme(self) -> None:
        t = self._theme
        if t is None:
            return
        c = t.colors
        # 依据主题深浅决定卡片底色（磨砂半透明）
        bg = QColor(c.bg_card)
        if t.type == "dark":
            bg.setAlpha(215)
        else:
            bg.setAlpha(225)
        self._card.setStyleSheet(
            f"#MiniFloatCard {{"
            f" background-color: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
            f" border: 1px solid {c.border_light};"
            f" border-radius: 12px; }}"
            f"#FloatTitle {{ color: {c.text_primary}; }}"
            f"#FloatStatus {{ color: {self._status_color()}; }}"
            f"#FloatStep {{ color: {c.text_secondary}; font-size: 10px; }}"
        )

    def _status_color(self) -> str:
        t = self._theme
        c = t.colors if t else None
        if self._agent_status in (AgentStatus.RUNNING, AgentStatus.THINKING, AgentStatus.TOOL_EXECUTING):
            return c.accent if c else "#32F08C"
        if self._agent_status == AgentStatus.ERROR:
            return c.error if c else "#F65A5A"
        return c.text_secondary if c else "#9599A6"

    # ===== 状态更新 =====

    def set_agent_status(
        self, status: AgentStatus, session_title: str = "", step_text: str = ""
    ) -> None:
        """更新浮窗展示内容。"""
        self._agent_status = status
        self._session_title = session_title
        self._step_text = step_text

        self._title_lbl.setText(session_title or "DSH Work")
        self._status_lbl.setText(self._status_text(status))
        self._status_lbl.setStyleSheet(
            f"#FloatStatus {{ color: {self._status_color()}; font-size: 10px; }}"
        )
        if status == AgentStatus.RUNNING and step_text:
            self._step_lbl.setText(step_text)
            self._step_lbl.show()
        else:
            self._step_lbl.clear()
            self._step_lbl.hide()

    @staticmethod
    def _status_text(status: AgentStatus) -> str:
        mapping = {
            AgentStatus.IDLE: "空闲",
            AgentStatus.RUNNING: "运行中",
            AgentStatus.THINKING: "推理中",
            AgentStatus.TOOL_EXECUTING: "执行工具",
            AgentStatus.ERROR: "出错",
        }
        return mapping.get(status, status.value)

    # ===== 拖动 =====

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._persist_position()
            self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.restore_requested.emit()
        event.accept()

    # ===== 位置持久化 =====

    def _restore_position(self) -> None:
        try:
            cfg = UserConfig.load()
            pos = getattr(cfg, "mini_float_pos", None)
            if pos is None:
                raise ValueError("no saved position")
            # 兼容 dict（asdict 序列化后加载）与对象两种形态
            x = pos.get("x") if isinstance(pos, dict) else getattr(pos, "x", None)
            y = pos.get("y") if isinstance(pos, dict) else getattr(pos, "y", None)
            if x is not None and y is not None:
                self.move(int(x), int(y))
                return
        except Exception as e:
            log.debug("读取浮窗位置失败: %s", e)
        self._default_position()

    def _default_position(self) -> None:
        """默认停靠到可用屏幕右下角。"""
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = geo.right() - _FLOAT_WIDTH - _EDGE_MARGIN
        y = geo.bottom() - _FLOAT_HEIGHT - _EDGE_MARGIN
        self.move(x, y)

    def _persist_position(self) -> None:
        try:
            cfg = UserConfig.load()
            cfg.mini_float_pos = {"x": self.x(), "y": self.y()}
            cfg.save()
        except Exception as e:
            log.debug("保存浮窗位置失败: %s", e)

    def closeEvent(self, event) -> None:
        # 关闭浮窗时解除主题监听，避免悬挂引用
        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().remove_listener(self._on_theme_changed)
        except Exception:
            pass
        super().closeEvent(event)