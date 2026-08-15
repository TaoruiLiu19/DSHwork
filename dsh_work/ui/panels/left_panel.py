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
    QStackedWidget,
    QPushButton,
)

from ... import constants as C


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
        self.setStyleSheet(
            "QListWidget {"
            "  background-color: transparent;"
            "  border: none;"
            "}"
            "QListWidget::item {"
            "  padding: 8px 12px;"
            "  border-radius: 6px;"
            "  margin: 1px 4px;"
            "}"
            "QListWidget::item:hover {"
            "  background-color: rgba(224, 226, 242, 0.05);"
            "}"
            "QListWidget::item:selected {"
            "  background-color: rgba(120, 119, 198, 0.15);"
            "  color: #D1D3DB;"
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
        self.setStyleSheet(
            "QTreeWidget {"
            "  background-color: transparent;"
            "  border: none;"
            "}"
            "QTreeWidget::item {"
            "  padding: 4px 8px;"
            "}"
            "QTreeWidget::item:hover {"
            "  background-color: rgba(224, 226, 242, 0.05);"
            "}"
            "QTreeWidget::item:selected {"
            "  background-color: rgba(120, 119, 198, 0.15);"
            "}"
        )
        self.itemDoubleClicked.connect(self._on_double_click)
        self._modified_files: dict[str, str] = {}

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
            str_path = str(entry)
            if str_path in self._modified_files:
                status = self._modified_files[str_path]
                if status == "new":
                    item.setForeground(0, self._color_green())
                else:
                    item.setForeground(0, self._color_orange())
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

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._branch_label = QLabel("分支: -")
        self._branch_label.setStyleSheet("font-weight: 600; font-size: 12px; color: #D1D3DB;")
        layout.addWidget(self._branch_label)

        layout.addWidget(QLabel("未提交变更:"))
        self._changes_list = QListWidget()
        self._changes_list.setMaximumHeight(120)
        self._changes_list.setStyleSheet(
            "QListWidget { background-color: transparent; border: none; }"
        )
        layout.addWidget(self._changes_list)

        layout.addWidget(QLabel("最近提交:"))
        self._commits_list = QListWidget()
        self._commits_list.setMaximumHeight(120)
        self._commits_list.setStyleSheet(
            "QListWidget { background-color: transparent; border: none; }"
        )
        layout.addWidget(self._commits_list)

    def refresh(self, branch: str = "", changes: list[str] | None = None, commits: list[str] | None = None) -> None:
        self._branch_label.setText(f"分支: {branch or '-'}")
        self._changes_list.clear()
        for c in changes or []:
            self._changes_list.addItem(c)
        self._commits_list.clear()
        for c in commits or []:
            self._commits_list.addItem(c)


def _section_label(text: str) -> QLabel:
    """创建面板分区的标题标签。"""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        "font-size: 11px; color: #666B75; font-weight: 600;"
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
        self.setStyleSheet(
            "QWidget#LeftPanel {"
            "  background-color: #111113;"
            "  border-right: 1px solid rgba(224, 226, 242, 0.06);"
            "}"
        )
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部关闭按钮行（纯净界面模式）
        close_row = QHBoxLayout()
        close_row.setContentsMargins(4, 4, 4, 0)
        close_row.addStretch()
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("关闭面板 (Ctrl+B)")
        close_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: transparent;"
            "  border: none;"
            "  border-radius: 4px;"
            "  color: #666B75;"
            "  font-size: 14px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(224, 226, 242, 0.1);"
            "  color: #D1D3DB;"
            "}"
        )
        close_btn.clicked.connect(self.close_requested)
        close_row.addWidget(close_btn)
        close_widget = QWidget()
        close_widget.setLayout(close_row)
        layout.addWidget(close_widget)

        self._stack = QStackedWidget()

        # ===== 页面 1: 会话列表（DSH Web UI 风格） =====
        sessions_page = QWidget()
        s_layout = QVBoxLayout(sessions_page)
        s_layout.setContentsMargins(0, 8, 0, 0)
        s_layout.setSpacing(0)

        # 顶部：新建会话按钮
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

        s_layout.addWidget(_section_label("工作区"))

        # 空状态提示（DSH Web UI 风格：浅灰色"暂无会话"）
        self._empty_sessions_label = QLabel("暂无会话")
        self._empty_sessions_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_sessions_label.setStyleSheet(
            "color: #4A4D56; font-size: 12px; padding: 24px 0;"
        )
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
        f_layout.addWidget(_section_label("文件树"))
        self._file_tree = FileTreeWidget()
        self._file_tree.file_activated.connect(self.file_activated)
        f_layout.addWidget(self._file_tree, stretch=1)
        self._files_index = self._stack.addWidget(files_page)

        # ===== 页面 3: 搜索（预留） =====
        search_page = QWidget()
        sr_layout = QVBoxLayout(search_page)
        sr_layout.setContentsMargins(8, 8, 8, 8)
        sr_layout.setSpacing(8)
        sr_layout.addWidget(_section_label("搜索"))
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索会话和文件...")
        self._search_input.setStyleSheet(
            "QLineEdit {"
            "  background-color: rgba(224, 226, 242, 0.05);"
            "  border: 1px solid rgba(224, 226, 242, 0.08);"
            "  border-radius: 6px;"
            "  padding: 6px 10px;"
            "  font-size: 12px;"
            "  color: #D1D3DB;"
            "}"
        )
        sr_layout.addWidget(self._search_input)
        self._search_results = QListWidget()
        self._search_results.setStyleSheet(
            "QListWidget { background-color: transparent; border: none; }"
        )
        sr_layout.addWidget(self._search_results, stretch=1)
        self._search_index = self._stack.addWidget(search_page)

        # ===== 页面 4: Git =====
        git_page = QWidget()
        g_layout = QVBoxLayout(git_page)
        g_layout.setContentsMargins(0, 8, 0, 0)
        g_layout.setSpacing(0)
        g_layout.addWidget(_section_label("Git"))
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
