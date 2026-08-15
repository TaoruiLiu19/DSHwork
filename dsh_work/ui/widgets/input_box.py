"""输入框（第 3.3 节）。

固定在对话区域底部，多行文本输入：
- Shift+Enter 换行，Enter 发送
- Agent 运行时输入框变为"追加消息"模式——输入内容通过 session.updateQueue 排队
  此时发送按钮变为"停止"按钮，点击调用 session.cancel

输入框上方可展开附件栏，支持拖拽文件到输入框（文件路径自动插入到消息文本中）。

上下文容量感知（第 9.2 节）：
- 输入框上方有可展开的细进度条，按比例填充
- 颜色编码：< 70% 蓝，70-90% 橙，> 90% 红
- ≥ 80% 输入框下方浅色提示
- ≥ 95% 发送按钮变为警示态，首次点击弹出确认
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QColor, QPalette
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QLabel,
    QFrame,
)

from ...core.session_manager import ContextUsage, AgentStatus
from ... import constants as C


class InputBox(QWidget):
    """消息输入框。

    信号：
        send_requested(str): 用户请求发送消息
        stop_requested: 用户请求停止 Agent
        files_dropped(list[str]): 文件拖拽
    """

    send_requested = Signal(str)
    stop_requested = Signal()
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._agent_status = AgentStatus.IDLE
        self._context: ContextUsage | None = None
        self._placeholder_work = "描述你想完成的工作..."
        self._placeholder_code = "输入指令或粘贴代码..."
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 上下文容量进度条（可展开，默认隐藏）
        self._context_bar = QProgressBar()
        self._context_bar.setFixedHeight(4)
        self._context_bar.setRange(0, 100)
        self._context_bar.setVisible(False)
        layout.addWidget(self._context_bar)

        # 上下文容量文本
        self._context_label = QLabel()
        self._context_label.setObjectName("Muted")
        self._context_label.setStyleSheet("font-size: 11px;")
        self._context_label.setVisible(False)
        layout.addWidget(self._context_label)

        # 阈值提示
        self._warn_label = QLabel()
        self._warn_label.setStyleSheet("color: #D27E24; font-size: 11px;")
        self._warn_label.setVisible(False)
        layout.addWidget(self._warn_label)

        # 输入区 + 发送按钮
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText(self._placeholder_work)
        self._text_edit.setAcceptDrops(True)
        self._text_edit.setFixedHeight(80)
        input_row.addWidget(self._text_edit, stretch=1)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("Primary")
        self._send_btn.setFixedSize(64, 80)
        self._send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self._send_btn)

        layout.addLayout(input_row)

        # 启用拖拽
        self.setAcceptDrops(True)

    def _on_send(self) -> None:
        """发送或停止。"""
        if self._agent_status == AgentStatus.RUNNING:
            self.stop_requested.emit()
            return

        # 上下文容量 ≥ 95% 警示态
        if self._context and self._context.is_danger:
            # 首次点击弹出确认（简化：直接在按钮文本提示）
            if self._send_btn.text() == "发送":
                self._send_btn.setText("确认发送?")
                return
            self._send_btn.setText("发送")

        text = self._text_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self._text_edit.clear()

    def set_agent_status(self, status: AgentStatus) -> None:
        """设置 Agent 状态，切换发送/停止按钮。"""
        self._agent_status = status
        if status == AgentStatus.RUNNING or status == AgentStatus.THINKING or status == AgentStatus.TOOL_EXECUTING:
            self._send_btn.setText("停止")
            self._send_btn.setStyleSheet("background-color: #F65A5A; color: #FFFFFF; border: none; border-radius: 6px;")
        else:
            self._send_btn.setText("发送")
            self._send_btn.setStyleSheet("")
        self._text_edit.setPlaceholderText(
            "追加消息到队列..." if status == AgentStatus.RUNNING else self._current_placeholder
        )

    _current_placeholder = "描述你想完成的工作..."

    def set_mode(self, mode: str) -> None:
        """根据模式设置占位符。"""
        if mode == C.MODE_CODE:
            self._current_placeholder = self._placeholder_code
        else:
            self._current_placeholder = self._placeholder_work
        if self._agent_status == AgentStatus.IDLE:
            self._text_edit.setPlaceholderText(self._current_placeholder)

    def set_context_usage(self, context: ContextUsage) -> None:
        """更新上下文容量显示。"""
        self._context = context
        pct = context.percentage
        self._context_bar.setValue(pct)
        self._context_bar.setVisible(True)
        self._context_label.setText(
            f"上下文 {context.used_tokens // 1000}k / {context.limit_tokens // 1000}k ({pct}%)"
        )
        self._context_label.setVisible(True)

        # 颜色编码
        color_key = context.color_key
        color_map = {
            "accent": "#32F08C",
            "warning": "#D27E24",
            "error": "#F65A5A",
        }
        color = color_map.get(color_key, "#32F08C")
        self._context_bar.setStyleSheet(
            f"QProgressBar {{ background-color: rgba(224, 226, 242, 0.1); border: none; border-radius: 2px; }}"
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 2px; }}"
        )

        # 阈值提示
        if context.is_warning:
            self._warn_label.setText("上下文即将用尽，可 Fork 或新建会话")
            self._warn_label.setVisible(True)
        else:
            self._warn_label.setVisible(False)

    def keyPressEvent(self, event) -> None:
        """Enter 发送，Shift+Enter 换行。"""
        if event.key() == Qt.Key.Key_Return and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._on_send()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        """拖拽文件：文件路径自动插入到消息文本中。"""
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            # 将文件路径插入到文本中
            current = self._text_edit.toPlainText()
            for p in paths:
                if current:
                    current += "\n"
                current += p
            self._text_edit.setPlainText(current)

    def fill_prompt(self, text: str) -> None:
        """填充提示词到输入框（空状态卡片点击时调用）。"""
        self._text_edit.setPlainText(text)
        self._text_edit.setFocus()

    def trigger_send(self) -> None:
        """外部触发发送（如 Ctrl+Return 全局快捷键）。"""
        self._on_send()
