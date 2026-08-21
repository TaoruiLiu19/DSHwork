"""中栏：对话/工作区主区域（第 3.3 节）。

参考 DSH Desktop 官方设计：
- 消息流占据主要区域，工具调用卡片内联嵌入
- 余额小部件在消息流和输入框之间
- 输入框固定在底部

布局结构（简化）：
┌──────────────────────────────────────┐
│  ┌────────────────────────────────┐  │
│  │      消息流（MessageList）      │  │
│  │  - 用户消息（右对齐，主题色）   │  │
│  │  - Assistant 消息（左对齐）     │  │
│  │  - 工具调用卡片（内联折叠）     │  │
│  │  - 空状态快捷入口卡片           │  │
│  └────────────────────────────────┘  │
│  ┌────────────────────────────────┐  │
│  │      余额内联小部件            │  │
│  └────────────────────────────────┘  │
│  ┌────────────────────────────────┐  │
│  │      输入框（InputBox）         │  │
│  │  - 模式切换（工作/代码）        │  │
│  │  - 多行文本输入 + 发送按钮      │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from ...api import MessageRecord
from ...api.balance_client import BalanceResult
from ...core.session_manager import AgentStatus, ContextUsage
from ..widgets.advanced_docks import ApprovalPanel, QueueDock, TodoDock
from ..widgets.balance_widget import BalanceWidget
from ..widgets.conversation_header import ConversationHeader
from ..widgets.empty_state_cards import EmptyStateCards
from ..widgets.input_box import InputBox
from ..widgets.message_list import MessageList
from ..widgets.stats_dock import StatsDock


class CenterPanel(QWidget):
    """中栏：Web 版对话列（ConversationHeader + 消息流 + 余额 + 输入条）。

    布局（对齐 Web 版 Session Header / ChatView / Composer 三明治结构）：
    ┌──────────────────────────────────────┐
    │ ConversationHeader（会话标题+视图）   │
    ├──────────────────────────────────────┤
    │ 消息流 / 空状态（ChatView）           │
    │ 余额内联小部件                        │
    │ 输入条（Composer）                    │
    └──────────────────────────────────────┘
    """

    send_requested = Signal(str)
    stop_requested = Signal()
    files_dropped = Signal(list)
    mode_changed = Signal(str)  # 工作/代码模式切换
    card_clicked = Signal(str, str)  # prompt, mode
    scrolled_to_top = Signal()
    balance_refresh_requested = Signal()  # 用户点击余额小部件触发强制刷新
    usage_requested = Signal()  # 点击「用量」视图
    conversation_requested = Signal()  # 点击「对话」视图

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CenterPanel")
        self._setup_ui()
        self._show_empty_state()

    def _setup_ui(self) -> None:
        """初始化 UI 组件。

        布局：Header → QStackedWidget（空状态/消息流）→ 余额小部件 → 输入框
        """
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ===== 对话列头部（会话标题 + 视图切换；新建入口在左侧栏） =====
        self._header = ConversationHeader()
        self._header.usage_requested.connect(self.usage_requested)
        self._header.conversation_requested.connect(self.conversation_requested)
        layout.addWidget(self._header)

        # ===== 堆叠：空状态 / 消息流 =====
        self._stack = QStackedWidget()

        # 空状态快捷卡片
        self._empty_state = EmptyStateCards()
        self._empty_state.card_clicked.connect(self.card_clicked)
        self._empty_index = self._stack.addWidget(self._empty_state)

        # 消息流（已集成工具调用聚合器管理）
        self._message_list = MessageList()
        self._message_list.scrolled_to_top.connect(self.scrolled_to_top)
        self._messages_index = self._stack.addWidget(self._message_list)

        layout.addWidget(self._stack, stretch=1)

        # ===== 余额内联小部件（消息列表与输入框之间） =====
        self._balance_widget = BalanceWidget()
        self._balance_widget.refresh_requested.connect(self.balance_refresh_requested)
        layout.addWidget(self._balance_widget)

        # ===== StatsDock 统计条（Web 版 sessionStats 行，位于 composer 上方） =====
        self._stats_dock = StatsDock()
        layout.addWidget(self._stats_dock)

        # ===== Web 版高级交互条（TodoDock / QueueDock / ApprovalPanel） =====
        self._todo_dock = TodoDock()
        layout.addWidget(self._todo_dock)
        self._queue_dock = QueueDock()
        layout.addWidget(self._queue_dock)
        self._approval_panel = ApprovalPanel()
        self._approval_panel.deny_clicked.connect(self._on_approval_deny)
        layout.addWidget(self._approval_panel)

        # ===== 输入框 =====
        self._input_box = InputBox()
        self._input_box.send_requested.connect(self.send_requested)
        self._input_box.stop_requested.connect(self.stop_requested)
        self._input_box.files_dropped.connect(self.files_dropped)
        self._input_box.mode_changed.connect(self.mode_changed)
        layout.addWidget(self._input_box)

    def _show_empty_state(self) -> None:
        self._stack.setCurrentIndex(self._empty_index)

    def _show_messages(self) -> None:
        self._stack.setCurrentIndex(self._messages_index)

    # ===== 消息管理 =====

    def add_message(self, message: MessageRecord) -> None:
        self._show_messages()
        self._message_list.add_message(message)

    def start_streaming(self) -> None:
        self._show_messages()
        self._message_list.start_streaming()

    def append_chunk(self, chunk: str) -> None:
        self._message_list.append_chunk(chunk)

    def finish_streaming(self):
        """结束流式输出，返回固化后的行（供 turn_end 补齐权威内容用）。"""
        return self._message_list.finish_streaming()

    def is_streaming(self) -> bool:
        """当前是否处于流式输出中（用于事件路由判断）。"""
        return self._message_list._current_streaming_row is not None

    def set_streaming_hint(self, text: str | None) -> None:
        """设置当前流式行的状态提示（思考中…/工具执行中…）。"""
        row = self._message_list._current_streaming_row
        if row is not None:
            row.set_status_hint(text)

    def set_last_assistant_meta(self, text: str | None) -> None:
        """给最后一条 assistant 行设置 turn tail 统计（Ran for Xs · tokens）。"""
        for row in reversed(self._message_list._rows):
            if not row._is_user:
                row.set_meta(text)
                return

    # ===== Web 版高级交互（Think 行 / Context 行）=====

    def start_think(self) -> None:
        """开始思考行（turn_start 时调用）。"""
        self._message_list.start_think()

    def update_think(self, delta: str) -> None:
        """追加思考增量（reasoning-delta 事件）。"""
        self._message_list.update_think(delta)

    def finish_think(self, reasoning: str | None = None) -> None:
        """结束思考行（turn_end 时调用）。"""
        self._message_list.finish_think(reasoning)

    def add_context_row(self, label: str, content: str = "") -> None:
        """添加上下文注入行。"""
        self._message_list.add_context_row(label, content)

    def set_session_stats(self, projections: dict | None) -> None:
        """更新 StatsDock（session.list 的 projections.values）。"""
        if projections:
            self._stats_dock.set_projections(projections)
        else:
            self._stats_dock.clear()

    # ===== Web 版高级交互（Todo / Queue / Approval）=====

    def set_todos(self, todos: list[dict] | None) -> None:
        """更新计划条（todo/write 或 projections.todos）。"""
        self._todo_dock.set_todos(todos or [])

    def set_queue(self, entries: list[dict] | None) -> None:
        """更新排队消息条（agent/inbox/spliced）。"""
        self._queue_dock.set_queue(entries or [])

    def show_approval(self, approval: dict | None) -> None:
        """显示审批条（approval/asked）。approval=None 隐藏。"""
        if approval:
            self._approval_panel.show_approval(approval)
        else:
            self._approval_panel.hide_approval()

    def hide_approval(self) -> None:
        """隐藏审批条（approval/decided）。"""
        self._approval_panel.hide_approval()

    def _on_approval_deny(self, approval: dict) -> None:
        """拒绝审批：中断当前 Agent 执行（尽力而为）。"""
        self.stop_requested.emit()

    def clear_messages(self) -> None:
        self._message_list.clear()
        self._show_empty_state()

    def load_history(self, messages: list) -> None:
        """批量加载历史消息（切换会话时使用，清空后一次性渲染）。"""
        self._show_messages()
        self._message_list.load_messages_batch(messages)

    def show_hint(self, title: str, body: str = "") -> None:
        """在空状态顶部插入一条提示条（离线草稿/降级信息等）。

        若当前不是空状态，则切换到空状态（保证提示可见）。
        提示条以一次性卡片形式插入到空状态 widget 顶部；不持久化到 _message_list。
        """
        from PySide6.QtWidgets import QFrame, QVBoxLayout

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

    # ===== 工具调用（由内部 MessageList 管理） =====

    def on_tool_call(self, tool_name: str, params: dict) -> None:
        """工具调用事件：由 MessageList 内部管理。"""
        self._show_messages()
        self._message_list.on_tool_call(tool_name, params)

    def on_tool_result(self, tool_name: str, status: str, result=None, error: str = "") -> None:
        """工具结果事件：由 MessageList 内部管理。"""
        self._message_list.on_tool_result(tool_name, status, result, error)

    # ===== 输入框 =====

    def set_agent_status(self, status: AgentStatus) -> None:
        self._input_box.set_agent_status(status)

    def set_mode(self, mode: str) -> None:
        self._input_box.set_mode(mode)

    def set_model_label(self, model: str) -> None:
        """更新输入条模型座（Web 版 model seat）。"""
        self._input_box.set_model_label(model)

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

    # ===== 余额内联小部件 =====

    def set_turn_cost(self, turn_cost: float, session_total: float = 0.0) -> None:
        """更新本轮消耗与会话累计消耗。

        在 TURN_END 事件后由 main_window 调用。
        """
        self._balance_widget.set_turn_cost(turn_cost, session_total)

    def set_balance(self, result: BalanceResult) -> None:
        """更新账户余额显示。

        在余额查询回调后由 main_window 调用。
        """
        self._balance_widget.set_balance(result)

    def set_balance_loading(self) -> None:
        """将余额显示切为「查询中…」状态。"""
        self._balance_widget.set_balance_loading()

    @property
    def balance_widget(self) -> BalanceWidget:
        return self._balance_widget

    # ===== Web 版对话列头部 =====

    def set_session_title(self, title: str) -> None:
        """更新对话列头部会话标题。"""
        self._header.set_title(title)

    def set_header_active_view(self, key: str) -> None:
        """外部切换头部视图高亮（main_window 切换中央堆叠时调用）。"""
        self._header.set_active_view(key)

    @property
    def header(self) -> ConversationHeader:
        return self._header
