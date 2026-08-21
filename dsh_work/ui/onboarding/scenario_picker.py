"""场景选择引导（第 7.3 节）。

首次启动的目的不是展示所有功能，而是帮助用户快速开始。
环境检测通过后，弹出场景选择界面——本质上就是三个快捷入口卡片的全屏弹窗形式。

用户选择后，系统做两件事：
1. 切换到对应模式（Work 或 Code）
2. 填充示例提示词到输入框

选择"跳过"的用户进入默认 Work 模式，界面展示空状态引导卡片。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import constants as C
from ...core.defaults import SCENARIO_CARDS


class ScenarioPicker(QDialog):
    """首次启动场景选择引导。

    信号：
        scenario_selected(str, str): (prompt, mode) 场景被选择
        skipped: 用户跳过
    """

    scenario_selected = Signal(str, str)
    skipped = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("DSH Work")
        self.setModal(True)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setFixedSize(560, 420)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 32)
        layout.setSpacing(24)

        # 标题
        title = QLabel("你今天想做什么？")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #32F08C;")
        layout.addWidget(title)

        layout.addSpacing(8)

        # 场景卡片
        for card_data in SCENARIO_CARDS:
            card = self._create_card(card_data)
            layout.addWidget(card)

        layout.addStretch()

        # 跳过按钮
        skip_btn = QPushButton("跳过，直接开始")
        skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skip_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #9599A6; font-size: 12px; }"
            "QPushButton:hover { color: #7BB8FF; }"
        )
        skip_btn.clicked.connect(self._on_skip)
        layout.addWidget(skip_btn, alignment=Qt.AlignmentFlag.AlignCenter)

    def _create_card(self, card_data: dict) -> QFrame:
        """创建场景卡片。"""
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setFixedHeight(64)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        is_code = card_data["mode"] == C.MODE_CODE
        border_color = "#7BB8FF" if is_code else "#32F08C"
        hover_bg = "rgba(123, 184, 255, 0.08)" if is_code else "rgba(50, 240, 140, 0.08)"
        card.setStyleSheet(f"""
            QFrame {{
                background-color: transparent;
                border: 1px solid #B5BDC5;
                border-radius: 10px;
            }}
            QFrame:hover {{
                background-color: {hover_bg};
                border-color: {border_color};
            }}
        """)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        icon = QLabel(card_data["icon"])
        icon.setStyleSheet(f"font-size: 20px; color: {border_color};")
        layout.addWidget(icon)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title = QLabel(card_data["title"])
        title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {border_color};")
        text_layout.addWidget(title)
        mode_label = QLabel(f"→ {'Code' if is_code else 'Work'} 模式")
        mode_label.setStyleSheet("font-size: 11px; color: #666B75;")
        text_layout.addWidget(mode_label)
        layout.addLayout(text_layout, stretch=1)

        prompt = card_data["prompt"]
        mode = card_data["mode"]
        card.mousePressEvent = lambda e: self._on_select(prompt, mode)
        return card

    def _on_select(self, prompt: str, mode: str) -> None:
        self.scenario_selected.emit(prompt, mode)
        self.accept()

    def _on_skip(self) -> None:
        self.skipped.emit()
        self.reject()
