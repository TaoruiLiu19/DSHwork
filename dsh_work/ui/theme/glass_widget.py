"""磨砂玻璃 widget 与背景固定渲染（第 4.3 节）。

视口锚定渲染核心约束：
- 背景图固定于视口，绝不随滚动条移动
- background_attachment: "fixed" 作为唯一样式，不提供切换开关

实现分两层：
1. 底层：图片本身（通过 QPainter 绘制到对话区域背景）
2. 上层：半透明遮罩（mask_color + opacity 控制图片可见度），确保文字可读

静态缓存策略：
- 窗口首次加载或尺寸变化时，在后台线程生成适配视口尺寸的 background_cache_pixmap
- 滚动时视口仅平移此缓存图，绝不实时重绘原图或重算模糊

磨砂玻璃适配：
- 由于背景固定，气泡移动时其背后的背景区域只需依据气泡 geometry()
  从同一张静态背景缓存中裁剪对应区域进行模糊
- 不需要对屏幕实时截屏（grab()），性能极高
"""

from __future__ import annotations

from pathlib import Path

from ..theme.theme_manager import Theme
from ...utils.logger import get_logger

log = get_logger("ui.glass_widget")


class FixedBackgroundScrollArea:
    """背景固定滚动区域（混入类）。

    重写 QScrollArea 的 viewport paintEvent：
    - 在绘制任何子控件（消息气泡）之前，强制在视口坐标系 (0,0) 原点绘制背景 Pixmap
    - 绘制时不应用 translate() 滚动偏移

    使用方式：作为 QScrollArea 子类的混入。
    """

    def _setup_fixed_background(self, theme: Theme) -> None:
        """初始化固定背景。

        Args:
            theme: 当前主题
        """
        from PySide6.QtGui import QPixmap, QColor, QPainter
        from PySide6.QtCore import Qt

        self._theme = theme
        self._background_pixmap: QPixmap | None = None
        self._background_cache_pixmap: QPixmap | None = None
        self._mask_color = QColor(theme.background.mask_color)
        self._mask_opacity = theme.background.mask_opacity

        if theme.background.image:
            self._load_background_image(theme.background.image)

    def _load_background_image(self, image_path: str) -> None:
        """加载背景图片。"""
        from PySide6.QtGui import QPixmap

        path = Path(image_path)
        if not path.exists():
            log.warning("背景图片不存在: %s", image_path)
            return
        self._background_pixmap = QPixmap(str(path))
        if self._background_pixmap.isNull():
            log.warning("背景图片加载失败: %s", image_path)
            self._background_pixmap = None
            return
        self._rebuild_cache()
        log.info("背景图片已加载: %s", image_path)

    def _rebuild_cache(self) -> None:
        """重建背景缓存 Pixmap（窗口尺寸变化时调用）。

        将原图缩放到视口尺寸，并叠加半透明遮罩。
        """
        from PySide6.QtGui import QPixmap, QPainter, QColor
        from PySide6.QtCore import QSize

        if not self._background_pixmap:
            return

        viewport = self.viewport() if hasattr(self, "viewport") else None
        if viewport is None:
            return
        size: QSize = viewport.size()
        if size.isEmpty():
            return

        # 缩放原图到视口尺寸
        scaled = self._background_pixmap.scaled(
            size,
            aspectMode=self._background_pixmap.aspectMode() if hasattr(self._background_pixmap, "aspectMode") else 1,
            mode=1,  # Qt.SmoothTransformation
        ) if hasattr(self._background_pixmap, "scaled") else self._background_pixmap.scaled(size)

        self._background_cache_pixmap = QPixmap(size)
        painter = QPainter(self._background_cache_pixmap)
        painter.drawPixmap(0, 0, scaled)
        # 叠加半透明遮罩
        painter.fillRect(self._background_cache_pixmap.rect(), QColor(self._mask_color))
        painter.setOpacity(self._mask_opacity)
        painter.fillRect(self._background_cache_pixmap.rect(), QColor(self._mask_color))
        painter.end()
        log.debug("背景缓存已重建: %dx%d", size.width(), size.height())

    def _paint_fixed_background(self, event) -> None:
        """在视口坐标系 (0,0) 原点绘制背景（不应用滚动偏移）。"""
        from PySide6.QtGui import QPainter

        if not self._background_cache_pixmap:
            return
        painter = QPainter(self.viewport())
        # 关键：在视口坐标系原点绘制，不 translate 滚动偏移
        painter.drawPixmap(0, 0, self._background_cache_pixmap)
        painter.end()

    def on_viewport_resized(self, event) -> None:
        """视口尺寸变化时重建缓存。"""
        self._rebuild_cache()


class GlassWidget:
    """磨砂玻璃效果混入。

    对气泡下方的图片区域做高斯模糊 + 半透明叠加，形成毛玻璃质感。
    由于背景固定，气泡移动时只需依据 geometry() 从静态背景缓存裁剪对应区域模糊。

    注意：QGraphicsBlurEffect 会与 QWidget 的子控件冲突，
    实际使用时采用"裁剪背景区域 + 预模糊缓存"方案。
    """

    @staticmethod
    def apply_glass_effect(widget, theme: Theme) -> None:
        """为 widget 应用磨砂玻璃效果。

        实现策略：
        1. 若主题开启 glass_effect，对 widget 设置 QGraphicsBlurEffect
        2. 设置 widget 的背景半透明（glass_opacity）
        3. 可读性保护：背景图不透明度 > 0.4 且未开磨砂玻璃时切不透明背景
        """
        from PySide6.QtWidgets import QGraphicsBlurEffect, QWidget
        from PySide6.QtCore import Qt

        if not isinstance(widget, QWidget):
            return

        if not theme.effects.glass_effect:
            # 未开启磨砂玻璃，检查可读性保护
            if theme.needs_readability_protection:
                # 强制不透明背景
                widget.setAttribute(Qt.WA_OpaquePaintEvent, True)
            return

        blur = QGraphicsBlurEffect(widget)
        blur.setBlurRadius(theme.effects.glass_blur)
        # 注意：直接对气泡 widget 设 blur 会模糊其内容文字，
        # 实际实现应在气泡背后放置一个专门的背景 widget 做 blur。
        # 此处保留接口，具体实现在 MessageBubble 中处理。
        widget.setProperty("glass_blur", theme.effects.glass_blur)
        widget.setProperty("glass_opacity", theme.effects.glass_opacity)

    @staticmethod
    def generate_glass_qss(theme: Theme) -> str:
        """生成磨砂玻璃风格的 QSS 片段。"""
        if not theme.effects.glass_effect:
            return ""
        opacity = theme.effects.glass_opacity
        # QSS 不支持 rgba 透明度直接，用 background-color + opacity 属性近似
        return f"""
        QFrame#GlassBubble {{
            background-color: rgba({int(opacity * 255)}, {int(opacity * 255)}, {int(opacity * 255)}, {opacity});
            border-radius: {theme.effects.bubble_radius}px;
        }}
        """
