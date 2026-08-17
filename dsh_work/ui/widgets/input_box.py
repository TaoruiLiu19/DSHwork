"""输入框。

固定在对话区域底部，多行文本输入：
- Shift+Enter 换行，Enter 发送
- Agent 运行时输入框变为"追加消息"模式——输入内容通过 session.updateQueue 排队
  此时发送按钮变为"停止"按钮，点击调用 session.cancel

输入框上方可展开附件栏，支持拖拽文件到输入框（文件路径自动插入到消息文本中）。

上下文容量感知：
- 输入框上方有可展开的细进度条，按比例填充
- 颜色编码：< 70% 蓝，70-90% 橙，> 90% 红
- ≥ 80% 输入框下方浅色提示
- ≥ 95% 发送按钮变为警示态，首次点击弹出确认

参考 DSH Web UI 输入框设计：
- 自动扩展输入区域，无固定高度
- 发送按钮紧凑集成在输入区域右下角
- 工作/代码模式切换
- 附带文件拖拽指示
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFontMetrics, QColor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QLabel,
    QFrame,
    QSizePolicy,
)

from ...core.session_manager import ContextUsage, AgentStatus
from ... import constants as C

# 输入框最小/最大行数
_MIN_VISIBLE_LINES = 1
_MAX_VISIBLE_LINES = 8


class _AutoResizeTextEdit(QPlainTextEdit):
    """自动根据内容调整高度的文本输入框。

    最小高度 1 行，最大高度 8 行，超出则出现滚动条。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # 拖拽由父级 InputBox 统一处理
        self.setAcceptDrops(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(False)
        # 尺寸策略：水平扩展，垂直固定（由 resize 控制）
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._min_height: int = 0
        self._max_height: int = 0
        # 文档内容变化时自动调整高度（处理粘贴、撤销等操作）
        self.document().contentsChanged.connect(self._on_content_changed)

    def _on_content_changed(self) -> None:
        """文档内容变化时调整高度。"""
        QTimer.singleShot(0, self._update_height)

    def _update_height(self) -> None:
        """根据文档内容调整 widget 高度。"""
        doc = self.document()
        fm = QFontMetrics(self.font())
        line_height = fm.lineSpacing()
        margins = self.contentsMargins()
        v_margin = margins.top() + margins.bottom() + self.document().documentMargin() * 2
        # 计算所需行数
        doc_height = int(doc.size().height())
        lines = max(1, doc_height // line_height)
        lines = min(lines, _MAX_VISIBLE_LINES)

        if self._min_height == 0:
            # 初始化最小/最大高度
            self._min_height = line_height + v_margin + 4
            self._max_height = line_height * _MAX_VISIBLE_LINES + v_margin + 4

        target = max(self._min_height, line_height * lines + v_margin + 4)
        target = min(target, self._max_height)
        if self.height() != target:
            self.setFixedHeight(target)
            self.updateGeometry()
            # 通知父容器重新布局
            parent = self.parentWidget()
            while parent is not None:
                ly = parent.layout()
                if ly is not None:
                    ly.activate()
                    ly.update()
                parent.updateGeometry()
                parent = parent.parentWidget()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_height)

    def keyPressEvent(self, event) -> None:
        # Ctrl+Backspace 删除前一个词
        if event.key() == Qt.Key.Key_Backspace and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            cursor = self.textCursor()
            cursor.select(cursor.Selection.WordUnderCursor)
            cursor.removeSelectedText()
            return
        super().keyPressEvent(event)


class InputBox(QWidget):
    """消息输入框。

    信号：
        send_requested(str): 用户请求发送消息
        stop_requested: 用户请求停止 Agent
        files_dropped(list[str]): 文件拖拽
        mode_changed(str): 模式切换
    """

    send_requested = Signal(str)
    stop_requested = Signal()
    files_dropped = Signal(list)
    mode_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._agent_status = AgentStatus.IDLE
        self._context: ContextUsage | None = None
        self._current_mode = C.MODE_WORK
        self._placeholder_work = "描述你想完成的工作..."
        self._placeholder_code = "输入指令或粘贴代码..."
        self._current_placeholder = self._placeholder_work
        self._attached_files: list[str] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        """初始化 UI 组件。

        布局结构（参考 DSH Web UI 设计）：
        ┌──────────────────────────────────────┐
        │  上下文容量进度条（默认隐藏）           │
        │  上下文容量文本（默认隐藏）             │
        │  阈值提示（默认隐藏）                   │
        │  ┌──────────────────────────────────┐ │
        │  │  [工作] [代码]                    │ │
        │  │  ┌──────────────────────────────┐ │ │
        │  │  │ 文本输入区域                  │ │ │
        │  │  │ （自动扩展高度）              │ │ │
        │  │  └──────────────────────────────┘ │ │
        │  │       [📎]          [发送]        │ │
        │  └──────────────────────────────────┘ │
        │  文件拖拽指示区（默认隐藏）             │
        └──────────────────────────────────────┘
        """
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 4, 16, 16)
        main_layout.setSpacing(4)
        main_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinAndMaxSize)

        # ===== 上下文容量指示器 =====
        self._setup_context_indicators(main_layout)

        # ===== 输入容器（带圆角背景） =====
        input_container = QFrame()
        input_container.setObjectName("InputContainer")
        container_layout = QVBoxLayout(input_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # 模式切换栏（pill 样式）
        self._setup_top_bar(container_layout)

        # 分隔线
        separator = QFrame()
        separator.setObjectName("InputSeparator")
        separator.setFixedHeight(1)
        container_layout.addWidget(separator)

        # 文本输入
        self._setup_text_area(container_layout)

        # 底部操作栏
        self._setup_bottom_bar(container_layout)

        main_layout.addWidget(input_container)

        # 文件拖拽指示区
        self._setup_drop_indicator(main_layout)

        # 启用拖拽
        self.setAcceptDrops(True)

    def _setup_context_indicators(self, parent_layout: QVBoxLayout) -> None:
        """上下文容量指示器。"""
        # 上下文容量进度条
        self._context_bar = QProgressBar()
        self._context_bar.setFixedHeight(3)
        self._context_bar.setRange(0, 100)
        self._context_bar.setVisible(False)
        parent_layout.addWidget(self._context_bar)

        # 上下文容量文本
        self._context_label = QLabel()
        self._context_label.setObjectName("Muted")
        self._context_label.setStyleSheet("font-size: 11px; color: #9599A6;")
        self._context_label.setVisible(False)
        parent_layout.addWidget(self._context_label)

        # 阈值提示
        self._warn_label = QLabel()
        self._warn_label.setStyleSheet("color: #D27E24; font-size: 11px;")
        self._warn_label.setVisible(False)
        parent_layout.addWidget(self._warn_label)

    def _setup_top_bar(self, parent_layout: QVBoxLayout) -> None:
        """模式切换栏（pill 样式）。"""
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(8, 6, 8, 4)
        top_bar.setSpacing(4)

        # 工作模式按钮
        self._work_btn = QPushButton("工作")
        self._work_btn.setObjectName("ModeWork")
        self._work_btn.setFixedHeight(24)
        self._work_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._work_btn.clicked.connect(lambda: self._switch_mode(C.MODE_WORK))
        # 代码模式按钮
        self._code_btn = QPushButton("代码")
        self._code_btn.setObjectName("ModeCode")
        self._code_btn.setFixedHeight(24)
        self._code_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._code_btn.clicked.connect(lambda: self._switch_mode(C.MODE_CODE))

        top_bar.addWidget(self._work_btn)
        top_bar.addWidget(self._code_btn)
        top_bar.addStretch()

        # 附件计数
        self._attach_label = QLabel()
        self._attach_label.setObjectName("Muted")
        self._attach_label.setStyleSheet("font-size: 11px; color: #9599A6;")
        self._attach_label.setVisible(False)
        top_bar.addWidget(self._attach_label)

        parent_layout.addLayout(top_bar)
        self._update_mode_buttons()

    def _setup_text_area(self, parent_layout: QVBoxLayout) -> None:
        """文本输入区域。"""
        self._text_edit = _AutoResizeTextEdit()
        self._text_edit.setPlaceholderText(self._current_placeholder)
        # 设置最小高度为一行高度
        self._text_edit.document().contentsChanged.connect(self._on_text_changed)
        parent_layout.addWidget(self._text_edit)

    def _setup_bottom_bar(self, parent_layout: QVBoxLayout) -> None:
        """底部操作栏：附件按钮 + 发送按钮。"""
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(8, 4, 8, 8)
        bottom_bar.setSpacing(6)

        # 附件按钮
        self._attach_btn = QPushButton("📎")
        self._attach_btn.setFixedSize(28, 28)
        self._attach_btn.setObjectName("InputActionBtn")
        self._attach_btn.setToolTip("拖拽文件到输入框，或点击选择文件")
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bottom_bar.addWidget(self._attach_btn)

        bottom_bar.addStretch()

        # 发送按钮
        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("Primary")
        self._send_btn.setFixedSize(64, 28)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.clicked.connect(self._on_send)
        bottom_bar.addWidget(self._send_btn)

        parent_layout.addLayout(bottom_bar)

    def _setup_drop_indicator(self, parent_layout: QVBoxLayout) -> None:
        """文件拖拽指示区。"""
        self._drop_indicator = QLabel("拖拽文件到此处以附加到消息")
        self._drop_indicator.setObjectName("DropIndicator")
        self._drop_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_indicator.setFixedHeight(60)
        self._drop_indicator.setVisible(False)
        parent_layout.addWidget(self._drop_indicator)

    def _switch_mode(self, mode: str) -> None:
        """切换工作/代码模式。"""
        if mode == self._current_mode:
            return
        self._current_mode = mode
        self._current_placeholder = self._placeholder_code if mode == C.MODE_CODE else self._placeholder_work
        if self._agent_status == AgentStatus.IDLE:
            self._text_edit.setPlaceholderText(self._current_placeholder)
        self._update_mode_buttons()
        self.mode_changed.emit(mode)

    def _update_mode_buttons(self) -> None:
        """更新模式按钮的选中状态。"""
        is_work = self._current_mode == C.MODE_WORK
        self._work_btn.setObjectName("ModeWork" if is_work else "ModeInactive")
        self._code_btn.setObjectName("ModeCode" if not is_work else "ModeInactive")
        # 刷新样式
        self._work_btn.style().unpolish(self._work_btn)
        self._work_btn.style().polish(self._work_btn)
        self._code_btn.style().unpolish(self._code_btn)
        self._code_btn.style().polish(self._code_btn)

    def _on_text_changed(self) -> None:
        """文本变化时，更新发送按钮状态。"""
        has_text = bool(self._text_edit.toPlainText().strip())
        self._send_btn.setEnabled(has_text or self._agent_status == AgentStatus.RUNNING)

    def _on_send(self) -> None:
        """发送或停止。"""
        if self._agent_status == AgentStatus.RUNNING:
            self.stop_requested.emit()
            return

        # 上下文容量 ≥ 95% 警示态
        if self._context and self._context.is_danger:
            if self._send_btn.text() == "发送":
                self._send_btn.setText("确认发送?")
                # 3秒后恢复
                QTimer.singleShot(3000, lambda: self._send_btn.setText("发送"))
                return

        text = self._text_edit.toPlainText().strip()
        if text:
            self.send_requested.emit(text)
            self._text_edit.clear()
            self._attached_files.clear()
            self._update_attach_label()

    def set_agent_status(self, status: AgentStatus) -> None:
        """设置 Agent 状态，切换发送/停止按钮。"""
        self._agent_status = status
        if status == AgentStatus.RUNNING or status == AgentStatus.THINKING or status == AgentStatus.TOOL_EXECUTING:
            self._send_btn.setText("停止")
            self._send_btn.setObjectName("Danger")
            self._send_btn.setEnabled(True)
        else:
            self._send_btn.setText("发送")
            self._send_btn.setObjectName("Primary")
            has_text = bool(self._text_edit.toPlainText().strip())
            self._send_btn.setEnabled(has_text)
        # 刷新样式
        self._send_btn.style().unpolish(self._send_btn)
        self._send_btn.style().polish(self._send_btn)
        self._text_edit.setPlaceholderText(
            "追加消息到队列..." if status == AgentStatus.RUNNING else self._current_placeholder
        )

    def set_mode(self, mode: str) -> None:
        """根据模式设置占位符和按钮状态。"""
        self._current_mode = mode
        self._current_placeholder = self._placeholder_code if mode == C.MODE_CODE else self._placeholder_work
        self._update_mode_buttons()
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
            f"QProgressBar {{ background-color: rgba(255,255,255,0.04); border: none; border-radius: 2px; }}"
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
        """拖拽进入时显示指示器。"""
        if event.mimeData().hasUrls():
            self._drop_indicator.setVisible(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        """拖拽离开时隐藏指示器。"""
        self._drop_indicator.setVisible(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        """拖拽文件：文件路径自动插入到消息文本中。"""
        self._drop_indicator.setVisible(False)
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            self._attached_files.extend(paths)
            self._update_attach_label()
            # 将文件路径插入到文本中
            current = self._text_edit.toPlainText()
            for p in paths:
                if current:
                    current += "\n"
                current += p
            self._text_edit.setPlainText(current)

    def _update_attach_label(self) -> None:
        """更新附件计数标签。"""
        count = len(self._attached_files)
        if count > 0:
            self._attach_label.setText(f"{count} 个文件已附加")
            self._attach_label.setVisible(True)
        else:
            self._attach_label.setVisible(False)

    def fill_prompt(self, text: str) -> None:
        """填充提示词到输入框（空状态卡片点击时调用）。"""
        self._text_edit.setPlainText(text)
        self._text_edit.setFocus()

    def trigger_send(self) -> None:
        """外部触发发送（如 Ctrl+Return 全局快捷键）。"""
        self._on_send()

    @property
    def text(self) -> str:
        """当前输入文本。"""
        return self._text_edit.toPlainText()