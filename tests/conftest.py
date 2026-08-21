"""pytest 共享 fixture。

提供 session 级 QApplication（UI 测试用），不依赖 pytest-qt：
- Windows/本地有显示环境时用默认平台；
- 无头 CI（Linux runner）自动切 offscreen 平台。
"""

from __future__ import annotations

import os

import pytest

# 无显示环境（CI Linux）时使用 offscreen 平台，避免 QApplication 创建失败
if os.environ.get("QT_QPA_PLATFORM") is None and os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    """返回全局唯一的 QApplication 实例（惰性创建，可复用）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
