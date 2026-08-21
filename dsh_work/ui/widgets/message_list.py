"""消息流（对齐 DSH Web 版 ChatView）。

Web 版对话流是全宽消息行（非气泡）：
- 用户消息：右对齐（最大 ~85% 宽），无气泡背景，头部「你 · 时间」
- Assistant 消息：左对齐全宽，Markdown 完整渲染（代码块/表格/行内码）
- 完成后的 assistant 消息 hover 显示操作（复制）
- 工具调用以折叠卡片形式内联嵌入（ToolRow，后续对齐 Web 折叠样式）
- 支持流式渲染——assistant 回复逐块显示（WebSocket chunk 事件）

消息内容用 MarkdownTextEdit 渲染（md_to_html → QTextEdit），
配色全部来自主题 token，与 Web 版 markdown 渲染一致。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...api import MessageRecord
from ...utils.logger import get_logger
from .markdown_view import MarkdownTextEdit
from .tool_call_card import ToolCallAggregator, ToolCallCard

log = get_logger("ui.message_list")

# 用户消息最大宽度 = 视口宽度 * 比例
_USER_MAX_WIDTH_RATIO = 0.85
# 时间戳格式
_TIMESTAMP_FORMAT = "%H:%M"


class CollapsibleRow(QFrame):
    """可折叠行基类（Web 版 DisclosureRow 语义）：头部点击切换展开/收起。"""

    def __init__(self, title: str, icon: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._expanded = False
        self._setup_ui(title, icon)

    def _setup_ui(self, title: str, icon: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)
        if icon:
            self._icon_label = QLabel(icon)
            self._icon_label.setObjectName("Caption")
            header.addWidget(self._icon_label)
        self._title_label = QLabel(title)
        self._title_label.setObjectName("ThinkTitle")
        header.addWidget(self._title_label, stretch=1)
        self._toggle_btn = QPushButton("▶")
        self._toggle_btn.setObjectName("MsgActionBtn")
        self._toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle_btn.setFixedWidth(24)
        self._toggle_btn.clicked.connect(self.toggle)
        header.addWidget(self._toggle_btn)
        layout.addLayout(header)

        self._body = MarkdownTextEdit()
        self._body.setVisible(False)
        layout.addWidget(self._body)

    def set_title(self, title: str) -> None:
        self._title_label.setText(title)

    def set_body_text(self, text: str) -> None:
        self._body.set_markdown(text)

    def set_body_visible(self, visible: bool) -> None:
        self._body.setVisible(visible)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._toggle_btn.setText("▼" if self._expanded else "▶")
        self._body.setVisible(self._expanded)
        self._on_toggled()

    def _on_toggled(self) -> None:
        pass


class ThinkRow(CollapsibleRow):
    """思考行（Web 版 ThinkRow）：默认折叠，实时摘要尾随，展开显示完整推理。

    - 流式中：标题「思考中…」，摘要实时更新（首行）
    - TURN_END 后：标题「已思考」，保持折叠
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__("思考中…", icon="🧠", parent=parent)
        self.setObjectName("ThinkRow")
        self._reasoning = ""

    def append_reasoning(self, delta: str) -> None:
        """追加思考文本，更新摘要与全文。"""
        self._reasoning += delta
        self._update_display()

    def set_reasoning(self, text: str) -> None:
        self._reasoning = text or ""
        self._update_display()

    def _update_display(self) -> None:
        summary = self._summary_line()
        if not self._expanded:
            self._title_label.setText(f"思考中… {summary}")
        self.set_body_text(self._reasoning)

    def _summary_line(self) -> str:
        """摘要：首个非空行，截断 60 字。"""
        for line in self._reasoning.splitlines():
            line = line.strip()
            if line:
                return line[:60] + ("…" if len(line) > 60 else "")
        return self._reasoning[:60]

    def finalize(self) -> None:
        """TURN_END：标记为已思考并收拢。"""
        summary = self._summary_line()
        self.set_title(f"已思考 {summary}" if summary else "已思考")
        self.set_body_text(self._reasoning)
        if not self._expanded:
            self._toggle_btn.setText("▶")


class ContextRow(CollapsibleRow):
    """上下文注入行（Web 版 DisclosureRow）：折叠展示注入/召回的上下文来源。"""

    def __init__(self, label: str, content: str = "", parent: QWidget | None = None):
        super().__init__(label, icon="📥", parent=parent)
        self.setObjectName("ContextRow")
        if content:
            self.set_body_text(content)


class MessageRow(QFrame):
    """Web 版消息行（全宽，无气泡背景）。

    用户消息右对齐（窄行），Assistant 消息左对齐全宽。
    """

    def __init__(self, role: str, content: str, timestamp: float = 0.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.role = role
        self.content = content
        self.timestamp = timestamp
        self._is_user = role == "user"
        self._max_width_override: int | None = None
        # 已渲染内容长度（流式增量用；构造时 set_markdown 已渲染全部）
        self._rendered_len = len(content)

        self._setup_ui()

    def set_max_width_viewport(self, viewport_width: int) -> None:
        """根据聊天区 viewport 宽度设置消息宽度。

        消息气泡与对话栏同宽（用户消息也全宽），不做 85% 窄化；
        用户消息通过内部右对齐体现"在右侧"。
        """
        if viewport_width <= 0:
            return
        self.setMaximumWidth(16777215)  # 不限制宽度（全宽）

    def _setup_ui(self) -> None:
        """布局：头部（角色·时间）→ Markdown 内容。"""
        if self._is_user:
            self.setObjectName("MessageRowUser")
        else:
            self.setObjectName("MessageRow")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(4)

        # 头部行：角色名 · 时间 + （assistant 完成时）复制按钮
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        self._header_label = QLabel()
        self._header_label.setObjectName("MessageRole" if not self._is_user else "MessageRoleUser")
        header_text = "你" if self._is_user else "Assistant"
        if self.timestamp > 0:
            from datetime import datetime
            ts_str = datetime.fromtimestamp(self.timestamp).strftime(_TIMESTAMP_FORMAT)
            header_text = f"{header_text} · {ts_str}"
        self._header_label.setText(header_text)

        # 位置：用户消息（我说的）头部靠右，回复头部靠左
        if self._is_user:
            header_row.addStretch()
            header_row.addWidget(self._header_label)
            # 用户消息状态提示跟在角色标签后（右侧）
            self._status_hint = QLabel()
            self._status_hint.setObjectName("Caption")
            self._status_hint.setVisible(False)
            header_row.addWidget(self._status_hint)
        else:
            header_row.addWidget(self._header_label)
            # 状态提示（思考中…/工具执行中…，Web 版 ThinkRow 头部）
            self._status_hint = QLabel()
            self._status_hint.setObjectName("Caption")
            self._status_hint.setVisible(False)
            header_row.addWidget(self._status_hint)
            header_row.addStretch()

        # hover 复制按钮（仅 assistant，且内容非空）
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setObjectName("MsgActionBtn")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.setVisible(False)
        self._copy_btn.clicked.connect(self._copy_content)
        header_row.addWidget(self._copy_btn)

        layout.addLayout(header_row)

        # Markdown 内容（自适应高度；用户消息内容右对齐）
        self._content_view = MarkdownTextEdit()
        self._content_view.set_markdown(self.content)
        if self._is_user:
            self._content_view.set_align_right(True)
        self._content_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._content_view)

        # turn tail 统计行（Ran for Xs · tokens 等，Web 版 assistant footer）
        self._meta_label = QLabel()
        self._meta_label.setObjectName("TurnTailMeta")
        self._meta_label.setVisible(False)
        layout.addWidget(self._meta_label)

        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )

    def set_status_hint(self, text: str | None) -> None:
        """设置状态提示（思考中…等）。text=None 隐藏。"""
        if text:
            self._status_hint.setText(text)
            self._status_hint.setVisible(True)
        else:
            self._status_hint.setVisible(False)

    def set_meta(self, text: str | None) -> None:
        """设置 turn tail 统计行（Ran for Xs · TTFT · tokens）。"""
        if text:
            self._meta_label.setText(text)
            self._meta_label.setVisible(True)
        else:
            self._meta_label.setVisible(False)

    def _copy_content(self) -> None:
        from PySide6.QtWidgets import QApplication as _QA
        _QA.clipboard().setText(self.content)
        self._copy_btn.setText("已复制")
        QTimer.singleShot(1200, lambda: self._copy_btn.setText("复制"))

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        if not self._is_user and self.content:
            self._copy_btn.setVisible(True)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self._copy_btn.setVisible(False)

    def update_content(self, content: str) -> None:
        """完整更新内容（历史加载 / 主题切换等一次性渲染）。"""
        self.content = content
        self._content_view.set_markdown(content)
        self._content_view.updateGeometry()
        self.updateGeometry()
        self.update()

        # 向上传播布局更新
        parent = self.parentWidget()
        while parent is not None:
            ly = parent.layout()
            if ly is not None:
                ly.activate()
                ly.update()
            parent.updateGeometry()
            parent.update()
            parent = parent.parentWidget()

    def append_stream_content(self, content: str) -> None:
        """流式增量：只追加新增文本（纯文本，避免每 chunk 全量重排卡顿）。"""
        self.content = content
        delta = content[self._rendered_len:]
        if delta:
            self._content_view.append_plain(delta)
            self._rendered_len = len(content)
        self.update()

    def finalize_content(self) -> None:
        """流式结束：全量 markdown 排版（恢复粗体/代码块等格式）。"""
        if self._rendered_len == len(self.content) and self._content_view.document().isEmpty() is False:
            self._content_view.set_markdown(self.content)
            self._rendered_len = len(self.content)
        self.update()

        # 发送 LayoutRequest 事件让 QScrollArea 重新计算
        QApplication.postEvent(self, QEvent(QEvent.Type.LayoutRequest))
        top = self
        while top.parentWidget() is not None:
            top = top.parentWidget()
            QApplication.postEvent(top, QEvent(QEvent.Type.LayoutRequest))


class MessageList(QScrollArea):
    """消息流滚动区域（Web 版 ChatView 滚动体）。

    支持流式渲染、背景固定、消息管理、工具调用卡片内联。
    """

    # 信号：用户滚动到顶部（触发追加加载历史）
    scrolled_to_top = Signal()
    # 信号：流式 chunk 到达（用于工具聚合卡更新）
    chunk_appended = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

        # 消息行列表
        self._rows: list[MessageRow] = []
        self._current_streaming_row: MessageRow | None = None
        # 当前思考行（ThinkRow）
        self._current_think_row: ThinkRow | None = None

        # 背景固定渲染
        self._background_pixmap: QPixmap | None = None
        self._background_cache_pixmap: QPixmap | None = None
        self._mask_color = QColor("#000000")
        self._mask_opacity = 0.3

        # 流式输出
        self._stream_pending = False
        self._user_is_scrolling_up = False
        # 历史分批渲染待处理队列
        self._pending_batch: list = []

        # 工具调用聚合器（内联管理工具卡片）
        self._tool_aggregator = ToolCallAggregator()
        self._tool_aggregator.set_create_callback(self._insert_tool_card)

        # 主题刷新：MessageList 是长存对象，统一注册一个监听，
        # 遍历现有行的 MarkdownTextEdit 重渲（单行不注册，避免删除后泄漏）
        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self._refresh_theme)
        except Exception:
            pass

    def _refresh_theme(self, theme=None) -> None:
        """主题切换：刷新所有现存 MarkdownTextEdit（含 ThinkRow/ContextRow 折叠体）。"""
        try:
            for row in self._rows:
                try:
                    if getattr(row, "_content_view", None) is not None:
                        row._content_view.rerender_for_theme(theme)
                except RuntimeError:
                    pass
            for view in self._container.findChildren(MarkdownTextEdit):
                try:
                    view.rerender_for_theme(theme)
                except RuntimeError:
                    pass
        except Exception:
            pass

    def _setup_ui(self) -> None:
        """使用 QScrollArea 包裹 QWidget 容器，消息从顶部排列。"""
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("MessageListContainer")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(24, 16, 24, 16)
        self._layout.setSpacing(8)
        # 顶部与底部 stretch：消息默认从顶部开始
        self._layout.addStretch()
        self.setWidget(self._container)

        # 监听滚动位置
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化：重算用户消息最大宽度 + 重建背景缓存。"""
        super().resizeEvent(event)
        vp_w = self.viewport().width()
        if vp_w > 0:
            for r in self._rows:
                r.set_max_width_viewport(vp_w)
        if self._background_pixmap:
            self._rebuild_background_cache()

    def _viewport_width(self) -> int:
        try:
            w = self.viewport().width()
        except Exception:
            w = 0
        return w if w > 0 else self.width()

    def _apply_row_viewport_width(self, row: MessageRow) -> None:
        vp_w = self._viewport_width()
        if vp_w > 0:
            row.set_max_width_viewport(vp_w)

    def _on_scroll(self, value: int) -> None:
        """滚动事件处理。"""
        bar = self.verticalScrollBar()
        if value <= bar.minimum() + 10:
            self.scrolled_to_top.emit()
        # 判断用户是否在回看历史
        self._user_is_scrolling_up = value < (bar.maximum() - 20)

    def _is_at_bottom(self) -> bool:
        """是否处于底部。"""
        bar = self.verticalScrollBar()
        return bar.value() >= (bar.maximum() - 5)

    # ===== 消息管理 =====

    def add_message(self, message: MessageRecord) -> MessageRow:
        """添加一条消息行。"""
        row = MessageRow(message.role, message.content, timestamp=getattr(message, "timestamp", 0))
        self._apply_row_viewport_width(row)
        self._insert_row(row)
        return row

    def load_messages_batch(self, messages: list[MessageRecord]) -> None:
        """批量加载历史消息（切换会话时使用）。

        分批渲染（每批 25 条）：历史消息多时避免一次性全量创建
        QTextBrowser 导致 UI 冻结（切换会话卡顿）。
        """
        self.clear()
        self._pending_batch = list(messages)
        QTimer.singleShot(0, self._load_batch_chunk)

    def _load_batch_chunk(self) -> None:
        """渲染下一批历史消息。"""
        if not getattr(self, "_pending_batch", None):
            QTimer.singleShot(0, self.scroll_to_bottom)
            return
        batch = self._pending_batch[:25]
        self._pending_batch = self._pending_batch[25:]
        for message in batch:
            row = MessageRow(message.role, message.content, timestamp=getattr(message, "timestamp", 0))
            self._apply_row_viewport_width(row)
            self._insert_row(row)
        self._layout.activate()
        self.viewport().update()
        if self._pending_batch:
            QTimer.singleShot(0, self._load_batch_chunk)
        else:
            QTimer.singleShot(0, self.scroll_to_bottom)

    def _insert_row(self, row: MessageRow) -> None:
        """插入到 stretch 之前。"""
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._rows.append(row)
        self._layout.activate()

    def start_streaming(self) -> MessageRow:
        """开始流式输出，创建临时行。"""
        row = MessageRow("assistant", "", timestamp=time.time())
        self._apply_row_viewport_width(row)
        self._current_streaming_row = row
        self._insert_row(row)
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        return row

    def append_chunk(self, chunk: str) -> None:
        """追加流式 chunk（事件循环末尾合并刷新）。"""
        if not self._current_streaming_row:
            log.debug("append_chunk 但无流式行，丢弃 chunk_len=%d", len(chunk))
            return
        self._current_streaming_row.content += chunk
        self.chunk_appended.emit(chunk)
        if not self._stream_pending:
            self._stream_pending = True
            QTimer.singleShot(0, self._flush_stream)

    def _flush_stream(self) -> None:
        """将累积的内容增量追加到渲染视图（事件循环末尾执行一次）。

        流式期间用纯文本增量插入（append_stream_content），避免每个 chunk
        全量重建 HTML + 重排大文档导致主线程卡顿（"对话不实时"）。
        """
        self._stream_pending = False
        if not self._current_streaming_row:
            return
        self._current_streaming_row.append_stream_content(self._current_streaming_row.content)
        self._layout.activate()
        self.viewport().update()
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)

    def finish_streaming(self) -> MessageRow | None:
        """结束流式输出：全量 markdown 排版并返回固化后的行。"""
        if self._stream_pending:
            self._flush_stream()
        row = self._current_streaming_row
        if row is not None:
            row.finalize_content()
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        self._current_streaming_row = None
        return row

    def clear(self) -> None:
        """清空所有消息（切换会话时调用）。"""
        self._stream_pending = False
        self._current_streaming_row = None
        self._current_think_row = None
        self._pending_batch = []  # 取消未完成的分批渲染
        # 重置工具调用聚合器
        self._tool_aggregator = ToolCallAggregator()
        self._tool_aggregator.set_create_callback(self._insert_tool_card)

        # 移除所有消息 widget（保留最后的 stretch）
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._rows.clear()

    # ===== 工具调用管理（内联嵌入消息流） =====

    def on_tool_call(self, tool_name: str, params: dict) -> None:
        """处理 tool_call 事件，聚合器自动管理卡片创建与插入。"""
        self._tool_aggregator.on_tool_call(tool_name, params)

    def on_tool_result(self, tool_name: str, status: str, result=None, error: str = "") -> None:
        """处理 tool_result 事件，更新对应聚合卡状态。"""
        self._tool_aggregator.on_tool_result(tool_name, status, result, error)

    def _insert_tool_card(self, card: ToolCallCard) -> None:
        """将工具调用卡片插入消息流（stretch 之前）。"""
        self._layout.insertWidget(self._layout.count() - 1, card)
        self._layout.activate()

    # ===== 思考行（ThinkRow）管理 =====

    def start_think(self) -> ThinkRow:
        """开始思考行（turn_start 时调用）。"""
        self._current_think_row = ThinkRow()
        self._insert_row_widget(self._current_think_row)
        return self._current_think_row

    def update_think(self, delta: str) -> None:
        """追加思考增量（reasoning-delta 事件）。"""
        row = self._current_think_row
        if row is not None:
            row.append_reasoning(delta)

    def finish_think(self, reasoning: str | None = None) -> None:
        """结束思考行（turn_end 时调用，用完整 reasoning 回填）。"""
        row = self._current_think_row
        if row is not None:
            if reasoning:
                row.set_reasoning(reasoning)
            row.finalize()
        self._current_think_row = None

    def _insert_row_widget(self, widget: QWidget) -> None:
        """通用插入（stretch 之前）。"""
        self._layout.insertWidget(self._layout.count() - 1, widget)
        self._layout.activate()

    def add_context_row(self, label: str, content: str = "") -> None:
        """添加上下文注入行（Web 版 ContextRow）。"""
        row = ContextRow(label, content)
        self._insert_row_widget(row)

    def scroll_to_bottom(self) -> None:
        """滚动到底部。"""
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ===== 背景固定渲染（主题背景图，Web 主题下无背景图时跳过） =====

    def set_background_image(self, image_path: str, mask_color: str, mask_opacity: float) -> None:
        from pathlib import Path

        path = Path(image_path)
        if not path.exists():
            return
        self._background_pixmap = QPixmap(str(path))
        if self._background_pixmap.isNull():
            self._background_pixmap = None
            return
        self._mask_color = QColor(mask_color)
        self._mask_opacity = mask_opacity
        self._rebuild_background_cache()
        self.viewport().update()

    def clear_background(self) -> None:
        self._background_pixmap = None
        self._background_cache_pixmap = None
        self.viewport().update()

    def _rebuild_background_cache(self) -> None:
        if not self._background_pixmap:
            return
        size = self.viewport().size()
        if size.isEmpty():
            return
        scaled = self._background_pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        cache = QPixmap(size)
        painter = QPainter(cache)
        painter.drawPixmap(0, 0, scaled)
        mask = QColor(self._mask_color)
        mask.setAlphaF(self._mask_opacity)
        painter.fillRect(cache.rect(), mask)
        painter.end()
        self._background_cache_pixmap = cache

    def paintEvent(self, event) -> None:
        if self._background_cache_pixmap:
            painter = QPainter(self.viewport())
            painter.drawPixmap(0, 0, self._background_cache_pixmap)
            painter.end()
        super().paintEvent(event)
