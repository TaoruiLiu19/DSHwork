"""跨线程桥接器 parent 类型测试。

回归：_MainThreadBridge 的 parent 必须是 QObject。
此前把 SessionManager（普通类）当 parent 传入导致主窗口初始化
TypeError: 'QObject.__init__' called with wrong argument types。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def test_bridge_parent_must_be_qobject() -> None:
    """parent 非 QObject 时构造函数必须抛 TypeError。"""
    from PySide6.QtWidgets import QWidget

    from dsh_work.ui.main_window import _MainThreadBridge

    class _Plain:
        pass

    with pytest.raises(TypeError):
        _MainThreadBridge(lambda *a: None, _Plain())

    # 合法用法：QObject parent
    parent = QWidget()
    bridge = _MainThreadBridge(lambda *a: None, parent)
    assert bridge is not None


def test_bridge_parent_reference_kept() -> None:
    """桥接器 parent 保持为传入的 QObject。"""
    from PySide6.QtWidgets import QWidget

    from dsh_work.ui.main_window import _MainThreadBridge

    def _cb(*a):
        return None

    holder = QWidget()
    bridge = _MainThreadBridge(_cb, holder)
    assert bridge.parent() is holder
