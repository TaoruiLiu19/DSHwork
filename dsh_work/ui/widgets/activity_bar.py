"""ActivityBar：TRAE Work 风格窄侧边栏（48px 宽，文字标签）。

文字式垂直导航条，固定在最左侧：
- 中部：导航文字按钮（会话、文件、搜索、Git）
- 底部：设置、主题切换

模式切换（Work/Code）已移至顶栏滑块。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QPushButton,
    QFrame,
)
from PySide6.QtGui import QFont


def _theme_colors():
    try:
        from ..theme.theme_manager import ThemeManager
        tm = ThemeManager()
        theme = tm.current
        if theme and theme.colors:
            return theme.colors
    except Exception:
        pass
    return None


_FALLBACK = {
    "bg_secondary": "#0D0D0E",
    "divider": "#B5BDC5",
    "bg_hover": "#B5BDC5",
    "text_primary": "#D1D3DB",
    "text_muted": "#666B75",
}


def _palette() -> dict:
    tc = _theme_colors()
    if tc:
        return {
            "bg_secondary": tc.bg_secondary or _FALLBACK["bg_secondary"],
            "divider": tc.divider or _FALLBACK["divider"],
            "bg_hover": tc.bg_hover or _FALLBACK["bg_hover"],
            "text_primary": tc.text_primary or _FALLBACK["text_primary"],
            "text_muted": tc.text_muted or _FALLBACK["text_muted"],
        }
    return dict(_FALLBACK)


class ActivityButton(QPushButton):
    """ActivityBar 文字按钮。"""

    def __init__(self, text: str, tooltip: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFixedHeight(32)
        self.setMinimumWidth(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        font = QFont("Microsoft YaHei", 8, QFont.Weight.DemiBold)
        self.setFont(font)
        self.setCheckable(True)
        self.setObjectName("ActivityButton")
        self._update_style(False)

    def _update_style(self, active: bool) -> None:
        p = _palette()
        if active:
            self.setStyleSheet(
                "QPushButton#ActivityButton {"
                "  background-color: rgba(120, 119, 198, 0.15);"
                "  border: none;"
                "  border-radius: 6px;"
                f"  color: {p['text_primary']};"
                "  font-weight: 600;"
                "  padding: 4px 6px;"
                "}"
                "QPushButton#ActivityButton:hover {"
                "  background-color: rgba(120, 119, 198, 0.25);"
                "}"
            )
        else:
            self.setStyleSheet(
                "QPushButton#ActivityButton {"
                "  background-color: transparent;"
                "  border: none;"
                "  border-radius: 6px;"
                f"  color: {p['text_muted']};"
                "  padding: 4px 6px;"
                "}"
                "QPushButton#ActivityButton:hover {"
                f"  background-color: {p['bg_hover']};"
                f"  color: {p['text_primary']};"
                "}"
            )

    def set_active(self, active: bool) -> None:
        self.setChecked(active)
        self._update_style(active)


class ActivitySeparator(QFrame):
    """ActivityBar 分隔线。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(1)
        self.setFixedWidth(28)
        p = _palette()
        self.setStyleSheet(f"background-color: {p['divider']};")


class ActivityBar(QWidget):
    """TRAE Work 风格窄侧边栏（文字标签版）。

    信号：
        nav_changed(str): 导航面板切换 ("sessions" / "files" / "search" / "git")
        settings_clicked(): 打开设置
        theme_clicked(): 切换主题
    """

    nav_changed = Signal(str)
    settings_clicked = Signal()
    theme_clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ActivityBar")
        self.setFixedWidth(48)
        self._current_nav = "sessions"
        self._setup_ui()
        self.apply_theme()

        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self.apply_theme)
        except Exception:
            pass

    def apply_theme(self, theme=None) -> None:
        """刷新样式表为当前主题配色。"""
        p = _palette()
        self.setStyleSheet(
            "QWidget#ActivityBar {"
            f"  background-color: {p['bg_secondary']};"
            f"  border-right: 1px solid {p['divider']};"
            "}"
        )
        for btn_id in ["_sessions_btn", "_files_btn", "_search_btn", "_git_btn", "_theme_btn", "_settings_btn"]:
            btn = getattr(self, btn_id, None)
            if btn is not None:
                btn._update_style(btn.isChecked())

    def _setup_ui(self) -> None:
        self.apply_theme()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # ===== 导航按钮 =====
        self._sessions_btn = ActivityButton("会话", "会话列表")
        self._sessions_btn.clicked.connect(lambda: self._set_nav("sessions"))
        layout.addWidget(self._sessions_btn)

        self._files_btn = ActivityButton("文件", "文件树")
        self._files_btn.clicked.connect(lambda: self._set_nav("files"))
        layout.addWidget(self._files_btn)

        self._search_btn = ActivityButton("搜索", "搜索")
        self._search_btn.clicked.connect(lambda: self._set_nav("search"))
        layout.addWidget(self._search_btn)

        layout.addWidget(ActivitySeparator())

        self._git_btn = ActivityButton("Git", "Git 面板")
        self._git_btn.clicked.connect(lambda: self._set_nav("git"))
        layout.addWidget(self._git_btn)

        # 弹性空间
        layout.addStretch()

        # ===== 底部 =====
        self._theme_btn = ActivityButton("主题", "切换主题")
        self._theme_btn.setCheckable(False)
        self._theme_btn.clicked.connect(self.theme_clicked)
        layout.addWidget(self._theme_btn)

        self._settings_btn = ActivityButton("设置", "设置")
        self._settings_btn.setCheckable(False)
        self._settings_btn.clicked.connect(self.settings_clicked)
        layout.addWidget(self._settings_btn)

        # 初始化激活状态
        self._update_nav_buttons()

    def _set_nav(self, nav: str) -> None:
        self._current_nav = nav
        self._update_nav_buttons()
        self.nav_changed.emit(nav)

    def _update_nav_buttons(self) -> None:
        for nav_id, btn in [
            ("sessions", self._sessions_btn),
            ("files", self._files_btn),
            ("search", self._search_btn),
            ("git", self._git_btn),
        ]:
            btn.set_active(nav_id == self._current_nav)

    # ===== 外部接口 =====

    @property
    def current_nav(self) -> str:
        return self._current_nav

    def set_nav(self, nav: str) -> None:
        """外部设置当前导航。"""
        self._current_nav = nav
        self._update_nav_buttons()
