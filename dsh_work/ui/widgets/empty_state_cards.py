"""空状态快捷入口卡片（第 5.3 节）。

当用户新建会话且尚未发送任何消息时，对话区域不显示空白，
而是展示三个可点击的卡片作为快捷填充入口。
点击卡片后，对应的提示词自动填入输入框。

卡片是纯 UI 组件——三个按钮 + 预定义文本，没有数据源、没有加载逻辑、没有管理界面。
整个功能大约 50 行代码。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSizePolicy,
)

from ...core.defaults import EMPTY_STATE_CARDS


class EmptyStateCards(QWidget):
    """空状态快捷入口卡片。

    信号：
        card_clicked(str, str): 卡片点击，返回 (prompt, mode)
    """

    card_clicked = Signal(str, str)  # prompt, mode

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(24)

        # 提示文案
        hint = QLabel("DSH Work 已就绪。选择一个任务开始，或直接输入。")
        hint.setObjectName("Muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("font-size: 14px;")
        layout.addWidget(hint)

        # 三个卡片横排
        cards_row = QHBoxLayout()
        cards_row.setSpacing(16)

        for card_data in EMPTY_STATE_CARDS:
            card = self._create_card(card_data)
            cards_row.addWidget(card, stretch=1)

        layout.addLayout(cards_row)
        layout.addStretch()

    def _create_card(self, card_data: dict) -> QFrame:
        """创建单个快捷卡片。"""
        card = QFrame()
        card.setObjectName("EmptyStateCard")
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        card.setFixedHeight(120)

        # 配色（purple → TRAE 品牌绿，teal → TRAE 品牌蓝）
        color = card_data.get("color", "purple")
        if color == "purple":
            border_color = "#32F08C"
            hover_bg = "rgba(50, 240, 140, 0.12)"
        else:
            border_color = "#7BB8FF"
            hover_bg = "rgba(123, 184, 255, 0.12)"
        card.setStyleSheet(f"""
            QFrame#EmptyStateCard {{
                background-color: rgba(224, 226, 242, 0.04);
                border: 1px solid {border_color};
                border-radius: 12px;
            }}
            QFrame#EmptyStateCard:hover {{
                background-color: {hover_bg};
                border-color: {border_color};
            }}
        """)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(6)

        # 图标 + 标题
        title = QLabel(f"{card_data['icon']}  {card_data['title']}")
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {border_color};")
        layout.addWidget(title)

        # 描述
        desc = QLabel(card_data["description"])
        desc.setObjectName("Secondary")
        desc.setStyleSheet("font-size: 12px; color: #9599A6;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addStretch()

        # 点击事件
        prompt = card_data["prompt"]
        mode = card_data["mode"]

        def on_click():
            self.card_clicked.emit(prompt, mode)

        card.mousePressEvent = lambda e: on_click()
        return card
