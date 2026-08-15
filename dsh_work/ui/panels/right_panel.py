"""右栏：工具/预览区（第 3.5 节）。

内容随模式变化：
- Work 模式：成果预览面板（多标签页，同时预览多个文件）
- Code 模式：工具调用详情（时间线）+ DiffView（文件变更差异对比）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QStackedWidget,
    QListWidget,
    QListWidgetItem,
)

from ... import constants as C
from ..widgets.inline_preview import InlinePreview


class ToolCallTimeline(QListWidget):
    """Code 模式工具调用详情时间线。

    以时间线形式展示当前会话的所有工具调用记录，
    包含参数、返回值、耗时。点击任意记录可跳转到对话流对应位置。
    """

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
    """Code 模式 DiffView。

    Agent 修改文件时，自动展示文件变更差异对比：
    - 新增行绿色
    - 删除行红色
    支持逐文件查看和批量 git diff。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

    def show_diff(self, diff_text: str) -> None:
        """显示 diff 文本。"""
        self.clear()
        for line in diff_text.splitlines():
            from PySide6.QtGui import QColor, QBrush
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
        # 检查是否已打开
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
    """右栏：随模式切换内容。

    Work 模式 → 成果预览面板
    Code 模式 → 工具调用详情 + DiffView

    信号：
        close_requested(): 请求关闭面板（纯净界面模式）
    """

    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("RightPanel")
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部关闭按钮行（纯净界面模式）
        close_row = QHBoxLayout()
        close_row.setContentsMargins(4, 4, 4, 0)
        close_row.addStretch()
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("关闭面板 (Ctrl+J)")
        close_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: transparent;"
            "  border: none;"
            "  border-radius: 4px;"
            "  color: #666B75;"
            "  font-size: 14px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(224, 226, 242, 0.1);"
            "  color: #D1D3DB;"
            "}"
        )
        close_btn.clicked.connect(self.close_requested)
        close_row.addWidget(close_btn)
        close_widget = QWidget()
        close_widget.setLayout(close_row)
        layout.addWidget(close_widget)

        self._stack = QStackedWidget()

        # Work 模式：成果预览
        work_page = QWidget()
        work_page.setObjectName("RightWorkPage")
        work_layout = QVBoxLayout(work_page)
        work_layout.setContentsMargins(8, 4, 8, 8)
        work_layout.addWidget(QLabel("成果预览"))
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

        layout.addWidget(self._stack)

    def set_mode(self, mode: str) -> None:
        if mode == C.MODE_CODE:
            self._stack.setCurrentIndex(self._code_index)
        else:
            self._stack.setCurrentIndex(self._work_index)

    def preview_file(self, file_path: str) -> None:
        self._preview_tabs.preview_file(file_path)

    def add_tool_call(self, tool_name: str, status: str, duration_ms: int = 0) -> None:
        self._tool_timeline.add_tool_call(tool_name, status, duration_ms)

    def show_diff(self, diff_text: str) -> None:
        self._diff_view.show_diff(diff_text)
