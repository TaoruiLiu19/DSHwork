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
    """托盘图标：鲸鱼底图 + 右下角状态点，多尺寸合成。"""
    from dsh_work.ui.system_tray import SystemTray, _tray_whale_pixmap

    # 鲸鱼底图可渲染
    pm = _tray_whale_pixmap(32)
    assert not pm.isNull()

    tray = SystemTray()
    icon = tray._create_icon(connected=True, running=False)
    assert not icon.isNull(), "托盘图标加载失败"
    # 多尺寸合成（高 DPI 下物理像素会放大，检查可用尺寸集合非空即可）
    sizes = icon.availableSizes()
    assert len(sizes) >= 3, f"托盘图标应有多个尺寸, got {list(sizes)}"

    # 状态点区域存在纯绿色（已连接 #33C192）
    img = icon.pixmap(64, 64).toImage()
    found_green = False
    for x in range(32, 64):
        for y in range(32, 64):
            c = img.pixelColor(x, y)
            if (c.red(), c.green(), c.blue()) == (51, 193, 146):
                found_green = True
                break
        if found_green:
            break
    assert found_green, "托盘图标右下角应有绿色状态点(已连接)"
