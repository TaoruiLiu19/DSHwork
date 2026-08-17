"""消息流。

垂直滚动区域，按时间顺序展示消息气泡。
- 用户消息右对齐，Assistant 消息左对齐
- 工具调用以折叠卡片形式内联嵌入
- 支持流式渲染——Assistant 回复时逐字显示（WebSocket chunk 事件）

参考 DSH Web UI 对话展示设计：
- 消息头部简洁，角色名 + 时间戳
- 用户消息气泡使用主题色背景，右对齐
- Assistant 消息气泡使用卡片背景，左对齐
- 消息间距紧凑，代码块有单独样式
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QPixmap, QColor, QPainter
from PySide6.QtWidgets import (
    QScrollArea,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
    QApplication,
)

from ...api import MessageRecord
from ...utils.logger import get_logger
from .tool_call_card import ToolCallAggregator, ToolCallCard

log = get_logger("ui.message_list")

# 气泡最大宽度 = 视口宽度 * 比例
_BUBBLE_MAX_WIDTH_RATIO = 0.80
# 气泡内部左右 padding
_BUBBLE_HPAD = 16
# 时间戳格式
_TIMESTAMP_FORMAT = "%H:%M"


class MessageBubble(QFrame):
    """单条消息气泡。

    用户消息右对齐，Assistant 消息左对齐。
    包含角色名头部、内容、时间戳。
    背景受主题系统控制。
    """

    def __init__(self, role: str, content: str, timestamp: float = 0.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.role = role
        self.content = content
        self.timestamp = timestamp
        self._max_width_override: int | None = None

        # 标记是否为用户消息
        self._is_user = role == "user"

        self._setup_ui()

    def set_max_width_viewport(self, viewport_width: int) -> None:
        """根据聊天区 viewport 宽度设置气泡最大宽度（外部 resize 时调用）。"""
        if viewport_width <= 0:
            return
        limit = int(viewport_width * _BUBBLE_MAX_WIDTH_RATIO)
        self._max_width_override = limit
        self.setMaximumWidth(limit)
        # 同步限制内容标签最大宽度
        if self._content_label is not None:
            margin_total = _BUBBLE_HPAD * 2 + 4  # 4 for border
            self._content_label.setMaximumWidth(max(1, limit - margin_total))
            self._content_label.updateGeometry()

    def _setup_ui(self) -> None:
        """初始化 UI 组件。

        布局结构：
        ┌──────────────────────────────────┐
        │  角色名 · 时间戳                  │
        │  ┌──────────────────────────┐    │
        │  │ 消息内容                  │    │
        │  │ (自动换行，填满气泡宽度)   │    │
        │  └──────────────────────────┘    │
        └──────────────────────────────────┘
        """
        # 设置 objectName 用于 QSS 样式
        if self._is_user:
            self.setObjectName("MessageBubbleUser")
        else:
            self.setObjectName("MessageBubbleAssistant")

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_BUBBLE_HPAD, 10, _BUBBLE_HPAD, 10)
        layout.setSpacing(6)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetDefaultConstraint)

        # 消息头部：角色名 + 时间戳（更简洁的样式）
        self._header_label = QLabel()
        self._header_label.setObjectName("MessageBubbleHeader")
        header_text = "你" if self._is_user else "Assistant"
        if self.timestamp > 0:
            from datetime import datetime
            ts_str = datetime.fromtimestamp(self.timestamp).strftime(_TIMESTAMP_FORMAT)
            header_text = f"{header_text} · {ts_str}"
        self._header_label.setText(header_text)
        self._header_label.setStyleSheet(
            "font-size: 11px; font-weight: 500; color: inherit;"
        )
        layout.addWidget(self._header_label)

        # 消息内容（支持 Markdown）
        self._content_label = QLabel(self.content)
        self._content_label.setWordWrap(True)
        self._content_label.setTextFormat(Qt.TextFormat.MarkdownText)
        self._content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._content_label.setOpenExternalLinks(True)
        # 最小宽度为 1 防止标签消失
        self._content_label.setMinimumWidth(1)

        # 内容标签：水平 Expanding 填满气泡宽度，垂直 Preferred 允许高度增长
        self._content_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(self._content_label)

        # 气泡自身尺寸策略
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred,
        )

    def update_content(self, content: str) -> None:
        """流式更新内容，同步刷新几何尺寸。"""
        self.content = content
        self._content_label.setText(content)
        # 强制重新布局
        self._content_label.updateGeometry()
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

        # 发送 LayoutRequest 事件让 QScrollArea 重新计算
        QApplication.postEvent(self, QEvent(QEvent.Type.LayoutRequest))
        top = self
        while top.parentWidget() is not None:
            top = top.parentWidget()
            QApplication.postEvent(top, QEvent(QEvent.Type.LayoutRequest))


class MessageList(QScrollArea):
    """消息流滚动区域。

    支持流式渲染、背景固定、消息管理。
    参考 DSH Web UI ChatView 设计。
    """

    # 信号：用户滚动到顶部（触发追加加载历史）
    scrolled_to_top = Signal()
    # 信号：流式 chunk 到达（用于工具聚合卡更新）
    chunk_appended = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

        # 消息气泡列表
        self._bubbles: list[MessageBubble] = []
        self._current_streaming_bubble: MessageBubble | None = None

        # 背景固定渲染
        self._background_pixmap: QPixmap | None = None
        self._background_cache_pixmap: QPixmap | None = None
        self._mask_color = QColor("#000000")
        self._mask_opacity = 0.3

        # 流式输出
        self._stream_pending = False
        self._user_is_scrolling_up = False

        # 工具调用聚合器（内联管理工具卡片）
        self._tool_aggregator = ToolCallAggregator()
        self._tool_aggregator.set_create_callback(self._insert_tool_card)

    def _setup_ui(self) -> None:
        """初始化 UI。

        使用 QScrollArea 包裹一个 QWidget 容器，
        容器内用 QVBoxLayout 管理消息气泡，底部有 stretch 让消息从底部开始排列。
        """
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("MessageListContainer")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 16, 16, 16)
        self._layout.setSpacing(16)
        # 底部 stretch：让消息从顶部开始排列，stretch 在底部
        self._layout.addStretch()
        self.setWidget(self._container)

        # 监听滚动位置
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化：重算所有气泡的最大宽度 + 重建背景缓存。"""
        super().resizeEvent(event)
        vp_w = self.viewport().width()
        if vp_w > 0:
            for b in self._bubbles:
                b.set_max_width_viewport(vp_w)
        if self._background_pixmap:
            self._rebuild_background_cache()

    def _viewport_width(self) -> int:
        try:
            w = self.viewport().width()
        except Exception:
            w = 0
        return w if w > 0 else self.width()

    def _apply_bubble_viewport_width(self, bubble: MessageBubble) -> None:
        vp_w = self._viewport_width()
        if vp_w > 0:
            bubble.set_max_width_viewport(vp_w)

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

    def add_message(self, message: MessageRecord) -> MessageBubble:
        """添加一条消息气泡。"""
        bubble = MessageBubble(message.role, message.content, timestamp=getattr(message, "timestamp", 0))
        self._apply_bubble_viewport_width(bubble)

        # 创建对齐包装器
        wrapper = self._wrap_alignment(bubble, align_right=message.role == "user")

        # 插入到 stretch 之前
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        self._bubbles.append(bubble)

        # 强制重新布局
        self._layout.activate()

        # 滚动到底部
        if message.role == "user" or not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        return bubble

    def load_messages_batch(self, messages: list[MessageRecord]) -> None:
        """批量加载历史消息（切换会话时使用）。"""
        self.clear()
        for message in messages:
            bubble = MessageBubble(message.role, message.content, timestamp=getattr(message, "timestamp", 0))
            self._apply_bubble_viewport_width(bubble)
            wrapper = self._wrap_alignment(bubble, align_right=message.role == "user")
            self._layout.insertWidget(self._layout.count() - 1, wrapper)
            self._bubbles.append(bubble)
        self._layout.activate()
        self.viewport().update()
        QTimer.singleShot(0, self.scroll_to_bottom)

    def _wrap_alignment(self, widget: QWidget, align_right: bool) -> QWidget:
        """包装对齐容器。

        用户消息右对齐，Assistant 消息左对齐。
        使用 QHBoxLayout + stretch 实现对齐。
        """
        wrapper = QWidget()
        wrapper.setObjectName("MessageWrapper")
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        wrapper_layout.setSizeConstraint(QHBoxLayout.SizeConstraint.SetDefaultConstraint)

        if align_right:
            wrapper_layout.addStretch(1)
            wrapper_layout.addWidget(widget, stretch=0)
        else:
            wrapper_layout.addWidget(widget, stretch=0)
            wrapper_layout.addStretch(1)

        wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return wrapper

    def start_streaming(self) -> MessageBubble:
        """开始流式输出，创建临时气泡。"""
        log.info("[DEBUG] MessageList.start_streaming() 被调用")
        bubble = MessageBubble("assistant", "", timestamp=time.time())
        self._apply_bubble_viewport_width(bubble)
        self._current_streaming_bubble = bubble
        wrapper = self._wrap_alignment(bubble, align_right=False)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        self._bubbles.append(bubble)
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        return bubble

    def append_chunk(self, chunk: str) -> None:
        """追加流式 chunk（立即刷新，无缓冲延迟）。

        使用 singleShot(0) 将同一事件循环内的多个 chunk 合并为一次更新，
        既保证文字逐步出现，又避免过度布局计算。
        """
        if not self._current_streaming_bubble:
            log.warning("[DEBUG] append_chunk 但 _current_streaming_bubble 为 None，丢弃 chunk_len=%d", len(chunk))
            return
        log.info("[DEBUG] append_chunk: chunk_len=%d, 当前内容长度=%d", len(chunk), len(self._current_streaming_bubble.content))
        # 直接追加到当前气泡内容
        self._current_streaming_bubble.content += chunk
        self.chunk_appended.emit(chunk)
        # 延迟到事件循环末尾刷新 UI，合并同一轮的所有 chunk
        if not self._stream_pending:
            self._stream_pending = True
            QTimer.singleShot(0, self._flush_stream)

    def _flush_stream(self) -> None:
        """将累积的内容刷新到气泡标签（事件循环末尾执行一次）。"""
        self._stream_pending = False
        if not self._current_streaming_bubble:
            return
        self._current_streaming_bubble.update_content(
            self._current_streaming_bubble.content
        )
        self._layout.activate()
        self.viewport().update()
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)

    def finish_streaming(self) -> MessageBubble | None:
        """结束流式输出，返回固化后的气泡。"""
        log.info("[DEBUG] finish_streaming: 被调用, 气泡内容长度=%d, has_bubble=%s",
                 len(self._current_streaming_bubble.content) if self._current_streaming_bubble else 0,
                 self._current_streaming_bubble is not None)
        if self._stream_pending:
            self._flush_stream()
        bubble = self._current_streaming_bubble
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        self._current_streaming_bubble = None
        log.info("[DEBUG] finish_streaming: 完成, 气泡已定稿")
        return bubble

    def clear(self) -> None:
        """清空所有消息（切换会话时调用）。"""
        log.info("[DEBUG] MessageList.clear: 被调用, 当前气泡数=%d, _current_streaming_bubble=%s",
                 len(self._bubbles), self._current_streaming_bubble is not None)
        self._stream_pending = False
        self._current_streaming_bubble = None
        # 重置工具调用聚合器
        self._tool_aggregator = ToolCallAggregator()
        self._tool_aggregator.set_create_callback(self._insert_tool_card)

        # 移除所有消息 widget（保留最后的 stretch）
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._bubbles.clear()

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

    def scroll_to_bottom(self) -> None:
        """滚动到底部。"""
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ===== 背景固定渲染 =====

    def set_background_image(self, image_path: str, mask_color: str, mask_opacity: float) -> None:
        """设置背景图片（视口锚定，fixed）。"""
        from pathlib import Path

        path = Path(image_path)
        if not path.exists():
            log.warning("背景图片不存在: %s", image_path)
            return
        self._background_pixmap = QPixmap(str(path))
        if self._background_pixmap.isNull():
            log.warning("背景图片加载失败: %s", image_path)
            self._background_pixmap = None
            return
        self._mask_color = QColor(mask_color)
        self._mask_opacity = mask_opacity
        self._rebuild_background_cache()
        self.viewport().update()

    def clear_background(self) -> None:
        """清除背景图片。"""
        self._background_pixmap = None
        self._background_cache_pixmap = None
        self.viewport().update()

    def _rebuild_background_cache(self) -> None:
        """重建背景缓存 Pixmap（窗口尺寸变化时调用）。"""
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
        # 叠加半透明遮罩
        mask = QColor(self._mask_color)
        mask.setAlphaF(self._mask_opacity)
        painter.fillRect(cache.rect(), mask)
        painter.end()
        self._background_cache_pixmap = cache

    def paintEvent(self, event) -> None:
        """重写绘制：在视口坐标系 (0,0) 原点绘制背景（不应用滚动偏移）。"""
        if self._background_cache_pixmap:
            painter = QPainter(self.viewport())
            painter.drawPixmap(0, 0, self._background_cache_pixmap)
            painter.end()
        super().paintEvent(event)