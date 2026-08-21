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

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QWidget,
)

from .. import constants as C
from ..api import CompatibilityMode
from ..core.session_manager import AgentStatus, ContextUsage


def _theme_colors():
    try:
        from .theme.theme_manager import ThemeManager
        tm = ThemeManager()
        theme = tm.current
        if theme and theme.colors:
            return theme.colors
    except Exception:
        pass
    return None


_FALLBACK = {
    "divider": "rgba(255,255,255,0.06)",
    "input_bg": "#232324",
    "text": "#F9FAFB",
    "text2": "#CFD3D6",
    "muted": "#ADB2B8",
    "accent": "#679EFE",
    "accent_secondary": "#679EFE",
    "success": "#22C55E",
    "warning": "#F59E0B",
    "error": "#F25A5A",
}


def _palette() -> dict:
    tc = _theme_colors()
    if tc:
        return {
            "divider": tc.divider or _FALLBACK["divider"],
            "input_bg": tc.input_bg or _FALLBACK["input_bg"],
            "text": tc.text_primary or _FALLBACK["text"],
            "text2": tc.text_secondary or _FALLBACK["text2"],
            "muted": tc.text_muted or _FALLBACK["muted"],
            "accent": tc.accent or _FALLBACK["accent"],
            "accent_secondary": tc.accent_secondary or _FALLBACK["accent_secondary"],
            "success": tc.success or _FALLBACK["success"],
            "warning": tc.warning or _FALLBACK["warning"],
            "error": tc.error or _FALLBACK["error"],
        }
    return dict(_FALLBACK)


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
        from PySide6.QtGui import QBrush, QPainter
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
        self.apply_theme()
        # 主题切换时刷新（避免硬编码色在深浅主题下不可读）
        try:
            from .theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self.apply_theme)
        except Exception:
            pass

    def apply_theme(self, theme=None) -> None:
        """刷新状态栏各标签为当前主题 token 色。"""
        p = _palette()
        for lbl in (self._conn_label, self._agent_label, self._context_label,
                    self._token_label, self._balance_label):
            if lbl is not None and lbl.text() and not self._is_colored_label(lbl):
                lbl.setStyleSheet(f"font-size: 11px; color: {p['text2']};")
        if getattr(self, "_version_label", None) is not None:
            self._version_label.setStyleSheet(f"font-size: 11px; color: {p['muted']};")
        if getattr(self, "_mode_label", None) is not None:
            self._mode_label.setStyleSheet(f"font-size: 11px; color: {p['accent']}; font-weight: 600;")
        # 连接状态色（按当前状态重刷）
        cur = getattr(self, "_conn_text", "")
        if cur:
            self._apply_conn_style(cur)

    @staticmethod
    def _is_colored_label(lbl: QLabel) -> bool:
        """是否已被语义色（连接/模式等）显式着色，避免 apply_theme 覆盖。"""
        return bool(lbl.property("semantic_color"))

    def _apply_conn_style(self, state: str) -> None:
        p = _palette()
        color = {
            "已连接": p["success"],
            "兼容模式": p["warning"],
            "离线": p["error"],
        }.get(state, p["text2"])
        self._conn_label.setStyleSheet(f"font-size: 11px; color: {color};")
        self._conn_label.setProperty("semantic_color", True)

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
        p = _palette()
        sep = QLabel("│")
        sep.setStyleSheet(f"color: {p['divider']};")
        return sep

    def set_connection_status(self, mode: CompatibilityMode) -> None:
        """设置连接状态。"""
        p = _palette()
        self._conn_text = {
            CompatibilityMode.FULL: "已连接",
            CompatibilityMode.DEGRADED: "兼容模式",
        }.get(mode, "离线")
        color = {
            CompatibilityMode.FULL: p["success"],
            CompatibilityMode.DEGRADED: p["warning"],
        }.get(mode, p["error"])
        self._conn_indicator.set_status(color, self._conn_text)
        self._conn_label.setText(self._conn_text)
        self._apply_conn_style(self._conn_text)

    def set_agent_status(self, status: AgentStatus, turn: int = 0, step: int = 0) -> None:
        """设置 Agent 状态。"""
        p = _palette()
        if status == AgentStatus.RUNNING:
            self._agent_label.setText(f"Running · Turn {turn}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {p['accent']};")
        elif status == AgentStatus.THINKING:
            self._agent_label.setText(f"Thinking · Turn {turn}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {p['accent_secondary']};")
        elif status == AgentStatus.TOOL_EXECUTING:
            self._agent_label.setText(f"Tool · Step {step}")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {p['success']};")
        elif status == AgentStatus.ERROR:
            self._agent_label.setText("Error")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {p['error']};")
        else:
            self._agent_label.setText("Idle")
            self._agent_label.setStyleSheet(f"font-size: 11px; color: {p['muted']};")

    def set_context_usage(self, context: ContextUsage) -> None:
        """设置上下文容量。"""
        p = _palette()
        used_k = context.used_tokens // 1000
        limit_k = context.limit_tokens // 1000
        self._context_label.setText(f"上下文 {used_k}k/{limit_k}k")
        self._context_bar.setValue(context.percentage)
        color_map = {
            "accent": p["accent"],
            "warning": p["warning"],
            "error": p["error"],
        }
        color = color_map.get(context.color_key, p["accent"])
        self._context_bar.setStyleSheet(
            f"QProgressBar {{ background-color: {p['input_bg']}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background-color: {color}; border-radius: 3px; }}"
        )

    def set_token_usage(self, input_tokens: int, output_tokens: int) -> None:
        """设置 Token 用量。"""
        p = _palette()
        self._token_label.setText(f"Token: ↑{input_tokens} ↓{output_tokens}")
        self._token_label.setStyleSheet(f"font-size: 11px; color: {p['text2']};")

    def set_balance(self, result) -> None:
        """设置余额显示。"""
        p = _palette()
        if not result.is_available:
            self._balance_label.setText("余额: 不可用")
            self._balance_label.setStyleSheet(f"font-size: 11px; color: {p['muted']};")
            return
        self._balance_label.setText(f"余额: ¥{result.balance:.2f} ({result.source_label})")
        self._balance_label.setStyleSheet(f"font-size: 11px; color: {p['text2']};")

    def set_dsh_version(self, version: str) -> None:
        self._version_label.setText(f"DSH v{version}")

    def set_mode(self, mode: str) -> None:
        """仅保留下方 mode 常量兼容逻辑。真实显式显示调用 set_preset。"""
        p = _palette()
        if mode == C.MODE_CODE:
            self._mode_label.setText("[Code]")
            self._mode_label.setStyleSheet(f"font-size: 11px; color: {p['accent']}; font-weight: 600;")
        else:
            self._mode_label.setText("[Work]")
            self._mode_label.setStyleSheet(f"font-size: 11px; color: {p['success']}; font-weight: 600;")

    def set_preset(self, preset_id: str, preset_name: str) -> None:
        """状态栏常驻显示当前 Agent Preset 中文名（区分 4 种预设）。"""
        p = _palette()
        # 颜色按 preset 区分（token 色系，深浅主题均可读）
        color_map = {
            "standard": p["success"],   # 绿（标准）
            "code":     p["accent"],    # 蓝（PTC/编码）
            "minimal":  p["warning"],   # 黄（极简）
            "cordis":   p["accent_secondary"],  # 紫（创造）
        }
        color = color_map.get(preset_id, p["text2"])
        self._tip_last_mode_text = f"[{preset_name}]"
        self._mode_label.setText(f"[{preset_name}]")
        self._mode_label.setStyleSheet(
            f"font-size: 11px; color: {color}; font-weight: 600; padding: 1px 4px;"
        )
        self._tip_last_mode = None  # 临时提示恢复时直接沿用已设置文本
