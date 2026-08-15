"""主题系统（第 4 章）。

主题以 JSON 文件存储，包含配色、背景和效果三组参数。
文件存放在 ~/.dsh-work/themes/ 目录下（用户自定义），
内置主题打包在 resources/themes/。

内置三套预设主题：
- Midnight Ocean（dark，默认）
- Daylight（light）
- Forest Green（dark，护眼）

主题热切换：用户可以随时切换，切换即时生效，不需要重启。
"""

from .theme_manager import ThemeManager, Theme, ThemeColors, ThemeBackground, ThemeEffects
from .glass_widget import GlassWidget, FixedBackgroundScrollArea

__all__ = [
    "ThemeManager",
    "Theme",
    "ThemeColors",
    "ThemeBackground",
    "ThemeEffects",
    "GlassWidget",
    "FixedBackgroundScrollArea",
]
