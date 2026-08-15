"""用量与消耗面板（移植自 dsh-usage-plugin Web UI）。

子页签：
  - 概览：总调用、总消耗、累计 token、时段分布卡片
  - 用量日历：按月查看每日用量热力图（按消耗或调用数着色）
  - 记录列表：完整记录表格 + 快捷筛选 + 自定义日期 + 导入导出
  - 价格表：基础价 / 峰谷价展示与编辑，一键恢复默认
  - 余额查询：复用 DSH Work 双通道 BalanceClient（而非插件的单通道）

风格：磨砂玻璃、TRAE 深色 token 配色，与主界面一致。
"""

from __future__ import annotations

import calendar
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal, QDate, QTimer
from PySide6.QtGui import QColor, QPainter, QBrush, QFont, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QComboBox,
    QDateEdit, QFileDialog, QMessageBox, QDoubleSpinBox, QGridLayout,
    QLineEdit, QSizePolicy, QScrollArea, QAbstractItemView,
)

from ...core.usage_tracker import UsageTracker, UsageRecord, DayAggregate, PRICE_MODELS
from ...api.balance_client import BalanceClient, BalanceResult
from ...utils.logger import get_logger

log = get_logger("ui.usage_panel")


# ===== 主题感知颜色（由 _apply_theme_colors() 原地更新）=====
_CARD_BG = "rgba(30, 33, 42, 0.75)"
_CARD_BORDER = "rgba(224, 226, 242, 0.08)"
_ACCENT = "#32F08C"
_WARNING = "#FFB454"
_ERROR = "#F65A5A"
_TEXT = "#E0E2F2"
_MUTED = "#9599A6"
_DIM = "#666B75"
_TRAE_BLUE = "#387BFF"
_BG_PRIMARY = "#1A1B1D"
_BG_SECONDARY = "#222427"
_BG_HOVER = "#2A2D31"


def _apply_theme_colors(theme) -> None:
    """从 Theme 对象原地更新模块级颜色变量。"""
    global _CARD_BG, _CARD_BORDER, _ACCENT, _WARNING, _ERROR
    global _TEXT, _MUTED, _DIM, _TRAE_BLUE, _BG_PRIMARY, _BG_SECONDARY, _BG_HOVER
    c = theme.colors
    _ACCENT = c.accent
    _WARNING = c.warning
    _ERROR = c.error
    _TEXT = c.text_primary
    _MUTED = c.text_secondary
    _DIM = c.text_muted
    _TRAE_BLUE = c.accent_secondary
    _BG_PRIMARY = c.bg_primary
    _BG_SECONDARY = c.bg_secondary
    _BG_HOVER = c.bg_hover
    # card_bg = bg_secondary 带 0.75 透明度
    r = int(c.bg_secondary[1:3], 16)
    g = int(c.bg_secondary[3:5], 16)
    b = int(c.bg_secondary[5:7], 16)
    _CARD_BG = f"rgba({r}, {g}, {b}, 0.75)"
    _CARD_BORDER = c.border


def _fmt_money(n: float) -> str:
    if n <= 0:
        return "0.00"
    if n < 0.01:
        return f"{n:.4f}"
    if n < 1:
        return f"{n:.3f}"
    return f"{n:.2f}"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _bj_month_range(year: int, month: int) -> tuple[int, int]:
    """返回北京时间该月第一天 00:00 和最后一天 23:59 的 UTC 毫秒。"""
    # 本月 1 号 00:00 北京 = 上月 31 号 16:00 UTC
    first_bj = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    last_day = calendar.monthrange(year, month)[1]
    last_bj_end = time.mktime((year, month, last_day, 23, 59, 59, 0, 0, -1))
    return int(first_bj * 1000 - 8 * 3600 * 1000), int(last_bj_end * 1000 - 8 * 3600 * 1000)


def _card_stylesheet() -> str:
    return (
        "QFrame#UsageCard {"
        f"  background-color: {_CARD_BG};"
        f"  border: 1px solid {_CARD_BORDER};"
        "  border-radius: 12px;"
        "}"
        f"QLabel#CardTitle {{ color: {_MUTED}; font-size: 12px; }}"
        f"QLabel#CardValue {{ color: {_TEXT}; font-size: 22px; font-weight: 700; }}"
        f"QLabel#CardSub   {{ color: {_DIM}; font-size: 11px; }}"
    )


class _Card(QFrame):
    def __init__(self, title: str, value: str, sub: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("UsageCard")
        self.setStyleSheet(_card_stylesheet())
        self.setMinimumHeight(88)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        layout.addWidget(t)
        self._value_lbl = QLabel(value)
        self._value_lbl.setObjectName("CardValue")
        layout.addWidget(self._value_lbl)
        self._sub_lbl = QLabel(sub)
        self._sub_lbl.setObjectName("CardSub")
        layout.addWidget(self._sub_lbl)
        layout.addStretch()

    def set_value(self, value: str, sub: str = "") -> None:
        self._value_lbl.setText(value)
        if sub:
            self._sub_lbl.setText(sub)

    def apply_theme(self, theme) -> None:
        self.setStyleSheet(_card_stylesheet())


# ============================================================
# 子页签 1：概览
# ============================================================
class _OverviewTab(QWidget):
    def __init__(self, tracker: UsageTracker, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # 顶部标题
        self._title = QLabel("用量概览")
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        root.addWidget(self._title)

        # 快捷筛选条
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self._preset = QComboBox()
        self._preset.addItems(["今日", "近 7 天", "近 30 天", "全部"])
        self._preset.setCurrentIndex(2)
        self._preset.currentIndexChanged.connect(self.refresh)
        self._preset.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        filter_row.addWidget(QLabel("时间范围："))
        filter_row.addWidget(self._preset)
        filter_row.addStretch()
        root.addLayout(filter_row)

        # 卡片网格 2x3
        grid = QGridLayout()
        grid.setSpacing(12)
        self._c_calls = _Card("总调用次数", "0", "次")
        self._c_cost = _Card("累计消耗", "¥ 0.00", "元（按自动计价）")
        self._c_peak = _Card("峰时调用占比", "0%", "高峰 9-12 / 14-18")
        self._c_input = _Card("输入未命中 token", "0", "")
        self._c_hit = _Card("缓存读取 token", "0", "")
        self._c_output = _Card("输出 token", "0", "")
        grid.addWidget(self._c_calls, 0, 0)
        grid.addWidget(self._c_cost, 0, 1)
        grid.addWidget(self._c_peak, 0, 2)
        grid.addWidget(self._c_input, 1, 0)
        grid.addWidget(self._c_hit, 1, 1)
        grid.addWidget(self._c_output, 1, 2)
        root.addLayout(grid)

        # 月度消耗趋势（简化：按最近 6 个月柱状图，用 QLabel 模拟）
        self._trend_title = QLabel("最近 6 个月消耗趋势")
        self._trend_title.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 600; margin-top: 12px;")
        root.addWidget(self._trend_title)
        self._trend = _TrendBars()
        root.addWidget(self._trend)

        root.addStretch()

    def _preset_range(self) -> tuple[int | None, int | None]:
        now_ms = int(time.time() * 1000)
        idx = self._preset.currentIndex()
        day_ms = 24 * 3600 * 1000
        if idx == 0:  # 今日（北京）
            bj_now = now_ms + 8 * 3600 * 1000
            t = time.gmtime(bj_now / 1000)
            start_bj = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            start_ms = int(start_bj * 1000) - 8 * 3600 * 1000
            return start_ms, now_ms
        if idx == 1:
            return now_ms - 7 * day_ms, now_ms
        if idx == 2:
            return now_ms - 30 * day_ms, now_ms
        return None, None

    def refresh(self) -> None:
        s, e = self._preset_range()
        summary = self._tracker.get_summary("auto", start_ms=s, end_ms=e)
        self._c_calls.set_value(_fmt_int(summary["total_calls"]), "次调用")
        self._c_cost.set_value(f"¥ {_fmt_money(summary['total_cost'])}", "元（自动计价）")
        calls = summary["total_calls"]
        peak_pct = int(summary["peak_calls"] / calls * 100) if calls else 0
        self._c_peak.set_value(f"{peak_pct}%", f"高峰 {summary['peak_calls']} · 空闲 {summary['off_peak_calls']}")
        self._c_input.set_value(_fmt_int(summary["total_input_tokens"]), "输入未命中")
        self._c_hit.set_value(_fmt_int(summary["total_cache_read_tokens"]), "缓存命中")
        self._c_output.set_value(_fmt_int(summary["total_output_tokens"]), "输出")
        # 6 个月趋势
        self._trend.set_data(self._tracker)

    def apply_theme(self, theme) -> None:
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        self._preset.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        self._trend_title.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 600; margin-top: 12px;")
        for card in (self._c_calls, self._c_cost, self._c_peak,
                     self._c_input, self._c_hit, self._c_output):
            card.apply_theme(theme)
        self._trend.apply_theme(theme)


class _TrendBars(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(180)
        self._data: list[tuple[str, float]] = []  # (标签, 消耗)

    def set_data(self, tracker: UsageTracker) -> None:
        """月度汇总。

        优化：把 daily key (YYYY-MM-DD) 先按 YYYY-MM 聚合到 dict，避免 6 次 O(N) 前缀扫描。
        """
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
        daily = tracker.get_daily_aggregates("auto")

        # 先按 YYYY-MM 做一次 O(N) 聚合
        month_totals: dict[str, float] = {}
        for d, agg in daily.items():
            if len(d) >= 7:
                mk = d[:7]  # YYYY-MM
                month_totals[mk] = month_totals.get(mk, 0.0) + agg.auto_cost

        months: list[tuple[str, float]] = []
        for _ in range(6):
            label = f"{year}-{month:02d}"
            total = month_totals.get(label, 0.0)
            months.insert(0, (label[2:], total))  # 只显示 YY-MM 的后半段
            month -= 1
            if month <= 0:
                month += 12
                year -= 1
        self._data = months
        self.update()

    def apply_theme(self, theme) -> None:
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 36, 12, 16, 28
        if not self._data:
            painter.end()
            return
        max_v = max((v for _, v in self._data), default=1.0)
        if max_v <= 0:
            max_v = 1.0
        n = len(self._data)
        slot = (w - pad_l - pad_r) / n
        bar_w = min(36, slot * 0.6)
        # 背景网格
        painter.setPen(QPen(QColor(224, 226, 242, 20), 1))
        for i in range(5):
            y = pad_t + (h - pad_t - pad_b) * i / 4
            painter.drawLine(pad_l, int(y), w - pad_r, int(y))
        # 柱体
        grad_color = QColor(_ACCENT)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, (label, v) in enumerate(self._data):
            cx = int(pad_l + slot * (i + 0.5))
            ratio = min(v / max_v, 1.0)
            bar_h = int((h - pad_t - pad_b) * ratio)
            x = cx - bar_w // 2
            y = h - pad_b - bar_h
            # 优化：用 QLinearGradient 替代逐像素绘制循环
            # （原代码 bar_h=100 时循环 100 次 drawRect，现在一次 drawRoundedRect）
            if bar_h > 0:
                from PySide6.QtGui import QLinearGradient
                gradient = QLinearGradient(0, y, 0, y + bar_h)
                r, g, b = grad_color.red(), grad_color.green(), grad_color.blue()
                gradient.setColorAt(0.0, QColor(r, g, b, 80))    # 顶部透明
                gradient.setColorAt(1.0, QColor(r, g, b, 255))   # 底部实色
                painter.setBrush(QBrush(gradient))
                painter.drawRoundedRect(x, y, bar_w, bar_h, 4, 4)
            # 数值标签
            painter.setPen(QColor(_TEXT))
            painter.setFont(QFont("", 9))
            v_text = f"¥{_fmt_money(v)}"
            painter.drawText(x - 20, y - 6, bar_w + 40, 14, Qt.AlignmentFlag.AlignHCenter, v_text)
            # x 轴标签
            painter.setPen(QColor(_MUTED))
            painter.drawText(x - 20, h - pad_b + 6, bar_w + 40, 18, Qt.AlignmentFlag.AlignHCenter, label)
        # y 轴刻度
        painter.setPen(QColor(_DIM))
        painter.setFont(QFont("", 8))
        for i in range(5):
            ratio = 1 - i / 4
            y = pad_t + (h - pad_t - pad_b) * i / 4
            painter.drawText(4, int(y) - 6, pad_l - 8, 12, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"{_fmt_money(max_v * ratio)}")
        painter.end()


# ============================================================
# 子页签 2：用量日历
# ============================================================
class _CalendarTab(QWidget):
    color_by_cost = Signal()
    color_by_calls = Signal()

    def __init__(self, tracker: UsageTracker, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._color_mode: str = "cost"
        today = QDate.currentDate()
        self._cur_year = today.year()
        self._cur_month = today.month()
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        self._title = QLabel("用量日历")
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        title_row.addWidget(self._title)
        title_row.addStretch()

        btn_prev = QPushButton("‹")
        btn_next = QPushButton("›")
        for b in (btn_prev, btn_next):
            b.setFixedSize(28, 28)
            b.setStyleSheet(
                "QPushButton { background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; font-size: 16px; }}"
                "QPushButton:hover { background: rgba(224,226,242,0.12); }"
            )
        btn_prev.clicked.connect(self._prev_month)
        btn_next.clicked.connect(self._next_month)

        self._month_lbl = QLabel()
        self._month_lbl.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 600; padding: 0 12px;")
        title_row.addWidget(btn_prev)
        title_row.addWidget(self._month_lbl)
        title_row.addWidget(btn_next)
        title_row.addSpacing(16)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["按消耗着色", "按调用数着色"])
        self._mode_combo.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_change)
        title_row.addWidget(self._mode_combo)
        root.addLayout(title_row)

        # 日历网格
        self._grid = _CalendarGrid(self._tracker)
        self._grid.day_clicked.connect(self._on_day_clicked)
        root.addWidget(self._grid, stretch=1)

        # 月度汇总
        self._summary_lbl = QLabel()
        self._summary_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px; padding: 8px 4px;")
        self._summary_lbl.setWordWrap(True)
        root.addWidget(self._summary_lbl)

        # 详情区
        self._detail_title = QLabel("点击日期查看当日明细")
        self._detail_title.setStyleSheet(f"color: {_MUTED}; font-size: 12px; margin-top: 4px;")
        root.addWidget(self._detail_title)
        self._detail_table = QTableWidget(0, 6)
        self._detail_table.setHorizontalHeaderLabels(["时间(北京)", "模型", "输入", "缓存命中", "输出", "消耗(¥)"])
        self._detail_table.verticalHeader().setVisible(False)
        self._detail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._detail_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._detail_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._detail_table.setAlternatingRowColors(True)
        self._detail_table.setStyleSheet(
            "QTableWidget { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08);"
            f"  border-radius: 8px; color: {_TEXT}; gridline-color: rgba(224,226,242,0.06); }}"
            f"QHeaderView::section {{ background: rgba(224,226,242,0.04); color: {_MUTED}; padding: 6px;"
            "  border: none; border-bottom: 1px solid rgba(224,226,242,0.1); }"
            "QTableWidget::item { padding: 4px 8px; }"
        )
        self._detail_table.setFixedHeight(200)
        root.addWidget(self._detail_table)

    def _prev_month(self) -> None:
        self._cur_month -= 1
        if self._cur_month <= 0:
            self._cur_month = 12
            self._cur_year -= 1
        self.refresh()

    def _next_month(self) -> None:
        self._cur_month += 1
        if self._cur_month > 12:
            self._cur_month = 1
            self._cur_year += 1
        self.refresh()

    def _on_mode_change(self, idx: int) -> None:
        self._color_mode = "calls" if idx == 1 else "cost"
        self.refresh()

    def _on_day_clicked(self, day_key: str) -> None:
        # 解析 day_key = YYYY-MM-DD（北京），转 ms 范围
        try:
            y, m, d = (int(x) for x in day_key.split("-"))
        except ValueError:
            return
        start_bj = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
        end_bj = time.mktime((y, m, d, 23, 59, 59, 0, 0, -1))
        start_ms = int(start_bj * 1000) - 8 * 3600 * 1000
        end_ms = int(end_bj * 1000) - 8 * 3600 * 1000
        recs = self._tracker.get_records(start_ms=start_ms, end_ms=end_ms)
        self._detail_table.setRowCount(len(recs))
        for i, r in enumerate(recs):
            bj = time.strftime("%H:%M:%S", time.gmtime((r.time + 8 * 3600 * 1000) / 1000))
            cells = [bj, r.model or "-",
                     _fmt_int(r.input_tokens), _fmt_int(r.cache_read_tokens),
                     _fmt_int(r.output_tokens), _fmt_money(self._tracker.cost_of(r))]
            for c_idx, val in enumerate(cells):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c_idx == 5:
                    item.setForeground(QColor(_ACCENT))
                self._detail_table.setItem(i, c_idx, item)

    def refresh(self) -> None:
        self._month_lbl.setText(f"{self._cur_year} 年 {self._cur_month} 月")
        self._grid.set_month(self._cur_year, self._cur_month, self._color_mode)
        # 月度汇总
        start_ms, end_ms = _bj_month_range(self._cur_year, self._cur_month)
        s = self._tracker.get_summary("auto", start_ms=start_ms, end_ms=end_ms)
        daily = self._tracker.get_daily_aggregates("auto")
        prefix = f"{self._cur_year:04d}-{self._cur_month:02d}"
        active_days = sum(1 for k in daily if k.startswith(prefix))
        self._summary_lbl.setText(
            f"本月共 {s['total_calls']} 次调用，累计消耗 ¥{_fmt_money(s['total_cost'])}，"
            f"活跃 {active_days} 天，输入 {_fmt_int(s['total_input_tokens'])} token，"
            f"缓存命中 {_fmt_int(s['total_cache_read_tokens'])} token，输出 {_fmt_int(s['total_output_tokens'])} token。"
        )

    def apply_theme(self, theme) -> None:
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        self._month_lbl.setStyleSheet(f"color: {_TEXT}; font-size: 14px; font-weight: 600; padding: 0 12px;")
        self._mode_combo.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        self._summary_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px; padding: 8px 4px;")
        self._detail_title.setStyleSheet(f"color: {_MUTED}; font-size: 12px; margin-top: 4px;")
        self._detail_table.setStyleSheet(
            "QTableWidget { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08);"
            f"  border-radius: 8px; color: {_TEXT}; gridline-color: rgba(224,226,242,0.06); }}"
            f"QHeaderView::section {{ background: rgba(224,226,242,0.04); color: {_MUTED}; padding: 6px;"
            "  border: none; border-bottom: 1px solid rgba(224,226,242,0.1); }"
            "QTableWidget::item { padding: 4px 8px; }"
        )
        self._grid.apply_theme(theme)


class _CalendarGrid(QWidget):
    day_clicked = Signal(str)  # day_key = YYYY-MM-DD

    _WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

    def __init__(self, tracker: UsageTracker, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._year = 0
        self._month = 0
        self._mode = "cost"
        self._cells: list[tuple[int, int, str, DayAggregate | None]] = []  # row, col, label, agg
        self.setMinimumHeight(320)

    def set_month(self, year: int, month: int, mode: str) -> None:
        self._year = year
        self._month = month
        self._mode = mode
        self._rebuild_cells()
        self.update()

    def apply_theme(self, theme) -> None:
        self.update()

    def _rebuild_cells(self) -> None:
        daily = self._tracker.get_daily_aggregates("auto")
        cal = calendar.Calendar(firstweekday=0)  # 周一开头
        self._cells = []
        for r, week in enumerate(cal.monthdayscalendar(self._year, self._month)):
            for c, day in enumerate(week):
                if day == 0:
                    self._cells.append((r, c, "", None))
                    continue
                key = f"{self._year:04d}-{self._month:02d}-{day:02d}"
                agg = daily.get(key)
                self._cells.append((r, c, str(day), agg))

    def _color_for(self, agg: DayAggregate | None) -> QColor:
        if agg is None:
            return QColor(224, 226, 242, 18)
        if self._mode == "cost":
            v = agg.auto_cost
            # 分级：0 / 0-0.1 / 0.1-1 / 1-5 / 5+
            if v <= 0:
                return QColor(224, 226, 242, 22)
            if v < 0.1:
                return QColor(50, 240, 140, 60)
            if v < 1:
                return QColor(50, 240, 140, 120)
            if v < 5:
                return QColor(56, 123, 255, 160)
            return QColor(182, 85, 252, 200)
        else:
            c = agg.calls
            if c <= 0:
                return QColor(224, 226, 242, 22)
            if c < 5:
                return QColor(50, 240, 140, 60)
            if c < 20:
                return QColor(50, 240, 140, 120)
            if c < 80:
                return QColor(56, 123, 255, 160)
            return QColor(182, 85, 252, 200)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = 8
        # 54 周标签行
        header_h = 28
        rows = 6
        cols = 7
        cell_w = (w - pad * 2) / cols
        cell_h = (h - pad * 2 - header_h) / rows

        # 画周标签
        painter.setPen(QColor(_MUTED))
        painter.setFont(QFont("", 11, QFont.Weight.Bold))
        for c in range(cols):
            cx = int(pad + cell_w * (c + 0.5))
            painter.drawText(cx - 20, pad, 40, header_h - 4,
                             Qt.AlignmentFlag.AlignCenter, self._WEEKDAYS[c])
        # 画格子
        font = QFont("", 11)
        painter.setFont(font)
        today_bj = time.strftime("%Y-%m-%d", time.gmtime((time.time() + 8 * 3600 * 1000) / 1000))
        for (r, c, label, agg) in self._cells:
            x = int(pad + cell_w * c) + 2
            y = int(pad + header_h + cell_h * r) + 2
            cw = int(cell_w) - 4
            ch = int(cell_h) - 4
            color = self._color_for(agg)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(x, y, cw, ch, 8, 8)
            # 日期数字
            if label:
                day_key = f"{self._year:04d}-{self._month:02d}-{int(label):02d}"
                is_today = day_key == today_bj
                if is_today:
                    painter.setPen(QPen(QColor(_WARNING), 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(x + 1, y + 1, cw - 2, ch - 2, 8, 8)
                    painter.setPen(QColor(_WARNING))
                else:
                    painter.setPen(QColor(_TEXT))
                painter.drawText(x, y, cw, 18, Qt.AlignmentFlag.AlignCenter, label)
                # 底部小数字：消耗或调用数
                if agg is not None:
                    painter.setPen(QColor(_TEXT, 200))
                    painter.setFont(QFont("", 9))
                    if self._mode == "cost":
                        sub = f"¥{_fmt_money(agg.auto_cost)}"
                    else:
                        sub = f"{agg.calls}次"
                    painter.drawText(x, y + ch - 18, cw, 14, Qt.AlignmentFlag.AlignCenter, sub)
                    painter.setFont(font)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        w, h = self.width(), self.height()
        pad = 8
        header_h = 28
        rows = 6
        cols = 7
        cell_w = (w - pad * 2) / cols
        cell_h = (h - pad * 2 - header_h) / rows
        px = event.position().x()
        py = event.position().y()
        if py < pad + header_h:
            return
        c = int((px - pad) // cell_w)
        r = int((py - pad - header_h) // cell_h)
        if 0 <= c < cols and 0 <= r < rows:
            for (rr, cc, label, agg) in self._cells:
                if rr == r and cc == c and label:
                    key = f"{self._year:04d}-{self._month:02d}-{int(label):02d}"
                    self.day_clicked.emit(key)
                    break


# ============================================================
# 子页签 3：记录列表
# ============================================================
class _RecordsTab(QWidget):
    def __init__(self, tracker: UsageTracker, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._btns: list[tuple[QPushButton, Any]] = []  # (button, color_getter)

        title_row = QHBoxLayout()
        self._title = QLabel("缓存命中列表")
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        title_row.addWidget(self._title)
        title_row.addStretch()

        self._preset = QComboBox()
        self._preset.addItems(["今天", "近 7 天", "近 30 天", "全部", "自定义"])
        self._preset.setCurrentIndex(2)
        self._preset.currentIndexChanged.connect(self._on_preset_change)
        self._preset.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        title_row.addWidget(self._preset)

        today = QDate.currentDate()
        self._start_edit = QDateEdit(today.addDays(-30))
        self._end_edit = QDateEdit(today)
        for d in (self._start_edit, self._end_edit):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
            d.setEnabled(False)
            d.setStyleSheet(
                "QDateEdit { padding: 4px 8px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
            )
            d.dateChanged.connect(self._on_date_change)
        title_row.addWidget(self._start_edit)
        title_row.addWidget(QLabel("~"))
        title_row.addWidget(self._end_edit)
        root.addLayout(title_row)

        # 操作按钮行
        action_row = QHBoxLayout()
        self._btn_refresh = self._mk_btn("🔄 刷新", _TRAE_BLUE)
        self._btns.append((self._btn_refresh, lambda: _TRAE_BLUE))
        self._btn_refresh.clicked.connect(self.refresh)
        self._btn_export_json = self._mk_btn("📥 导出 JSON", _ACCENT)
        self._btns.append((self._btn_export_json, lambda: _ACCENT))
        self._btn_export_json.clicked.connect(self._export_json)
        self._btn_export_csv = self._mk_btn("📊 导出 CSV", _ACCENT)
        self._btns.append((self._btn_export_csv, lambda: _ACCENT))
        self._btn_export_csv.clicked.connect(self._export_csv)
        self._btn_import_json = self._mk_btn("📤 导入 JSON", _WARNING)
        self._btns.append((self._btn_import_json, lambda: _WARNING))
        self._btn_import_json.clicked.connect(self._import_json)
        self._btn_import_csv = self._mk_btn("📤 导入 CSV", _WARNING)
        self._btns.append((self._btn_import_csv, lambda: _WARNING))
        self._btn_import_csv.clicked.connect(self._import_csv)
        self._btn_open_dir = self._mk_btn("📂 数据目录", _MUTED)
        self._btns.append((self._btn_open_dir, lambda: _MUTED))
        self._btn_open_dir.clicked.connect(self._open_dir)
        for b in (self._btn_refresh, self._btn_export_json, self._btn_export_csv,
                  self._btn_import_json, self._btn_import_csv, self._btn_open_dir):
            action_row.addWidget(b)
        action_row.addStretch()
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        action_row.addWidget(self._count_lbl)
        root.addLayout(action_row)

        # 表格
        self._table = QTableWidget(0, 9)
        headers = ["时间(北京)", "模型", "用途", "输入", "缓存命中", "缓存写入", "输出", "推理", "消耗(¥)"]
        self._table.setHorizontalHeaderLabels(headers)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QTableWidget { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08);"
            f"  border-radius: 8px; color: {_TEXT}; gridline-color: rgba(224,226,242,0.06); }}"
            f"QHeaderView::section {{ background: rgba(224,226,242,0.04); color: {_MUTED}; padding: 6px;"
            "  border: none; border-bottom: 1px solid rgba(224,226,242,0.1); }"
            "QTableWidget::item { padding: 4px 8px; }"
        )
        root.addWidget(self._table, stretch=1)

    @staticmethod
    def _mk_btn(text: str, color: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ padding: 5px 12px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {color};"
            f"  font-size: 12px; }}"
            f"QPushButton:hover {{ background: rgba(224,226,242,0.12); }}"
        )
        return btn

    def apply_theme(self, theme) -> None:
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        self._preset.setStyleSheet(
            "QComboBox { padding: 4px 10px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
        )
        for d in (self._start_edit, self._end_edit):
            d.setStyleSheet(
                "QDateEdit { padding: 4px 8px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.1); border-radius: 6px; color: {_TEXT}; }}"
            )
        self._table.setStyleSheet(
            "QTableWidget { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08);"
            f"  border-radius: 8px; color: {_TEXT}; gridline-color: rgba(224,226,242,0.06); }}"
            f"QHeaderView::section {{ background: rgba(224,226,242,0.04); color: {_MUTED}; padding: 6px;"
            "  border: none; border-bottom: 1px solid rgba(224,226,242,0.1); }"
            "QTableWidget::item { padding: 4px 8px; }"
        )
        self._count_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        for btn, color_getter in self._btns:
            color = color_getter()
            btn.setStyleSheet(
                f"QPushButton {{ padding: 5px 12px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {color};"
                f"  font-size: 12px; }}"
                f"QPushButton:hover {{ background: rgba(224,226,242,0.12); }}"
            )

    def _on_preset_change(self, idx: int) -> None:
        custom = idx == 4
        self._start_edit.setEnabled(custom)
        self._end_edit.setEnabled(custom)
        self.refresh()

    def _on_date_change(self) -> None:
        if self._preset.currentIndex() == 4:
            self.refresh()

    def _get_range(self) -> tuple[int | None, int | None]:
        idx = self._preset.currentIndex()
        now_ms = int(time.time() * 1000)
        day_ms = 24 * 3600 * 1000
        if idx == 0:
            bj_now = now_ms + 8 * 3600 * 1000
            t = time.gmtime(bj_now / 1000)
            start_bj = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
            return int(start_bj * 1000) - 8 * 3600 * 1000, now_ms
        if idx == 1:
            return now_ms - 7 * day_ms, now_ms
        if idx == 2:
            return now_ms - 30 * day_ms, now_ms
        if idx == 4:
            sd = self._start_edit.date()
            ed = self._end_edit.date()
            start_bj = time.mktime((sd.year(), sd.month(), sd.day(), 0, 0, 0, 0, 0, -1))
            end_bj = time.mktime((ed.year(), ed.month(), ed.day(), 23, 59, 59, 0, 0, -1))
            return int(start_bj * 1000) - 8 * 3600 * 1000, int(end_bj * 1000) - 8 * 3600 * 1000
        return None, None

    def refresh(self) -> None:
        s, e = self._get_range()
        recs = self._tracker.get_records(start_ms=s, end_ms=e)
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(recs))
        for i, r in enumerate(recs):
            bj = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime((r.time + 8 * 3600 * 1000) / 1000))
            row = [
                bj, r.model or "-", r.purpose or "-",
                _fmt_int(r.input_tokens), _fmt_int(r.cache_read_tokens),
                _fmt_int(r.cache_write_tokens), _fmt_int(r.output_tokens),
                _fmt_int(r.reasoning_tokens), _fmt_money(self._tracker.cost_of(r)),
            ]
            for c_idx, val in enumerate(row):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c_idx == 8:
                    item.setForeground(QColor(_ACCENT))
                self._table.setItem(i, c_idx, item)
        self._table.setSortingEnabled(True)
        self._count_lbl.setText(f"共 {len(recs)} 条记录（总量 {self._tracker.total_records}）")

    # ===== 导入导出 =====

    def _export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 JSON",
            str(self._tracker.data_dir / "json" / f"usage-{int(time.time())}.json"),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        try:
            n = self._tracker.export_json(path)
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条记录到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV",
            str(self._tracker.data_dir / "csv" / f"usage-{int(time.time())}.csv"),
            "CSV 文件 (*.csv)",
        )
        if not path:
            return
        try:
            n = self._tracker.export_csv(path)
            QMessageBox.information(self, "导出成功", f"已导出 {n} 条记录到：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入 JSON", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            added, skipped = self._tracker.import_json(path)
            QMessageBox.information(self, "导入完成", f"新增 {added} 条，跳过（重复/无效）{skipped} 条")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _import_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入 CSV", "", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            added, skipped = self._tracker.import_csv(path)
            QMessageBox.information(self, "导入完成", f"新增 {added} 条，跳过（重复/无效）{skipped} 条")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _open_dir(self) -> None:
        import os, sys, subprocess
        p = str(self._tracker.data_dir)
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", f"目录路径：{p}\n\n{e}")


# ============================================================
# 子页签 4：价格表
# ============================================================
class _PricingTab(QWidget):
    pricing_changed = Signal()

    def __init__(self, tracker: UsageTracker, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._setup_ui()
        self._load_current()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._spins: list[QDoubleSpinBox] = []
        self._btns: list[tuple[QPushButton, Any]] = []  # (button, color_getter)

        self._title = QLabel("价格表")
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        root.addWidget(self._title)

        self._info = QLabel(
            "· <b>基础价</b>（base）：2026-08-17 之前的老价格表，兼容旧记录。<br>"
            "· <b>峰谷价</b>（peakValley）：2026-08-17 00:00 起生效。<br>"
            "· 高峰时段（北京）：9:00–12:00、14:00–18:00；其余为空闲时段。<br>"
            "· 自动计价（auto）：按调用时间自动切换基础价 / 峰谷价。"
        )
        self._info.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._info.setTextFormat(Qt.TextFormat.RichText)
        self._info.setWordWrap(True)
        root.addWidget(self._info)

        # 生效时间标注
        self._effect_lbl = QLabel(f"新价格表生效时间（北京）：2026-08-17 00:00")
        self._effect_lbl.setStyleSheet(f"color: {_WARNING}; font-size: 12px; font-weight: 600;")
        root.addWidget(self._effect_lbl)

        # 分组：基础价 / 峰谷价
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            "QTabWidget::pane { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08); border-radius: 10px; }"
            f"QTabBar::tab {{ padding: 6px 16px; background: rgba(224,226,242,0.03); color: {_MUTED};"
            "  border: 1px solid rgba(224,226,242,0.06); border-bottom: none; margin-right: 4px;"
            "  border-top-left-radius: 8px; border-top-right-radius: 8px; }"
            f"QTabBar::tab:selected {{ background: rgba(50,240,140,0.08); color: {_ACCENT}; }}"
        )

        self._base_tab = self._build_base_tab()
        self._tabs.addTab(self._base_tab, "基础价 (base)")

        self._peak_tab = self._build_peak_tab()
        self._tabs.addTab(self._peak_tab, "峰谷价 (peakValley)")
        root.addWidget(self._tabs, stretch=1)

        # 底部按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_reset = _RecordsTab._mk_btn("↺ 恢复默认", _ERROR)
        self._btns.append((self._btn_reset, lambda: _ERROR))
        self._btn_reset.clicked.connect(self._reset)
        self._btn_save = _RecordsTab._mk_btn("💾 保存价格表", _ACCENT)
        self._btn_save.setStyleSheet(
            "QPushButton { padding: 6px 18px; background: rgba(50,240,140,0.15);"
            f"  border: 1px solid rgba(50,240,140,0.35); border-radius: 8px; color: {_ACCENT};"
            "  font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(50,240,140,0.25); }"
        )
        self._btn_save.clicked.connect(self._save)
        btn_row.addWidget(self._btn_reset)
        btn_row.addWidget(self._btn_save)
        root.addLayout(btn_row)

    def _spin(self, value: float) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(0.0, 9999.99)
        sp.setDecimals(4)
        sp.setSingleStep(0.01)
        sp.setValue(value)
        sp.setStyleSheet(
            "QDoubleSpinBox { padding: 4px 6px; background: rgba(224,226,242,0.05);"
            f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {_TEXT}; }}"
        )
        self._spins.append(sp)
        return sp

    def _build_base_tab(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(16, 16, 16, 16)
        grid.setSpacing(12)
        headers = ["模型", "缓存命中 (元/百万 token)", "输入未命中 (元/百万 token)", "输出 (元/百万 token)"]
        for c, h in enumerate(headers):
            lbl = QLabel(h)
            lbl.setStyleSheet("color: #9599A6; font-size: 12px; font-weight: 600;")
            grid.addWidget(lbl, 0, c)
        self._base_spins: dict[str, dict[str, QDoubleSpinBox]] = {}
        for r, mk in enumerate(PRICE_MODELS, start=1):
            name_map = {"deepseek-v4-flash": "deepseek-v4-flash", "deepseek-v4-pro": "deepseek-v4-pro"}
            lbl = QLabel(name_map[mk])
            lbl.setStyleSheet("color: #E0E2F2; font-weight: 600;")
            grid.addWidget(lbl, r, 0)
            self._base_spins[mk] = {}
            for c, k in enumerate(["cacheHit", "cacheMiss", "output"], start=1):
                sp = self._spin(0)
                self._base_spins[mk][k] = sp
                grid.addWidget(sp, r, c)
        return w

    def _build_peak_tab(self) -> QWidget:
        w = QScrollArea()
        w.setWidgetResizable(True)
        inner = QFrame()
        inner.setStyleSheet("QFrame { background: transparent; }")
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)
        self._peak_spins: dict[str, dict[str, dict[str, QDoubleSpinBox]]] = {}
        for tier_name, tier_key in [("🌞 高峰时段 (peak)", "peak"), ("🌙 空闲时段 (offPeak)", "offPeak")]:
            section = QLabel(tier_name)
            section.setStyleSheet("color: #E0E2F2; font-size: 14px; font-weight: 700; margin-top: 4px;")
            layout.addWidget(section)
            grid = QGridLayout()
            grid.setSpacing(12)
            headers = ["模型", "缓存命中 (元/百万)", "输入未命中 (元/百万)", "输出 (元/百万)"]
            for c, h in enumerate(headers):
                lbl = QLabel(h)
                lbl.setStyleSheet("color: #9599A6; font-size: 12px; font-weight: 600;")
                grid.addWidget(lbl, 0, c)
            self._peak_spins[tier_key] = {}
            for r, mk in enumerate(PRICE_MODELS, start=1):
                lbl = QLabel(mk)
                lbl.setStyleSheet("color: #E0E2F2; font-weight: 600;")
                grid.addWidget(lbl, r, 0)
                self._peak_spins[tier_key][mk] = {}
                for c, k in enumerate(["cacheHit", "cacheMiss", "output"], start=1):
                    sp = self._spin(0)
                    self._peak_spins[tier_key][mk][k] = sp
                    grid.addWidget(sp, r, c)
            layout.addLayout(grid)
        layout.addStretch()
        w.setWidget(inner)
        return w

    def _load_current(self) -> None:
        pricing = self._tracker.pricing
        for mk in PRICE_MODELS:
            for k in ("cacheHit", "cacheMiss", "output"):
                self._base_spins[mk][k].setValue(float(pricing["base"][mk][k]))
        for tier in ("peak", "offPeak"):
            for mk in PRICE_MODELS:
                for k in ("cacheHit", "cacheMiss", "output"):
                    self._peak_spins[tier][mk][k].setValue(float(pricing["peakValley"][mk][tier][k]))

    def apply_theme(self, theme) -> None:
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        self._info.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._effect_lbl.setStyleSheet(f"color: {_WARNING}; font-size: 12px; font-weight: 600;")
        self._tabs.setStyleSheet(
            "QTabWidget::pane { background: rgba(30,33,42,0.5); border: 1px solid rgba(224,226,242,0.08); border-radius: 10px; }"
            f"QTabBar::tab {{ padding: 6px 16px; background: rgba(224,226,242,0.03); color: {_MUTED};"
            "  border: 1px solid rgba(224,226,242,0.06); border-bottom: none; margin-right: 4px;"
            "  border-top-left-radius: 8px; border-top-right-radius: 8px; }"
            f"QTabBar::tab:selected {{ background: rgba(50,240,140,0.08); color: {_ACCENT}; }}"
        )
        for sp in self._spins:
            sp.setStyleSheet(
                "QDoubleSpinBox { padding: 4px 6px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {_TEXT}; }}"
            )
        for btn, color_getter in self._btns:
            color = color_getter()
            btn.setStyleSheet(
                f"QPushButton {{ padding: 5px 12px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {color};"
                f"  font-size: 12px; }}"
                f"QPushButton:hover {{ background: rgba(224,226,242,0.12); }}"
            )
        self._btn_save.setStyleSheet(
            "QPushButton { padding: 6px 18px; background: rgba(50,240,140,0.15);"
            f"  border: 1px solid rgba(50,240,140,0.35); border-radius: 8px; color: {_ACCENT};"
            "  font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(50,240,140,0.25); }"
        )

    def _collect(self) -> dict:
        import copy
        new_pricing = copy.deepcopy(self._tracker.pricing)
        for mk in PRICE_MODELS:
            for k in ("cacheHit", "cacheMiss", "output"):
                new_pricing["base"][mk][k] = float(self._base_spins[mk][k].value())
        for tier in ("peak", "offPeak"):
            for mk in PRICE_MODELS:
                for k in ("cacheHit", "cacheMiss", "output"):
                    new_pricing["peakValley"][mk][tier][k] = float(self._peak_spins[tier][mk][k].value())
        return new_pricing

    def _save(self) -> None:
        new_pricing = self._collect()
        if self._tracker.update_pricing(new_pricing):
            self.pricing_changed.emit()
            QMessageBox.information(self, "保存成功", "价格表已更新并持久化。")
        else:
            QMessageBox.warning(self, "保存失败", "数据校验未通过，请检查数值。")

    def _reset(self) -> None:
        if QMessageBox.question(
            self, "确认恢复",
            "将覆盖当前价格表为官方默认值，是否继续？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._tracker.reset_pricing()
        self._load_current()
        self.pricing_changed.emit()


# ============================================================
# 子页签 5：余额查询（沿用 DSH Work BalanceClient 双通道）
# ============================================================
class _BalanceTab(QWidget):
    def __init__(self, balance_client: BalanceClient, parent=None):
        super().__init__(parent)
        self._bc = balance_client
        self._temp_key_dialog_open = False
        self._setup_ui()
        # 如果已有缓存则显示
        cached = self._bc.cached
        if cached:
            self._apply_result(cached)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._btns: list[tuple[QPushButton, Any]] = []  # (button, color_getter)

        self._title = QLabel("剩余余额查询")
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        root.addWidget(self._title)

        self._info = QLabel(
            "💡 DSH Work 使用 <b>双通道容错</b>查询余额：<br>"
            "&nbsp;&nbsp;① 首选：<b>DSH 代理</b>（credentials.getBalance RPC，DSH 内部持有 Key，客户端只拿脱敏数字）<br>"
            "&nbsp;&nbsp;② 降级：<b>平台直连</b>（api.deepseek.com/user/balance，临时 Key 仅本次会话内存中存活，<b>不落盘</b>）<br>"
            "状态栏会明确标注余额来源。"
        )
        self._info.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._info.setTextFormat(Qt.TextFormat.RichText)
        self._info.setWordWrap(True)
        root.addWidget(self._info)

        # 大卡片显示余额
        self._card = QFrame()
        self._card.setObjectName("UsageCard")
        self._card.setStyleSheet(
            "QFrame#UsageCard {"
            "  background: rgba(50, 240, 140, 0.05);"
            "  border: 1px solid rgba(50, 240, 140, 0.20);"
            "  border-radius: 16px;"
            "}"
        )
        self._card.setMinimumHeight(160)
        cl = QVBoxLayout(self._card)
        cl.setContentsMargins(32, 24, 32, 24)
        cl.setSpacing(8)
        label = QLabel("当前账户余额")
        label.setStyleSheet(f"color: {_MUTED}; font-size: 13px;")
        cl.addWidget(label)
        self._value_lbl = QLabel("--")
        self._value_lbl.setStyleSheet(f"color: {_ACCENT}; font-size: 42px; font-weight: 800;")
        cl.addWidget(self._value_lbl)
        self._source_lbl = QLabel("尚未查询")
        self._source_lbl.setStyleSheet(f"color: {_DIM}; font-size: 12px;")
        cl.addWidget(self._source_lbl)
        self._time_lbl = QLabel("")
        self._time_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        cl.addWidget(self._time_lbl)
        cl.addStretch()
        root.addWidget(self._card)

        # 按钮行
        btn_row = QHBoxLayout()
        self._btn_query = _RecordsTab._mk_btn("🔍 查询余额", _ACCENT)
        self._btn_query.setStyleSheet(
            "QPushButton { padding: 8px 20px; background: rgba(50,240,140,0.15);"
            f"  border: 1px solid rgba(50,240,140,0.35); border-radius: 8px; color: {_ACCENT};"
            "  font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(50,240,140,0.25); }"
        )
        self._btn_query.clicked.connect(self._query)
        self._btn_force = _RecordsTab._mk_btn("🔄 强制刷新（忽略缓存）", _TRAE_BLUE)
        self._btns.append((self._btn_force, lambda: _TRAE_BLUE))
        self._btn_force.clicked.connect(lambda: self._query(force=True))
        self._btn_set_key = _RecordsTab._mk_btn("🔑 设置降级 Key（仅内存）", _WARNING)
        self._btns.append((self._btn_set_key, lambda: _WARNING))
        self._btn_set_key.clicked.connect(self._set_temp_key)
        self._btn_clear_key = _RecordsTab._mk_btn("🗑 清除临时 Key", _ERROR)
        self._btns.append((self._btn_clear_key, lambda: _ERROR))
        self._btn_clear_key.clicked.connect(self._clear_temp_key)
        for b in (self._btn_query, self._btn_force, self._btn_set_key, self._btn_clear_key):
            btn_row.addWidget(b)
        btn_row.addStretch()
        root.addLayout(btn_row)

        # 最近一次错误提示
        self._err_lbl = QLabel("")
        self._err_lbl.setStyleSheet(f"color: {_ERROR}; font-size: 12px; padding: 8px 4px;")
        self._err_lbl.setWordWrap(True)
        root.addWidget(self._err_lbl)

        root.addStretch()

    def _apply_result(self, r: BalanceResult) -> None:
        if not r.is_available:
            self._value_lbl.setText("不可用")
            self._value_lbl.setStyleSheet(f"color: {_ERROR}; font-size: 42px; font-weight: 800;")
            self._source_lbl.setText("查询失败")
            self._err_lbl.setText(f"错误：{r.error or '未知原因'}\n"
                                  "提示：首选 DSH 代理不可用？尝试设置降级临时 Key（🔑 按钮）。")
        else:
            self._value_lbl.setText(f"¥ {r.balance:.2f}")
            self._value_lbl.setStyleSheet(f"color: {_ACCENT}; font-size: 42px; font-weight: 800;")
            self._source_lbl.setText(f"来源：{r.source_label}　·　币种：{r.currency}")
            self._err_lbl.setText("")
        if r.queried_at:
            bj = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.queried_at))
            self._time_lbl.setText(f"查询时间（本地）：{bj}")

    def apply_theme(self, theme) -> None:
        self._title.setStyleSheet(f"color: {_TEXT}; font-size: 16px; font-weight: 700;")
        self._info.setStyleSheet(f"color: {_MUTED}; font-size: 12px;")
        self._card.setStyleSheet(
            "QFrame#UsageCard {"
            "  background: rgba(50, 240, 140, 0.05);"
            "  border: 1px solid rgba(50, 240, 140, 0.20);"
            "  border-radius: 16px;"
            "}"
        )
        self._source_lbl.setStyleSheet(f"color: {_DIM}; font-size: 12px;")
        self._time_lbl.setStyleSheet(f"color: {_DIM}; font-size: 11px;")
        self._err_lbl.setStyleSheet(f"color: {_ERROR}; font-size: 12px; padding: 8px 4px;")
        self._btn_query.setStyleSheet(
            "QPushButton { padding: 8px 20px; background: rgba(50,240,140,0.15);"
            f"  border: 1px solid rgba(50,240,140,0.35); border-radius: 8px; color: {_ACCENT};"
            "  font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(50,240,140,0.25); }"
        )
        for btn, color_getter in self._btns:
            color = color_getter()
            btn.setStyleSheet(
                f"QPushButton {{ padding: 5px 12px; background: rgba(224,226,242,0.05);"
                f"  border: 1px solid rgba(224,226,242,0.12); border-radius: 6px; color: {color};"
                f"  font-size: 12px; }}"
                f"QPushButton:hover {{ background: rgba(224,226,242,0.12); }}"
            )
        # 恢复 value_lbl 的正确状态
        cached = self._bc.cached
        if cached:
            self._apply_result(cached)
        else:
            self._value_lbl.setStyleSheet(f"color: {_ACCENT}; font-size: 42px; font-weight: 800;")

    def _query(self, force: bool = False) -> None:
        self._btn_query.setEnabled(False)
        self._btn_force.setEnabled(False)
        self._value_lbl.setText("查询中...")
        self._value_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 42px; font-weight: 800;")

        def _cb(result: BalanceResult) -> None:
            # 回到 UI 线程
            QTimer.singleShot(0, lambda: self._apply_cb(result))

        self._bc.query_async(_cb, force=force)

    def _apply_cb(self, result: BalanceResult) -> None:
        self._apply_result(result)
        self._btn_query.setEnabled(True)
        self._btn_force.setEnabled(True)
        # 如果首选失败且没有临时 Key，提示用户
        if not result.is_available and not self._bc._temp_api_key:
            if QMessageBox.question(
                self, "DSH 代理查询失败",
                f"{result.error}\n\n是否立即设置降级通道临时 API Key（仅本次会话内存，不落盘）？",
            ) == QMessageBox.StandardButton.Yes:
                self._set_temp_key()

    def _set_temp_key(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        if self._temp_key_dialog_open:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("设置降级通道临时 API Key")
        dlg.setMinimumWidth(460)
        lay = QVBoxLayout(dlg)
        tip = QLabel(
            "该 Key <b>仅在本次会话内存中存活</b>，绝不写入磁盘或日志。\n"
            "关闭应用即失效。用于 DSH 代理不可用时的平台直连降级。"
        )
        tip.setStyleSheet("color: #FFB454; font-size: 12px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        input_le = QLineEdit()
        input_le.setPlaceholderText("sk-xxxxxxxxxxxxxxxxxxxxxxxx")
        input_le.setEchoMode(QLineEdit.EchoMode.Password)
        input_le.setStyleSheet(
            "QLineEdit { padding: 8px 12px; background: rgba(224,226,242,0.05);"
            "  border: 1px solid rgba(224,226,242,0.15); border-radius: 8px; color: #E0E2F2; font-family: 'Consolas', monospace; }"
        )
        lay.addWidget(input_le)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("保存（仅内存）")
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        self._temp_key_dialog_open = True
        if dlg.exec() == QDialog.DialogCode.Accepted:
            key = input_le.text().strip()
            if key:
                self._bc.set_temp_api_key(key)
                QMessageBox.information(self, "已设置", "临时 Key 已设置（仅本次会话内存）。可以再次查询余额。")
        self._temp_key_dialog_open = False

    def _clear_temp_key(self) -> None:
        self._bc.clear_temp_api_key()
        QMessageBox.information(self, "已清除", "临时 API Key 已从内存中清除。")


# ============================================================
# 主面板：承载 5 个子页签
# ============================================================
class UsagePanel(QWidget):
    """用量与消耗主面板（与对话区域并列，建议放入 QTabWidget 中）。"""

    def __init__(self, tracker: UsageTracker, balance_client: BalanceClient, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._balance_client = balance_client
        self._setup_ui()
        # 监听数据变化，自动刷新概览/日历/列表
        self._tracker.add_listener(self._on_data_changed)
        # 注册主题监听器：主题切换时刷新所有子页签样式
        from ..theme.theme_manager import ThemeManager
        tm = ThemeManager()
        tm.add_listener(self.apply_theme)
        # 立即应用当前主题
        if tm._current:
            self.apply_theme(tm._current)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        # 不用 documentMode：Windows 下 documentMode 会启用原生绘制，导致 Tab 栏背景白色、不受 QSS 控制
        self._tabs.setStyleSheet(
            "QTabWidget::pane { border: none; }"
            "QTabBar::tab {"
            f"  padding: 10px 22px; font-size: 13px; color: {_MUTED};"
            "  background: transparent; border: none; border-bottom: 2px solid transparent;"
            "}"
            f"QTabBar::tab:selected {{ color: {_ACCENT}; border-bottom-color: {_ACCENT}; font-weight: 600; }}"
            f"QTabBar::tab:hover:!selected {{ color: {_TEXT}; background: rgba(224,226,242,0.04); }}"
            "QTabBar::tab-bar { alignment: left; padding-left: 16px; }"
        )

        self._overview = _OverviewTab(self._tracker)
        self._tabs.addTab(self._overview, "📊 概览")

        self._calendar = _CalendarTab(self._tracker)
        self._tabs.addTab(self._calendar, "🗓 用量日历")

        self._records = _RecordsTab(self._tracker)
        self._tabs.addTab(self._records, "📋 记录列表")

        self._pricing = _PricingTab(self._tracker)
        self._pricing.pricing_changed.connect(self._on_pricing_changed)
        self._tabs.addTab(self._pricing, "💰 价格表")

        self._balance = _BalanceTab(self._balance_client)
        self._tabs.addTab(self._balance, "💳 余额查询")

        layout.addWidget(self._tabs)

    # 当 tracker 有新记录或价格表变化时，懒刷新当前子页签
    def _on_data_changed(self) -> None:
        idx = self._tabs.currentIndex()
        try:
            if idx == 0:
                self._overview.refresh()
            elif idx == 1:
                self._calendar.refresh()
            elif idx == 2:
                self._records.refresh()
        except Exception as e:
            log.warning("UsagePanel 自动刷新异常: %s", e)

    def _on_pricing_changed(self) -> None:
        # 价格表变化后，所有聚合都要刷新
        self._overview.refresh()
        self._calendar.refresh()
        self._records.refresh()

    def apply_theme(self, theme) -> None:
        """主题切换：更新模块级颜色变量，刷新主 Tab 样式，并级联到所有子页签。"""
        _apply_theme_colors(theme)
        self._tabs.setStyleSheet(
            "QTabWidget::pane { border: none; }"
            "QTabBar::tab {"
            f"  padding: 10px 22px; font-size: 13px; color: {_MUTED};"
            "  background: transparent; border: none; border-bottom: 2px solid transparent;"
            "}"
            f"QTabBar::tab:selected {{ color: {_ACCENT}; border-bottom-color: {_ACCENT}; font-weight: 600; }}"
            f"QTabBar::tab:hover:!selected {{ color: {_TEXT}; background: rgba(224,226,242,0.04); }}"
            "QTabBar::tab-bar { alignment: left; padding-left: 16px; }"
        )
        self._overview.apply_theme(theme)
        self._calendar.apply_theme(theme)
        self._records.apply_theme(theme)
        self._pricing.apply_theme(theme)
        self._balance.apply_theme(theme)
