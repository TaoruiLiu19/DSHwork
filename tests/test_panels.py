"""右侧栏 Details 折叠栏测试。

设计：
- 折叠态：窄图标栏 rail（48px），含「◀ 展开」按钮与功能图标——始终有展开入口
- 展开态：完整内容区，头部含「✕ 收纳」按钮
- 工具事件只更新内容不强制展开（对齐网页版）
- 切换会话自动折叠（对齐 closeDetails）
- 中栏头部无重复新建按钮（新建入口统一在左侧栏）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def test_conversation_header_has_no_new_session_button() -> None:
    """中栏头部不应有新建按钮（新建入口统一在左侧栏）。"""
    from dsh_work.ui.widgets.conversation_header import ConversationHeader

    h = ConversationHeader()
    assert not hasattr(h, "_new_btn"), "ConversationHeader 不应有新建按钮"
    assert not hasattr(h, "new_session_requested"), "不应有 new_session_requested 信号"
    assert hasattr(h, "_view_conversation") and hasattr(h, "_view_usage")


def test_right_panel_default_collapsed_rail() -> None:
    """右侧栏默认折叠成窄条（48px），rail 含展开按钮。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    assert rp.is_collapsed()
    assert rp.minimumWidth() == 48 and rp.maximumWidth() == 48
    assert rp._rail.isVisibleTo(rp) and not rp._content.isVisibleTo(rp)
    # 折叠态必须有展开入口
    assert rp._expand_btn is not None
    assert rp._expand_btn.isVisibleTo(rp._rail)


def test_right_panel_expand_releases_width() -> None:
    """展开后宽度约束放开（可拖拽调宽），且收纳按钮可见并有内联样式。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp.expand()
    assert not rp.is_collapsed()
    assert rp._content.isVisibleTo(rp) and not rp._rail.isVisibleTo(rp)
    assert rp.minimumWidth() >= 280
    assert rp.maximumWidth() > 1000
    assert rp._collapse_btn.isVisibleTo(rp._content), "展开态必须有收纳按钮"
    # 内联样式（防被全局 QSS 的 QPushButton 规则覆盖导致按钮不可见）
    assert "background-color" in rp._collapse_btn.styleSheet(), "收纳按钮应有内联背景样式"


def test_right_panel_rail_icon_expands_to_section() -> None:
    """点击窄条图标：展开并切到对应内容页。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp._on_rail_clicked("preview")
    assert not rp.is_collapsed()
    assert rp._stack.currentIndex() == rp._work_index


def test_right_panel_expand_button_opens() -> None:
    """点击 ◀ 展开按钮：展开面板。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp._expand_btn.click()
    assert not rp.is_collapsed(), "点击 ◀ 应展开"


def test_right_panel_collapse_button_closes_and_signals() -> None:
    """点击 ✕ 收纳按钮：折叠 + 发出 close_requested（仅用户点击时）。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp.expand()
    fired = []
    rp.close_requested.connect(lambda: fired.append(True))

    rp._collapse_btn.click()
    assert rp.is_collapsed(), "点击 ✕ 应收纳"
    assert fired, "用户点击 ✕ 应发出 close_requested"

    # 程序化 collapse 不发出信号（会话切换等场景）
    fired.clear()
    rp.expand()
    rp.collapse()
    assert rp.is_collapsed()
    assert not fired, "程序化 collapse 不应发出 close_requested"


def test_right_panel_toggle_roundtrip() -> None:
    """toggle 在折叠/展开间往返。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp.toggle()
    assert not rp.is_collapsed()
    rp.toggle()
    assert rp.is_collapsed()


def test_right_panel_close_signal_handled_as_collapse() -> None:
    """close_requested 应由外层折叠处理，不能 toggle 回来（信号循环回归）。

    模拟 main_window 的完整连接：close_requested -> collapse + 保存。
    此前连接成 _toggle_panel 导致：点 ✕ 折叠后立刻被 toggle 弹回展开，
    用户看到"点击没反应"。
    """
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp.expand()

    # 模拟外层 handler（等价于 main_window._on_right_panel_close_requested）
    def handler():
        rp.collapse()

    rp.close_requested.connect(handler)
    rp._collapse_btn.click()
    assert rp.is_collapsed(), "点 ✕ 后应保持折叠（外层 handler 不能 toggle 回来）"


def test_right_panel_preview_auto_expands() -> None:
    """用户主动预览文件：自动展开并切到预览页。"""
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    rp.preview_file("__nonexistent__.txt")
    assert not rp.is_collapsed(), "preview_file 应自动展开"
    assert rp._stack.currentIndex() == rp._work_index


def test_right_panel_tool_events_do_not_force_expand() -> None:
    """工具事件不强制展开已折叠的面板（对齐网页版）。

    回归：此前 add_tool_call/show_diff 每次都展开，会话运行中工具
    频繁触发导致用户刚收起就被强制展开（"只有展开没有收回"）。
    """
    from dsh_work.ui.panels.right_panel import RightPanel

    rp = RightPanel()
    assert rp.is_collapsed()
    rp.add_tool_call("bash", "running", 0)
    rp.show_diff("+ new\n- old")
    assert rp.is_collapsed(), "工具事件不应强制展开 Details"
    # 内容已更新（折叠态也维护内容，展开后可见）
    assert rp._tool_timeline.count() == 1
    assert rp._diff_view.count() >= 2

    # 用户展开后，工具事件继续更新内容且保持展开
    rp.expand()
    rp.add_tool_call("bash", "success", 100)
    assert not rp.is_collapsed(), "已展开时工具事件不应折叠"
    assert rp._tool_timeline.count() == 2
