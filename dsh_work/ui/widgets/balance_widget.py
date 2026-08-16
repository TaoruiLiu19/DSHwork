"""余额内联小部件（对话底部）。

显示格式：「本轮 ¥X.XX · 余额 ¥YYY.YY」
- 本轮消耗：来自 SessionState.last_turn_cost（TURN_END 时计算）
- 账户余额：来自 BalanceClient（双通道容错，5 分钟缓存）

余额不可用时文案降级为「本轮 ¥X.XX · 余额查询中…」或「本轮 ¥X.XX · 余额不可用」
并在 tooltip 中给出具体原因与降级通道信息。
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QFrame

from ...api.balance_client import BalanceResult, BalanceSource
from ...utils.logger import get_logger

log = get_logger("ui.balance_widget")


class BalanceWidget(QFrame):
    """对话底部余额小部件。

    数据来源：
      - set_turn_cost(turn_cost: float, session_total: float) —— 每次 TURN_END 调用
      - set_balance(result: BalanceResult) —— 余额查询回调调用

    交互：
      - 单击：refresh_requested 信号，触发强制刷新余额
    """

    refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("BalanceWidget")
        self._turn_cost: float = 0.0
        self._session_total: float = 0.0
        self._balance: BalanceResult | None = None
        self._setup_ui()
        self._apply_style()
        self._refresh_text()

    # ===== UI =====

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self._icon_label = QLabel("💰")
        self._icon_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self._icon_label)

        self._turn_label = QLabel()
        self._turn_label.setObjectName("BalanceTurnLabel")
        self._turn_label.setStyleSheet(
            "color: #2ECC71; font-size: 12px; font-weight: 600;"
        )
        layout.addWidget(self._turn_label)

        self._sep_label = QLabel("·")
        self._sep_label.setStyleSheet("color: #6B7280; font-size: 12px; margin: 0 2px;")
        layout.addWidget(self._sep_label)

        self._balance_label = QLabel()
        self._balance_label.setObjectName("BalanceAmountLabel")
        self._balance_label.setStyleSheet(
            "color: #B3B6BF; font-size: 12px; font-weight: 500;"
        )
        layout.addWidget(self._balance_label)

        layout.addStretch()

        self._source_label = QLabel()
        self._source_label.setObjectName("BalanceSourceLabel")
        self._source_label.setStyleSheet(
            "color: #6B7280; font-size: 10px; padding: 2px 6px;"
            "background-color: rgba(107, 114, 128, 0.08);"
            "border-radius: 6px;"
        )
        layout.addWidget(self._source_label)

        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            "QFrame#BalanceWidget {"
            "  background-color: rgba(255, 255, 255, 0.03);"
            "  border-top: 1px solid rgba(107, 114, 128, 0.15);"
            "  border-bottom: none;"
            "  border-left: none;"
            "  border-right: none;"
            "}"
        )

    # ===== 文本刷新 =====

    def _format_yuan(self, amount: float, decimals: int = 2) -> str:
        """格式化人民币金额。"""
        if amount < 0.01 and amount > 0:
            # 极小金额用科学计数法（避免显示 0.00）
            return f"¥{amount:.4f}"
        return f"¥{amount:.{decimals}f}"

    def _refresh_text(self) -> None:
        """根据当前数据刷新所有标签文本。"""
        # 本轮消耗
        if self._turn_cost > 0:
            turn_text = f"本轮 {self._format_yuan(self._turn_cost)}"
            tooltip_turn = (
                f"本轮对话消耗 {self._format_yuan(self._turn_cost)}\n"
                f"本会话累计 {self._format_yuan(self._session_total)}"
            )
        else:
            turn_text = "本轮 ¥0.00"
            tooltip_turn = (
                "本轮对话暂未产生消耗\n"
                f"本会话累计 {self._format_yuan(self._session_total)}"
            )
        self._turn_label.setText(turn_text)
        self._turn_label.setToolTip(tooltip_turn)

        # 账户余额
        bal = self._balance
        if bal is None:
            self._balance_label.setText("余额查询中…")
            self._balance_label.setStyleSheet(
                "color: #6B7280; font-size: 12px; font-weight: 500; font-style: italic;"
            )
            self._source_label.setText("")
            self._source_label.setVisible(False)
            self.setToolTip("点击立即刷新余额")
        elif not bal.is_available:
            self._balance_label.setText("余额不可用")
            self._balance_label.setStyleSheet(
                "color: #EF4444; font-size: 12px; font-weight: 500;"
            )
            self._source_label.setText("错误")
            self._source_label.setStyleSheet(
                "color: #EF4444; font-size: 10px; padding: 2px 6px;"
                "background-color: rgba(239, 68, 68, 0.08);"
                "border-radius: 6px;"
            )
            self._source_label.setVisible(True)
            err = bal.error or "未知错误"
            self.setToolTip(
                f"余额查询失败：{err}\n"
                "建议：\n"
                "  1. 检查网络连接\n"
                "  2. 右上角设置 → 验证 API Key\n"
                "  3. 点击此处重试"
            )
        else:
            self._balance_label.setText(f"余额 {self._format_yuan(bal.balance)}")
            # 余额低于 5 元时警示色
            if bal.balance < 5.0:
                self._balance_label.setStyleSheet(
                    "color: #F59E0B; font-size: 12px; font-weight: 600;"
                )
            elif bal.balance < 1.0:
                self._balance_label.setStyleSheet(
                    "color: #EF4444; font-size: 12px; font-weight: 700;"
                )
            else:
                self._balance_label.setStyleSheet(
                    "color: #B3B6BF; font-size: 12px; font-weight: 500;"
                )
            # 来源标签
            if bal.source == BalanceSource.DSH_PROXY:
                self._source_label.setText("DSH")
                self._source_label.setStyleSheet(
                    "color: #3B82F6; font-size: 10px; padding: 2px 6px;"
                    "background-color: rgba(59, 130, 246, 0.08);"
                    "border-radius: 6px;"
                )
            elif bal.source == BalanceSource.PLATFORM_DIRECT:
                self._source_label.setText("直连")
                self._source_label.setStyleSheet(
                    "color: #8B5CF6; font-size: 10px; padding: 2px 6px;"
                    "background-color: rgba(139, 92, 246, 0.08);"
                    "border-radius: 6px;"
                )
            else:
                self._source_label.setText("")
            self._source_label.setVisible(bool(self._source_label.text()))

            # Tooltip 含完整信息
            t = time.localtime(bal.queried_at) if bal.queried_at > 0 else None
            time_str = time.strftime("%Y-%m-%d %H:%M", t) if t else "未知时间"
            self.setToolTip(
                f"账户余额 {self._format_yuan(bal.balance)} ({bal.currency})\n"
                f"数据来源：{bal.source_label}\n"
                f"更新时间：{time_str}\n"
                f"点击立即刷新"
            )

    # ===== 对外接口 =====

    def set_turn_cost(self, turn_cost: float, session_total: float = 0.0) -> None:
        """设置本轮消耗与会话累计消耗。"""
        if turn_cost < 0:
            turn_cost = 0.0
        if session_total < 0:
            session_total = 0.0
        self._turn_cost = turn_cost
        self._session_total = session_total
        self._refresh_text()

    def set_balance(self, result: BalanceResult) -> None:
        """设置余额查询结果。"""
        self._balance = result
        self._refresh_text()

    def set_balance_loading(self) -> None:
        """切换到「查询中…」状态。"""
        self._balance = None
        self._refresh_text()

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:
        """点击触发强制刷新。"""
        if ev.button() == Qt.MouseButton.LeftButton:
            self.refresh_requested.emit()
        super().mouseReleaseEvent(ev)
