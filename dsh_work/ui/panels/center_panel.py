"""中栏：对话/工作区主区域（第 3.3 节）。

由三部分组成：消息流、输入框、工具调用内联展示。
两种模式共享的核心区域，差异在于信息密度和呈现方式。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget, QLabel

from ...api import MessageRecord
from ...core.session_manager import AgentStatus, ContextUsage
from ..widgets.message_list import MessageList
from ..widgets.input_box import InputBox
from ..widgets.empty_state_cards import EmptyStateCards
from ..widgets.tool_call_card import ToolCallAggregator, ToolCallCard
from ... import constants as C


class CenterPanel(QWidget):
    """中栏：对话区域 + 输入框。

    空状态时显示快捷入口卡片，有消息时显示消息流。
    """

    send_requested = Signal(str)
    stop_requested = Signal()
    files_dropped = Signal(list)
    card_clicked = Signal(str, str)  # prompt, mode
    scrolled_to_top = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CenterPanel")
        self._setup_ui()
        self._show_empty_state()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 堆叠：空状态 / 消息流
        self._stack = QStackedWidget()

        # 空状态快捷卡片
        self._empty_state = EmptyStateCards()
        self._empty_state.card_clicked.connect(self.card_clicked)
        self._empty_index = self._stack.addWidget(self._empty_state)

        # 消息流
        self._message_list = MessageList()
        self._message_list.scrolled_to_top.connect(self.scrolled_to_top)
        self._messages_index = self._stack.addWidget(self._message_list)

        layout.addWidget(self._stack, stretch=1)

        # 输入框
        self._input_box = InputBox()
        self._input_box.send_requested.connect(self.send_requested)
        self._input_box.stop_requested.connect(self.stop_requested)
        self._input_box.files_dropped.connect(self.files_dropped)
        layout.addWidget(self._input_box)

        # 工具调用聚合器
        self._tool_aggregator = ToolCallAggregator()
        self._tool_aggregator.set_create_callback(self._insert_tool_card)

    def _show_empty_state(self) -> None:
        self._stack.setCurrentIndex(self._empty_index)

    def _show_messages(self) -> None:
        self._stack.setCurrentIndex(self._messages_index)

    def _insert_tool_card(self, card: ToolCallCard) -> None:
        """将工具调用卡片插入消息流。"""
        self._show_messages()
        # 插入到消息流末尾（stretch 之前）
        layout = self._message_list._layout
        layout.insertWidget(layout.count() - 1, card)

    # ===== 消息管理 =====

    def add_message(self, message: MessageRecord) -> None:
        self._show_messages()
        self._message_list.add_message(message)

    def start_streaming(self) -> None:
        self._show_messages()
        self._message_list.start_streaming()

    def append_chunk(self, chunk: str) -> None:
        self._message_list.append_chunk(chunk)

    def finish_streaming(self) -> None:
        self._message_list.finish_streaming()

    def is_streaming(self) -> bool:
        """当前是否处于流式输出中（用于事件路由判断）。"""
        return self._message_list._current_streaming_bubble is not None

    def clear_messages(self) -> None:
        self._message_list.clear()
        self._show_empty_state()

    def load_history(self, messages: list) -> None:
        """批量加载历史消息（切换会话时使用，清空后一次性渲染）。"""
        self._message_list.clear()
        self._show_messages()
        for msg in messages:
            self._message_list.add_message(msg)

    def show_hint(self, title: str, body: str = "") -> None:
        """在空状态顶部插入一条提示条（离线草稿/降级信息等）。

        若当前不是空状态，则切换到空状态（保证提示可见）。
        提示条以一次性卡片形式插入到空状态 widget 顶部；不持久化到 _message_list。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont
        from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel

        existing: QFrame | None = getattr(self._empty_state, "_banner", None)
        if existing is not None:
            try:
                existing.setParent(None)
                existing.deleteLater()
            except Exception:
                pass
        banner = QFrame(self._empty_state)
        banner.setStyleSheet(
            "QFrame {"
            "  background-color: rgba(255, 180, 84, 0.08);"
            "  border: 1px solid rgba(255, 180, 84, 0.25);"
            "  border-radius: 10px;"
            "}"
        )
        bl = QVBoxLayout(banner)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(4)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet("color: #FFB454; font-size: 13px; font-weight: 600;")
        title_lbl.setWordWrap(True)
        bl.addWidget(title_lbl)
        if body:
            body_lbl = QLabel(body)
            body_lbl.setStyleSheet("color: #B3B6BF; font-size: 12px;")
            body_lbl.setWordWrap(True)
            bl.addWidget(body_lbl)
        banner.setMinimumHeight(60)
        # 插入到 EmptyStateCards 的主 layout 顶部
        el: QVBoxLayout = self._empty_state.layout()
        if el is not None:
            el.insertWidget(0, banner, alignment=Qt.AlignmentFlag.AlignTop)
        self._empty_state._banner = banner
        self._show_empty_state()

    # ===== 工具调用 =====

    def on_tool_call(self, tool_name: str, params: dict) -> None:
        self._tool_aggregator.on_tool_call(tool_name, params)

    def on_tool_result(self, tool_name: str, status: str, result=None, error: str = "") -> None:
        self._tool_aggregator.on_tool_result(tool_name, status, result, error)

    # ===== 输入框 =====

    def set_agent_status(self, status: AgentStatus) -> None:
        self._input_box.set_agent_status(status)

    def set_mode(self, mode: str) -> None:
        self._input_box.set_mode(mode)

    def set_context_usage(self, context: ContextUsage) -> None:
        self._input_box.set_context_usage(context)

    def fill_prompt(self, text: str) -> None:
        self._input_box.fill_prompt(text)

    def trigger_send(self) -> None:
        """外部触发发送（Ctrl+Return 快捷键）。"""
        self._input_box.trigger_send()

    @property
    def message_list(self) -> MessageList:
        return self._message_list
