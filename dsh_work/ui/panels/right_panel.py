"""右栏 Details 面板（对齐 DSH Web 版 Details 行为 + 桌面折叠控制）。

设计（兼顾网页版语义与桌面可用性）：
- 折叠态：一条窄图标栏 rail（48px），含「◀ 展开」按钮与功能图标——
  用户随时可以点开，且不占主空间（对齐网页版 details=0 的"不占空间"，
  但保留桌面端明确的展开入口）
- 展开态：完整内容区（Work 预览 tabs / Code 工具时间线 + DiffView），
  头部含「✕ 收纳」按钮（对齐网页版 closeDetails）
- preview_file（用户主动预览）→ 自动展开
- add_tool_call / show_diff（工具事件）→ 只更新内容，不强制展开
  （对齐网页版：工具到达时不强行打开已关闭的 Details——否则会话运行中
   用户刚收起就被强制展开）
- 切换会话 → 自动折叠（对齐网页版 closeDetails）

信号：
- close_requested(): 用户点击「✕ 收纳」时发出（外层负责折叠 + 保存配置）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ... import constants as C
from ..widgets.inline_preview import InlinePreview

# 折叠窄条宽度
_RAIL_WIDTH = 48


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
    "text_primary": "#D1D3DB",
    "text_muted": "#666B75",
    "bg_hover": "#B5BDC5",
    "bg_active": "#B5BDC5",
    "accent": "#679EFE",
}


def _palette() -> dict:
    tc = _theme_colors()
    if tc:
        return {
            "text_primary": tc.text_primary or _FALLBACK["text_primary"],
            "text_muted": tc.text_muted or _FALLBACK["text_muted"],
            "bg_hover": tc.bg_hover or _FALLBACK["bg_hover"],
            "bg_active": tc.bg_active or _FALLBACK["bg_active"],
            "accent": tc.accent or _FALLBACK["accent"],
        }
    return dict(_FALLBACK)


class ToolCallTimeline(QListWidget):
    """Code 模式工具调用详情时间线。"""

    tool_call_clicked = Signal(int)  # index

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int = 0) -> None:
        status_icon = {"running": "🔵", "success": "🟢", "failed": "🔴"}.get(status, "⚪")
        text = f"{status_icon} {tool_name} · {status}"
        if duration_ms:
            text += f" · {duration_ms}ms"
        self.addItem(QListWidgetItem(text))


class DiffView(QListWidget):
    """Code 模式 DiffView。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

    def show_diff(self, diff_text: str) -> None:
        """显示 diff 文本。"""
        self.clear()
        for line in diff_text.splitlines():
            from PySide6.QtGui import QBrush, QColor
            item = QListWidgetItem(line)
            if line.startswith("+") and not line.startswith("+++"):
                item.setForeground(QBrush(QColor("#33C192")))
            elif line.startswith("-") and not line.startswith("---"):
                item.setForeground(QBrush(QColor("#F65A5A")))
            elif line.startswith("@@"):
                item.setForeground(QBrush(QColor("#387BFF")))
            self.addItem(item)


class PreviewTabs(QTabWidget):
    """Work 模式成果预览面板（多标签页）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setTabsClosable(True)
        self.tabCloseRequested.connect(self._on_close_tab)
        self.setAutoFillBackground(False)

    def preview_file(self, file_path: str) -> None:
        """在标签页中预览文件。"""
        from pathlib import Path
        path = Path(file_path)
        for i in range(self.count()):
            if self.tabText(i) == path.name:
                self.setCurrentIndex(i)
                return
        preview = InlinePreview()
        preview.preview_file(file_path)
        self.addTab(preview, path.name)
        self.setCurrentIndex(self.count() - 1)

    def _on_close_tab(self, index: int) -> None:
        self.removeTab(index)


class RightPanel(QWidget):
    """右栏 Details：折叠窄条（含展开按钮）↔ 展开内容（含收纳按钮）。"""

    close_requested = Signal()

    # 窄条图标：key -> (emoji, tooltip)
    _RAIL_ICONS = [
        ("preview", "📄", "成果预览"),
        ("tools", "🛠️", "工具调用"),
    ]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("RightPanel")
        self._mode = C.MODE_WORK
        self._collapsed = True  # 显式折叠状态（isVisible 依赖父窗口，不可靠）
        self._setup_ui()
        self._apply_theme()

        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self._on_theme_changed)
        except Exception:
            pass

    # ===== UI 构建 =====

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 折叠态：窄图标栏 ----
        self._rail = QWidget()
        self._rail.setObjectName("DetailsRail")
        rail_layout = QVBoxLayout(self._rail)
        rail_layout.setContentsMargins(4, 8, 4, 8)
        rail_layout.setSpacing(6)
        rail_layout.addStretch()

        self._rail_btns: dict[str, QPushButton] = {}
        for key, icon, tip in self._RAIL_ICONS:
            btn = QPushButton(icon)
            btn.setObjectName("RailBtn")
            btn.setFixedSize(36, 36)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._on_rail_clicked(k))
            self._rail_btns[key] = btn
            rail_layout.addWidget(btn)

        rail_layout.addStretch()
        # 底部：展开按钮（◀ 对齐 openDetails）
        self._expand_btn = QPushButton("◀")
        self._expand_btn.setObjectName("RailBtn")
        self._expand_btn.setFixedSize(36, 36)
        self._expand_btn.setToolTip("展开详情面板")
        self._expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._expand_btn.clicked.connect(self.expand)
        rail_layout.addWidget(self._expand_btn)
        layout.addWidget(self._rail)

        # ---- 展开态：内容区 ----
        self._content = QWidget()
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # 头部行：标题 + 收纳按钮（✕ 对齐 closeDetails）
        header_row = QHBoxLayout()
        header_row.setContentsMargins(12, 6, 8, 2)
        header_row.setSpacing(8)
        self._content_title = QLabel("详情")
        self._content_title.setObjectName("DetailsTitle")
        header_row.addWidget(self._content_title, stretch=1)
        self._collapse_btn = QPushButton("✕")
        self._collapse_btn.setObjectName("DetailsCloseBtn")
        self._collapse_btn.setFixedSize(28, 28)
        self._collapse_btn.setToolTip("收纳详情面板 (Ctrl+J)")
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        header_row.addWidget(self._collapse_btn)
        content_layout.addLayout(header_row)

        self._stack = QStackedWidget()

        # Work 模式：成果预览
        work_page = QWidget()
        work_page.setObjectName("RightWorkPage")
        work_layout = QVBoxLayout(work_page)
        work_layout.setContentsMargins(8, 4, 8, 8)
        self._preview_tabs = PreviewTabs()
        work_layout.addWidget(self._preview_tabs, stretch=1)
        self._work_index = self._stack.addWidget(work_page)

        # Code 模式：工具调用详情 + DiffView
        code_page = QWidget()
        code_page.setObjectName("RightCodePage")
        code_layout = QVBoxLayout(code_page)
        code_layout.setContentsMargins(8, 4, 8, 8)
        code_layout.addWidget(QLabel("工具调用详情"))
        self._tool_timeline = ToolCallTimeline()
        code_layout.addWidget(self._tool_timeline, stretch=1)
        code_layout.addWidget(QLabel("文件变更 (Diff)"))
        self._diff_view = DiffView()
        code_layout.addWidget(self._diff_view, stretch=1)
        self._code_index = self._stack.addWidget(code_page)

        content_layout.addWidget(self._stack, stretch=1)
        layout.addWidget(self._content)

        # 初始：折叠态（rail 可见，固定 48px）
        self._rail.setVisible(True)
        self._content.setVisible(False)
        self.setMinimumWidth(_RAIL_WIDTH)
        self.setMaximumWidth(_RAIL_WIDTH)

    # ===== 折叠 / 展开 =====

    def is_collapsed(self) -> bool:
        return self._collapsed

    def collapse(self) -> None:
        """折叠回窄条（对齐网页版 closeDetails；保留 rail 展开入口）。"""
        if not self._collapsed:
            self._collapsed = True
            self._rail.setVisible(True)
            self._content.setVisible(False)
            self.setMinimumWidth(_RAIL_WIDTH)
            self.setMaximumWidth(_RAIL_WIDTH)
            self._apply_theme()

    def expand(self) -> None:
        """展开详情面板（对齐网页版 openDetails；释放宽度给 splitter 调宽）。"""
        if self._collapsed:
            self._collapsed = False
            self._rail.setVisible(False)
            self._content.setVisible(True)
            self.setMinimumWidth(280)
            self.setMaximumWidth(16777215)
            self._update_content_title()
            self._apply_theme()

    def toggle(self) -> None:
        if self._collapsed:
            self.expand()
        else:
            self.collapse()

    def _on_collapse_clicked(self) -> None:
        """用户点击 ✕ 收纳：折叠并通知外层（外层保存配置）。"""
        self.collapse()
        self.close_requested.emit()

    def _on_rail_clicked(self, key: str) -> None:
        """点击窄条图标：展开并切到对应内容页。"""
        if key == "tools":
            self._stack.setCurrentIndex(self._code_index)
        else:
            self._stack.setCurrentIndex(self._work_index)
        self.expand()

    def _update_content_title(self) -> None:
        if not self._collapsed:
            idx = self._stack.currentIndex()
            if idx == self._code_index:
                self._content_title.setText("工具调用详情")
            else:
                self._content_title.setText("成果预览")

    # ===== 模式 / 内容 =====

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        if mode == C.MODE_CODE:
            self._stack.setCurrentIndex(self._code_index)
        else:
            self._stack.setCurrentIndex(self._work_index)
        self._update_content_title()

    def preview_file(self, file_path: str) -> None:
        """用户主动预览文件：切到预览页并展开面板。"""
        self._stack.setCurrentIndex(self._work_index)
        self._preview_tabs.preview_file(file_path)
        self._update_content_title()
        self.expand()

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int = 0) -> None:
        """工具调用事件：更新时间线，但不强制展开。

        对齐网页版：面板已展开时更新内容；已折叠时保持折叠——
        否则会话运行中工具频繁触发，用户刚收起就被强制展开。
        """
        self._stack.setCurrentIndex(self._code_index)
        self._tool_timeline.add_tool_call(tool_name, status, duration_ms)
        self._update_content_title()

    def show_diff(self, diff_text: str) -> None:
        """diff 事件：更新 DiffView，但不强制展开（同 add_tool_call）。"""
        self._stack.setCurrentIndex(self._code_index)
        self._diff_view.show_diff(diff_text)
        self._update_content_title()

    # ===== 主题 =====

    def _on_theme_changed(self, theme=None) -> None:
        self._apply_theme()

    def _apply_theme(self, theme=None) -> None:
        try:
            from ..theme.theme_manager import ThemeManager
            tm = ThemeManager()
            t = theme or tm.current
            accent = t.colors.accent if t else "#679EFE"
            bg_hover = t.colors.bg_hover if t else "rgba(255,255,255,0.08)"
            bg_active = t.colors.bg_active if t else "rgba(255,255,255,0.14)"
            text_secondary = t.colors.text_secondary if t else "#CFD3D6"
            text_primary = t.colors.text_primary if t else "#F9FAFB"
            border_color = t.colors.border if t else "rgba(255,255,255,0.12)"
        except Exception:
            accent = "#679EFE"
            bg_hover = "rgba(255,255,255,0.08)"
            bg_active = "rgba(255,255,255,0.14)"
            text_secondary = "#CFD3D6"
            text_primary = "#F9FAFB"
            border_color = "rgba(255,255,255,0.12)"

        # 面板级 QSS（RailBtn / 标题）
        self.setStyleSheet(
            f"QPushButton#RailBtn {{"
            "  background-color: transparent;"
            "  border: none;"
            "  border-radius: 8px;"
            f"  color: {text_secondary};"
            "  font-size: 16px;"
            "}"
            f"QPushButton#RailBtn:hover {{ background-color: {bg_hover}; color: {text_primary}; }}"
            f"QPushButton#RailBtn:checked {{ background-color: {bg_active}; color: {accent}; }}"
            f"QLabel#DetailsTitle {{ color: {text_primary}; font-size: 13px; font-weight: 600; }}"
        )

        # 收纳按钮用内联样式（优先级最高，避免被全局 QSS 的 QPushButton 规则覆盖）
        if getattr(self, "_collapse_btn", None) is not None:
            self._collapse_btn.setStyleSheet(
                f"QPushButton {{"
                f"  background-color: {bg_hover};"
                f"  border: 1px solid {border_color};"
                "  border-radius: 6px;"
                f"  color: {text_secondary};"
                "  font-size: 14px;"
                "  font-weight: 600;"
                "}"
                f"QPushButton:hover {{ background-color: {bg_active}; color: {accent}; }}"
            )
