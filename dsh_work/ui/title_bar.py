"""极简顶栏 + 滑块式 Work/Code 切换（TRAE Work 风格）。

高度 40px：
- 左侧：工作区路径（可点击切换）
- 中部：滑块式 Work/Code 切换 + 模型选择器
- 右侧：新建会话按钮

滑块外观类似 iOS Toggle：胶囊形容器内有一个可滑动的指示块，
点击左半边滑到 Work，点击右半边滑到 Code。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, Property, QTimer
from PySide6.QtGui import QFont, QPainter, QColor, QBrush, QPen
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QComboBox,
    QSizePolicy,
)

from .. import constants as C


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
    "bg_secondary": "#0D0D0E",
    "divider": "#B5BDC5",
    "input_bg": "#B5BDC5",
    "input_border": "#B5BDC5",
    "border": "#B5BDC5",
}


def _palette() -> dict:
    tc = _theme_colors()
    if tc:
        return {
            "bg_secondary": tc.bg_secondary or _FALLBACK["bg_secondary"],
            "divider": tc.divider or _FALLBACK["divider"],
            "input_bg": tc.input_bg or _FALLBACK["input_bg"],
            "input_border": tc.input_border or _FALLBACK["input_border"],
            "border": tc.border or _FALLBACK["border"],
        }
    return dict(_FALLBACK)


class ModeSlider(QWidget):
    """滑块式 Work/Code 切换控件。

    胶囊形容器（120×28），内部分为左右两半：
    - 左半：Work（绿色文字）
    - 右半：Code（蓝色文字）
    当前激活的一侧有半透明色块背景，可动画滑动。

    点击任意一侧即可切换，带平滑滑动动画。
    """

    mode_changed = Signal(str)  # "work" / "code"

    _TRACK_W = 120
    _TRACK_H = 28
    _HALF_W = _TRACK_W // 2  # 60

    # 品牌色
    _WORK_COLOR = "#32F08C"
    _CODE_COLOR = "#7BB8FF"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ModeSlider")
        self.setFixedSize(self._TRACK_W, self._TRACK_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode = C.MODE_WORK
        self._slider_pos = 0.0  # 0.0 = Work (left), 1.0 = Code (right)

        # 滑动动画
        self._anim = QPropertyAnimation(self, b"slider_pos")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_slider_pos(self) -> float:
        return self._slider_pos

    def set_slider_pos(self, pos: float) -> None:
        self._slider_pos = max(0.0, min(1.0, pos))
        self.update()

    slider_pos = Property(float, get_slider_pos, set_slider_pos)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        p = _palette()
        track_bg = QColor(p["input_bg"])
        track_border = QColor(p["border"])

        # 1. 绘制胶囊背景
        from PySide6.QtCore import QRectF
        track = QRectF(0, 0, w, h)
        painter.setBrush(QBrush(track_bg))
        painter.setPen(QPen(track_border))
        painter.drawRoundedRect(track, h / 2, h / 2)

        # 2. 绘制滑动指示块（半透明色块）
        indicator_x = self._slider_pos * self._HALF_W
        indicator_rect = QRectF(indicator_x + 2, 2, self._HALF_W - 4, h - 4)

        if self._mode == C.MODE_WORK:
            indicator_color = QColor(50, 240, 140, 50)   # #32F08C alpha=50
            text_active_color = QColor("#32F08C")
            text_inactive_color = QColor("#666B75")
        else:
            indicator_color = QColor(123, 184, 255, 50)  # #7BB8FF alpha=50
            text_active_color = QColor("#7BB8FF")
            text_inactive_color = QColor("#666B75")

        painter.setBrush(QBrush(indicator_color))
        painter.drawRoundedRect(indicator_rect, (h - 4) // 2, (h - 4) // 2)

        # 3. 绘制文字 "Work" 和 "Code"
        font = QFont("Microsoft YaHei", 8, QFont.Weight.DemiBold)
        painter.setFont(font)

        # Work (left half)
        is_work = self._mode == C.MODE_WORK
        painter.setPen(QColor(self._WORK_COLOR) if is_work else QColor("#666B75"))
        work_rect = QRectF(0, 0, self._HALF_W, h)
        painter.drawText(work_rect, Qt.AlignmentFlag.AlignCenter, "Work")

        # Code (right half)
        painter.setPen(QColor(self._CODE_COLOR) if not is_work else QColor("#666B75"))
        code_rect = QRectF(self._HALF_W, 0, self._HALF_W, h)
        painter.drawText(code_rect, Qt.AlignmentFlag.AlignCenter, "Code")

        painter.end()

    def mousePressEvent(self, event) -> None:
        """点击左半边→Work，点击右半边→Code。"""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        if x < self._HALF_W:
            self._set_mode(C.MODE_WORK)
        else:
            self._set_mode(C.MODE_CODE)

    def _set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        # 动画滑动
        target = 0.0 if mode == C.MODE_WORK else 1.0
        self._anim.setStartValue(self._slider_pos)
        self._anim.setEndValue(target)
        self._anim.start()
        self.mode_changed.emit(mode)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """外部设置模式（无动画，立即跳转）。"""
        if mode == self._mode:
            return
        self._mode = mode
        self._slider_pos = 0.0 if mode == C.MODE_WORK else 1.0
        self.update()


class TitleBar(QWidget):
    """极简顶栏：工作区 + Agent 预设下拉 + 模型选择。"""

    preset_changed = Signal(str)     # preset id: "standard" / "code" / "minimal" / "cordis"
    model_changed = Signal(str)
    workspace_clicked = Signal()

    # DSH 原生内置 agent preset 列表（匹配 DSH Web UI 顶部下拉）
    # 目录: dsh/config/agent-presets/<id>/preset.yml → name
    PRESETS: list[tuple[str, str]] = [
        ("standard", "标准模式"),
        ("code",     "PTC 模式"),
        ("minimal",  "极简模式"),
        ("cordis",   "创造模式"),
    ]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(40)
        self._setup_ui()
        self.apply_theme()

        try:
            from .theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self.apply_theme)
        except Exception:
            pass

    def apply_theme(self, theme=None) -> None:
        """刷新样式表为当前主题配色。"""
        p = _palette()
        self.setStyleSheet(
            "QWidget#TopBar {"
            f"  background-color: {p['bg_secondary']};"
            f"  border-bottom: 1px solid {p['divider']};"
            "}"
        )
        combo_style = (
            "QComboBox {"
            f"  background-color: {p['input_bg']};"
            f"  border: 1px solid {p['input_border']};"
            "  border-radius: 6px;"
            "  padding: 4px 8px;"
            "  font-size: 12px;"
            "  color: #D1D3DB;"
            "}"
            "QComboBox::drop-down { border: none; }"
            "QComboBox QAbstractItemView {"
            "  background-color: #1A1B1D;"
            "  color: #D1D3DB;"
            "  selection-background-color: rgba(120, 119, 198, 0.2);"
            "}"
        )
        if getattr(self, "_preset_combo", None) is not None:
            self._preset_combo.setStyleSheet(combo_style)
        if getattr(self, "_model_combo", None) is not None:
            self._model_combo.setStyleSheet(combo_style)

    def _setup_ui(self) -> None:

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(10)

        # 左侧：工作区路径
        self._workspace_label = QLabel("未设置工作区")
        self._workspace_label.setStyleSheet("font-size: 12px; color: #666B75;")
        self._workspace_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._workspace_label.mousePressEvent = lambda e: self.workspace_clicked.emit()
        layout.addWidget(self._workspace_label)

        layout.addStretch()

        # 中部：Agent Preset 选择器（替代旧的 Work/Code 二元滑块）
        self._preset_combo = QComboBox()
        self._preset_combo.setMinimumWidth(140)
        for pid, pname in TitleBar.PRESETS:
            self._preset_combo.addItem(pname, pid)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_index_changed)
        layout.addWidget(self._preset_combo)

        # 右侧：模型选择器
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(180)
        self._model_combo.setPlaceholderText("选择模型")
        self._model_combo.currentTextChanged.connect(self.model_changed)
        layout.addWidget(self._model_combo)

        layout.addStretch()

    def _on_preset_index_changed(self, index: int) -> None:
        pid = self._preset_combo.itemData(index)
        if pid:
            self.preset_changed.emit(str(pid))

    def set_workspace(self, workspace: str) -> None:
        from pathlib import Path
        if workspace:
            name = Path(workspace).name or workspace
            self._workspace_label.setText(f"📁 {name}")
            self._workspace_label.setStyleSheet("font-size: 12px; color: #9599A6;")
            self._workspace_label.setToolTip(workspace)
        else:
            self._workspace_label.setText("未设置工作区")
            self._workspace_label.setStyleSheet("font-size: 12px; color: #666B75;")

    def set_preset(self, preset_id: str) -> None:
        """按 id 切换当前 preset。"""
        for i, (pid, _) in enumerate(TitleBar.PRESETS):
            if pid == preset_id:
                if self._preset_combo.currentIndex() != i:
                    self._preset_combo.blockSignals(True)
                    self._preset_combo.setCurrentIndex(i)
                    self._preset_combo.blockSignals(False)
                return

    def set_models(self, models: list[str], current: str = "") -> None:
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItems(models)
        if current and current in models:
            self._model_combo.setCurrentText(current)
        self._model_combo.blockSignals(False)

    def set_presets(self, presets: list[str], current: str = "") -> None:
        self._preset_combo.blockSignals(True)
        self._preset_combo.clear()
        self._preset_combo.addItems(presets)
        if current and current in presets:
            self._preset_combo.setCurrentText(current)
        self._preset_combo.blockSignals(False)
