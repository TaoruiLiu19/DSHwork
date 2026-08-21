"""顶栏（对齐 DSH Web 版视觉，保留窗口级控制）。

高度 44px：
- 左侧：DSH Work 品牌 + 工作区路径（可点击切换）
- 右侧：Agent 预设下拉 + 模型选择器 + 主题切换 + 设置

配色全部取自当前主题 token，不再硬编码。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)


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
    "bg_secondary": "#1B1B1C",
    "divider": "rgba(255,255,255,0.06)",
    "input_bg": "#232324",
    "input_border": "rgba(255,255,255,0.12)",
    "border": "rgba(255,255,255,0.12)",
    "text": "#F9FAFB",
    "text2": "#CFD3D6",
    "muted": "#ADB2B8",
    "accent": "#679EFE",
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
            "text": tc.text_primary or _FALLBACK["text"],
            "text2": tc.text_secondary or _FALLBACK["text2"],
            "muted": tc.text_muted or _FALLBACK["muted"],
            "accent": tc.accent or _FALLBACK["accent"],
        }
    return dict(_FALLBACK)


class TitleBar(QWidget):
    """顶栏：品牌 + 工作区 + Agent 预设 + 模型选择 + 主题/设置入口。"""

    preset_changed = Signal(str)     # preset id: "standard" / "code" / "minimal" / "cordis"
    model_changed = Signal(str)
    workspace_clicked = Signal()
    theme_clicked = Signal()
    settings_clicked = Signal()

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
        self.setFixedHeight(44)
        self._setup_ui()
        self.apply_theme()

        try:
            from .theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self.apply_theme)
        except Exception:
            pass

    def apply_theme(self, theme=None) -> None:
        """刷新样式表为当前主题配色（对齐 Web token，无硬编码色）。"""
        p = _palette()
        self.setStyleSheet(
            "QWidget#TopBar {"
            f"  background-color: {p['bg_secondary']};"
            f"  border-bottom: 1px solid {p['divider']};"
            "}"
        )
        # 品牌
        if getattr(self, "_brand_label", None) is not None:
            self._brand_label.setStyleSheet(
                f"color: {p['text']}; font-size: 14px; font-weight: 700;"
            )
        # 工作区
        if getattr(self, "_workspace_label", None) is not None:
            self._workspace_label.setStyleSheet(
                f"font-size: 12px; color: {p['muted']};"
            )
        # 下拉框
        combo_style = (
            "QComboBox {"
            f"  background-color: {p['input_bg']};"
            f"  border: 1px solid {p['input_border']};"
            "  border-radius: 6px;"
            "  padding: 4px 10px;"
            "  font-size: 12px;"
            f"  color: {p['text']};"
            "}"
            "QComboBox::drop-down { border: none; width: 18px; }"
            "QComboBox QAbstractItemView {"
            f"  background-color: {p['input_bg']};"
            f"  color: {p['text']};"
            f"  selection-background-color: {p['accent']};"
            f"  selection-color: {p['text']};"
            "  border: 1px solid " + p["border"] + ";"
            "}"
        )
        if getattr(self, "_preset_combo", None) is not None:
            self._preset_combo.setStyleSheet(combo_style)
        if getattr(self, "_model_combo", None) is not None:
            self._model_combo.setStyleSheet(combo_style)
        # 图标按钮（主题/设置）
        icon_style = (
            "QPushButton {"
            "  background-color: transparent;"
            "  border: none;"
            "  border-radius: 6px;"
            f"  color: {p['muted']};"
            "  font-size: 14px;"
            "  padding: 4px 8px;"
            "}"
            "QPushButton:hover {"
            f"  background-color: {p['input_bg']};"
            f"  color: {p['text']};"
            "}"
        )
        for name in ("_theme_btn", "_settings_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.setStyleSheet(icon_style)

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)

        # 品牌
        self._brand_label = QLabel("DSH Work")
        layout.addWidget(self._brand_label)

        # 工作区路径（可点击）
        self._workspace_label = QLabel("未设置工作区")
        self._workspace_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._workspace_label.mousePressEvent = lambda e: self.workspace_clicked.emit()
        layout.addWidget(self._workspace_label)

        layout.addStretch()

        # Agent Preset 选择器
        self._preset_combo = QComboBox()
        self._preset_combo.setMinimumWidth(120)
        for pid, pname in TitleBar.PRESETS:
            self._preset_combo.addItem(pname, pid)
        self._preset_combo.currentIndexChanged.connect(self._on_preset_index_changed)
        layout.addWidget(self._preset_combo)

        # 模型选择器
        self._model_combo = QComboBox()
        self._model_combo.setMinimumWidth(170)
        self._model_combo.setPlaceholderText("选择模型")
        self._model_combo.currentTextChanged.connect(self.model_changed)
        layout.addWidget(self._model_combo)

        # 主题切换 / 设置入口
        self._theme_btn = QPushButton("◐")
        self._theme_btn.setToolTip("切换主题 (Ctrl+.)")
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.clicked.connect(self.theme_clicked)
        layout.addWidget(self._theme_btn)

        self._settings_btn = QPushButton("⚙")
        self._settings_btn.setToolTip("设置 (Ctrl+,)")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.clicked.connect(self.settings_clicked)
        layout.addWidget(self._settings_btn)

    def _on_preset_index_changed(self, index: int) -> None:
        pid = self._preset_combo.itemData(index)
        if pid:
            self.preset_changed.emit(str(pid))

    def set_workspace(self, workspace: str) -> None:
        from pathlib import Path
        p = _palette()
        if workspace:
            name = Path(workspace).name or workspace
            self._workspace_label.setText(f"📁 {name}")
            self._workspace_label.setStyleSheet(f"font-size: 12px; color: {p['text2']};")
            self._workspace_label.setToolTip(workspace)
        else:
            self._workspace_label.setText("未设置工作区")
            self._workspace_label.setStyleSheet(f"font-size: 12px; color: {p['muted']};")

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
