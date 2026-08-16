"""左栏：二级面板（TRAE Work 风格，由 ActivityBar 导航控制）。

四个视图页面，由 ActivityBar 的 nav_changed 信号切换：
- sessions: 会话/任务列表 + 文档大纲
- files: 文件树
- search: 搜索（预留）
- git: Git 面板
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QStackedWidget,
    QPushButton,
)

from ... import constants as C


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
    "input_bg": "#B5BDC5",
    "input_border": "#B5BDC5",
    "border_light": "#B5BDC5",
}


def _palette() -> dict:
    """读取当前主题配色，回退到暗色默认值。"""
    tc = _theme_colors()
    if tc:
        return {
            "bg": tc.bg_primary or _FALLBACK["bg"],
            "text": tc.text_primary or _FALLBACK["text"],
            "text2": tc.text_secondary or _FALLBACK["text2"],
            "text_mute": tc.text_muted or _FALLBACK["text_mute"],
            "border": tc.border or _FALLBACK["border"],
            "bg_hover": tc.bg_hover or _FALLBACK["bg_hover"],
            "input_bg": tc.input_bg or _FALLBACK["input_bg"],
            "input_border": tc.input_border or _FALLBACK["input_border"],
            "border_light": tc.border_light or _FALLBACK["border_light"],
        }
    return dict(_FALLBACK)


class TaskListWidget(QListWidget):
    """会话/任务列表（DSH Web UI 风格：简洁、无多余信息）。"""

    session_selected = Signal(str)  # session_id
    session_delete_requested = Signal(str)  # session_id
    session_rename_requested = Signal(str)  # session_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAlternatingRowColors(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.apply_theme()

    def apply_theme(self, theme=None) -> None:
        """刷新样式表为当前主题配色。"""
        p = _palette()
        self.setStyleSheet(
            "QListWidget {"
            "  background-color: transparent;"
            "  border: none;"
            f"  color: {p['text']};"
            "}"
            "QListWidget::item {"
            "  padding: 8px 12px;"
            "  border-radius: 6px;"
            "  margin: 1px 4px;"
            f"  color: {p['text']};"
            "}"
            "QListWidget::item:hover {"
            f"  background-color: {p['bg_hover']};"
            "}"
            "QListWidget::item:selected {"
            "  background-color: rgba(120, 119, 198, 0.15);"
            f"  color: {p['text']};"
            "}"
        )

    def refresh(self, sessions: list) -> None:
        self.clear()
        for session in sessions:
            title = session.title or "新对话"
            time_str = _format_relative_time(session.updated_at or session.created_at)
            item_text = f"{title}\n{time_str}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, session.id)
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
        from PySide6.QtGui import QPalette, QColor
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
    """二级面板：由 ActivityBar 导航控制显示内容。

    信号：
        session_selected(str): 会话点击
        file_activated(str): 文件双击
        new_session_requested(): 新建会话
        close_requested(): 请求关闭面板（纯净界面模式）
    """

    session_selected = Signal(str)
    session_delete_requested = Signal(str)
    session_rename_requested = Signal(str)
    file_activated = Signal(str)
    new_session_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("LeftPanel")
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
            "QWidget#LeftPanel {"
            f"  background-color: {p['bg']};"
            f"  border-right: 1px solid {p['border']};"
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

        # 顶部关闭按钮行（纯净界面模式）
        close_row = QHBoxLayout()
        close_row.setContentsMargins(4, 4, 4, 0)
        close_row.addStretch()
        self._close_btn = QPushButton("×")
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setToolTip("关闭面板 (Ctrl+B)")
        self._close_btn.clicked.connect(self.close_requested)
        close_row.addWidget(self._close_btn)
        close_widget = QWidget()
        close_widget.setLayout(close_row)
        layout.addWidget(close_widget)

        self._stack = QStackedWidget()

        # ===== 页面 1: 会话列表（DSH Web UI 风格） =====
        sessions_page = QWidget()
        s_layout = QVBoxLayout(sessions_page)
        s_layout.setContentsMargins(0, 8, 0, 0)
        s_layout.setSpacing(0)

        # 顶部：新建会话按钮（accent 色，不随主题文本色变，保持品牌绿）
        self._new_session_btn = QPushButton("＋  新建会话")
        self._new_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_session_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(50, 240, 140, 0.08);"
            "  border: 1px solid rgba(50, 240, 140, 0.15);"
            "  border-radius: 8px;"
            "  padding: 8px 12px;"
            "  font-size: 12px;"
            "  color: #32F08C;"
            "  text-align: left;"
            "  margin: 4px 8px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(50, 240, 140, 0.15);"
            "  border-color: rgba(50, 240, 140, 0.3);"
            "}"
        )
        self._new_session_btn.clicked.connect(self.new_session_requested)
        s_layout.addWidget(self._new_session_btn)

        lbl = _section_label("工作区")
        self._section_labels.append(lbl)
        s_layout.addWidget(lbl)

        # 空状态提示（DSH Web UI 风格：浅灰色"暂无会话"）
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

        layout.addWidget(self._stack)

        # 默认显示会话列表
        self._stack.setCurrentIndex(self._sessions_index)

    def set_nav(self, nav: str) -> None:
        """根据 ActivityBar 导航切换页面。"""
        nav_map = {
            "sessions": self._sessions_index,
            "files": self._files_index,
            "search": self._search_index,
            "git": self._git_index,
        }
        index = nav_map.get(nav, self._sessions_index)
        self._stack.setCurrentIndex(index)

    def set_mode(self, mode: str) -> None:
        """兼容接口：模式切换不再影响左栏内容，由导航控制。"""
        pass

    def refresh_sessions(self, sessions: list) -> None:
        self._task_list.refresh(sessions)
        # 空状态：无会话时显示"暂无会话"，有会话时隐藏
        self._empty_sessions_label.setVisible(len(sessions) == 0)

    def load_workspace(self, workspace: str) -> None:
        self._file_tree.load_workspace(workspace)
