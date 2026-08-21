"""Web 版高级交互条（对齐 dsh-client-ui-conversation 的 dock 体系）。

三个组件挂在 composer 输入条上方（Web 版 composer stack）：
- TodoDock：计划条（todo/write 数据源，折叠，状态计数）
- QueueDock：排队消息条（agent/inbox/spliced 数据源）
- ApprovalPanel：审批条（approval/asked 数据源，composer 接管式琥珀警示条）

配色全部走主题 token（复用 ThinkRow/ToolRow/StatsLabel QSS 样式）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...utils.logger import get_logger

log = get_logger("ui.advanced_docks")


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


def _palette() -> dict:
    tc = _theme_colors()
    if tc:
        return {
            "text": tc.text_primary,
            "text2": tc.text_secondary,
            "muted": tc.text_muted,
            "accent": tc.accent,
            "success": tc.success,
            "warning": tc.warning,
            "error": tc.error,
            "card": tc.bg_secondary,
            "border": tc.divider,
            "warn_bg": tc.state_dot_pending if hasattr(tc, "state_dot_pending") else tc.warning,
        }
    return {
        "text": "#F9FAFB", "text2": "#CFD3D6", "muted": "#ADB2B8",
        "accent": "#679EFE", "success": "#22C55E", "warning": "#F59E0B",
        "error": "#F25A5A", "card": "#1B1B1C", "border": "rgba(255,255,255,0.06)",
        "warn_bg": "#F59E0B",
    }


def _status_cn(status: str) -> str:
    return {
        "in_progress": "进行中",
        "completed": "完成",
        "pending": "待办",
        "cancelled": "已取消",
    }.get(status, status)


class TodoDock(QWidget):
    """计划条（Web 版 TodoDock）：折叠标题 + 状态计数，展开显示计划列表。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatsDock")
        self._todos: list[dict] = []
        self._expanded = False
        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(4)

        self._header_btn = QPushButton("📋 计划")
        self._header_btn.setObjectName("SidebarBtn")
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.clicked.connect(self._toggle)
        layout.addWidget(self._header_btn)

        self._list_label = QLabel()
        self._list_label.setObjectName("TurnTailMeta")
        self._list_label.setWordWrap(True)
        self._list_label.setVisible(False)
        layout.addWidget(self._list_label)

        self.setVisible(False)

    def apply_theme(self, theme=None) -> None:
        p = _palette()
        self._header_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {p['muted']}; font-size: 12px; text-align: left;"
            " border-radius: 6px; padding: 3px 8px; }"
            "QPushButton:hover { background-color: " + p["card"] + "; }"
        )

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._list_label.setVisible(self._expanded)
        if self._expanded:
            self._render_list()

    def set_todos(self, todos: list[dict]) -> None:
        """更新计划（todo/write 或 projections.todos）。"""
        self._todos = [t for t in (todos or []) if isinstance(t, dict)]
        if not self._todos:
            self.setVisible(False)
            return
        counts: dict[str, int] = {}
        for t in self._todos:
            s = t.get("status", "pending")
            counts[s] = counts.get(s, 0) + 1
        parts = []
        for s in ("in_progress", "completed", "pending"):
            if counts.get(s):
                parts.append(f"{counts[s]} {_status_cn(s)}")
        self._header_btn.setText(f"📋 计划 · {' · '.join(parts)}")
        self.setVisible(True)
        if self._expanded:
            self._render_list()

    def _render_list(self) -> None:
        lines = []
        for i, t in enumerate(self._todos, 1):
            status = t.get("status", "pending")
            marker = {"in_progress": "●", "completed": "✓", "pending": "○", "cancelled": "✕"}.get(status, "○")
            lines.append(f"{marker} {t.get('content', '')}")
        self._list_label.setText("\n".join(lines))


class QueueDock(QWidget):
    """排队消息条（Web 版 QueueDock）：Agent 运行时的追加消息队列。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatsDock")
        self._entries: list[dict] = []
        self._expanded = False
        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(4)

        self._header_btn = QPushButton("⏳ 队列")
        self._header_btn.setObjectName("SidebarBtn")
        self._header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_btn.clicked.connect(self._toggle)
        layout.addWidget(self._header_btn)

        self._list_label = QLabel()
        self._list_label.setObjectName("TurnTailMeta")
        self._list_label.setWordWrap(True)
        self._list_label.setVisible(False)
        layout.addWidget(self._list_label)

        self.setVisible(False)

    def apply_theme(self, theme=None) -> None:
        p = _palette()
        self._header_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            f" color: {p['accent']}; font-size: 12px; text-align: left;"
            " border-radius: 6px; padding: 3px 8px; }"
            "QPushButton:hover { background-color: " + p["card"] + "; }"
        )

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._list_label.setVisible(self._expanded)
        if self._expanded:
            self._render_list()

    def set_queue(self, entries: list[dict]) -> None:
        """更新排队消息（agent/inbox/spliced 的 inserted 内容）。"""
        self._entries = [e for e in (entries or []) if isinstance(e, dict)]
        if not self._entries:
            self.setVisible(False)
            return
        n = len(self._entries)
        self._header_btn.setText(f"⏳ {n} 条排队消息")
        self.setVisible(True)
        if self._expanded:
            self._render_list()

    def _render_list(self) -> None:
        lines = []
        for i, e in enumerate(self._entries, 1):
            text = ""
            content = e.get("content") or e.get("inserted")
            if isinstance(content, list):
                texts = []
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        texts.append(b["text"])
                text = "\n".join(texts)
            elif isinstance(content, str):
                text = content
            lines.append(f"{i}. {text[:120]}")
        self._list_label.setText("\n".join(lines))


class ApprovalPanel(QFrame):
    """审批条（Web 版 ApprovalPanel）：composer 接管式琥珀警示条。

    数据源：approval/asked（id/toolName/callId/reason）。
    响应通道：当前 DSH (0.0.1) 无审批 RPC，按方案降级——
    -「在 Web 版批准」打开 http://127.0.0.1:3080（Web 版可处理审批）
    -「拒绝」：尝试 session.cancel 中断当前工具执行（尽力而为）
    """

    allow_once_clicked = Signal(dict)   # 审批数据
    deny_clicked = Signal(dict)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ApprovalPanel")
        self._approval: dict | None = None
        self._setup_ui()
        self.apply_theme()
        self.setVisible(False)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(4)

        self._title = QLabel("⚠ 等待审批")
        self._title.setObjectName("ThinkTitle")
        layout.addWidget(self._title)

        self._detail = QLabel()
        self._detail.setObjectName("TurnTailMeta")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

        btns = QHBoxLayout()
        btns.addStretch()
        self._deny_btn = QPushButton("拒绝")
        self._deny_btn.setObjectName("HeaderBtn")
        self._deny_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._deny_btn.clicked.connect(lambda: self._emit_action("deny"))
        btns.addWidget(self._deny_btn)
        self._web_btn = QPushButton("在 Web 版中批准")
        self._web_btn.setObjectName("HeaderBtn")
        self._web_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._web_btn.clicked.connect(self._open_web)
        btns.addWidget(self._web_btn)
        layout.addLayout(btns)

    def apply_theme(self, theme=None) -> None:
        p = _palette()
        self.setStyleSheet(
            f"QFrame#ApprovalPanel {{ background-color: {p['warn_bg']}22;"
            f" border: 1px solid {p['warning']}; border-radius: 8px; }}"
            f"QLabel {{ color: {p['text']}; }}"
        )

    def show_approval(self, approval: dict) -> None:
        """显示审批请求（approval/asked 数据）。"""
        self._approval = approval or {}
        tool = self._approval.get("toolName") or self._approval.get("tool") or ""
        reason = self._approval.get("reason") or self._approval.get("message") or ""
        self._title.setText(f"⚠ 等待审批：{tool}" if tool else "⚠ 等待审批")
        self._detail.setText(reason if reason else f"Agent 请求执行 {tool or '操作'}")
        self.setVisible(True)

    def hide_approval(self) -> None:
        self._approval = None
        self.setVisible(False)

    def _emit_action(self, action: str) -> None:
        if self._approval is None:
            return
        data = dict(self._approval)
        if action == "allow":
            self.allow_once_clicked.emit(data)
        else:
            self.deny_clicked.emit(data)
        self.hide_approval()

    def _open_web(self) -> None:
        """降级通道：打开 Web 版处理审批。"""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from ..constants import DSH_BASE_URL
        try:
            QDesktopServices.openUrl(QUrl(DSH_BASE_URL))
        except Exception as e:
            log.warning("打开 Web 版失败: %s", e)
        # 视为已转交 Web 处理，本地收起
        self.hide_approval()
