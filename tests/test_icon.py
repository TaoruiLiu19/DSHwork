"""应用图标测试：DeepSeek Harness 黑色小鲸鱼。

验证：
- get_builtin_icon_path 返回存在的 SVG
- QIcon 能加载且非空（黑色鲸鱼）
- 构建的 ICO 文件存在（供 PyInstaller exe 图标）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def test_builtin_icon_path_exists() -> None:
    """内置鲸鱼图标 SVG 必须存在。"""
    from dsh_work.config import get_builtin_icon_path

    path = get_builtin_icon_path()
    assert path.exists(), f"鲸鱼图标不存在: {path}"


def test_builtin_icon_loads_as_qicon() -> None:
    """SVG 可被 QIcon 加载且非空（黑色鲸鱼）。"""
    from PySide6.QtGui import QIcon

    from dsh_work.config import get_builtin_icon_path

    icon = QIcon(str(get_builtin_icon_path()))
    assert not icon.isNull(), "鲸鱼图标加载失败"
    pm = icon.pixmap(50, 50)
    assert not pm.isNull()
    # 鲸鱼主体为黑色
    img = pm.toImage()
    c = img.pixelColor(25, 25)
    assert c.red() == 0 and c.green() == 0 and c.blue() == 0, (
        f"鲸鱼图标应为黑色, got rgb({c.red()},{c.green()},{c.blue()})"
    )


def test_builtin_ico_exists_for_pyinstaller() -> None:
    """ICO 文件存在（供 PyInstaller exe 图标）。"""
    from pathlib import Path

    ico = Path(__file__).resolve().parent.parent / "dsh_work" / "resources" / "icons" / "dsh_whale.ico"
    assert ico.exists(), f"exe 图标 ICO 缺失: {ico}"


def test_tray_icon_uses_whale_with_status_dot() -> None:
    """托盘图标：鲸鱼底图 + 状态点，多尺寸合成。

    注意：不做像素级断言——QIcon 的 pixmap 在隐式共享/高 DPI 下的
    具体渲染跨环境（本地 vs CI offscreen）不可靠。这里验证逻辑：
    - 鲸鱼底图可渲染
    - _create_icon 生成多尺寸图标
    - 各状态（已连接/未连接/工作态）都能生成非空图标
    """
    from dsh_work.ui.system_tray import SystemTray, _tray_whale_pixmap

    # 鲸鱼底图可渲染
    pm = _tray_whale_pixmap(32)
    assert not pm.isNull()

    tray = SystemTray()
    for connected, running in [(False, False), (True, False), (True, True)]:
        icon = tray._create_icon(connected=connected, running=running)
        assert not icon.isNull(), f"托盘图标加载失败 ({connected},{running})"
        sizes = icon.availableSizes()
        assert len(sizes) >= 3, f"托盘图标应有多个尺寸, got {list(sizes)}"
