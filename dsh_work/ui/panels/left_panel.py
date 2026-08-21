"""左栏：Web 版 Sidebar（对齐 DSH Web UI 侧边栏）。

四个视图页面（顶部导航按钮切换，替代旧的 ActivityBar）：
- sessions: 会话列表（含状态点：running 蓝 / pending 琥珀 / done 绿）
- files: 文件树
- search: 搜索
- git: Git 面板
底部固定「设置」入口（sidebar.settings 座）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)


def _theme_colors():
    """获取当前主题颜色（运行时从单例 ThemeManager 读取）。

    返回 ThemeColors 或 None（主题未加载时）。
    注意：必须在 app._load_theme() 之后调用才能拿到真实主题；
    组件初始化时若拿到 None，apply_theme 会在主题加载/切换时补刷新。
    """
    try:
        from ..theme.theme_manager import ThemeManager
        tm = ThemeManager()
        theme = tm.current
        if theme and theme.colors:
            return theme.colors
    except Exception:
        pass
    return None


# 暗色回退配色（主题系统未就绪时用，保证文字在深色背景上可读）
_FALLBACK = {
    "bg": "#111113",
    "text": "#D1D3DB",
    "text2": "#9599A6",
    "text_mute": "#666B75",
    "border": "#B5BDC5",
    "bg_hover": "#B5BDC5",
    "bg_active": "#B5BDC5",
    "input_bg": "#B5BDC5",
    "input_border": "#B5BDC5",
    "border_light": "#B5BDC5",
    "accent": "#679EFE",
    "brand": "#F9FAFB",
    "on_brand": "#151517",
    "brand_hover": "#CFD3D6",
}


def _is_dark_color(hex_or_rgba: str) -> bool:
    """粗略判断一个颜色串是深色还是浅色（用于算品牌按钮上的文字对比色）。"""
    try:
        import re
        m = re.match(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)", hex_or_rgba)
        if m:
            r, g, b = (int(x) for x in m.groups())
        else:
            s = hex_or_rgba.lstrip("#")
            r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
        return (0.299 * r + 0.587 * g + 0.114 * b) < 128
    except Exception:
        return True


def _qcolor(value: str) -> QColor:
    """把主题颜色字符串安全解析为 QColor。

    主题 token 使用 CSS 风格 `rgba(r, g, b, 0.10)`（alpha 为 0-1 浮点），
    而 Qt 的 QColor.setNamedColor 只接受 0-255 整数 alpha，直接传入会得到
    无效颜色（渲染为黑色，浅色主题选中态会黑底深字看不清）。这里手动解析。
    """
    import re

    if not value:
        return QColor("#000000")
    m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+)\s*)?\)", value)
    if m:
        r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        a = m.group(4)
        alpha = int(round(float(a) * 255)) if a else 255
        return QColor(r, g, b, alpha)
    return QColor(value)


def _palette() -> dict:
    """读取当前主题配色，回退到暗色默认值。"""
    tc = _theme_colors()
    if tc:
        brand = tc.brand_primary or _FALLBACK["brand"]
        on_brand = "#F9FAFB" if _is_dark_color(brand) else "#151517"
        return {
            "bg": tc.bg_primary or _FALLBACK["bg"],
            "text": tc.text_primary or _FALLBACK["text"],
            "text2": tc.text_secondary or _FALLBACK["text2"],
            "text_mute": tc.text_muted or _FALLBACK["text_mute"],
            "border": tc.border or _FALLBACK["border"],
            "bg_hover": tc.bg_hover or _FALLBACK["bg_hover"],
            "bg_active": tc.bg_active or _FALLBACK["bg_active"],
            "input_bg": tc.input_bg or _FALLBACK["input_bg"],
            "input_border": tc.input_border or _FALLBACK["input_border"],
            "border_light": tc.border_light or _FALLBACK["border_light"],
            "accent": tc.accent or _FALLBACK["accent"],
            "brand": brand,
            "on_brand": on_brand,
            "brand_hover": tc.text_secondary or _FALLBACK["brand_hover"],
        }
    return dict(_FALLBACK)


class SessionItemDelegate(QStyledItemDelegate):
    """会话行渲染器（对齐 Web 版会话行：状态点 + 标题 + 相对时间）。

    行内结构：
        ● 会话标题
          3 分钟前
    状态点颜色：running=accent蓝 / pending=琥珀 / done=绿 / idle=透明。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dot_colors = {
            "running": "#679EFE",
            "pending": "#F59E0B",
            "done": "#22C55E",
        }
        self._palette_ctx = {}

    def set_palette_ctx(self, ctx: dict) -> None:
        """由外层 apply_theme 注入当前主题色。"""
        self._palette_ctx = ctx or {}

    def paint(self, painter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        p = self._palette_ctx

        # 工作区组标题（UserRole+3 == "header"）
        if index.data(Qt.ItemDataRole.UserRole + 3) == "header":
            painter.fillRect(rect, QColor("transparent"))
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            f_h = QFont(option.font)
            f_h.setPointSize(8)
            f_h.setWeight(QFont.Weight.DemiBold)
            painter.setFont(f_h)
            painter.setPen(_qcolor(p.get("muted", "#81858C")))
            h_rect = QRect(rect.left() + 14, rect.top(), rect.width() - 24, rect.height())
            painter.drawText(h_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            painter.restore()
            return

        # 背景：选中 / 悬停
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, _qcolor(p.get("active", "rgba(255,255,255,0.14)")))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, _qcolor(p.get("hover", "rgba(255,255,255,0.08)")))

        # 状态点（左侧 12px 处，8px 圆）
        status = index.data(Qt.ItemDataRole.UserRole + 1) or "idle"
        color = self.dot_colors.get(status, "transparent")
        dot_x = rect.left() + 14
        dot_y = rect.top() + 12
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(dot_x - 4, dot_y - 4, 8, 8)

        # 标题（状态点右侧）
        title = index.data(Qt.ItemDataRole.DisplayRole) or "新对话"
        time_str = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        text_x = rect.left() + 26
        title_rect = QRect(text_x, rect.top() + 6, rect.width() - text_x - 10, 18)
        f_title = QFont(option.font)
        f_title.setPointSize(10)
        f_title.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f_title)
        painter.setPen(_qcolor(p.get("text", "#F9FAFB")))
        elided = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)

        # 时间（muted 色）
        if time_str:
            time_rect = QRect(text_x, rect.top() + 24, rect.width() - text_x - 10, 16)
            f_time = QFont(option.font)
            f_time.setPointSize(8)
            painter.setFont(f_time)
            painter.setPen(_qcolor(p.get("muted", "#81858C")))
            painter.drawText(time_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, time_str)

        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return QSize(super().sizeHint(option, index).width(), 46)


class TaskListWidget(QListWidget):
    """会话/任务列表（Web 版风格：状态点 + 标题 + 相对时间）。"""

    session_selected = Signal(str)  # session_id
    session_delete_requested = Signal(str)  # session_id
    session_rename_requested = Signal(str)  # session_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._delegate = SessionItemDelegate(self)
        self.setItemDelegate(self._delegate)
        self.setAlternatingRowColors(False)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.apply_theme()

    def apply_theme(self, theme=None) -> None:
        """刷新样式表与 delegate 配色为当前主题。"""
        p = _palette()
        self._delegate.set_palette_ctx({
            "text": p["text"],
            "muted": p["text_mute"],
            "hover": p["bg_hover"],
            "active": p["bg_active"],
        })
        self.setStyleSheet(
            "QListWidget {"
            "  background-color: transparent;"
            "  border: none;"
            f"  color: {p['text']};"
            "}"
        )

    def refresh(self, sessions: list, status_map: dict | None = None) -> None:
        """刷新会话列表（按工作区分组，对齐 Web 版 Workspace 分组）。

        Args:
            sessions: SessionInfo 列表
            status_map: {session_id: "running"|"pending"|"done"|"idle"}
        """
        self.clear()
        status_map = status_map or {}

        # 按 cwd 工作区分组（cwd 为空 → 归入"未分组"）
        groups: dict[str, list] = {}
        for session in sessions:
            key = session.cwd or ""
            groups.setdefault(key, []).append(session)

        # 组排序：按组内最新会话的 updated_at 倒序（最近活跃在前）
        ordered_groups = sorted(
            groups.items(),
            key=lambda kv: -(kv[1][0].updated_at or kv[1][0].created_at or 0),
        )

        for key, group in ordered_groups:
            if key:
                self._add_group_header(key)
            # 组内会话按 updated_at 倒序
            group.sort(key=lambda s: -(s.updated_at or s.created_at or 0))
            for session in group:
                self._add_session_item(session, status_map)

    def _add_group_header(self, cwd: str) -> None:
        """插入工作区组标题（不可选中，delegate 画小字 muted 样式）。"""
        from pathlib import Path
        try:
            name = Path(cwd).name or cwd
        except Exception:
            name = cwd
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole + 3, "header")  # 标记为组标题
        item.setData(Qt.ItemDataRole.DisplayRole, name)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # 不可选/不可点击
        item.setSizeHint(QSize(0, 26))
        self.addItem(item)

    def _add_session_item(self, session, status_map: dict) -> None:
        """插入单个会话项。"""
        title = session.title or "新对话"
        time_str = _format_relative_time(session.updated_at or session.created_at)
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, session.id)
        item.setData(Qt.ItemDataRole.UserRole + 1, status_map.get(session.id, "idle"))
        item.setData(Qt.ItemDataRole.UserRole + 2, time_str)
        item.setSizeHint(QSize(0, 46))
        self.addItem(item)

    def on_item_clicked(self, item: QListWidgetItem) -> None:
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if session_id:
            self.session_selected.emit(session_id)

    def _on_context_menu(self, pos) -> None:
        """右键菜单：重命名 / 删除。"""
        from PySide6.QtWidgets import QMenu
        item = self.itemAt(pos)
        if not item:
            return
        session_id = item.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            return
        menu = QMenu(self)
        act_rename = menu.addAction("重命名")
        act_delete = menu.addAction("删除")
        act_delete.triggered.connect(lambda: self.session_delete_requested.emit(session_id))
        act_rename.triggered.connect(lambda: self.session_rename_requested.emit(session_id))
        menu.exec(self.mapToGlobal(pos))


def _format_relative_time(ts: float) -> str:
    """格式化相对时间（匹配 DSH Web UI 风格）。"""
    if not ts:
        return ""
    import time as _time
    diff = _time.time() - ts
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff / 60)} 分钟前"
    if diff < 86400:
        return f"{int(diff / 3600)} 小时前"
    if diff < 604800:
        return f"{int(diff / 86400)} 天前"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class FileTreeWidget(QTreeWidget):
    """文件树。"""

    file_activated = Signal(str)  # file_path

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self._modified_files: dict[str, str] = {}
        self.itemDoubleClicked.connect(self._on_double_click)
        self.apply_theme()

    def apply_theme(self, theme=None) -> None:
        """刷新样式表 + 调色板为当前主题配色。

        QTreeWidget::item { color } 的 QSS 在部分 Qt 版本/平台对未显式 setForeground
        的 item 不可靠（会用系统调色板的WindowText，深色主题下默认是黑色）。
        因此同时设置 widget 调色板的 Text/WindowText 颜色做兜底，确保文字可读。
        """
        p = _palette()
        self.setStyleSheet(
            "QTreeWidget {"
            "  background-color: transparent;"
            "  border: none;"
            f"  color: {p['text']};"
            "}"
            "QTreeWidget::item {"
            "  padding: 4px 8px;"
            f"  color: {p['text']};"
            "}"
            "QTreeWidget::item:hover {"
            f"  background-color: {p['bg_hover']};"
            "}"
            "QTreeWidget::item:selected {"
            "  background-color: rgba(120, 119, 198, 0.15);"
            f"  color: {p['text']};"
            "}"
        )
        # 调色板兜底：WindowText 决定 QTreeWidgetItem 默认文字色
        from PySide6.QtGui import QColor, QPalette
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Text, QColor(p["text"]))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(p["text"]))
        self.setPalette(pal)
        # 已有节点刷新前景色（未显式标记的节点随调色板，标记过的重新上色）
        self._refresh_items_foreground()

    def _refresh_items_foreground(self) -> None:
        """遍历现有节点：未标记的清除显式前景（回落到调色板），标记的重新上色。"""
        from PySide6.QtGui import QBrush
        it = QTreeWidgetItemIterator(self)
        while it.value() is not None:
            item = it.value()
            str_path = item.data(0, Qt.ItemDataRole.UserRole) or ""
            status = self._modified_files.get(str_path)
            if status == "new":
                item.setForeground(0, QBrush(self._color_green()))
            elif status:
                item.setForeground(0, QBrush(self._color_orange()))
            else:
                # 清除显式前景，回落到 widget 调色板（随主题变色）
                item.setForeground(0, QBrush())
            it += 1

    def mark_file(self, path: str, status: str) -> None:
        self._modified_files[path] = status

    def load_workspace(self, workspace: str) -> None:
        from pathlib import Path
        self.clear()
        if not workspace:
            return
        root = Path(workspace)
        if not root.exists():
            return
        root_item = QTreeWidgetItem([root.name])
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(root))
        self.addTopLevelItem(root_item)
        self._build_tree(root, root_item, depth=0)
        # 载入完成后刷新一次前景，让调色板兜底生效
        self._refresh_items_foreground()

    def _build_tree(self, path: Path, parent_item: QTreeWidgetItem, depth: int) -> None:
        if depth > 5:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return
        ignore = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode"}
        for entry in entries:
            if entry.name in ignore:
                continue
            item = QTreeWidgetItem([entry.name])
            item.setData(0, Qt.ItemDataRole.UserRole, str(entry))
            # 不在此处 setForeground，统一交给 _refresh_items_foreground 处理，
            # 避免显式前景覆盖调色板导致切主题后颜色不刷新。
            parent_item.addChild(item)
            if entry.is_dir():
                self._build_tree(entry, item, depth + 1)

    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.file_activated.emit(path)

    @staticmethod
    def _color_green():
        from PySide6.QtGui import QColor
        return QColor("#33C192")

    @staticmethod
    def _color_orange():
        from PySide6.QtGui import QColor
        return QColor("#D27E24")


class GitPanel(QWidget):
    """Git 面板。"""

    commit_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()
        self.apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._branch_label = QLabel("分支: -")
        layout.addWidget(self._branch_label)

        self._changes_title = QLabel("未提交变更:")
        layout.addWidget(self._changes_title)
        self._changes_list = QListWidget()
        self._changes_list.setMaximumHeight(120)
        layout.addWidget(self._changes_list)

        self._commits_title = QLabel("最近提交:")
        layout.addWidget(self._commits_title)
        self._commits_list = QListWidget()
        self._commits_list.setMaximumHeight(120)
        layout.addWidget(self._commits_list)

    def apply_theme(self, theme=None) -> None:
        p = _palette()
        self._branch_label.setStyleSheet(
            f"font-weight: 600; font-size: 12px; color: {p['text']};"
        )
        mute = f"font-size: 11px; color: {p['text_mute']};"
        self._changes_title.setStyleSheet(mute)
        self._commits_title.setStyleSheet(mute)
        for w in (self._changes_list, self._commits_list):
            w.setStyleSheet(
                "QListWidget { background-color: transparent; border: none; }"
            )

    def refresh(self, branch: str = "", changes: list[str] | None = None, commits: list[str] | None = None) -> None:
        self._branch_label.setText(f"分支: {branch or '-'}")
        self._changes_list.clear()
        for c in changes or []:
            self._changes_list.addItem(c)
        self._commits_list.clear()
        for c in commits or []:
            self._commits_list.addItem(c)


def _section_label(text: str) -> QLabel:
    """创建面板分区的标题标签（用当前主题 muted 色）。"""
    p = _palette()
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"font-size: 11px; color: {p['text_mute']}; font-weight: 600;"
        " text-transform: uppercase; letter-spacing: 0.05em;"
        " padding: 4px 8px;"
    )
    return lbl


class LeftPanel(QWidget):
    """Web 版 Sidebar：顶部导航 + 会话/文件树/搜索/Git + 底部设置。

    信号：
        session_selected(str): 会话点击
        file_activated(str): 文件双击
        new_session_requested(): 新建会话
        nav_requested(str): 顶部导航点击（sessions/files/search/git）
        settings_requested(): 底部设置点击
        close_requested(): 请求关闭面板（纯净界面模式）
    """

    session_selected = Signal(str)
    session_delete_requested = Signal(str)
    session_rename_requested = Signal(str)
    file_activated = Signal(str)
    new_session_requested = Signal()
    nav_requested = Signal(str)
    settings_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("WebSidebar")
        self.setMinimumWidth(220)
        self._section_labels: list[QLabel] = []
        self._setup_ui()
        self.apply_theme()

        # 注册主题监听器：切主题时刷新本面板及子组件样式
        try:
            from ..theme.theme_manager import ThemeManager
            ThemeManager().add_listener(self.apply_theme)
        except Exception:
            pass

    def apply_theme(self, theme=None) -> None:
        """刷新本面板及所有子组件的样式为当前主题配色。"""
        p = _palette()
        self.setStyleSheet(
            "QWidget#WebSidebar {"
            f"  background-color: {p['bg']};"
            f"  border-right: 1px solid {p['border']};"
            "}"
        )
        # 顶部导航按钮
        nav_style = (
            "QPushButton {"
            "  background-color: transparent;"
            "  border: none;"
            "  border-radius: 6px;"
            f"  color: {p['text_mute']};"
            "  font-size: 12px;"
            "}"
            "QPushButton:hover {"
            f"  background-color: {p['bg_hover']};"
            f"  color: {p['text']};"
            "}"
            f"QPushButton:checked {{ color: {p['accent']}; font-weight: 600; }}"
        )
        for btn in getattr(self, "_nav_btns", {}).values():
            btn.setStyleSheet(nav_style)
        # 新建会话按钮（Web 版 brand 风格）
        if getattr(self, "_new_session_btn", None) is not None:
            self._new_session_btn.setStyleSheet(
                "QPushButton {"
                f"  background-color: {p['brand']};"
                f"  color: {p['on_brand']};"
                "  border: none;"
                "  border-radius: 8px;"
                "  padding: 8px 12px;"
                "  font-size: 13px;"
                "  font-weight: 600;"
                "  text-align: center;"
                "}"
                "QPushButton:hover {"
                f"  background-color: {p['brand_hover']};"
                "}"
            )
        # 底部设置按钮
        if getattr(self, "_settings_btn", None) is not None:
            self._settings_btn.setStyleSheet(
                "QPushButton {"
                "  background-color: transparent;"
                "  border: none;"
                "  border-radius: 6px;"
                f"  color: {p['text_mute']};"
                "  font-size: 13px;"
                "  padding: 8px 12px;"
                "  text-align: left;"
                "}"
                "QPushButton:hover {"
                f"  background-color: {p['bg_hover']};"
                f"  color: {p['text']};"
                "}"
            )
        # 关闭按钮
        if getattr(self, "_close_btn", None) is not None:
            self._close_btn.setStyleSheet(
                "QPushButton {"
                "  background-color: transparent;"
                "  border: none;"
                "  border-radius: 4px;"
                f"  color: {p['text_mute']};"
                "  font-size: 14px;"
                "}"
                "QPushButton:hover {"
                f"  background-color: {p['bg_hover']};"
                f"  color: {p['text']};"
                "}"
            )
        # 空状态提示
        if getattr(self, "_empty_sessions_label", None) is not None:
            self._empty_sessions_label.setStyleSheet(
                f"color: {p['text_mute']}; font-size: 12px; padding: 24px 0;"
            )
        # 搜索输入框
        if getattr(self, "_search_input", None) is not None:
            self._search_input.setStyleSheet(
                "QLineEdit {"
                f"  background-color: {p['input_bg']};"
                f"  border: 1px solid {p['input_border']};"
                "  border-radius: 6px;"
                "  padding: 6px 10px;"
                "  font-size: 12px;"
                f"  color: {p['text']};"
                "}"
            )
        # 搜索结果列表
        if getattr(self, "_search_results", None) is not None:
            self._search_results.setStyleSheet(
                "QListWidget { background-color: transparent; border: none; }"
            )
        # 分区标题（muted 色）
        mute = (
            f"font-size: 11px; color: {p['text_mute']}; font-weight: 600;"
            " text-transform: uppercase; letter-spacing: 0.05em;"
            " padding: 4px 8px;"
        )
        for lbl in getattr(self, "_section_labels", []):
            lbl.setStyleSheet(mute)
        # 子组件
        if getattr(self, "_task_list", None) is not None:
            self._task_list.apply_theme()
        if getattr(self, "_file_tree", None) is not None:
            self._file_tree.apply_theme()
        if getattr(self, "_git_panel", None) is not None:
            self._git_panel.apply_theme()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ===== 顶部导航行（替代 ActivityBar）：会话 / 文件 / 搜索 / Git =====
        nav_row = QHBoxLayout()
        nav_row.setContentsMargins(8, 8, 8, 4)
        nav_row.setSpacing(2)
        self._nav_btns: dict[str, QPushButton] = {}
        for key, icon_text in (
            ("sessions", "会话"),
            ("files", "文件"),
            ("search", "搜索"),
            ("git", "Git"),
        ):
            btn = QPushButton(icon_text)
            btn.setObjectName("SidebarBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._on_nav_clicked(k))
            self._nav_btns[key] = btn
            nav_row.addWidget(btn)
        nav_row.addStretch()
        # 关闭面板按钮
        self._close_btn = QPushButton("×")
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("关闭面板 (Ctrl+B)")
        self._close_btn.clicked.connect(self.close_requested)
        nav_row.addWidget(self._close_btn)
        nav_widget = QWidget()
        nav_widget.setObjectName("SidebarHeader")
        nav_widget.setLayout(nav_row)
        layout.addWidget(nav_widget)
        self._nav_btns["sessions"].setChecked(True)

        self._stack = QStackedWidget()

        # ===== 页面 1: 会话列表（Web 版风格） =====
        sessions_page = QWidget()
        s_layout = QVBoxLayout(sessions_page)
        s_layout.setContentsMargins(8, 4, 8, 0)
        s_layout.setSpacing(4)

        # 顶部：新建会话按钮（Web 版 brand 风格，无自定义硬编码色）
        self._new_session_btn = QPushButton("＋ 新建会话")
        self._new_session_btn.setObjectName("NewSessionBtn")
        self._new_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_session_btn.clicked.connect(self.new_session_requested)
        s_layout.addWidget(self._new_session_btn)

        # 空状态提示
        self._empty_sessions_label = QLabel("暂无会话")
        self._empty_sessions_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s_layout.addWidget(self._empty_sessions_label)

        # 会话列表
        self._task_list = TaskListWidget()
        self._task_list.itemClicked.connect(self._task_list.on_item_clicked)
        self._task_list.session_selected.connect(self.session_selected)
        self._task_list.session_delete_requested.connect(self.session_delete_requested)
        self._task_list.session_rename_requested.connect(self.session_rename_requested)
        s_layout.addWidget(self._task_list, stretch=1)
        self._sessions_index = self._stack.addWidget(sessions_page)

        # ===== 页面 2: 文件树 =====
        files_page = QWidget()
        f_layout = QVBoxLayout(files_page)
        f_layout.setContentsMargins(0, 8, 0, 0)
        f_layout.setSpacing(0)
        lbl = _section_label("文件树")
        self._section_labels.append(lbl)
        f_layout.addWidget(lbl)
        self._file_tree = FileTreeWidget()
        self._file_tree.file_activated.connect(self.file_activated)
        f_layout.addWidget(self._file_tree, stretch=1)
        self._files_index = self._stack.addWidget(files_page)

        # ===== 页面 3: 搜索（预留） =====
        search_page = QWidget()
        sr_layout = QVBoxLayout(search_page)
        sr_layout.setContentsMargins(8, 8, 8, 8)
        sr_layout.setSpacing(8)
        lbl = _section_label("搜索")
        self._section_labels.append(lbl)
        sr_layout.addWidget(lbl)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索会话和文件...")
        sr_layout.addWidget(self._search_input)
        self._search_results = QListWidget()
        sr_layout.addWidget(self._search_results, stretch=1)
        self._search_index = self._stack.addWidget(search_page)

        # ===== 页面 4: Git =====
        git_page = QWidget()
        g_layout = QVBoxLayout(git_page)
        g_layout.setContentsMargins(0, 8, 0, 0)
        g_layout.setSpacing(0)
        lbl = _section_label("Git")
        self._section_labels.append(lbl)
        g_layout.addWidget(lbl)
        self._git_panel = GitPanel()
        g_layout.addWidget(self._git_panel, stretch=1)
        self._git_index = self._stack.addWidget(git_page)

        layout.addWidget(self._stack, stretch=1)

        # ===== 底部：设置入口（Web sidebar.settings 座） =====
        self._settings_btn = QPushButton("⚙  设置")
        self._settings_btn.setObjectName("SidebarBtn")
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.clicked.connect(self.settings_requested)
        settings_row = QHBoxLayout()
        settings_row.setContentsMargins(8, 4, 8, 8)
        settings_row.addWidget(self._settings_btn)
        settings_widget = QWidget()
        settings_widget.setObjectName("SidebarFooter")
        settings_widget.setLayout(settings_row)
        layout.addWidget(settings_widget)

        # 默认显示会话列表
        self._stack.setCurrentIndex(self._sessions_index)

    def _on_nav_clicked(self, nav: str) -> None:
        """顶部导航点击：切换页面并同步按钮选中态。"""
        for key, btn in self._nav_btns.items():
            btn.setChecked(key == nav)
        self.set_nav(nav)
        self.nav_requested.emit(nav)

    def set_nav(self, nav: str) -> None:
        """根据导航切换页面。"""
        nav_map = {
            "sessions": self._sessions_index,
            "files": self._files_index,
            "search": self._search_index,
            "git": self._git_index,
        }
        index = nav_map.get(nav, self._sessions_index)
        self._stack.setCurrentIndex(index)
        if nav in self._nav_btns:
            for key, btn in self._nav_btns.items():
                btn.setChecked(key == nav)

    def set_mode(self, mode: str) -> None:
        """兼容接口：模式切换不再影响左栏内容，由导航控制。"""
        pass

    def refresh_sessions(self, sessions: list, status_map: dict | None = None) -> None:
        self._task_list.refresh(sessions, status_map=status_map)
        # 空状态：无会话时显示"暂无会话"，有会话时隐藏
        self._empty_sessions_label.setVisible(len(sessions) == 0)

    def load_workspace(self, workspace: str) -> None:
        self._file_tree.load_workspace(workspace)
