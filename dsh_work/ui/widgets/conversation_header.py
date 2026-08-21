"""对话列头部（对齐 DSH Web 版 Session Header）。

Web 版对话列顶部是一个 session header：左侧会话标题，
右侧视图 tabs。本组件承载：
- 会话标题（ConversationTitle）
- 视图切换按钮：对话 / 用量（HeaderBtn，选中态用主题 accent 下划线）

新建会话入口统一在左侧栏（Sidebar 顶部「＋ 新建会话」），此处不重复。

信号：
- usage_requested(): 点击「用量」视图
- conversation_requested(): 点击「对话」视图
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ...utils.logger import get_logger

log = get_logger("ui.conversation_header")


class ConversationHeader(QWidget):
    """对话列头部：会话标题 + 视图切换。"""

    usage_requested = Signal()
    conversation_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ConversationHeader")
        self.setFixedHeight(48)
        self._setup_ui()
        self._apply_theme()

        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self._apply_theme)
        except Exception:
            pass

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(8)

        # 会话标题
        self._title_label = QLabel("新会话")
        self._title_label.setObjectName("ConversationTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._title_label, stretch=1)

        # 视图切换按钮组（对话 / 用量）
        self._view_btns: dict[str, QPushButton] = {}
        self._view_conversation = self._make_view_btn("对话", "conversation")
        self._view_usage = self._make_view_btn("用量", "usage")
        layout.addWidget(self._view_conversation)
        layout.addWidget(self._view_usage)

        self.set_active_view("conversation")

    def _make_view_btn(self, text: str, key: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("HeaderBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setCheckable(True)
        btn.clicked.connect(lambda checked=False, k=key: self._on_view_clicked(k))
        self._view_btns[key] = btn
        return btn

    def _on_view_clicked(self, key: str) -> None:
        self.set_active_view(key)
        if key == "usage":
            self.usage_requested.emit()
        else:
            self.conversation_requested.emit()

    def set_active_view(self, key: str) -> None:
        """高亮当前视图按钮（选中态 accent 色）。"""
        for k, btn in self._view_btns.items():
            btn.setChecked(k == key)

    def set_title(self, title: str) -> None:
        """更新会话标题。"""
        if not title:
            title = "新会话"
        self._title_label.setText(title)
        self._title_label.setToolTip(title)

    def _apply_theme(self, theme=None) -> None:
        """主题刷新：选中按钮加 accent 色文字。"""
        try:
            from ..theme.theme_manager import ThemeManager
            tm = ThemeManager()
            t = theme or tm.current
            accent = t.colors.accent if t else "#679EFE"
            active = t.colors.bg_active if t else "rgba(255,255,255,0.14)"
            muted = t.colors.text_muted if t else "#81858C"
        except Exception:
            accent = "#679EFE"
            active = "rgba(255,255,255,0.14)"
            muted = "#81858C"
        for k, btn in self._view_btns.items():
            btn.setStyleSheet(
                "QPushButton {"
                "  background-color: transparent;"
                "  border: none;"
                "  border-radius: 6px;"
                "  padding: 5px 12px;"
                "  font-size: 13px;"
                f"  color: {muted if not btn.isChecked() else accent};"
                "}"
                "QPushButton:hover {"
                f"  background-color: {active};"
                "}"
                f"QPushButton:checked {{ color: {accent}; font-weight: 600; }}"
            )
