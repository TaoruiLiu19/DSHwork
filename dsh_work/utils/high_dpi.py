"""高 DPI 适配。

PySide6 基于 Qt6，高 DPI 缩放默认开启。QT_ENABLE_HIGHDPI_SCALING 属 Qt5 遗留开关、
在 Qt6 中为空操作；真正生效的是 setHighDpiScaleFactorRoundingPolicy 与
QT_SCALE_FACTOR_ROUNDING_POLICY。

Windows 1.5x 缩放建议用 PassThrough 避免整数取整导致发糊，
1.25x/1.75x 场景可按需切 RoundPreferFloor。

必须在 QApplication 实例化之前调用。
"""

from __future__ import annotations

import os

from .. import constants as C


def setup_high_dpi() -> None:
    """在 QApplication 实例化之前设置高 DPI 缩放策略。

    保留 Qt5 遗留环境变量以兼容可能的 Qt5 回退构建，但不要把它当作 Qt6 下的生效路径。
    """
    # Qt5 遗留开关（Qt6 下为空操作，但保留以兼容回退构建）
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
    os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"

    # Qt6 真正生效路径
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except ImportError:
        # PySide6 尚未安装时静默跳过，允许在无 GUI 环境下导入此模块
        pass
