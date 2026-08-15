"""底部状态栏（第 3.6 节）。

高度约 28px，显示运行时状态信息：

| 位置  | 内容                    | 说明                                            |
|-------|-------------------------|-------------------------------------------------|
| 左    | 连接状态指示灯 + 文字   | 绿色"已连接" / 红色"连接丢失" / 黄色"重连中"     |
| 中左  | Agent 状态              | Running（显示当前 turn/step 数）或 Idle          |
| 中    | 上下文容量              | 已用/上限 token + 进度示意，颜色编码（蓝/橙/红） |
| 中右  | Token 用量 + 余额       | 累计输入/输出 token；账户余额，灰态表示不可用    |
| 右    | DSH 版本号 + 当前模式   | 版本适配器获取；模式标签                          |
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QProgressBar,
)

from ..api import CompatibilityMode
from ..api.balance_client import BalanceResult
from ..core.session_manager import AgentStatus, ContextUsage
from .. import constants as C


class StatusIndicator(QWidget):
    """连接状态指示灯。"""

    # TRAE 状态色
    _COLOR_SUCCESS = "#33C192"
    _COLOR_WARNING = "#D27E24"
    _COLOR_ERROR = "#F65A5A"
    _COLOR_MUTED = "#666B75"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = self._COLOR_MUTED
        self._text = "未连接"

    def set_status(self, color: str, text: str) -> None:
        self._color = color
        self._text = text
        self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainter, QBrush
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(self._color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 10, 10)
        painter.end()


class StatusBar(QWidget):
    """底部状态栏。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(28)
        self._tip_timer = None
        self._tip_last_mode = C.MODE_WORK
        self._setup_ui()

    def show_temporary(self, text: str, color: str = "#FFB454", duration_ms: int = 3000) -> None:
        """用 mode_label 临时显示一条提示，duration_ms 之后自动还原为当前 mode。"""
        from PySide6.QtCore import QTimer

        # 保存当前状态（如果已经不在 show_temporary 的覆盖里才记一次）
        if self._tip_timer is None:
            current = self._mode_label.text()
            self._tip_last_mode = C.MODE_CODE if "[Code]" in current else C.MODE_WORK
        else:
            try:
                self._tip_timer.stop()
            except Exception:
                pass
        self._mode_label.setText(text)
        self._mode_label.setStyleSheet(
            f"font-size: 11px; color: {color}; font-weight: 600;"
        )
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self._restore_mode)
        self._tip_timer.start(duration_ms)

    def _restore_mode(self) -> None:
        self.set_mode(self._tip_last_mode)
        self._tip_timer = None

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(16)

        # 左：连接状态
        self._conn_indicator = StatusIndicator()
        layout.addWidget(self._conn_indicator)
        self._conn_label = QLabel("未连接")
        self._conn_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._conn_label)

        layout.addWidget(self._separator())

        # 中左：Agent 状态
        self._agent_label = QLabel("Idle")
        self._agent_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._agent_label)

        layout.addWidget(self._separator())

        # 中：上下文容量
        self._context_label = QLabel("上下文 -")
        self._context_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._context_label)
        self._context_bar = QProgressBar()
        self._context_bar.setFixedHeight(6)
        self._context_bar.setFixedWidth(60)
        self._context_bar.setRange(0, 100)
        layout.addWidget(self._context_bar)

        layout.addWidget(self._separator())

        # 中右：Token + 余额
        self._token_label = QLabel("Token: 0")
        self._token_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._token_label)

        self._balance_label = QLabel("余额: -")
        self._balance_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._balance_label)

        layout.addStretch()

        # 右：DSH 版本 + 模式
        self._version_label = QLabel("DSH v-")
        self._version_label.setStyleSheet("font-size: 11px; color: #666B75;")
        layout.addWidget(self._version_label)

        self._mode_label = QLabel("[Work]")
        self._mode_label.setStyleSheet("font-size: 11px; color: #32F08C; font-weight: 600;")
        layout.addWidget(self._mode_label)

    @staticmethod
    def _separator() -> QLabel:
        sep = QLabel("│")
        sep.setStyleSheet("color: rgba(224, 226, 242, 0.1);")
        return sep

    def set_connection_status(self, mode: CompatibilityMode) -> None:
        """设置连接状态。"""
        if mode == CompatibilityMode.FULL:
            self._conn_indicator.set_status(StatusIndicator._COLOR_SUCCESS, "已连接")
            self._conn_label.setText("已连接")
            self._conn_label.setStyleSheet(f"font-size: 11px; color: {StatusIndicator._COLOR_SUCCESS};")
        elif mode == CompatibilityMode.DEGRADED:
            self._conn_indicator.set_status(StatusIndicator._COLOR_WARNING, "兼容模式")
            self._conn_label.setText("兼容模式")
            self._conn_label.setStyleSheet(f"font-size: 11px; color: {StatusIndicator._COLOR_WARNING};")
        else:
            self._conn_indicator.set_status(StatusIndicator._COLOR_ERROR, "离线")
            self._conn_label.setText("离线")
            self._conn_label.setStyleSheet(f"font-size: 11px; color: {StatusIndicator._COLOR_ERROR};")

    def set_agent_status(self, status: AgentStatus, turn: int = 0, step: int = 0) -> None:
        """设置 Agent 状态。"""
        # TRAE 语义色
        trae_blue = "#387BFF"
        trae_purple = "#B655FC"
        trae_teal = "#2DD288"
        trae_error = "#F65A5A"
        trae_muted = "#9599A6"
        if status == AgentStatus.RUNNING:
            self._agent_label.setText(f"Running · Turn {turn}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {trae_blue};")
        elif status == AgentStatus.THINKING:
            self._agent_label.setText(f"Thinking · Turn {turn}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {trae_purple};")
        elif status == AgentStatus.TOOL_EXECUTING:
            self._agent_label.setText(f"Tool · Step {step}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {trae_teal};")
        elif status == AgentStatus.ERROR:
            self._agent_label.setText("Error")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {trae_error};")
        else:
            self._agent_label.setText("Idle")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {trae_muted};")

    def set_context_usage(self, context: ContextUsage) -> None:
        """设置上下文容量。"""
        used_k = context.used_tokens // 1000
        limit_k = context.limit_tokens // 1000
        self._context_label.setText(f"上下文 {used_k}k/{limit_k}k")
        self._context_bar.setValue(context.percentage)
        color_map = {
            "accent": "#32F08C",
            "warning": "#D27E24",
            "error": "#F65A5A",
        }
        color = color_map.get(context.color_key, "#32F08C")
        self._context_bar.setStyleSheet(
            f"QProgressBar {{ background-color: rgba(224, 226, 242, 0.1); border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}"
        )

    def set_token_usage(self, input_tokens: int, output_tokens: int) -> None:
        """设置 Token 用量。"""
        self._token_label.setText(f"Token: ↑{input_tokens} ↓{output_tokens}")

    def set_balance(self, result) -> None:
        """设置余额显示。"""
        from ..api.balance_client import BalanceSource
        if not result.is_available:
            self._balance_label.setText("余额: 不可用")
            self._balance_label.setStyleSheet("font-size: 11px; color: #666B75;")
            return
        self._balance_label.setText(f"余额: ¥{result.balance:.2f} ({result.source_label})")
        self._balance_label.setStyleSheet("font-size: 11px; color: #9599A6;")

    def set_dsh_version(self, version: str) -> None:
        self._version_label.setText(f"DSH v{version}")

    def set_mode(self, mode: str) -> None:
        """仅保留下方 mode 常量兼容逻辑。真实显式显示调用 set_preset。"""
        if mode == C.MODE_CODE:
            self._mode_label.setText("[Code]")
            self._mode_label.setStyleSheet("font-size: 11px; color: #7BB8FF; font-weight: 600;")
        else:
            self._mode_label.setText("[Work]")
            self._mode_label.setStyleSheet("font-size: 11px; color: #32F08C; font-weight: 600;")

    def set_preset(self, preset_id: str, preset_name: str) -> None:
        """状态栏常驻显示当前 Agent Preset 中文名（区分 4 种预设）。"""
        # 颜色按 preset 区分，对应 Web UI 原生区分感
        color_map = {
            "standard": "#32F08C",  # 绿（标准）
            "code":     "#7BB8FF",  # 蓝（PTC/编码）
            "minimal":  "#F2C94C",  # 黄（极简）
            "cordis":   "#BB86FC",  # 紫（创造）
        }
        color = color_map.get(preset_id, "#D1D3DB")
        self._tip_last_mode_text = f"[{preset_name}]"
        self._mode_label.setText(f"[{preset_name}]")
        self._mode_label.setStyleSheet(
            f"font-size: 11px; color: {color}; font-weight: 600; padding: 1px 4px;"
        )
        self._tip_last_mode = None  # 临时提示恢复时直接沿用已设置文本
