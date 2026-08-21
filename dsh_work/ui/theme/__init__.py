"""主题系统（对齐 DeepSeek Harness Web 版视觉）。

主题以 JSON 文件存储，包含配色、背景和效果三组参数。
文件存放在 ~/.dsh-work/themes/ 目录下（用户自定义），
内置主题打包在 resources/themes/。

内置两套 Web 版预设主题（配色取自 dsh-web-frontend 的 --dsw-* token）：
- Web Dark（dark，默认，bg #151517 / label #F9FAFB / accent #679EFE）
- Web Light（light，bg #FFFFFF / label #0F1115 / accent #4176E6）

主题热切换：用户可以随时切换，切换即时生效，不需要重启。
旧版主题名（midnight_ocean / daylight / qinghua / forest_green）自动迁移。
"""

from .glass_widget import FixedBackgroundScrollArea, GlassWidget
from .theme_manager import Theme, ThemeBackground, ThemeColors, ThemeEffects, ThemeManager

__all__ = [
    "ThemeManager",
    "Theme",
    "ThemeColors",
    "ThemeBackground",
    "ThemeEffects",
    "GlassWidget",
    "FixedBackgroundScrollArea",
]
