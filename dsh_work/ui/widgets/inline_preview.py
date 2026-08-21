"""内联预览（第 3.3 节）。

当 Agent 产出可预览的内容时，在对话区域直接嵌入内联预览面板：
- HTML 文件：内嵌 WebView 渲染
- Markdown：渲染为富文本（标题/列表/代码块/表格）
- CSV / 表格数据：渲染为可排序表格
- JSON：语法高亮 + 折叠树视图
- 图片：直接显示

内联预览面板高度自适应（最大 400px），支持全屏展开。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ... import constants as C
from ...utils.logger import get_logger

log = get_logger("ui.inline_preview")


class InlinePreview(QFrame):
    """内联预览面板。

    根据 Agent 产出的内容类型自动选择渲染方式。
    高度自适应（最大 400px），支持全屏展开。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("InlinePreview")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMaximumHeight(C.INLINE_PREVIEW_MAX_HEIGHT_PX)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 标题栏
        header_layout = QVBoxLayout()
        header_layout.setSpacing(2)

        self._title_label = QLabel("预览")
        self._title_label.setObjectName("Secondary")  # 颜色走全局 QSS 主题化
        self._title_label.setStyleSheet("font-size: 12px; font-weight: 600;")
        header_layout.addWidget(self._title_label)

        self._type_label = QLabel("")
        self._type_label.setObjectName("Muted")  # 颜色走全局 QSS 主题化
        self._type_label.setStyleSheet("font-size: 11px;")
        header_layout.addWidget(self._type_label)
        layout.addLayout(header_layout)

        # 内容区（根据类型动态切换）
        self._content_widget: QWidget | None = None
        self._content_layout = QVBoxLayout()
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._content_layout, stretch=1)

    def preview_file(self, file_path: str) -> None:
        """根据文件类型预览。"""
        path = Path(file_path)
        if not path.exists():
            log.warning("预览文件不存在: %s", file_path)
            return

        suffix = path.suffix.lower()
        self._title_label.setText(path.name)

        if suffix == ".html":
            self._type_label.setText("HTML")
            self._preview_html(file_path)
        elif suffix == ".md":
            self._type_label.setText("Markdown")
            self._preview_markdown(file_path)
        elif suffix == ".csv":
            self._type_label.setText("CSV 表格")
            self._preview_csv(file_path)
        elif suffix == ".json":
            self._type_label.setText("JSON")
            self._preview_json(file_path)
        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            self._type_label.setText("图片")
            self._preview_image(file_path)
        else:
            self._type_label.setText("文本")
            self._preview_text(file_path)

    def _clear_content(self) -> None:
        if self._content_widget:
            self._content_widget.deleteLater()
            self._content_widget = None

    def _preview_html(self, file_path: str) -> None:
        """HTML 文件：内嵌 WebView 渲染。"""
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            self._clear_content()
            web_view = QWebEngineView()
            web_view.setUrl(QUrl.fromLocalFile(file_path))
            self._content_widget = web_view
            self._content_layout.addWidget(web_view)
        except ImportError:
            log.warning("QWebEngineView 不可用，回退到文本预览")
            self._preview_text(file_path)

    def _preview_markdown(self, file_path: str) -> None:
        """Markdown：渲染为富文本。"""
        self._clear_content()
        label = QLabel()
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.MarkdownText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        try:
            with open(file_path, encoding="utf-8") as f:
                label.setText(f.read())
        except OSError:
            label.setText("无法读取文件")
        self._content_widget = label
        self._content_layout.addWidget(label)

    def _preview_csv(self, file_path: str) -> None:
        """CSV：渲染为可排序表格。"""
        self._clear_content()
        try:
            import csv

            from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
            with open(file_path, encoding="utf-8") as f:
                reader = csv.reader(f)
                rows = list(reader)
            table = QTableWidget()
            if rows:
                table.setColumnCount(len(rows[0]))
                table.setRowCount(len(rows))
                table.setHorizontalHeaderLabels(rows[0])
                for r, row in enumerate(rows[1:], 1):
                    for c, cell in enumerate(row):
                        table.setItem(r - 1, c, QTableWidgetItem(cell))
            table.setAlternatingRowColors(True)
            self._content_widget = table
            self._content_layout.addWidget(table)
        except Exception as e:
            log.warning("CSV 预览失败: %s", e)
            self._preview_text(file_path)

    def _preview_json(self, file_path: str) -> None:
        """JSON：语法高亮 + 折叠树视图。"""
        self._clear_content()
        try:
            import json

            from PySide6.QtWidgets import QTreeWidget
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            tree = QTreeWidget()
            tree.setHeaderHidden(True)
            self._populate_tree(tree, data, "root")
            self._content_widget = tree
            self._content_layout.addWidget(tree)
        except Exception as e:
            log.warning("JSON 预览失败: %s", e)
            self._preview_text(file_path)

    def _populate_tree(self, tree, data, name: str) -> None:
        """递归填充 JSON 树。"""
        from PySide6.QtWidgets import QTreeWidgetItem
        item = QTreeWidgetItem([f"{name}: {type(data).__name__}"])
        if isinstance(data, dict):
            for k, v in data.items():
                child = QTreeWidgetItem([f"{k}: {self._json_summary(v)}"])
                if isinstance(v, (dict, list)):
                    self._populate_tree_item(child, v)
                item.addChild(child)
        elif isinstance(data, list):
            for i, v in enumerate(data):
                child = QTreeWidgetItem([f"[{i}]: {self._json_summary(v)}"])
                if isinstance(v, (dict, list)):
                    self._populate_tree_item(child, v)
                item.addChild(child)
        tree.addTopLevelItem(item)
        tree.expandToDepth(0)

    def _populate_tree_item(self, parent_item, data) -> None:
        from PySide6.QtWidgets import QTreeWidgetItem
        if isinstance(data, dict):
            for k, v in data.items():
                child = QTreeWidgetItem([f"{k}: {self._json_summary(v)}"])
                if isinstance(v, (dict, list)):
                    self._populate_tree_item(child, v)
                parent_item.addChild(child)
        elif isinstance(data, list):
            for i, v in enumerate(data):
                child = QTreeWidgetItem([f"[{i}]: {self._json_summary(v)}"])
                if isinstance(v, (dict, list)):
                    self._populate_tree_item(child, v)
                parent_item.addChild(child)

    @staticmethod
    def _json_summary(v) -> str:
        if isinstance(v, str):
            return f'"{v[:50]}"'
        if isinstance(v, (dict, list)):
            return f"{type(v).__name__}({len(v)})"
        return str(v)

    def _preview_image(self, file_path: str) -> None:
        """图片：直接显示。"""
        self._clear_content()
        from PySide6.QtGui import QPixmap
        label = QLabel()
        pixmap = QPixmap(file_path)
        if pixmap.isNull():
            label.setText("图片加载失败")
        else:
            # 缩放适应预览区域
            scaled = pixmap.scaledToHeight(
                C.INLINE_PREVIEW_MAX_HEIGHT_PX - 40,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_widget = label
        self._content_layout.addWidget(label)

    def _preview_text(self, file_path: str) -> None:
        """纯文本预览（回退方案）。"""
        self._clear_content()
        from PySide6.QtWidgets import QPlainTextEdit
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        try:
            with open(file_path, encoding="utf-8") as f:
                editor.setPlainText(f.read())
        except OSError:
            editor.setPlainText("无法读取文件")
        self._content_widget = editor
        self._content_layout.addWidget(editor)
