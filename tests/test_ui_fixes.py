"""UI 修复测试。

覆盖三组修复的纯逻辑与渲染行为：
1. `_qcolor`：rgba 浮点 alpha 字符串必须解析为有效 QColor（修复浅色主题
   选中会话变黑——Qt 原生 QColor("rgba(r,g,b,0.10)") 解析失败返回黑色）。
2. 消息行布局：用户消息与 assistant 相同的全宽显示逻辑，内容右对齐。
3. 右对齐真实性：必须逐块设置 blockFormat 且 textWidth 不被 adjustSize
   压缩（否则文档在窄宽内对齐、视觉上仍靠左）。
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor

from dsh_work.ui.panels.left_panel import _qcolor

pytestmark = pytest.mark.usefixtures("qapp")  # 需要 QApplication（conftest 提供）


def test_qcolor_parses_rgba_float_alpha() -> None:
    """rgba 浮点 alpha 必须解析为有效颜色（浅色主题 bg_active）。"""
    c = _qcolor("rgba(38, 49, 72, 0.10)")
    assert c.isValid(), "rgba 浮点 alpha 解析失败 → 无效颜色（渲染为黑色）"
    assert c.alpha() == 26  # 0.10 * 255 ≈ 26


def test_qcolor_parses_dark_theme_active() -> None:
    c = _qcolor("rgba(255, 255, 255, 0.14)")
    assert c.isValid()
    assert c.alpha() == 36  # 0.14 * 255 ≈ 36
    assert c.red() == 255


def test_qcolor_parses_hex() -> None:
    c = _qcolor("#679EFE")
    assert c.isValid()
    assert c.name() == "#679efe"
    assert c.alpha() == 255


def test_qcolor_parses_rgb_three_args() -> None:
    c = _qcolor("rgb(38, 49, 72)")
    assert c.isValid()
    assert c.alpha() == 255
    assert (c.red(), c.green(), c.blue()) == (38, 49, 72)


def test_qcolor_fallback_for_empty() -> None:
    c = _qcolor("")
    assert c.isValid()


def test_qcolor_does_not_accept_invalid() -> None:
    # 非颜色字符串回退给 QColor 原生解析（无效时返回无效色，不抛异常）
    c = _qcolor("not-a-color")
    assert c.isValid() is False


def test_qcolor_hex_matches_native() -> None:
    assert _qcolor("#22C55E") == QColor("#22C55E")


def _blended(c: QColor, bg: QColor) -> QColor:
    """模拟半透明前景色叠加在背景上的最终颜色。"""
    a = c.alphaF()
    return QColor(
        int(c.red() * a + bg.red() * (1 - a)),
        int(c.green() * a + bg.green() * (1 - a)),
        int(c.blue() * a + bg.blue() * (1 - a)),
    )


def test_light_theme_selection_is_visible() -> None:
    """浅色主题：选中态半透明深色叠加浅色背景后必须仍是浅色（修复黑底深字）。"""
    from dsh_work.ui.theme.theme_manager import ThemeManager

    tm = ThemeManager()
    tm.load_all()
    tm.set_current("web_light")
    t = tm.current
    bg = QColor(t.colors.bg_primary)
    blended = _blended(_qcolor(t.colors.bg_active), bg)
    assert blended.lightness() > 128, f"浅色主题选中态叠加后过暗: {blended.name()}"


def test_dark_theme_selection_is_visible() -> None:
    """深色主题：选中态叠加后保持深色，浅色文字可读。"""
    from dsh_work.ui.theme.theme_manager import ThemeManager

    tm = ThemeManager()
    tm.load_all()
    tm.set_current("web_dark")
    t = tm.current
    bg = QColor(t.colors.bg_primary)
    blended = _blended(_qcolor(t.colors.bg_active), bg)
    assert blended.lightness() < 200, f"深色主题选中态叠加后过亮: {blended.name()}"


def test_message_row_user_full_width_like_assistant() -> None:
    """用户消息与 assistant 相同的全宽显示逻辑（无气泡容器）。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    user_row = MessageRow("user", "hello")
    assert not hasattr(user_row, "_user_bubble"), "用户消息不应有气泡容器"
    assert not hasattr(user_row, "_sync_user_bubble_width"), "不应保留气泡宽度同步"
    # 与 assistant 相同的全宽限制
    user_row.set_max_width_viewport(800)
    assert user_row.maximumWidth() >= 16777215


def test_message_row_user_right_aligned_content() -> None:
    """用户消息内容右对齐：所有非空块 blockFormat 对齐为 AlignRight。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    md = "第一行\n\n第二段 **加粗** 和 `code`\n\n第三行"
    user_row = MessageRow("user", md)
    assert user_row._content_view._align_right is True
    assert _blocks_right_aligned(user_row._content_view.document()), "用户消息文本块未实际右对齐（仅标志位不够）"

    agent_row = MessageRow("assistant", md)
    assert getattr(agent_row._content_view, "_align_right", False) is False, "agent 不应右对齐"
    assert not _blocks_right_aligned(agent_row._content_view.document()), "agent 消息不应右对齐"


def _blocks_right_aligned(doc) -> bool:
    """文档中所有非空块是否右对齐。"""
    from PySide6.QtCore import Qt

    block = doc.firstBlock()
    while block.isValid():
        if block.text().strip():
            if not (block.blockFormat().alignment() & Qt.AlignmentFlag.AlignRight):
                return False
        block = block.next()
    return True


def test_markdown_view_align_survives_rerender() -> None:
    """右对齐在主题重渲后保持（setHtml 会重置对齐，须重新应用）。"""
    from dsh_work.ui.widgets.markdown_view import MarkdownTextEdit

    v = MarkdownTextEdit()
    v.set_align_right(True)
    v.set_markdown("第一行\n\n第二行")
    v.rerender_for_theme(None)
    assert _blocks_right_aligned(v.document()), "主题重渲后右对齐丢失"


def test_markdown_view_align_survives_append() -> None:
    """流式追加后已有块仍保持右对齐。"""
    from dsh_work.ui.widgets.markdown_view import MarkdownTextEdit

    v = MarkdownTextEdit()
    v.set_align_right(True)
    v.set_markdown("第一行")
    v.append_plain(" 追加")
    assert _blocks_right_aligned(v.document()), "流式追加后右对齐丢失"


def test_markdown_view_textwidth_not_shrunk_by_adjust_size() -> None:
    """textWidth 不能被 adjustSize 压缩——否则文档在窄宽内右对齐、视觉靠左。

    回归：_sync_height/heightForWidth 曾调用 doc.adjustSize()，把 798px
    视口的 textWidth 压成 ~128px，导致右对齐不生效。
    """
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    from dsh_work.ui.widgets.message_list import MessageRow

    w = QWidget()
    w.resize(900, 400)
    lay = QVBoxLayout(w)
    row = MessageRow("user", "第一行内容\n\n第二段内容")
    lay.addWidget(row)
    w.show()
    QApplication.processEvents()
    QApplication.processEvents()

    vp = row._content_view
    doc = vp.document()
    # textWidth 应等于视口宽（而非被压缩成内容宽）
    assert doc.textWidth() >= vp.viewport().width() - 2, (
        f"textWidth 被压缩: {doc.textWidth()} vs 视口 {vp.viewport().width()}"
    )

    # 像素级：文本块右边缘应贴近视口右缘
    block = doc.firstBlock()
    rect = None
    while block.isValid():
        if block.text().strip():
            rect = doc.documentLayout().blockBoundingRect(block)
            break
        block = block.next()
    assert rect is not None
    right_edge = rect.x() + rect.width()
    assert right_edge >= vp.viewport().width() - 4, (
        f"文本右边缘 {right_edge} 未贴近视口右缘 {vp.viewport().width()}"
    )


def test_message_row_user_markdown_renders_like_assistant() -> None:
    """用户消息与 agent 使用相同的 Markdown 渲染（同一渲染器与宽度策略）。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    md = "```python\nprint(1)\n```\n\n**加粗** 和 `inline`"
    user_row = MessageRow("user", md)
    agent_row = MessageRow("assistant", md)
    assert type(user_row._content_view) is type(agent_row._content_view)
    # 两个消息行宽度策略一致（都全宽、Expanding）
    assert user_row._content_view.sizePolicy().horizontalPolicy() == agent_row._content_view.sizePolicy().horizontalPolicy()


def test_message_row_user_content_not_truncated() -> None:
    """全宽行内长文本完整渲染（不出现半个字裁剪）。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    content = "这是一段比较长的中文消息，用于验证自动换行是否正常，每个字都应该完整显示。" * 3
    row = MessageRow("user", content)
    row.set_max_width_viewport(600)
    rendered = row._content_view.toPlainText().replace("\n", "")
    assert len(rendered) >= len(content), "用户消息文本被裁剪"


def test_message_row_user_align_after_update() -> None:
    """update_content 后用户消息仍保持右对齐（setHtml 会重置对齐，须重新应用）。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    row = MessageRow("user", "第一版")
    row.update_content("第二版内容更长一些")
    assert row._content_view._align_right is True
    # 重渲后文档块对齐仍为右（defaultTextOption 只影响新块，须逐块应用）
    assert _blocks_right_aligned(row._content_view.document()), "update_content 后右对齐丢失"


def test_message_row_assistant_full_width() -> None:
    """assistant 消息不做宽度限制（全宽）。"""
    from dsh_work.ui.widgets.message_list import MessageRow

    row = MessageRow("assistant", "long reply")
    row.set_max_width_viewport(800)
    assert row.maximumWidth() >= 16777215
