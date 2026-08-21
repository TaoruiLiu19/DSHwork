"""工具调用折叠卡片与聚合（对齐 Web 版 ToolRow）。

Web 版工具行是默认折叠的 disclosure：工具名 + 状态 + 展开箭头，
展开后显示参数与返回结果。配色全部取自主题 token。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...utils.logger import get_logger

log = get_logger("ui.tool_call_card")


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
            "accent": tc.accent,
            "success": tc.success,
            "error": tc.error,
            "text": tc.text_primary,
            "text2": tc.text_secondary,
            "muted": tc.text_muted,
            "card_bg": tc.bg_secondary,
            "border": tc.divider,
        }
    return {
        "accent": "#679EFE",
        "success": "#22C55E",
        "error": "#F25A5A",
        "text": "#F9FAFB",
        "text2": "#CFD3D6",
        "muted": "#ADB2B8",
        "card_bg": "#1B1B1C",
        "border": "rgba(255,255,255,0.06)",
    }


@dataclass
class ToolCallEntry:
    """单次工具调用记录（聚合卡内的一条）。"""

    tool_name: str
    status: str = "running"  # running / success / failed
    params: dict = field(default_factory=dict)
    result: object = None
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    @property
    def color(self) -> str:
        p = _palette()
        if self.status == "running":
            return p["accent"]
        if self.status == "success":
            return p["success"]
        return p["error"]


class ToolCallCard(QFrame):
    """工具调用折叠卡片（Web 版 ToolRow：默认折叠的 disclosure）。"""

    def __init__(self, tool_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ToolRow")
        self.tool_name = tool_name
        self._entries: list[ToolCallEntry] = []
        self._expanded = False
        self._setup_ui()
        self.apply_theme()

    def apply_theme(self, theme=None) -> None:
        p = _palette()
        self.setStyleSheet(
            f"QFrame#ToolRow {{ background-color: {p['card_bg']};"
            f" border: 1px solid {p['border']}; border-radius: 8px; }}"
        )

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # 标题行（disclosure 头）
        header = QHBoxLayout()
        self._icon_label = QLabel("⚙")
        self._icon_label.setFixedWidth(18)
        self._icon_label.setStyleSheet(f"color: {_palette()['muted']};")
        header.addWidget(self._icon_label)

        self._title_label = QLabel(self.tool_name)
        self._title_label.setObjectName("ToolName")
        self._title_label.setStyleSheet(f"font-weight: 600; color: {_palette()['text']};")
        header.addWidget(self._title_label, stretch=1)

        self._status_label = QLabel("执行中...")
        self._status_label.setStyleSheet(f"color: {_palette()['accent']}; font-size: 11px;")
        header.addWidget(self._status_label)

        self._expand_btn = QToolButton()
        self._expand_btn.setText("▶")
        self._expand_btn.setCheckable(True)
        self._expand_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; color: "
            + _palette()["muted"] + "; }"
        )
        self._expand_btn.clicked.connect(self._toggle_expand)
        header.addWidget(self._expand_btn)

        layout.addLayout(header)

        # 时间线竖条容器
        self._timeline_widget = TimelineBar()
        layout.addWidget(self._timeline_widget)

        # 展开内容（默认隐藏）
        self._detail_label = QLabel()
        self._detail_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px; color: "
            + _palette()["text2"] + ";"
        )
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._detail_label.setVisible(False)
        layout.addWidget(self._detail_label)

    def add_entry(self, entry: ToolCallEntry) -> None:
        """添加一次工具调用记录到聚合卡。"""
        self._entries.append(entry)
        self._timeline_widget.add_color(entry.color)
        self._update_display()

    def _update_display(self) -> None:
        """更新卡片显示。"""
        p = _palette()
        count = len(self._entries)
        running = sum(1 for e in self._entries if e.status == "running")
        failed = sum(1 for e in self._entries if e.status == "failed")
        success = count - running - failed

        if count == 1:
            entry = self._entries[0]
            if entry.status == "running":
                self._status_label.setText("执行中...")
                self._status_label.setStyleSheet(f"color: {p['accent']}; font-size: 11px;")
            elif entry.status == "success":
                self._status_label.setText(f"成功 · {entry.duration_ms}ms")
                self._status_label.setStyleSheet(f"color: {p['success']}; font-size: 11px;")
            else:
                self._status_label.setText(f"失败 · {entry.error}")
                self._status_label.setStyleSheet(f"color: {p['error']}; font-size: 11px;")
        else:
            # 聚合显示
            parts = []
            if running:
                parts.append(f"进行中 {running}")
            if success:
                parts.append(f"成功 {success}")
            if failed:
                parts.append(f"失败 {failed}")
            self._title_label.setText(f"{self.tool_name} · 已执行 {count} 次")
            self._status_label.setText(" · ".join(parts))

        if self._expanded:
            self._render_detail()

    def _toggle_expand(self) -> None:
        self._expanded = self._expand_btn.isChecked()
        self._expand_btn.setText("▼" if self._expanded else "▶")
        self._detail_label.setVisible(self._expanded)
        if self._expanded:
            self._render_detail()

    def _render_detail(self) -> None:
        """渲染展开详情。"""
        lines = []
        for i, entry in enumerate(self._entries, 1):
            lines.append(f"[{i}] {entry.tool_name} ({entry.status}) {entry.duration_ms}ms")
            if entry.params:
                lines.append(f"    参数: {entry.params}")
            if entry.result:
                result_str = str(entry.result)
                if len(result_str) > 200:
                    result_str = result_str[:200] + "..."
                lines.append(f"    返回: {result_str}")
            if entry.error:
                lines.append(f"    错误: {entry.error}")
        self._detail_label.setText("\n".join(lines))

    def finalize_aggregation(self) -> None:
        """完结聚合卡（工具类型切换或失败时调用）。"""
        self._update_display()


class TimelineBar(QWidget):
    """微型时间线竖条。

    用颜色区分每次调用的执行状态：
    蓝=进行中，绿=成功，红=失败
    保留执行顺序与耗时感知。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._colors: list[str] = []

    def add_color(self, color: str) -> None:
        self._colors.append(color)
        self.update()

    def paintEvent(self, event) -> None:
        if not self._colors:
            return
        painter = QPainter(self)
        width = self.width()
        segment_width = max(width // max(len(self._colors), 1), 2)
        for i, color in enumerate(self._colors):
            painter.setPen(QPen(QColor(color), 0))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(
                i * segment_width, 0, segment_width - 1, self.height(), 2, 2
            )
        painter.end()


class ToolCallAggregator:
    """工具调用聚合管理器。

    连续同类工具聚合：同一工具连续调用只显示一个卡片。
    工具类型切换或出现 status: failed 时，完结上一个聚合卡并另起新卡。

    使用方法：
        aggregator = ToolCallAggregator(message_list)
        aggregator.on_tool_call(record)  # 收到 tool_call 事件
        aggregator.on_tool_result(record)  # 收到 tool_result 事件
    """

    def __init__(self):
        self._current_card: ToolCallCard | None = None
        self._current_tool: str = ""
        self._entries: list[ToolCallEntry] = []
        # 回调：创建新卡片时调用（让 MessageList 插入到消息流）
        self._create_card_callback: Callable[[ToolCallCard], None] | None = None

    def set_create_callback(self, callback: Callable[[ToolCallCard], None]) -> None:
        self._create_card_callback = callback

    def on_tool_call(self, tool_name: str, params: dict) -> None:
        """处理 tool_call 事件。"""
        # 工具类型切换 → 完结上一个聚合卡
        if self._current_tool and tool_name != self._current_tool:
            self._finalize_current()

        # 新建聚合卡（首次或切换后）
        if self._current_card is None:
            self._current_card = ToolCallCard(tool_name)
            self._current_tool = tool_name
            if self._create_card_callback:
                self._create_card_callback(self._current_card)

        entry = ToolCallEntry(tool_name=tool_name, params=params)
        self._entries.append(entry)
        self._current_card.add_entry(entry)

    def on_tool_result(self, tool_name: str, status: str, result: object = None, error: str = "") -> None:
        """处理 tool_result 事件。"""
        # 更新最后一个匹配的 entry
        for entry in reversed(self._entries):
            if entry.tool_name == tool_name and entry.status == "running":
                entry.status = status
                entry.result = result
                entry.error = error
                entry.finished_at = time.time()
                break
        if self._current_card:
            self._current_card._update_display()

        # 出现 failed → 完结当前聚合卡（第 3.3 节）
        if status == "failed":
            self._finalize_current()

    def _finalize_current(self) -> None:
        """完结当前聚合卡。"""
        if self._current_card:
            self._current_card.finalize_aggregation()
        self._current_card = None
        self._current_tool = ""
        # entries 保留用于历史记录，但聚合卡已完结
