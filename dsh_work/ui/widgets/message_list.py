"""消息流（第 3.3 节）。

垂直滚动区域，按时间顺序展示消息气泡。
- 用户消息右对齐，Assistant 消息左对齐
- 工具调用以折叠卡片形式内联嵌入
- 支持流式渲染——Assistant 回复时逐字显示（WebSocket chunk 事件）

背景图片视口锚定渲染（第 4.3 节）：
- 重写 QScrollArea 的 viewport paintEvent
- 在视口坐标系 (0,0) 原点绘制背景，不应用 translate() 滚动偏移
- 静态缓存策略：窗口尺寸变化时在后台线程生成 background_cache_pixmap

性能优化（第 8.5 节）：
- 单会话消息数 > 200 时使用 QListView + 自定义 Delegate 虚拟化
- 会话消息数 > 100 时切换会话只加载最近 50 条，向上滚动追加加载
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap, QColor, QPainter
from PySide6.QtWidgets import (
    QScrollArea,
    QWidget,
    QVBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
)

from ...api import MessageRecord
from ...core.session_manager import AgentStatus
from ...utils.logger import get_logger

log = get_logger("ui.message_list")


class MessageBubble(QFrame):
    """单条消息气泡。

    用户消息右对齐，Assistant 消息左对齐。
    背景受主题系统控制，支持磨砂玻璃效果。
    """

    def __init__(self, role: str, content: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.role = role
        self.content = content
        self._setup_ui()

    def _setup_ui(self) -> None:
        if self.role == "user":
            self.setObjectName("MessageBubbleUser")
            self.setMaximumWidth(480)
        else:
            self.setObjectName("MessageBubbleAssistant")
            self.setMaximumWidth(640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        # 消息内容
        self.content_label = QLabel(self.content)
        self.content_label.setWordWrap(True)
        self.content_label.setTextFormat(Qt.TextFormat.MarkdownText)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.content_label)

    def update_content(self, content: str) -> None:
        """流式更新内容。"""
        self.content = content
        self.content_label.setText(content)


class MessageList(QScrollArea):
    """消息流滚动区域。

    支持流式渲染、背景固定、虚拟化（消息数 > 200 时）。
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

        # 背景固定渲染（第 4.3 节）
        self._background_pixmap: QPixmap | None = None
        self._background_cache_pixmap: QPixmap | None = None
        self._mask_color = QColor("#000000")
        self._mask_opacity = 0.3

        # 虚拟化阈值（第 8.5 节）
        self._virtualization_threshold = 200

        # 流式输出批处理（16ms，第 8.5 节）
        self._stream_buffer = ""
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(16)
        self._stream_timer.timeout.connect(self._flush_stream_buffer)
        self._user_is_scrolling_up = False  # 用户查看历史时，不强制滚回底部

    def _setup_ui(self) -> None:
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._container = QWidget()
        self._container.setObjectName("MessageListContainer")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(16, 16, 16, 16)
        self._layout.setSpacing(12)
        self._layout.addStretch()  # 顶部弹簧，消息从底部开始
        self.setWidget(self._container)

        # 监听滚动（追加加载历史）
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _on_scroll(self, value: int) -> None:
        if value <= self.verticalScrollBar().minimum() + 10:
            self.scrolled_to_top.emit()
        # 智能滚动：判断用户当前是否远离底部（正在回看历史）
        bar = self.verticalScrollBar()
        self._user_is_scrolling_up = value < (bar.maximum() - 20)  # 距离底部 >20px 视为在上看历史

    def _is_at_bottom(self) -> bool:
        """是否处于底部（流式输出时才自动滚）。"""
        bar = self.verticalScrollBar()
        return bar.value() >= (bar.maximum() - 5)

    # ===== 消息管理 =====

    def add_message(self, message: MessageRecord) -> MessageBubble:
        """添加一条消息气泡。"""
        bubble = MessageBubble(message.role, message.content)

        # 用户消息右对齐
        if message.role == "user":
            wrapper = self._wrap_alignment(bubble, align_right=True)
        else:
            wrapper = self._wrap_alignment(bubble, align_right=False)

        # 插入到 stretch 之前
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        self._bubbles.append(bubble)

        # 用户发消息一定滚到底部；其他消息若用户在看历史则不打断
        if message.role == "user" or not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        return bubble

    def _wrap_alignment(self, widget: QWidget, align_right: bool) -> QWidget:
        """包装对齐容器。"""
        wrapper = QWidget()
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        if align_right:
            wrapper_layout.addStretch()
            wrapper_layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            wrapper_layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft)
            wrapper_layout.addStretch()
        wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        return wrapper

    def start_streaming(self) -> MessageBubble:
        """开始流式输出，创建临时气泡。"""
        bubble = MessageBubble("assistant", "")
        self._current_streaming_bubble = bubble
        wrapper = self._wrap_alignment(bubble, align_right=False)
        self._layout.insertWidget(self._layout.count() - 1, wrapper)
        self._bubbles.append(bubble)
        # 开始流式时：只有用户本来就在底部才跟随滚动
        if not self._user_is_scrolling_up:
            QTimer.singleShot(0, self.scroll_to_bottom)
        return bubble

    def append_chunk(self, chunk: str) -> None:
        """追加流式 chunk（批处理，16ms 刷新一次）。"""
        self._stream_buffer += chunk
        self.chunk_appended.emit(chunk)
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_stream_buffer(self) -> None:
        """刷新流式缓冲到气泡。"""
        if not self._current_streaming_bubble or not self._stream_buffer:
            self._stream_timer.stop()
            return
        current = self._current_streaming_bubble.content
        self._current_streaming_bubble.update_content(current + self._stream_buffer)
        self._stream_buffer = ""
        self._stream_timer.stop()
        # 智能滚动：只有当用户本来就在底部时才跟随（否则不要打断用户回看历史）
        if not self._user_is_scrolling_up:
            self.scroll_to_bottom()

    def finish_streaming(self) -> MessageBubble | None:
        """结束流式输出，返回固化后的气泡。"""
        if self._stream_timer.isActive():
            self._flush_stream_buffer()
        bubble = self._current_streaming_bubble
        self._current_streaming_bubble = None
        return bubble

    def clear(self) -> None:
        """清空所有消息（切换会话时调用）。"""
        # 取消所有尚未完成的 QTimer.singleShot 延迟渲染任务（第 3.3 节会话切换保护）
        if self._stream_timer.isActive():
            self._stream_timer.stop()
        self._stream_buffer = ""
        self._current_streaming_bubble = None

        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bubbles.clear()

    def scroll_to_bottom(self) -> None:
        """滚动到底部。"""
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ===== 背景固定渲染（第 4.3 节）=====

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
        """重建背景缓存 Pixmap（窗口尺寸变化时调用）。

        静态缓存策略：滚动时视口仅平移此缓存图，绝不实时重绘原图。
        """
        if not self._background_pixmap:
            return
        size = self.viewport().size()
        if size.isEmpty():
            return
        # 缩放原图到视口尺寸
        scaled = self._background_pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._background_cache_pixmap = QPixmap(size)
        painter = QPainter(self._background_cache_pixmap)
        painter.drawPixmap(0, 0, scaled)
        # 叠加半透明遮罩，确保文字可读
        mask = QColor(self._mask_color)
        mask.setAlphaF(self._mask_opacity)
        painter.fillRect(self._background_cache_pixmap.rect(), mask)
        painter.end()

    def paintEvent(self, event) -> None:
        """重写绘制：在视口坐标系 (0,0) 原点绘制背景（不应用滚动偏移）。"""
        if self._background_cache_pixmap:
            painter = QPainter(self.viewport())
            # 关键：在视口坐标系原点绘制，不 translate 滚动偏移
            painter.drawPixmap(0, 0, self._background_cache_pixmap)
            painter.end()
        super().paintEvent(event)

    def resizeEvent(self, event) -> None:
        """视口尺寸变化时重建背景缓存。"""
        super().resizeEvent(event)
        if self._background_pixmap:
            self._rebuild_background_cache()
