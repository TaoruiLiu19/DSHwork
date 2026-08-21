"""用量与消耗面板。

包含四个标签页：
- 概览：统计卡片 + 调用明细表
- 模型价格：各模型层级峰谷定价编辑
- 计费设置：日期范围筛选 + 按层级聚合 + CSV 导出
- 设置：API Key 临时记忆、主题切换、DSH 路径
"""

from __future__ import annotations

import calendar
import re
import time
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.usage_tracker import PRICE_MODELS, UsageTracker
from ...utils.logger import get_logger
from ..theme.theme_manager import ThemeManager

log = get_logger("ui.usage_panel")


# ===== 模块级颜色 Token（由 _apply_theme_colors 填充）=====

_ACCENT: str = "#7BB8FF"
_WARNING: str = "#FF8F3D"
_ERROR: str = "#FF5C5C"
_TEXT: str = "#E0E2F2"
_MUTED: str = "#9599A6"
_DIM: str = "#6B6F7D"
_TRAE_BLUE: str = "#387BFF"
_BG_PRIMARY: str = "#0f1118"
_BG_SECONDARY: str = "#171a26"
_BG_HOVER: str = "#1e2233"
_CARD_BG: str = "#191c2a"
_CARD_BORDER: str = "rgba(224, 226, 242, 0.10)"

# 新增行 Token
_SOFT_BG: str = "rgba(255, 255, 255, 0.04)"
_HARD_BG: str = "rgba(224, 226, 242, 0.16)"
_INPUT_BORDER: str = "rgba(224, 226, 242, 0.12)"
_BTN_BORDER: str = "rgba(224, 226, 242, 0.14)"
_PANEL_BORDER: str = "rgba(224, 226, 242, 0.10)"
_TAB_BORDER: str = "rgba(224, 226, 242, 0.08)"
_CELL_DIVIDER: str = "rgba(224, 226, 242, 0.08)"
_GRIDLINE: str = "rgba(224, 226, 242, 0.06)"
_TAB_PANE_BG: str = "rgba(30,33,42,0.5)"
_CODE_INPUT_BORDER: str = "rgba(224, 226, 242, 0.12)"


# ===== 工具函数 =====

def _rgba_from_hex(hex_color: str, alpha: float = 1.0) -> str:
    """将 #RRGGBB / #RRGGBBAA 转 rgba(r,g,b,a) 字符串。"""
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"
    if len(h) == 8:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = int(h[6:8], 16) / 255.0
        return f"rgba({r},{g},{b},{round(a * alpha, 3)})"
    return hex_color


def _apply_theme_colors(theme) -> None:
    """从 ThemeColors dataclass 填充模块级颜色 Token。"""
    global _ACCENT, _WARNING, _ERROR, _TEXT, _MUTED, _DIM, _TRAE_BLUE
    global _BG_PRIMARY, _BG_SECONDARY, _BG_HOVER, _CARD_BG, _CARD_BORDER
    global _SOFT_BG, _HARD_BG, _INPUT_BORDER, _BTN_BORDER, _PANEL_BORDER
    global _TAB_BORDER, _CELL_DIVIDER, _GRIDLINE, _TAB_PANE_BG, _CODE_INPUT_BORDER

    c = theme.colors

    _ACCENT = c.accent or "#7BB8FF"
    _WARNING = c.warning or "#FF8F3D"
    _ERROR = c.error or "#FF5C5C"
    _TEXT = c.text_primary or "#E0E2F2"
    _MUTED = c.text_secondary or "#9599A6"
    _DIM = c.text_muted or "#6B6F7D"
    _TRAE_BLUE = c.accent_secondary or "#387BFF"
    _BG_PRIMARY = c.bg_primary or "#0f1118"
    _BG_SECONDARY = c.bg_secondary or "#171a26"
    _BG_HOVER = c.bg_hover or "#1e2233"

    # _CARD_BG = bg_secondary rgba 化（alpha=0.75）
    _CARD_BG = _rgba_from_hex(_BG_SECONDARY, 0.75)
    _CARD_BORDER = c.border or "rgba(224, 226, 242, 0.10)"

    # 新增行 Token
    _SOFT_BG = c.input_bg if c.input_bg else c.bg_hover
    _HARD_BG = c.border_light or "rgba(224, 226, 242, 0.16)"
    _INPUT_BORDER = c.input_border if c.input_border else c.border
    _BTN_BORDER = c.btn_border if c.btn_border else c.border_light
    _PANEL_BORDER = c.border or "rgba(224, 226, 242, 0.10)"
    _TAB_BORDER = c.tab_border if c.tab_border else c.border
    _CELL_DIVIDER = c.divider if c.divider else c.border
    _GRIDLINE = c.gridline if c.gridline else c.border

    # _TAB_PANE_BG：暗色用半透明，亮色用 bg_card 或 bg_secondary
    if theme.is_dark:
        _TAB_PANE_BG = "rgba(30,33,42,0.5)"
    else:
        _TAB_PANE_BG = c.bg_card or c.bg_secondary or _BG_SECONDARY

    _CODE_INPUT_BORDER = c.input_border if c.input_border else c.border_light


def _fmt_money(amount: float) -> str:
    """格式化金额字符串。

    - <= 0 → "0.00"
    - >0 且 <0.01 → "<0.01"
    - < 1 → ".2f" 精确到 0.000 分（保留 3 位小数）
    - >= 1 → ".2f" 保留 2 位小数
    """
    if amount <= 0:
        return "0.00"
    if amount < 0.01:
        return "<0.01"
    if amount < 1:
        return f"{amount:.3f}"
    return f"{amount:.2f}"


def _fmt_int(value: int) -> str:
    """千分位格式化整数。"""
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def _bj_month_range(year: int, month: int) -> tuple[int, int]:
    """返回北京时间该月 [月初 00:00, 月末 23:59:59] 的 UTC 毫秒范围。"""
    bj_tz_offset_ms = 8 * 3600 * 1000
    last_day = calendar.monthrange(year, month)[1]
    # 北京时间 Y-M-1 00:00:00 → UTC 减 8 小时
    start_struct = time.strptime(f"{year}-{month:02d}-01 00:00:00", "%Y-%m-%d %H:%M:%S")
    start_utc_ms = int(time.mktime(start_struct)) * 1000 - bj_tz_offset_ms
    # 北京时间 Y-M-last_day 23:59:59 → UTC 减 8 小时
    end_struct = time.strptime(f"{year}-{month:02d}-{last_day:02d} 23:59:59", "%Y-%m-%d %H:%M:%S")
    end_utc_ms = int(time.mktime(end_struct)) * 1000 - bj_tz_offset_ms
    return start_utc_ms, end_utc_ms


def _card_stylesheet() -> str:
    """生成 _Card 组件的 QSS（引用模块颜色 Token）。"""
    return (
        f"QFrame#UsageCard {{"
        f"  background: {_CARD_BG};"
        f"  border: 1px solid {_CARD_BORDER};"
        f"  border-radius: 12px;"
        f"}}"
    )


# ===== _Card 组件 =====

class _Card(QFrame):
    """概览统计卡片：标题 + 主数值 + 副标题。"""

    def __init__(self, title: str, value: str = "0", sub: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("UsageCard")
        self.setMinimumHeight(100)
        self._title_text = title
        self._value_text = value
        self._sub_text = sub

        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        self._title_label = QLabel(self._title_text)
        self._title_label.setObjectName("CardTitle")
        self._title_label.setStyleSheet(
            f"color: {_MUTED}; font-size: 12px; font-weight: 500;"
        )
        layout.addWidget(self._title_label)

        self._value_label = QLabel(self._value_text)
        self._value_label.setObjectName("CardValue")
        self._value_label.setStyleSheet(
            f"color: {_TEXT}; font-size: 22px; font-weight: 700;"
        )
        layout.addWidget(self._value_label)

        self._sub_label = QLabel(self._sub_text)
        self._sub_label.setObjectName("CardSub")
        self._sub_label.setStyleSheet(
            f"color: {_DIM}; font-size: 11px; margin-top: 2px;"
        )
        layout.addWidget(self._sub_label)
        layout.addStretch()

    def set_value(self, value: str) -> None:
        self._value_text = value
        self._value_label.setText(value)

    def set_sub(self, sub: str) -> None:
        self._sub_text = sub
        self._sub_label.setText(sub)

    def set_value_color(self, color: str) -> None:
        self._value_label.setStyleSheet(
            f"color: {color}; font-size: 22px; font-weight: 700;"
        )

    def apply_theme(self, theme=None) -> None:
        """应用主题到卡片样式与内部标签。"""
        self.setStyleSheet(_card_stylesheet())
        self._title_label.setStyleSheet(
            f"color: {_MUTED}; font-size: 12px; font-weight: 500;"
        )
        self._value_label.setStyleSheet(
            f"color: {_TEXT}; font-size: 22px; font-weight: 700;"
        )
        self._sub_label.setStyleSheet(
            f"color: {_DIM}; font-size: 11px; margin-top: 2px;"
        )


# ===== _OverviewTab =====

class _OverviewTab(QWidget):
    """用量概览页：筛选 + 4 张统计卡片 + 调用明细表。"""

    _PRESETS = ["今日", "近 7 天", "近 30 天", "全部"]

    def __init__(self, tracker: UsageTracker, parent: QWidget | None = None, default_model: str = ""):
        super().__init__(parent)
        self._tracker = tracker
        # DSH 默认模型名（用量记录 model 为空的显示/计价兜底）
        self._default_model = default_model or ""
        self._current_preset_idx: int = 2  # 默认「近 30 天」
        self._setup_ui()

        # 连接 tracker 变化通知（优先用 add_listener 回调机制）
        try:
            if hasattr(self._tracker, "add_listener"):
                self._tracker.add_listener(self.refresh)
        except Exception:
            pass

    # ---- UI ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # 标题行 + 筛选
        header_row = QHBoxLayout()
        header_row.setSpacing(12)

        title_lbl = QLabel("用量概览")
        title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
        )
        header_row.addWidget(title_lbl)
        header_row.addStretch()

        filter_lbl = QLabel("时间范围：")
        filter_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        header_row.addWidget(filter_lbl)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(self._PRESETS)
        self._filter_combo.setCurrentIndex(self._current_preset_idx)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        header_row.addWidget(self._filter_combo)

        header_wrap = QWidget()
        header_wrap.setLayout(header_row)
        layout.addWidget(header_wrap)

        # 卡片 2x2 网格
        cards_grid = QGridLayout()
        cards_grid.setSpacing(12)

        self._card_today = _Card("今日消耗", "0.00", "¥ 人民币 · 自动计价")
        self._card_month = _Card("30 日累计", "0.00", "¥ 人民币 · 近 30 天")
        self._card_calls = _Card("总消息数", "0", "累计对话轮次")
        self._card_tokens = _Card("总 Token 量", "0", "输入 + 缓存命中 + 输出")

        cards_grid.addWidget(self._card_today, 0, 0)
        cards_grid.addWidget(self._card_month, 0, 1)
        cards_grid.addWidget(self._card_calls, 1, 0)
        cards_grid.addWidget(self._card_tokens, 1, 1)
        layout.addLayout(cards_grid)

        # 明细表
        table_title = QLabel("调用明细")
        table_title.setStyleSheet(
            f"color: {_TEXT}; font-size: 14px; font-weight: 600; margin-top: 4px;"
        )
        layout.addWidget(table_title)

        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "时间", "模型", "层级", "输入", "输出", "费用 $"
        ])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setColumnWidth(0, 150)
        self._table.setColumnWidth(1, 180)
        self._table.setColumnWidth(2, 80)
        self._table.setColumnWidth(3, 90)
        self._table.setColumnWidth(4, 90)
        layout.addWidget(self._table, stretch=1)

        self.apply_theme()
        QTimer.singleShot(50, self.refresh)

    # ---- 事件 ----

    def _on_filter_changed(self, idx: int) -> None:
        self._current_preset_idx = idx
        self.refresh()

    def _preset_time_range(self, preset_idx: int) -> tuple[int | None, int | None]:
        """根据筛选预设返回 (start_ms, end_ms)，None 表示不限。"""
        now_ms = int(time.time() * 1000)
        bj_now_ms = now_ms + 8 * 3600 * 1000
        bj_struct = time.gmtime(bj_now_ms / 1000)
        bj_year, bj_month, bj_mday = bj_struct.tm_year, bj_struct.tm_mon, bj_struct.tm_mday

        if preset_idx == 0:  # 今日
            day_start = time.strptime(
                f"{bj_year}-{bj_month:02d}-{bj_mday:02d} 00:00:00", "%Y-%m-%d %H:%M:%S"
            )
            start_ms = int(time.mktime(day_start)) * 1000 - 8 * 3600 * 1000
            return start_ms, now_ms
        if preset_idx == 1:  # 近 7 天
            return now_ms - 7 * 86400 * 1000, now_ms
        if preset_idx == 2:  # 近 30 天
            return now_ms - 30 * 86400 * 1000, now_ms
        return None, None

    # ---- 刷新 ----

    def refresh(self) -> None:
        """刷新卡片数据 + 表格。"""
        self._refresh_cards()
        self._refresh_table()

    def _refresh_cards(self) -> None:
        try:
            # 今日卡片
            today_start, _ = self._preset_time_range(0)
            today_summary = self._tracker.get_summary(start_ms=today_start)
            today_cost = today_summary.get("total_cost", 0.0)
            self._card_today.set_value(f"¥ {_fmt_money(today_cost)}")
            if today_cost < 1:
                self._card_today.set_value_color(_TRAE_BLUE)
            elif today_cost < 10:
                self._card_today.set_value_color(_TEXT)
            elif today_cost < 50:
                self._card_today.set_value_color(_WARNING)
            else:
                self._card_today.set_value_color(_ERROR)

            # 30 日累计
            month_start, _ = self._preset_time_range(2)
            month_summary = self._tracker.get_summary(start_ms=month_start)
            month_cost = month_summary.get("total_cost", 0.0)
            self._card_month.set_value(f"¥ {_fmt_money(month_cost)}")

            # 当前筛选范围的消息数 / Token
            s, e = self._preset_time_range(self._current_preset_idx)
            sel_summary = self._tracker.get_summary(start_ms=s, end_ms=e)
            calls = sel_summary.get("total_calls", 0)
            self._card_calls.set_value(_fmt_int(calls))

            total_tok = (
                sel_summary.get("total_input_tokens", 0)
                + sel_summary.get("total_cache_read_tokens", 0)
                + sel_summary.get("total_output_tokens", 0)
            )
            self._card_tokens.set_value(_fmt_int(total_tok))
        except Exception as ex:
            log.warning("刷新概览卡片失败: %s", ex)

    def _refresh_table(self) -> None:
        s, e = self._preset_time_range(self._current_preset_idx)
        try:
            records = self._tracker.get_records(start_ms=s, end_ms=e)
        except Exception as ex:
            log.warning("获取用量记录失败: %s", ex)
            records = []

        # 限制表格显示条数（避免 UI 卡顿）
        DISPLAY_LIMIT = 500
        if len(records) > DISPLAY_LIMIT:
            records = records[:DISPLAY_LIMIT]

        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            # 时间（北京时间）
            bj_ms = rec.time + 8 * 3600 * 1000
            bj_struct = time.gmtime(bj_ms / 1000)
            time_str = time.strftime("%Y-%m-%d %H:%M", bj_struct)
            self._table.setItem(row, 0, QTableWidgetItem(time_str))

            # 模型名（空模型兜底到 DSH 默认模型，避免显示 unknown）
            model_name = rec.model or self._default_model or "unknown"
            model_item = QTableWidgetItem(model_name)
            model_item.setToolTip(model_name)
            self._table.setItem(row, 1, model_item)

            # 层级（高峰/平峰）
            from ...core.usage_tracker import _is_peak
            tier = "高峰" if _is_peak(rec.time) else "平峰"
            tier_item = QTableWidgetItem(tier)
            tier_color = _WARNING if tier == "高峰" else _TRAE_BLUE
            tier_item.setForeground(QColor(tier_color))
            self._table.setItem(row, 2, tier_item)

            # 输入（cache_hit + miss）
            inp = rec.cache_read_tokens + rec.input_tokens
            self._table.setItem(row, 3, QTableWidgetItem(_fmt_int(inp)))

            # 输出
            self._table.setItem(row, 4, QTableWidgetItem(_fmt_int(rec.output_tokens)))

            # 费用（每个任务单独计价；空模型用默认模型兜底计算，避免全 0）
            try:
                if not rec.model and self._default_model:
                    rec = rec.__class__(**{**rec.__dict__, "model": self._default_model})
                cost = self._tracker.cost_of(rec)
            except Exception:
                cost = 0.0
            cost_str = _fmt_money(cost)
            cost_item = QTableWidgetItem(f"$ {cost_str}")
            if cost > 0.5:
                cost_item.setForeground(QColor(_WARNING))
            if cost > 2.0:
                cost_item.setForeground(QColor(_ERROR))
            self._table.setItem(row, 5, cost_item)

    # ---- 主题 ----

    def apply_theme(self, theme=None) -> None:
        """应用主题到本页所有控件。"""
        if theme is not None:
            _apply_theme_colors(theme)

        # ComboBox
        self._filter_combo.setStyleSheet(
            f"QComboBox {{ padding: 4px 10px; background: {_SOFT_BG};"
            f" border: 1px solid {_INPUT_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            f"QComboBox QAbstractItemView {{ background: {_BG_SECONDARY}; color: {_TEXT};"
            f" selection-background-color: {_ACCENT}; border: 1px solid {_INPUT_BORDER}; }}"
        )

        # 卡片
        self._card_today.apply_theme(theme)
        self._card_month.apply_theme(theme)
        self._card_calls.apply_theme(theme)
        self._card_tokens.apply_theme(theme)

        # 表格
        self._table.setStyleSheet(
            f"QTableWidget {{ background: {_TAB_PANE_BG}; border: 1px solid {_PANEL_BORDER};"
            f" border-radius: 8px; color: {_TEXT}; gridline-color: {_GRIDLINE}; }}"
            f"QTableWidget::item {{ padding: 4px 8px; border: none; }}"
            f"QTableWidget::item:selected {{ background: {_BG_HOVER}; color: {_TEXT}; }}"
        )
        self._table.horizontalHeader().setStyleSheet(
            f"QHeaderView::section {{ background: {_SOFT_BG}; color: {_MUTED};"
            f" padding: 6px; border: none; border-bottom: 1px solid {_CELL_DIVIDER}; }}"
        )
        self._table.verticalHeader().setStyleSheet(
            f"QHeaderView::section {{ background: transparent; color: {_DIM};"
            f" border: none; border-right: 1px solid {_CELL_DIVIDER}; }}"
        )


# ===== _ModelsTab =====

_MODEL_DISPLAY_NAMES = {
    "deepseek-v4-flash": "DeepSeek-V4 Flash",
    "deepseek-v4-pro": "DeepSeek-V4 Pro",
}


class _ModelsTab(QWidget):
    """模型价格配置页：每个模型层级的峰谷定价 SpinBox + 保存。"""

    def __init__(self, tracker: UsageTracker, parent: QWidget | None = None):
        super().__init__(parent)
        self._tracker = tracker
        self._price_spins: dict[str, dict[str, dict[str, QDoubleSpinBox]]] = {}
        self._scroll_area: QScrollArea | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        # 外层：可滚动区域（内容自然铺开，不再固定高度压缩，彻底避免重叠）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        content = QWidget()
        content.setObjectName("ModelsPageContent")
        content.setAutoFillBackground(False)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        title_lbl = QLabel("模型价格")
        title_lbl.setObjectName("SettingsTitle")
        title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
        )
        layout.addWidget(title_lbl)

        hint_lbl = QLabel(
            "单位：元 / 百万 Tokens（cacheHit / cacheMiss / output 分别计价）。\n"
            "2026-08-17 起自动启用峰谷计价（高峰 9:00-12:00, 14:00-18:00 北京时间）。"
        )
        hint_lbl.setObjectName("ModelsHint")
        hint_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        hint_lbl.setWordWrap(True)
        layout.addWidget(hint_lbl)

        current_pricing = self._safe_get_pricing()

        for mk in PRICE_MODELS:
            display_name = _MODEL_DISPLAY_NAMES.get(mk, mk)
            grp = QGroupBox(display_name)
            grp.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
            )
            grp_layout = QVBoxLayout(grp)
            grp_layout.setSpacing(12)
            grp_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

            self._price_spins[mk] = {}

            for tier_name, tier_label in (("peak", "高峰时段"), ("offPeak", "平峰时段")):
                tier_frame = QFrame()
                # 内容自然高度（滚动区会纵向扩展），不再固定高度，避免行被压缩或溢出重叠
                tier_frame.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
                )
                tier_outer = QVBoxLayout(tier_frame)
                tier_outer.setSpacing(6)
                tier_outer.setContentsMargins(8, 8, 8, 8)
                # 强制布局不压缩子项，避免第 3 行 output 被挤掉
                tier_outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

                # 时段标题（独立一行，不再放进 QFormLayout，避免干扰表单两列宽度计算）
                tier_lbl = QLabel(tier_label)
                tier_lbl.setStyleSheet(
                    f"color: {_WARNING if tier_name == 'peak' else _TRAE_BLUE};"
                    f" font-size: 13px; font-weight: 600;"
                )
                tier_outer.addWidget(tier_lbl)

                # 真正的 3 行定价输入表单（只有 label + spin，不掺标题行）
                form_wrap = QWidget()
                form_wrap.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
                )
                form_layout = QFormLayout(form_wrap)
                form_layout.setSpacing(10)
                form_layout.setContentsMargins(0, 0, 0, 0)
                form_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
                # 字段列可扩展；标签列固定右对齐，避免挤压 SpinBox
                form_layout.setFieldGrowthPolicy(
                    QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
                )
                form_layout.setLabelAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                form_layout.setFormAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                )

                pv = (current_pricing.get("peakValley", {})
                       .get(mk, {})
                       .get(tier_name, {}))
                self._price_spins[mk][tier_name] = {}

                for k, k_label in (
                    ("cacheHit", "缓存命中 (cacheHit)"),
                    ("cacheMiss", "输入未命中 (cacheMiss)"),
                    ("output", "输出 (output)"),
                ):
                    # 左侧标签：和 SpinBox 相同最小高度 + 垂直居中，保证左右对齐
                    lbl = QLabel(f"{k_label}：")
                    lbl.setObjectName("PriceFormLabel")   # 给 apply_theme 按名刷新主题色用
                    lbl.setMinimumHeight(32)
                    lbl.setSizePolicy(
                        QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
                    )
                    # 显式设置标签最小宽度，使三行输入框左缘严格对齐
                    lbl.setMinimumWidth(180)
                    lbl.setAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                    lbl.setStyleSheet(f"color: {_TEXT};")

                    spin = QDoubleSpinBox()
                    spin.setDecimals(4)
                    spin.setRange(0.0, 1000.0)
                    spin.setSingleStep(0.05)
                    spin.setSuffix(" 元/MTok")
                    spin.setValue(float(pv.get(k, 0.0)))
                    # 保证 SpinBox 宽度够完整显示 "0.0000 元/MTok"（含 Windows 高 DPI 放大裕量）
                    spin.setMinimumWidth(360)
                    spin.setMinimumHeight(32)
                    spin.setSizePolicy(
                        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
                    )

                    form_layout.addRow(lbl, spin)
                    self._price_spins[mk][tier_name][k] = spin

                tier_outer.addWidget(form_wrap)
                grp_layout.addWidget(tier_frame)

            layout.addWidget(grp)

        layout.addStretch()

        # 按钮行
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self._reset_btn = QPushButton("恢复默认")
        self._reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(self._reset_btn)

        self._save_btn = QPushButton("保存定价")
        self._save_btn.setObjectName("Primary")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)

        btn_wrap = QWidget()
        btn_wrap.setLayout(btn_row)
        layout.addWidget(btn_wrap)

        # 挂载滚动区
        outer.addWidget(scroll)
        scroll.setWidget(content)
        self._scroll_area = scroll

        self.apply_theme()
        QTimer.singleShot(60, self._reload_from_tracker)

    def _safe_get_pricing(self) -> dict:
        try:
            return self._tracker.pricing
        except Exception:
            return {}

    def _reload_from_tracker(self) -> None:
        current = self._safe_get_pricing()
        for mk, tiers in self._price_spins.items():
            for tier_name, keys in tiers.items():
                pv = (current.get("peakValley", {})
                       .get(mk, {})
                       .get(tier_name, {}))
                for k, spin in keys.items():
                    try:
                        spin.setValue(float(pv.get(k, spin.value())))
                    except Exception:
                        pass

    def _on_reset(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("恢复默认")
        box.setText("确定将所有定价恢复到默认值吗？")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        by = box.button(QMessageBox.StandardButton.Yes)
        if by:
            by.setText("恢复")
        bc = box.button(QMessageBox.StandardButton.Cancel)
        if bc:
            bc.setText("取消")
        # 应用主题文本颜色，避免在浅色主题下背景变白但文字仍为浅色导致看不清
        box.setStyleSheet(
            f"QMessageBox {{ background: {_BG_SECONDARY}; }}"
            f"QLabel {{ color: {_TEXT}; }}"
            f"QPushButton {{ padding: 5px 14px; background: {_SOFT_BG};"
            f" color: {_TEXT}; border: 1px solid {_BTN_BORDER}; border-radius: 6px; }}"
            f"QPushButton:hover {{ background: {_HARD_BG}; }}"
        )
        if box.exec() != QMessageBox.StandardButton.Yes.value:
            return
        try:
            self._tracker.reset_pricing()
        except Exception as ex:
            log.warning("reset_pricing 失败: %s", ex)
        self._reload_from_tracker()
        self._toast("已恢复默认定价", _ACCENT)

    def _on_save(self) -> None:
        try:
            # 从当前 tracker 拿一份基准，保持 base  regime 不变
            current = self._safe_get_pricing()
            import copy
            new_pricing = copy.deepcopy(current) if current else {}
            if "base" not in new_pricing:
                new_pricing["base"] = {}
            if "peakValley" not in new_pricing:
                new_pricing["peakValley"] = {}

            for mk, tiers in self._price_spins.items():
                if mk not in new_pricing["peakValley"]:
                    new_pricing["peakValley"][mk] = {}
                for tier_name, keys in tiers.items():
                    if tier_name not in new_pricing["peakValley"][mk]:
                        new_pricing["peakValley"][mk][tier_name] = {}
                    for k, spin in keys.items():
                        new_pricing["peakValley"][mk][tier_name][k] = float(spin.value())

            ok = self._tracker.update_pricing(new_pricing)
            if ok:
                self._toast("定价已保存", _ACCENT)
            else:
                self._toast("保存失败：定价数据格式校验未通过", _ERROR)
        except Exception as ex:
            log.warning("保存定价失败: %s", ex)
            self._toast(f"保存异常：{ex}", _ERROR)

    def _toast(self, text: str, color: str) -> None:
        try:
            parent_win = self.window()
            sb = getattr(parent_win, "status_bar", None)
            if sb is not None and hasattr(sb, "show_temporary"):
                sb.show_temporary(text, color=color, duration_ms=2200)
                return
        except Exception:
            pass
        log.info("[ModelsTab] %s", text)

    def apply_theme(self, theme=None) -> None:
        if theme is not None:
            _apply_theme_colors(theme)

        base_qss_btn = (
            f"QPushButton {{ padding: 5px 12px; background: {_SOFT_BG};"
            f" border: 1px solid {_BTN_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            f"QPushButton:hover {{ background: {_HARD_BG}; }}"
        )
        self._reset_btn.setStyleSheet(base_qss_btn)
        self._save_btn.setStyleSheet(
            f"QPushButton {{ padding: 5px 16px; background: {_ACCENT};"
            f" border: none; border-radius: 6px; color: {_BG_PRIMARY};"
            f" font-weight: 600; }}"
            f"QPushButton:hover {{ background: {_rgba_from_hex(_ACCENT, 0.85)}; }}"
        )

        grp_style = (
            f"QGroupBox {{ color: {_TEXT}; font-size: 13px; font-weight: 600;"
            f" border: 1px solid {_PANEL_BORDER}; border-radius: 10px;"
            f" margin-top: 10px; padding-top: 8px; background: {_TAB_PANE_BG}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}"
        )
        for c in self.findChildren(QGroupBox):
            c.setStyleSheet(grp_style)

        # 标题与提示随主题刷新
        for lbl in self.findChildren(QLabel, "SettingsTitle"):
            lbl.setStyleSheet(
                f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
            )
        for lbl in self.findChildren(QLabel, "ModelsHint"):
            lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")

        # 表单左侧标签主题色刷新（切主题时保持与 SpinBox 同一行视觉匹配）
        for lbl in self.findChildren(QLabel, "PriceFormLabel"):
            lbl.setStyleSheet(f"color: {_TEXT};")

        for spin in self.findChildren(QDoubleSpinBox):
            spin.setStyleSheet(
                f"QDoubleSpinBox {{ padding: 2px 34px 2px 8px; background: {_SOFT_BG};"
                f" color: {_TEXT}; border: 1px solid {_INPUT_BORDER};"
                f" border-radius: 6px; min-height: 28px; }}"
                f"QDoubleSpinBox::up-button {{ subcontrol-position: top right;"
                f" width: 22px; height: 14px; border: none;"
                f" background: {_HARD_BG}; border-top-right-radius: 4px;"
                f" margin: 2px 2px 0 0; }}"
                f"QDoubleSpinBox::down-button {{ subcontrol-position: bottom right;"
                f" width: 22px; height: 14px; border: none;"
                f" background: {_HARD_BG}; border-bottom-right-radius: 4px;"
                f" margin: 0 2px 2px 0; }}"
                f"QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{"
                f" background: {_rgba_from_hex(_ACCENT, 0.22)}; }}"
                f"QDoubleSpinBox::up-arrow {{ width: 6px; height: 6px;"
                f" border-left: 3px solid transparent; border-right: 3px solid transparent;"
                f" border-bottom: 4px solid {_TEXT}; image: none; }}"
                f"QDoubleSpinBox::down-arrow {{ width: 6px; height: 6px;"
                f" border-left: 3px solid transparent; border-right: 3px solid transparent;"
                f" border-top: 4px solid {_TEXT}; image: none; }}"
            )


# ===== _BillingTab =====

class _BillingTab(QWidget):
    """计费设置页：日期范围 + 按层级汇总 + CSV 导出。"""

    def __init__(self, tracker: UsageTracker, parent: QWidget | None = None):
        super().__init__(parent)
        self._tracker = tracker
        self._summary_labels: dict[str, QLabel] = {}
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        title_lbl = QLabel("计费设置")
        title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
        )
        layout.addWidget(title_lbl)

        # 日期范围行
        date_row = QHBoxLayout()
        date_row.setSpacing(12)

        range_lbl = QLabel("计费周期：")
        range_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        date_row.addWidget(range_lbl)

        today = QDate.currentDate()
        self._start_date = QDateEdit()
        self._start_date.setCalendarPopup(True)
        self._start_date.setDisplayFormat("yyyy-MM-dd")
        self._start_date.setDate(today.addDays(-29))
        date_row.addWidget(self._start_date)

        sep_lbl = QLabel("→")
        sep_lbl.setStyleSheet(f"color: {_MUTED};")
        date_row.addWidget(sep_lbl)

        self._end_date = QDateEdit()
        self._end_date.setCalendarPopup(True)
        self._end_date.setDisplayFormat("yyyy-MM-dd")
        self._end_date.setDate(today)
        date_row.addWidget(self._end_date)

        self._calc_btn = QPushButton("统计")
        self._calc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._calc_btn.clicked.connect(self._on_calc)
        date_row.addWidget(self._calc_btn)

        date_row.addStretch()
        date_wrap = QWidget()
        date_wrap.setLayout(date_row)
        layout.addWidget(date_wrap)

        # 层级汇总
        summary_grp = QGroupBox("层级消耗汇总")
        summary_layout = QFormLayout(summary_grp)
        summary_layout.setSpacing(8)
        for tier_key, tier_name in (
            ("flash", "Flash 层级 (V4-Flash)"),
            ("pro", "Pro 层级 (V4-Pro)"),
            ("unknown", "其他模型"),
            ("total", "合计"),
        ):
            lbl_title = QLabel(tier_name)
            lbl_title.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
            val = QLabel("¥ 0.00  ·  0 次调用 ·  0 Tokens")
            val.setStyleSheet(
                f"color: {_TEXT}; font-size: 13px; font-family: Consolas, monospace;"
            )
            self._summary_labels[tier_key] = val
            summary_layout.addRow(lbl_title, val)
        layout.addWidget(summary_grp)

        # 导出区
        export_grp = QGroupBox("数据导出")
        export_layout = QVBoxLayout(export_grp)
        export_layout.setSpacing(8)

        export_hint = QLabel(
            "支持导出全部用量记录为 CSV / JSON，方便二次分析与对账。"
        )
        export_hint.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        export_hint.setWordWrap(True)
        export_layout.addWidget(export_hint)

        export_row = QHBoxLayout()
        export_row.addStretch()
        self._export_csv_btn = QPushButton("导出 CSV")
        self._export_csv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export_csv_btn.clicked.connect(self._on_export_csv)
        export_row.addWidget(self._export_csv_btn)

        self._export_json_btn = QPushButton("导出 JSON")
        self._export_json_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._export_json_btn.clicked.connect(self._on_export_json)
        export_row.addWidget(self._export_json_btn)
        export_wrap = QWidget()
        export_wrap.setLayout(export_row)
        export_layout.addWidget(export_wrap)

        try:
            ddir = str(self._tracker.data_dir) if hasattr(self._tracker, "data_dir") else ""
            path_lbl = QLabel(f"导出默认目录：{ddir}")
            path_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
            path_lbl.setWordWrap(True)
            export_layout.addWidget(path_lbl)
        except Exception:
            pass

        layout.addWidget(export_grp)

        layout.addStretch()
        self.apply_theme()
        QTimer.singleShot(70, self._on_calc)

    def _qdate_to_utc_ms(self, qd: QDate, end_of_day: bool = False) -> int:
        """QDate（按北京时间理解）转 UTC 毫秒。"""
        y, m, d = qd.year(), qd.month(), qd.day()
        hhmmss = "23:59:59" if end_of_day else "00:00:00"
        bj_struct = time.strptime(f"{y}-{m:02d}-{d:02d} {hhmmss}", "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(bj_struct)) * 1000 - 8 * 3600 * 1000

    def _on_calc(self) -> None:
        try:
            start_ms = self._qdate_to_utc_ms(self._start_date.date(), False)
            end_ms = self._qdate_to_utc_ms(self._end_date.date(), True)
            records = self._tracker.get_records(start_ms=start_ms, end_ms=end_ms)
        except Exception as ex:
            log.warning("计费统计失败: %s", ex)
            records = []

        from ...core.usage_tracker import _model_key
        agg = {
            "flash": {"cost": 0.0, "calls": 0, "tokens": 0},
            "pro": {"cost": 0.0, "calls": 0, "tokens": 0},
            "unknown": {"cost": 0.0, "calls": 0, "tokens": 0},
            "total": {"cost": 0.0, "calls": 0, "tokens": 0},
        }
        for rec in records:
            mk = _model_key(rec.model)
            if mk == "deepseek-v4-flash":
                bucket = "flash"
            elif mk == "deepseek-v4-pro":
                bucket = "pro"
            else:
                bucket = "unknown"
            try:
                c = self._tracker.cost_of(rec)
            except Exception:
                c = 0.0
            toks = (
                rec.input_tokens + rec.cache_read_tokens
                + rec.cache_write_tokens + rec.output_tokens
                + rec.reasoning_tokens
            )
            agg[bucket]["cost"] += c
            agg[bucket]["calls"] += 1
            agg[bucket]["tokens"] += toks
            agg["total"]["cost"] += c
            agg["total"]["calls"] += 1
            agg["total"]["tokens"] += toks

        for k, v in agg.items():
            lbl = self._summary_labels.get(k)
            if lbl is None:
                continue
            text = (
                f"¥ {_fmt_money(v['cost'])}  ·  "
                f"{_fmt_int(v['calls'])} 次调用  ·  "
                f"{_fmt_int(v['tokens'])} Tokens"
            )
            lbl.setText(text)

    def _export_dir_default(self) -> str:
        try:
            return str(self._tracker.data_dir)
        except Exception:
            return str(Path.home())

    def _on_export_csv(self) -> None:
        default_dir = self._export_dir_default()
        default_name = time.strftime("usage-records-%Y%m%d-%H%M%S.csv", time.localtime())
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV",
            str(Path(default_dir) / default_name),
            "CSV 文件 (*.csv)",
        )
        if not path:
            return
        try:
            n = self._tracker.export_csv(path)
            self._toast(f"已导出 {n} 条记录 → CSV", _ACCENT)
        except Exception as ex:
            log.warning("CSV 导出失败: %s", ex)
            self._toast(f"导出失败：{ex}", _ERROR)

    def _on_export_json(self) -> None:
        default_dir = self._export_dir_default()
        default_name = time.strftime("usage-records-%Y%m%d-%H%M%S.json", time.localtime())
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 JSON",
            str(Path(default_dir) / default_name),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        try:
            n = self._tracker.export_json(path)
            self._toast(f"已导出 {n} 条记录 → JSON", _ACCENT)
        except Exception as ex:
            log.warning("JSON 导出失败: %s", ex)
            self._toast(f"导出失败：{ex}", _ERROR)

    def _toast(self, text: str, color: str) -> None:
        try:
            parent_win = self.window()
            sb = getattr(parent_win, "status_bar", None)
            if sb is not None and hasattr(sb, "show_temporary"):
                sb.show_temporary(text, color=color, duration_ms=2500)
                return
        except Exception:
            pass
        log.info("[BillingTab] %s", text)

    def apply_theme(self, theme=None) -> None:
        if theme is not None:
            _apply_theme_colors(theme)

        for de in (self._start_date, self._end_date):
            de.setStyleSheet(
                f"QDateEdit {{ padding: 4px 8px; background: {_SOFT_BG}; color: {_TEXT};"
                f" border: 1px solid {_INPUT_BORDER}; border-radius: 6px; }}"
            )

        btn_style = (
            f"QPushButton {{ padding: 5px 12px; background: {_SOFT_BG};"
            f" border: 1px solid {_BTN_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            f"QPushButton:hover {{ background: {_HARD_BG}; }}"
        )
        self._calc_btn.setStyleSheet(btn_style)
        self._export_csv_btn.setStyleSheet(btn_style)
        self._export_json_btn.setStyleSheet(btn_style)

        grp_style = (
            f"QGroupBox {{ color: {_TEXT}; font-size: 13px; font-weight: 600;"
            f" border: 1px solid {_PANEL_BORDER}; border-radius: 10px;"
            f" margin-top: 10px; padding-top: 8px; background: {_TAB_PANE_BG}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}"
        )
        for c in self.findChildren(QGroupBox):
            c.setStyleSheet(grp_style)


# ===== _SettingsTab =====

class _ApiKeyDialog(QDialog):
    """临时记忆 API Key 对话框（保存到 ~/.dsh/.credentials.yaml）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("设置 API Key（临时记忆）")
        self.resize(520, 240)
        self._result_key: str = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        desc = QLabel(
            "将 DeepSeek API Key 写入本地凭据文件，DSH 服务会自动热加载。\n"
            "仅保存在本机 ~/.dsh/.credentials.yaml，不会上传任何第三方服务器。"
        )
        desc.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(10)

        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("sk-xxxxxxxxxxxxxxxxxxxxxxxx")
        self._key_edit.setStyleSheet(
            f"QLineEdit {{ padding: 8px 12px; background: {_SOFT_BG};"
            f" border: 1px solid {_CODE_INPUT_BORDER}; border-radius: 8px;"
            f" color: {_TEXT}; font-family: 'Consolas', monospace; }}"
        )
        form.addRow("API Key：", self._key_edit)

        self._show_cb = QCheckBox("显示明文")
        self._show_cb.toggled.connect(self._toggle_visible)
        form.addRow(self._show_cb)

        layout.addLayout(form)

        hint = QLabel("获取地址：https://platform.deepseek.com/")
        hint.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        layout.addWidget(hint)

        layout.addStretch()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        sb = btns.button(QDialogButtonBox.StandardButton.Save)
        if sb:
            sb.setText("保存")
        cb_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if cb_btn:
            cb_btn.setText("取消")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._apply_theme_style()

    def _toggle_visible(self, checked: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        self._key_edit.setEchoMode(mode)

    def _apply_theme_style(self) -> None:
        base_btn = (
            f"QPushButton {{ padding: 5px 14px; background: {_SOFT_BG};"
            f" border: 1px solid {_BTN_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            f"QPushButton:hover {{ background: {_HARD_BG}; }}"
        )
        self.setStyleSheet(base_btn)
        try:
            cb = self.findChild(QDialogButtonBox)
            if cb:
                save_btn = cb.button(QDialogButtonBox.StandardButton.Save)
                if save_btn:
                    save_btn.setStyleSheet(
                        f"QPushButton {{ padding: 5px 16px; background: {_ACCENT};"
                        f" border: none; border-radius: 6px; color: {_BG_PRIMARY};"
                        f" font-weight: 600; }}"
                    )
        except Exception:
            pass

    def _on_accept(self) -> None:
        self._result_key = self._key_edit.text().strip()
        self.accept()

    def get_key(self) -> str:
        return self._result_key

    def set_initial_key(self, key: str) -> None:
        self._key_edit.setText(key or "")


class _SettingsTab(QWidget):
    """设置页：API Key 临时记忆 + 主题 + DSH 路径 + 保存。"""

    def __init__(self, tracker: UsageTracker, balance_widget=None, parent: QWidget | None = None):
        super().__init__(parent)
        self._tracker = tracker
        self._balance_widget = balance_widget
        self._theme_combo: QComboBox | None = None
        self._dsh_path_edit: QLineEdit | None = None
        self._workspace_edit: QLineEdit | None = None
        self._scroll_area: QScrollArea | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        # 外层：可滚动区域（内容自然铺开，避免固定高度压缩导致文字/控件重叠）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        content = QWidget()
        content.setObjectName("SettingsPageContent")
        content.setAutoFillBackground(False)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        title_lbl = QLabel("设置")
        title_lbl.setObjectName("SettingsTitle")
        title_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
        )
        layout.addWidget(title_lbl)

        # API Key
        grp_key = QGroupBox("API 密钥")
        key_layout = QVBoxLayout(grp_key)
        key_layout.setSpacing(10)
        key_desc = QLabel(
            "DeepSeek API Key 用于对话调用与余额查询。"
            " 存储位置：~/.dsh/.credentials.yaml"
        )
        key_desc.setObjectName("SettingsHint")
        key_desc.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        key_desc.setWordWrap(True)
        key_layout.addWidget(key_desc)

        key_row = QHBoxLayout()
        self._key_status_lbl = QLabel("尚未读取")
        self._key_status_lbl.setObjectName("SettingsKeyStatus")
        self._key_status_lbl.setStyleSheet(
            f"color: {_DIM}; font-size: 12px; font-family: Consolas, monospace;"
        )
        key_row.addWidget(self._key_status_lbl, stretch=1)

        self._read_key_btn = QPushButton("读取")
        self._read_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._read_key_btn.clicked.connect(self._on_read_key)
        key_row.addWidget(self._read_key_btn)

        self._set_key_btn = QPushButton("设置 / 修改")
        self._set_key_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_key_btn.clicked.connect(self._on_set_key)
        key_row.addWidget(self._set_key_btn)
        key_wrap = QWidget()
        key_wrap.setLayout(key_row)
        key_layout.addWidget(key_wrap)
        layout.addWidget(grp_key)

        # 主题
        grp_theme = QGroupBox("主题")
        theme_layout = QFormLayout(grp_theme)
        theme_layout.setSpacing(10)

        self._theme_combo = QComboBox()
        tm = ThemeManager()
        try:
            tm.load_all()
        except Exception:
            pass
        for key_name, display_name in tm.theme_keys.items():
            cn = tm.cn_display_name(key_name)
            label = cn if cn != display_name else display_name
            self._theme_combo.addItem(label, key_name)
        # 表单左侧标签也挂 objectName 以便 apply_theme 统一刷新
        theme_row_label = QLabel("配色方案：")
        theme_row_label.setObjectName("SettingsFormLabel")
        theme_layout.addRow(theme_row_label, self._theme_combo)

        self._preview_hint_cb = QCheckBox("背景图可读性自动保护")
        self._preview_hint_cb.setChecked(True)
        theme_layout.addRow(self._preview_hint_cb)
        layout.addWidget(grp_theme)

        # 路径
        grp_path = QGroupBox("路径与工作区")
        path_layout = QFormLayout(grp_path)
        path_layout.setSpacing(10)

        self._dsh_path_edit = QLineEdit()
        self._dsh_path_edit.setPlaceholderText("DSH 工作目录（可选，留空使用 ~/.dsh-work/）")
        dsh_label = QLabel("DSH 数据路径：")
        dsh_label.setObjectName("SettingsFormLabel")
        path_layout.addRow(dsh_label, self._dsh_path_edit)

        ws_row = QHBoxLayout()
        self._workspace_edit = QLineEdit()
        self._workspace_edit.setPlaceholderText("默认工作区目录")
        ws_row.addWidget(self._workspace_edit, stretch=1)
        self._ws_browse_btn = QPushButton("选择...")
        self._ws_browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ws_browse_btn.clicked.connect(self._on_browse_ws)
        ws_row.addWidget(self._ws_browse_btn)
        ws_wrap = QWidget()
        ws_wrap.setLayout(ws_row)
        ws_label = QLabel("默认工作区：")
        ws_label.setObjectName("SettingsFormLabel")
        path_layout.addRow(ws_label, ws_wrap)

        layout.addWidget(grp_path)

        # 杂项
        grp_misc = QGroupBox("杂项")
        misc_layout = QFormLayout(grp_misc)
        misc_layout.setSpacing(10)

        self._auto_start_cb = QCheckBox("系统启动时自动运行 DSH Work")
        misc_layout.addRow(self._auto_start_cb)

        self._minimize_tray_cb = QCheckBox("关闭窗口时最小化到系统托盘")
        self._minimize_tray_cb.setChecked(True)
        misc_layout.addRow(self._minimize_tray_cb)

        self._check_updates_cb = QCheckBox("启动时自动检查更新")
        self._check_updates_cb.setChecked(True)
        misc_layout.addRow(self._check_updates_cb)

        layout.addWidget(grp_misc)

        layout.addStretch()

        # 保存
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self._on_reload)
        btn_row.addWidget(self._cancel_btn)

        self._save_btn = QPushButton("保存设置")
        self._save_btn.setObjectName("Primary")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        btn_wrap = QWidget()
        btn_wrap.setLayout(btn_row)
        layout.addWidget(btn_wrap)

        # 挂载滚动区
        outer.addWidget(scroll)
        scroll.setWidget(content)
        self._scroll_area = scroll

        self.apply_theme()
        QTimer.singleShot(80, self._on_reload)
        QTimer.singleShot(120, self._refresh_key_status)

    # ---- API Key ----

    def _creds_path(self) -> Path:
        return Path.home() / ".dsh" / ".credentials.yaml"

    def _refresh_key_status(self) -> None:
        try:
            p = self._creds_path()
            if not p.is_file():
                self._key_status_lbl.setText("未配置")
                self._key_status_lbl.setStyleSheet(
                    f"color: {_WARNING}; font-size: 12px; font-family: Consolas, monospace;"
                )
                return
            txt = p.read_text(encoding="utf-8")
            m = re.search(r"DEEPSEEK_API_KEY\s*:\s*['\"]?([^'\"\n]+)", txt)
            if not m or not m.group(1).strip():
                self._key_status_lbl.setText("凭据文件存在但未找到 Key")
                self._key_status_lbl.setStyleSheet(
                    f"color: {_WARNING}; font-size: 12px; font-family: Consolas, monospace;"
                )
                return
            k = m.group(1).strip()
            masked = k[:6] + "…" + k[-4:] if len(k) > 12 else "sk-****"
            self._key_status_lbl.setText(f"已配置  ({masked})")
            self._key_status_lbl.setStyleSheet(
                f"color: {_ACCENT}; font-size: 12px; font-family: Consolas, monospace;"
            )
        except Exception as ex:
            self._key_status_lbl.setText(f"读取异常：{ex}")
            self._key_status_lbl.setStyleSheet(
                f"color: {_ERROR}; font-size: 12px; font-family: Consolas, monospace;"
            )

    def _current_key(self) -> str:
        try:
            p = self._creds_path()
            if not p.is_file():
                return ""
            txt = p.read_text(encoding="utf-8")
            m = re.search(r"DEEPSEEK_API_KEY\s*:\s*['\"]?([^'\"\n]+)", txt)
            return m.group(1).strip() if m else ""
        except Exception:
            return ""

    def _on_read_key(self) -> None:
        self._refresh_key_status()
        self._toast("已重新读取凭据状态", _ACCENT)

    def _on_set_key(self) -> None:
        dlg = _ApiKeyDialog(self)
        dlg.set_initial_key(self._current_key())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_key = dlg.get_key()
        try:
            import yaml
            p = self._creds_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if p.is_file():
                try:
                    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                except Exception:
                    data = {}
            if new_key:
                data["DEEPSEEK_API_KEY"] = new_key
            else:
                data.pop("DEEPSEEK_API_KEY", None)
            with open(p, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            self._refresh_key_status()
            self._toast("API Key 已写入凭据文件", _ACCENT)
        except Exception as ex:
            log.warning("写入 API Key 失败: %s", ex)
            self._toast(f"写入失败：{ex}", _ERROR)

    # ---- 路径 ----

    def _on_browse_ws(self) -> None:
        current = (self._workspace_edit.text() if self._workspace_edit else "").strip()
        start_dir = current or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择工作区目录", start_dir)
        if d and self._workspace_edit:
            self._workspace_edit.setText(d)

    # ---- 配置装载/保存 ----

    def _cfg(self):
        try:
            from ...config import UserConfig
            return UserConfig.load()
        except Exception:
            return None

    def _on_reload(self) -> None:
        cfg = self._cfg()
        if cfg is None:
            return
        # 主题下拉
        if self._theme_combo is not None:
            theme_key = getattr(cfg, "theme", "") or ""
            for i in range(self._theme_combo.count()):
                if self._theme_combo.itemData(i) == theme_key:
                    self._theme_combo.setCurrentIndex(i)
                    break
        # 路径
        if self._dsh_path_edit is not None:
            self._dsh_path_edit.setText(getattr(cfg, "custom_dsh_endpoint", "") or "")
        if self._workspace_edit is not None:
            self._workspace_edit.setText(getattr(cfg, "workspace", "") or "")
        # 复选框
        try:
            self._preview_hint_cb.setChecked(bool(getattr(cfg, "readability_protection", True)))
        except Exception:
            pass
        try:
            self._minimize_tray_cb.setChecked(bool(getattr(cfg, "minimize_to_tray", True)))
        except Exception:
            pass
        try:
            self._check_updates_cb.setChecked(bool(getattr(cfg, "check_updates", True)))
        except Exception:
            pass

    def _on_save(self) -> None:
        cfg = self._cfg()
        if cfg is None:
            self._toast("无法访问用户配置", _ERROR)
            return
        try:
            if self._theme_combo is not None:
                new_key = self._theme_combo.currentData()
                if new_key:
                    tm = ThemeManager()
                    theme = tm.set_current(new_key)
                    if theme:
                        from PySide6.QtWidgets import QApplication
                        try:
                            qss = tm.generate_qss(theme)
                            QApplication.instance().setStyleSheet(qss)
                        except Exception:
                            pass
                    try:
                        cfg.theme = new_key
                    except Exception:
                        pass
            try:
                cfg.custom_dsh_endpoint = (
                    self._dsh_path_edit.text().strip() if self._dsh_path_edit else ""
                )
            except Exception:
                pass
            try:
                cfg.workspace = (
                    self._workspace_edit.text().strip() if self._workspace_edit else ""
                )
            except Exception:
                pass
            try:
                cfg.readability_protection = self._preview_hint_cb.isChecked()
            except Exception:
                pass
            try:
                cfg.minimize_to_tray = self._minimize_tray_cb.isChecked()
            except Exception:
                pass
            try:
                cfg.check_updates = self._check_updates_cb.isChecked()
            except Exception:
                pass
            try:
                cfg.save()
            except Exception as ex:
                self._toast(f"保存配置失败：{ex}", _ERROR)
                return
            self._toast("设置已保存", _ACCENT)
        except Exception as ex:
            log.warning("保存设置失败: %s", ex)
            self._toast(f"保存异常：{ex}", _ERROR)

    # ---- 其他 ----

    def _toast(self, text: str, color: str) -> None:
        try:
            parent_win = self.window()
            sb = getattr(parent_win, "status_bar", None)
            if sb is not None and hasattr(sb, "show_temporary"):
                sb.show_temporary(text, color=color, duration_ms=2200)
                return
        except Exception:
            pass
        log.info("[SettingsTab] %s", text)

    def apply_theme(self, theme=None) -> None:
        if theme is not None:
            _apply_theme_colors(theme)

        if self._theme_combo is not None:
            self._theme_combo.setStyleSheet(
                f"QComboBox {{ padding: 4px 10px; background: {_SOFT_BG};"
                f" border: 1px solid {_INPUT_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
                f"QComboBox QAbstractItemView {{ background: {_BG_SECONDARY}; color: {_TEXT};"
                f" selection-background-color: {_ACCENT}; border: 1px solid {_INPUT_BORDER}; }}"
            )
        for le in (self._dsh_path_edit, self._workspace_edit):
            if le is None:
                continue
            le.setStyleSheet(
                f"QLineEdit {{ padding: 6px 10px; background: {_SOFT_BG};"
                f" border: 1px solid {_INPUT_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            )

        btn_style = (
            f"QPushButton {{ padding: 5px 12px; background: {_SOFT_BG};"
            f" border: 1px solid {_BTN_BORDER}; border-radius: 6px; color: {_TEXT}; }}"
            f"QPushButton:hover {{ background: {_HARD_BG}; }}"
        )
        for b in (self._read_key_btn, self._set_key_btn, self._ws_browse_btn,
                   self._cancel_btn):
            if b is not None:
                b.setStyleSheet(btn_style)
        if self._save_btn is not None:
            self._save_btn.setStyleSheet(
                f"QPushButton {{ padding: 5px 16px; background: {_ACCENT};"
                f" border: none; border-radius: 6px; color: {_BG_PRIMARY};"
                f" font-weight: 600; }}"
                f"QPushButton:hover {{ background: {_rgba_from_hex(_ACCENT, 0.85)}; }}"
            )

        grp_style = (
            f"QGroupBox {{ color: {_TEXT}; font-size: 13px; font-weight: 600;"
            f" border: 1px solid {_PANEL_BORDER}; border-radius: 10px;"
            f" margin-top: 10px; padding-top: 8px; background: {_TAB_PANE_BG}; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; }}"
        )
        for c in self.findChildren(QGroupBox):
            c.setStyleSheet(grp_style)

        for cb in self.findChildren(QCheckBox):
            cb.setStyleSheet(
                f"QCheckBox {{ color: {_TEXT}; spacing: 6px; }}"
                f"QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px;"
                f" border: 1px solid {_INPUT_BORDER}; background: {_SOFT_BG}; }}"
                f"QCheckBox::indicator:checked {{ background: {_ACCENT}; border-color: {_ACCENT}; }}"
            )

        # ===== 所有 QLabel 按 objectName 统一刷新主题色 =====
        # 标题
        for lbl in self.findChildren(QLabel, "SettingsTitle"):
            lbl.setStyleSheet(
                f"color: {_TEXT}; font-size: 18px; font-weight: 700;"
            )
        # 表单左侧静态标签（"配色方案："、"DSH 数据路径："、"默认工作区："）
        for lbl in self.findChildren(QLabel, "SettingsFormLabel"):
            lbl.setStyleSheet(f"color: {_TEXT};")
        # 提示文本（API Key 区描述）
        for lbl in self.findChildren(QLabel, "SettingsHint"):
            lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        # Key 状态标签：只在没被 _refresh_key_status 单独染色时才统一刷新
        for lbl in self.findChildren(QLabel, "SettingsKeyStatus"):
            # 如果没有单独 objectName 冲突，保持 _refresh_key_status 动态结果即可
            pass


# ===== UsagePanel（主容器）=====

class UsagePanel(QWidget):
    """用量与消耗主面板（QTabWidget 承载 4 个标签页）。"""

    back_requested = Signal()  # 返回对话视图

    def __init__(self, tracker: UsageTracker, balance_widget=None, parent: QWidget | None = None,
                 default_model: str = ""):
        super().__init__(parent)
        self.setObjectName("UsagePanel")
        self._tracker = tracker
        self._balance_widget = balance_widget
        # DSH 默认模型名（用量记录 model 为空的显示/计价兜底）
        self._default_model = default_model or ""

        self._overview_tab: _OverviewTab | None = None
        self._models_tab: _ModelsTab | None = None
        self._billing_tab: _BillingTab | None = None
        self._settings_tab: _SettingsTab | None = None
        self._tab_widget: QTabWidget | None = None

        self._setup_ui()

        # 主题监听
        tm = ThemeManager()
        try:
            tm.add_listener(self.apply_theme)
        except Exception as ex:
            log.warning("注册主题监听器失败: %s", ex)

        # tracker 变化通知
        try:
            if hasattr(self._tracker, "add_listener"):
                self._tracker.add_listener(self._on_tracker_changed)
        except Exception as ex:
            log.warning("注册 tracker 监听器失败: %s", ex)

        # 初始化时同步当前主题
        try:
            cur = tm.current
            if cur is not None:
                self.apply_theme(cur)
        except Exception:
            pass

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部：返回对话入口（用量面板全屏占中央列，必须能回到对话）
        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 12, 4)
        self._back_btn = QPushButton("← 返回对话")
        self._back_btn.setObjectName("HeaderBtn")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.clicked.connect(self.back_requested)
        header.addWidget(self._back_btn)
        title = QLabel("用量与消耗")
        title.setObjectName("ConversationTitle")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        self._tab_widget = QTabWidget()
        self._tab_widget.setTabPosition(QTabWidget.TabPosition.North)
        self._tab_widget.setDocumentMode(False)

        self._overview_tab = _OverviewTab(self._tracker, default_model=getattr(self, "_default_model", ""))
        self._tab_widget.addTab(self._overview_tab, "概览")

        self._models_tab = _ModelsTab(self._tracker)
        self._tab_widget.addTab(self._models_tab, "模型价格")

        self._billing_tab = _BillingTab(self._tracker)
        self._tab_widget.addTab(self._billing_tab, "计费设置")

        self._settings_tab = _SettingsTab(self._tracker, self._balance_widget)
        self._tab_widget.addTab(self._settings_tab, "设置")

        layout.addWidget(self._tab_widget, stretch=1)
        self.apply_theme()

    def _on_tracker_changed(self) -> None:
        """用量记录变化 → 只刷新概览页，其他页懒加载。"""
        if self._overview_tab is not None:
            try:
                self._overview_tab.refresh()
            except Exception as ex:
                log.warning("刷新概览失败: %s", ex)

    # ---- 主题转发 ----

    def apply_theme(self, theme=None) -> None:
        """应用主题：先更新模块颜色，再转发到每个子页。"""
        if theme is None:
            tm = ThemeManager()
            theme = getattr(tm, "current", None) or theme
        if theme is not None:
            _apply_theme_colors(theme)

        if self._tab_widget is not None:
            pane_bg = _TAB_PANE_BG
            self._tab_widget.setStyleSheet(
                f"QTabWidget {{ background: transparent; border: none; }}"
                f"QTabWidget::pane {{ background: {pane_bg};"
                f" border: 1px solid {_PANEL_BORDER}; border-radius: 10px; top: -1px; padding: 0; }}"
                f"QTabBar::tab {{ padding: 6px 16px; background: {_SOFT_BG}; color: {_MUTED};"
                f" border: 1px solid {_TAB_BORDER}; border-bottom: none; margin-right: 4px;"
                f" border-top-left-radius: 8px; border-top-right-radius: 8px; font-size: 13px; }}"
                f"QTabBar::tab:selected {{ background: {pane_bg}; color: {_TEXT};"
                f" border-color: {_PANEL_BORDER}; border-bottom: 1px solid {pane_bg};"
                f" font-weight: 600; }}"
                f"QTabBar::tab:hover:!selected {{ color: {_TEXT}; background: {_SOFT_BG}; }}"
            )

        for tab in (self._overview_tab, self._models_tab,
                     self._billing_tab, self._settings_tab):
            if tab is None:
                continue
            try:
                tab.apply_theme(theme)
            except Exception as ex:
                log.warning("转发主题到子页失败: %s", ex)


__all__ = ["UsagePanel"]
