"""Web 风格 Markdown 渲染器（对齐 dsh-web-frontend 的 markdown token）。

把 Markdown 文本转换为带内联样式的 HTML，交给只读 QTextBrowser 渲染：

- 代码块：深色背景圆角块 + 语言标签条（MarkdownCodeBlock / Banner token）
- 行内代码：底色圆角（MarkdownInlineCode token）
- 标题 h1-h4、粗体、斜体、删除线、链接、引用、无序/有序列表、表格

颜色从当前主题 token 注入（theme_manager 的 markdown_code_bg 等），
浅色/深色主题下自动适配。
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QTextBrowser, QWidget

_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc(s: str) -> str:
    return "".join(_ESCAPE.get(c, c) for c in s)


def _get_colors(theme: Any = None) -> dict:
    """从当前主题读取 markdown 渲染需要的颜色。"""
    try:
        from ..theme.theme_manager import ThemeManager
        t = theme or ThemeManager().current
        if t is not None:
            c = t.colors
            return {
                "text": c.text_primary,
                "text2": c.text_secondary,
                "muted": c.text_muted,
                "accent": c.accent,
                "code_bg": c.markdown_code_bg,
                "banner_bg": c.markdown_code_banner,
                "inline_bg": c.markdown_inline_bg,
                "block_border": c.markdown_block_border,
                "divider": c.divider,
            }
    except Exception:
        pass
    return {
        "text": "#F9FAFB",
        "text2": "#CFD3D6",
        "muted": "#ADB2B8",
        "accent": "#679EFE",
        "code_bg": "#1B1B1C",
        "banner_bg": "#232324",
        "inline_bg": "#232324",
        "block_border": "rgba(255,255,255,0.06)",
        "divider": "rgba(255,255,255,0.06)",
    }


def _inline(text: str, col: dict) -> str:
    """行内解析：行内代码 → 转义 → 链接/粗体/斜体/删除线 → 恢复行内代码。"""
    code_holder: list[str] = []

    def _save_code(m) -> str:
        code_holder.append(m.group(1))
        return f"\x00{len(code_holder) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_code, text)
    text = _esc(text)

    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}" style="color:{col["accent"]}; text-decoration:none;">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", text)

    def _restore(m) -> str:
        idx = int(m.group(1))
        code = _esc(code_holder[idx])
        return (
            f'<span style="background-color:{col["inline_bg"]}; color:{col["text"]};'
            f' border-radius:4px; padding:1px 5px; font-family:Consolas,\'Courier New\',monospace;'
            f' font-size:13px;">{code}</span>'
        )

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text


def md_to_html(md: str, theme: Any = None) -> str:
    """Markdown → HTML（块级 + 行内）。"""
    col = _get_colors(theme)
    if not md:
        return ""
    lines = md.replace("\r\n", "\n").split("\n")

    body: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # 代码块 ```lang ... ```
        m = re.match(r"^```(\w*)", line)
        if m:
            lang = m.group(1)
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code = _esc("\n".join(code_lines))
            lang_html = (
                f'<span style="color:{col["muted"]}; font-size:12px; font-family:Consolas,monospace;">{_esc(lang)}</span>'
                if lang
                else ""
            )
            body.append(
                f'<div style="background-color:{col["code_bg"]}; border:1px solid {col["block_border"]};'
                f' border-radius:8px; margin:8px 0; padding:0;">'
                f'<div style="background-color:{col["banner_bg"]}; border-top-left-radius:8px; border-top-right-radius:8px;'
                f' padding:4px 10px; border-bottom:1px solid {col["block_border"]};">{lang_html}</div>'
                f'<pre style="margin:0; padding:10px 12px; font-family:Consolas,\'Courier New\',monospace;'
                f' font-size:13px; color:{col["text2"]}; white-space:pre-wrap; line-height:1.6;">{code}</pre>'
                f"</div>"
            )
            continue

        # 表格：当前行以 | 开头，且下一行是分隔行
        if line.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|?$", lines[i + 1]):
            header_cells = [c.strip() for c in line.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip("|").split("|")]
                rows.append(
                    "<tr>"
                    + "".join(
                        f'<td style="border:1px solid {col["block_border"]}; padding:6px 10px;">{_inline(c, col)}</td>'
                        for c in cells
                    )
                    + "</tr>"
                )
                i += 1
            thead = (
                "<tr>"
                + "".join(
                    f'<th style="border:1px solid {col["block_border"]}; padding:6px 10px; font-weight:600;'
                    f' background-color:{col["banner_bg"]};">{_inline(c, col)}</th>'
                    for c in header_cells
                )
                + "</tr>"
            )
            body.append(
                f'<table style="border-collapse:collapse; margin:8px 0; font-size:14px; color:{col["text"]};">'
                f"<thead>{thead}</thead><tbody>{''.join(rows)}</tbody></table>"
            )
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            sizes = {1: "22px", 2: "19px", 3: "17px", 4: "15px"}
            body.append(
                f'<div style="font-size:{sizes[level]}; font-weight:700; color:{col["text"]};'
                f' margin:10px 0 6px 0;">{_inline(m.group(2), col)}</div>'
            )
            i += 1
            continue

        # 引用
        if line.startswith(">"):
            quote_lines = []
            while i < n and lines[i].startswith(">"):
                quote_lines.append(lines[i].lstrip(">").strip())
                i += 1
            body.append(
                f'<div style="border-left:3px solid {col["accent"]}; padding:4px 12px; margin:8px 0;'
                f' color:{col["text2"]}; background-color:{col["banner_bg"]}; border-radius:0 8px 8px 0;">'
                f"{_inline('<br>'.join(quote_lines), col)}</div>"
            )
            continue

        # 无序列表
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(_inline(re.sub(r"^\s*[-*]\s+", "", lines[i]), col))
                i += 1
            body.append(
                '<ul style="margin:6px 0; padding-left:22px; color:' + col["text"] + ';">'
                + "".join(f"<li style='margin:2px 0;'>{it}</li>" for it in items)
                + "</ul>"
            )
            continue

        # 有序列表
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(_inline(re.sub(r"^\s*\d+\.\s+", "", lines[i]), col))
                i += 1
            body.append(
                '<ol style="margin:6px 0; padding-left:22px; color:' + col["text"] + ';">'
                + "".join(f"<li style='margin:2px 0;'>{it}</li>" for it in items)
                + "</ol>"
            )
            continue

        # 分隔线
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            body.append(f'<hr style="border:none; border-top:1px solid {col["divider"]}; margin:10px 0;">')
            i += 1
            continue

        # 普通段落（合并空行分隔）
        # 注意：不能按首字符排除 "-"/"*"/数字 —— "**粗体**"、"1.5 倍" 等
        # 段落会被误判为列表前缀而整体丢失；只排除真正匹配列表前缀的行。
        para_lines = []
        while (
            i < n
            and lines[i].strip()
            and not lines[i].startswith(("```", "|", "#", ">"))
            and not re.match(r"^\s*[-*]\s+", lines[i])
            and not re.match(r"^\s*\d+\.\s+", lines[i])
        ):
            para_lines.append(lines[i])
            i += 1
        if para_lines:
            body.append(
                f'<div style="color:{col["text"]}; line-height:1.75; margin:4px 0;">'
                + _inline("<br>".join(para_lines), col)
                + "</div>"
            )
            continue

        i += 1

    return "".join(body)


class MarkdownTextEdit(QTextBrowser):
    """只读 Markdown 渲染视图（对齐 Web 版 MarkdownText）。

    - 支持文本选择/复制、链接点击（外部浏览器打开）、自动换行
    - 高度自适应内容（heightForWidth 机制，避免消息行被压缩）
    - 监听主题切换：重新渲染 HTML（md_to_html 的颜色是主题相关的一次性注入，
      主题切换后必须重渲，否则深/浅主题下文字与背景同色看不清）
    - 正文字号对齐 Web 版 markdown base（16px / 28px 行高）
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._md = ""
        self._cached_height = 0
        self._align_right = False
        self.setReadOnly(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setAcceptRichText(True)
        self.document().setDocumentMargin(0)
        self.setOpenExternalLinks(True)
        # 高度自适应：显式 setFixedHeight 按内容高度（不依赖布局协商，
        # 避免 QTextBrowser 在真实窗口中被压缩导致内容裁剪显示不全）
        from PySide6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QTextBrowser { background: transparent; border: none;"
            " font-size: 16px; line-height: 28px; }"
        )
        # 注意：不在此注册 ThemeManager 全局监听 —— 每条消息一个 MarkdownTextEdit，
        # 消息删除（切换会话 clear）后 listener 仍被引用会导致
        # "Internal C++ object already deleted" 异常刷屏且 listener 列表膨胀。
        # 主题刷新由 MessageList 统一遍历现有行调用 _on_theme_changed。

    def rerender_for_theme(self, theme=None) -> None:
        """外部（MessageList）主题刷新入口：重渲已保存的 Markdown。"""
        self._on_theme_changed(theme)

    def set_align_right(self, right: bool = True) -> None:
        """用户消息右对齐：文档默认对齐右（头部/内容整体靠右）。"""
        self._align_right = right
        self._apply_align()

    def _on_theme_changed(self, theme=None) -> None:
        """主题切换：重新渲染已保存的 Markdown（颜色注入主题相关）。"""
        if self._md:
            self.setHtml(md_to_html(self._md, theme))
            self._apply_align()
            self._sync_height()

    def set_markdown(self, md: str) -> None:
        self._md = md or ""
        self.setHtml(md_to_html(self._md))
        self._apply_align()
        self._sync_height()

    def _apply_align(self) -> None:
        """重渲后恢复文档对齐（setHtml 会重置 defaultTextOption）。

        注意：只设置 defaultTextOption 不够——已有文本块的对齐由各自
        blockFormat 控制，defaultTextOption 仅影响新块。必须遍历所有
        非空块 mergeBlockFormat(AlignRight)，否则右对齐不生效。
        """
        if getattr(self, "_align_right", False):
            from PySide6.QtGui import QTextBlockFormat, QTextCursor

            doc = self.document()
            opt = doc.defaultTextOption()
            opt.setAlignment(Qt.AlignmentFlag.AlignRight)
            doc.setDefaultTextOption(opt)

            cursor = QTextCursor(doc)
            cursor.beginEditBlock()
            block = doc.firstBlock()
            while block.isValid():
                if block.text().strip():
                    fmt = QTextBlockFormat()
                    fmt.setAlignment(Qt.AlignmentFlag.AlignRight)
                    cursor.setPosition(block.position())
                    cursor.mergeBlockFormat(fmt)
                block = block.next()
            cursor.endEditBlock()

    def append_plain(self, text: str) -> None:
        """流式增量：在文档末尾追加纯文本（不做 markdown 重排，性能快 100 倍）。

        高度同步延后到事件循环末尾（合并多次增量），避免流式时频繁全量重排
        导致主线程卡顿（"对话不实时"的根因）。
        """
        if not text:
            return
        from PySide6.QtGui import QTextCursor
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        # 追加后自绘刷新（无需等待布局）
        self.viewport().update()
        self._schedule_height_sync()

    def _schedule_height_sync(self) -> None:
        """延后合并高度同步（事件循环末尾执行一次）。"""
        from PySide6.QtCore import QTimer
        if getattr(self, "_height_sync_pending", False):
            return
        self._height_sync_pending = True
        QTimer.singleShot(0, self._do_height_sync)

    def _do_height_sync(self) -> None:
        self._height_sync_pending = False
        self._sync_height()

    def _content_width(self) -> int:
        try:
            w = self.viewport().width()
        except Exception:
            w = 0
        return w if w > 0 else (self.width() if self.width() > 0 else 400)

    def _sync_height(self) -> None:
        """按视图当前渲染宽度重算内容高度并显式设置（防裁剪）。

        注意：不能调用 doc.adjustSize()——它会按内容 idealWidth 压缩
        textWidth（例如 798px 视口被压成 128px），破坏全宽布局，导致
        右对齐文本在压缩后的窄文档内对齐、视觉上仍靠左。
        QTextBrowser 的 textWidth 由视图自动跟随 viewport 宽度，
        documentLayout().documentSize() 即当前宽度下的真实渲染高度。
        """
        doc = self.document()
        h = int(doc.documentLayout().documentSize().height()) + 4
        h = max(h, 24)
        if h != self._cached_height:
            self._cached_height = h
            self.setFixedHeight(h)
            self.updateGeometry()

    def heightForWidth(self, width: int) -> int:
        """兜底：布局若咨询 heightForWidth 时返回内容高度。

        同样不能 adjustSize()（会压缩 textWidth）；setTextWidth 后
        直接读布局高度即可。
        """
        if width <= 0:
            width = 400
        doc = self.document()
        doc.setTextWidth(width)
        return int(doc.documentLayout().documentSize().height()) + 4

    def sizeHint(self) -> Any:
        from PySide6.QtCore import QSize
        return QSize(self._content_width(), self._cached_height or self.heightForWidth(self._content_width()))

    def minimumSizeHint(self) -> Any:
        from PySide6.QtCore import QSize
        return QSize(80, 24)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 宽度变化后重新布局（保持内容自适应高度）
        self._sync_height()
