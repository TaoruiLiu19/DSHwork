"""主题管理器。

主题数据结构（第 4.1 节）：
{
  "name": "Midnight Ocean",
  "type": "dark",
  "colors": { ... },
  "background": { "image", "mask_color", "mask_opacity", "background_attachment": "fixed" },
  "effects": { "glass_effect", "glass_blur", "glass_opacity", "bubble_radius", "animation" }
}

实时预览的渲染限频（第 4.2 节）：
- 拖动滑块调整主题颜色时，仅刷新当前视口可见的前 20 条消息气泡
- 松开滑块停止交互 300ms 后，才触发全量消息列表完整刷新

背景图片视口锚定渲染（第 4.3 节）：
- background_attachment 仅支持 "fixed"，不提供切换开关
- 重写 QScrollArea 的 viewport paintEvent，在视口坐标系 (0,0) 原点绘制背景
- 静态缓存策略：窗口尺寸变化时在后台线程生成适配视口的 background_cache_pixmap

可读性保障规则（第 4.3 节 callout）：
- 背景图不透明度 > 0.4 且未开启磨砂玻璃效果时，自动将消息气泡背景切换为不透明模式
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

from ... import constants as C
from ...config import get_builtin_themes_dir, get_themes_dir, UserConfig
from ...utils.logger import get_logger

log = get_logger("ui.theme_manager")


@dataclass
class ThemeColors:
    """配色方案（TraeCode 深色 token）。"""

    bg_primary: str = "#1A1B1D"
    bg_secondary: str = "#222427"
    bg_hover: str = "#2A2D31"
    text_primary: str = "#D1D3DB"
    text_secondary: str = "#9599A6"
    text_muted: str = "#666B75"
    accent: str = "#32F08C"
    accent_secondary: str = "#7BB8FF"
    success: str = "#33C192"
    warning: str = "#D27E24"
    error: str = "#F65A5A"
    border: str = "rgba(224, 226, 242, 0.1)"
    border_light: str = "rgba(224, 226, 242, 0.16)"

    @classmethod
    def from_dict(cls, data: dict) -> ThemeColors:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class ThemeBackground:
    """背景图片配置。"""

    image: str = ""
    mask_color: str = "#000000"
    mask_opacity: float = 0.3
    background_attachment: str = "fixed"  # 仅支持 fixed（第 4.3 节）

    @classmethod
    def from_dict(cls, data: dict) -> ThemeBackground:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class ThemeEffects:
    """视觉效果配置。"""

    glass_effect: bool = True
    glass_blur: int = 20
    glass_opacity: float = 0.75
    bubble_radius: int = 12
    animation: str = "smooth"

    @classmethod
    def from_dict(cls, data: dict) -> ThemeEffects:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Theme:
    """完整主题描述。"""

    name: str = "Midnight Ocean"
    type: str = "dark"  # dark / light
    colors: ThemeColors = field(default_factory=ThemeColors)
    background: ThemeBackground = field(default_factory=ThemeBackground)
    effects: ThemeEffects = field(default_factory=ThemeEffects)
    # 主题文件来源路径（内置或用户自定义）
    source_path: str = ""
    # 是否为内置主题
    is_builtin: bool = False

    @classmethod
    def from_dict(cls, data: dict, path: str = "", is_builtin: bool = False) -> Theme:
        return cls(
            name=data.get("name", "Unknown"),
            type=data.get("type", "dark"),
            colors=ThemeColors.from_dict(data.get("colors", {})),
            background=ThemeBackground.from_dict(data.get("background", {})),
            effects=ThemeEffects.from_dict(data.get("effects", {})),
            source_path=path,
            is_builtin=is_builtin,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("source_path", None)
        d.pop("is_builtin", None)
        return d

    @property
    def is_dark(self) -> bool:
        return self.type == "dark"

    @property
    def is_light(self) -> bool:
        return self.type == "light"

    @property
    def needs_readability_protection(self) -> bool:
        """可读性保障规则：背景图不透明度 > 0.4 且未开启磨砂玻璃效果时，
        自动将消息气泡背景切换为不透明模式。

        用户可在设置中关闭此自动保护。
        """
        return (
            self.background.mask_opacity > 0.4
            and not self.effects.glass_effect
            and bool(self.background.image)
        )


class ThemeManager:
    """主题管理器。

    负责加载内置与用户主题、切换主题、生成 QSS 样式表、实时预览限频。

    主题名采用双索引：
    - display_name：JSON 文件中声明的人类可读名（如 "Midnight Ocean"）
    - key_name：snake_case 标识名，用于 UserConfig 持久化（如 "midnight_ocean"）

    set_current 时按 key_name 优先匹配，回退 display_name，避免配置与 JSON 字面名不一致。
    """

    # 内置主题 key → display_name 的固定映射
    BUILTIN_KEY_TO_NAME = {
        "midnight_ocean": "Midnight Ocean",
        "daylight": "Daylight",
        "forest_green": "Forest Green",
    }

    def __init__(self):
        self._themes: dict[str, Theme] = {}  # display_name → Theme
        self._key_to_name: dict[str, str] = {}  # key_name(snake_case) → display_name
        self._current: Theme | None = None
        self._listeners: list[Callable[[Theme], None]] = []

        # 实时预览限频状态
        self._preview_viewport_count = C.THEME_PREVIEW_VIEWPORT_LIMIT
        self._settle_pending = False

    @staticmethod
    def _to_key_name(display_name: str) -> str:
        """将 display_name 转成 snake_case key 名。"""
        import re
        s = display_name.strip().lower().replace(" ", "_")
        s = re.sub(r"[^a-z0-9_]", "", s)
        return s

    @property
    def current(self) -> Theme | None:
        return self._current

    @property
    def themes(self) -> dict[str, Theme]:
        return self._themes

    @property
    def theme_keys(self) -> dict[str, str]:
        """key_name → display_name 映射。"""
        return self._key_to_name

    def load_all(self) -> dict[str, Theme]:
        """加载所有主题（内置 + 用户自定义）。"""
        self._themes.clear()
        self._key_to_name.clear()

        # 加载内置主题（先注册固定 key 映射）
        for k, n in self.BUILTIN_KEY_TO_NAME.items():
            self._key_to_name[k] = n

        builtin_dir = get_builtin_themes_dir()
        if builtin_dir.exists():
            for theme_file in builtin_dir.glob("*.json"):
                try:
                    with open(theme_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    theme = Theme.from_dict(data, path=str(theme_file), is_builtin=True)
                    self._themes[theme.name] = theme
                    self._key_to_name[self._to_key_name(theme.name)] = theme.name
                    log.debug("加载内置主题: %s", theme.name)
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("加载主题文件失败 %s: %s", theme_file, e)

        # 加载用户自定义主题
        user_dir = get_themes_dir()
        if user_dir.exists():
            for theme_file in user_dir.glob("*.json"):
                try:
                    with open(theme_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    theme = Theme.from_dict(data, path=str(theme_file), is_builtin=False)
                    self._themes[theme.name] = theme
                    self._key_to_name[self._to_key_name(theme.name)] = theme.name
                    log.debug("加载用户主题: %s", theme.name)
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("加载用户主题失败 %s: %s", theme_file, e)

        log.info("已加载 %d 个主题", len(self._themes))
        return self._themes

    def get_theme(self, name: str) -> Theme | None:
        """按 key_name 或 display_name 查找主题。"""
        if name in self._themes:
            return self._themes[name]
        display = self._key_to_name.get(name)
        if display and display in self._themes:
            return self._themes[display]
        return None

    def _resolve_name(self, name: str) -> tuple[str, Theme] | None:
        """将 key_name / display_name 解析为 (persist_key, theme)。"""
        if name in self._themes:
            # 传的是 display_name，求它的 persist_key
            persist_key = next(
                (k for k, v in self._key_to_name.items() if v == name),
                self._to_key_name(name),
            )
            return persist_key, self._themes[name]
        display = self._key_to_name.get(name)
        if display and display in self._themes:
            return name, self._themes[display]
        return None

    def set_current(self, name: str) -> Theme | None:
        """设置当前主题。

        支持 key_name（如 midnight_ocean）和 display_name（如 Midnight Ocean）。
        持久化一律用 key_name，避免配置中的字面名与 JSON 名不一致。
        """
        resolved = self._resolve_name(name)
        if not resolved:
            # 回退：如果找不到，尝试用第一个内置主题
            fallback_key = next(iter(self.BUILTIN_KEY_TO_NAME), None)
            if fallback_key and fallback_key in self._key_to_name:
                resolved = (fallback_key, self._themes[self._key_to_name[fallback_key]])
                log.warning("主题不存在: %s，回退到默认 %s", name, fallback_key)
            else:
                log.warning("主题不存在: %s，且无可用主题", name)
                return None
        persist_key, theme = resolved
        self._current = theme
        # 持久化到用户配置（统一使用 key_name）
        cfg = UserConfig.load()
        cfg.theme = persist_key
        cfg.save()
        # 通知监听器
        for listener in list(self._listeners):
            try:
                listener(theme)
            except Exception as e:
                log.error("主题监听器异常: %s", e)
        log.info("当前主题已切换: %s (key=%s)", theme.name, persist_key)
        return theme

    def add_listener(self, listener: Callable[[Theme], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Theme], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def generate_qss(self, theme: Theme | None = None) -> str:
        """根据主题生成 QSS 样式表。

        磨砂玻璃效果通过 QGraphicsBlurEffect 在 widget 层实现，
        QSS 只负责配色与圆角。
        """
        t = theme or self._current
        if not t:
            return ""
        c = t.colors
        radius = t.effects.bubble_radius
        return f"""
        QWidget#MainWindow {{
            background-color: {c.bg_primary};
        }}
        QWidget#TopBar {{
            background-color: {c.bg_secondary};
            border-bottom: 1px solid {c.border};
        }}
        QWidget#StatusBar {{
            background-color: {c.bg_secondary};
            border-top: 1px solid {c.border};
        }}
        QWidget#LeftPanel, QWidget#RightPanel {{
            background-color: {c.bg_secondary};
            border-right: 1px solid {c.border};
        }}
        QWidget#RightPanel {{
            border-right: none;
            border-left: 1px solid {c.border};
        }}
        QWidget#CenterPanel {{
            background-color: {c.bg_primary};
        }}
        QLabel {{
            color: {c.text_primary};
        }}
        QLabel#Secondary {{
            color: {c.text_secondary};
        }}
        QLabel#Muted {{
            color: {c.text_muted};
        }}
        QPushButton {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: {radius}px;
            padding: 6px 14px;
        }}
        QPushButton:hover {{
            background-color: {c.border};
            border-color: {c.accent};
        }}
        QPushButton:pressed {{
            background-color: {c.border_light};
        }}
        QPushButton#Primary {{
            background-color: {c.accent};
            color: {c.bg_primary};
            border: none;
        }}
        QPushButton#Primary:hover {{
            background-color: {c.accent_secondary};
        }}
        QPushButton#ModeWork {{
            background-color: #32F08C;
            color: #0C0C0D;
            border: none;
            border-radius: 12px;
            padding: 4px 16px;
        }}
        QPushButton#ModeCode {{
            background-color: #7BB8FF;
            color: #0C0C0D;
            border: none;
            border-radius: 12px;
            padding: 4px 16px;
        }}
        QTextEdit, QPlainTextEdit {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: {radius}px;
            padding: 8px;
        }}
        QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {c.accent};
        }}
        QListWidget, QTreeWidget {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: {radius}px;
        }}
        QListWidget::item:hover, QTreeWidget::item:hover {{
            background-color: {c.bg_hover};
        }}
        QListWidget::item:selected, QTreeWidget::item:selected {{
            background-color: {c.accent};
            color: {c.bg_primary};
        }}
        QComboBox {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: 6px;
            padding: 4px 10px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            selection-background-color: {c.accent};
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
        }}
        QScrollBar::handle:vertical {{
            background: {c.border};
            border-radius: 5px;
            min-height: 30px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {c.text_muted};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
        }}
        QScrollBar::handle:horizontal {{
            background: {c.border};
            border-radius: 5px;
            min-width: 30px;
        }}
        QSplitter::handle {{
            background-color: {c.border};
        }}
        QSplitter::handle:hover {{
            background-color: {c.accent};
        }}
        QToolTip {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: 6px;
            padding: 4px 8px;
        }}
        QStatusBar {{
            color: {c.text_secondary};
        }}
        QFrame#MessageBubbleUser {{
            background-color: {c.accent};
            color: {c.bg_primary};
            border-radius: {radius}px;
        }}
        QFrame#MessageBubbleAssistant {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: {radius}px;
        }}
        QFrame#ToolCallCard {{
            background-color: {c.bg_hover};
            border: 1px solid {c.border};
            border-radius: 8px;
        }}
        QTabWidget::pane {{
            background-color: {c.bg_primary};
            border: 1px solid {c.border};
            border-radius: 6px;
            top: -1px;
        }}
        QTabBar::tab {{
            background-color: {c.bg_hover};
            color: {c.text_secondary};
            border: 1px solid {c.border};
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 6px 12px;
            margin-right: 2px;
        }}
        QTabBar::tab:selected {{
            background-color: {c.bg_primary};
            color: {c.text_primary};
            border-color: {c.border_light};
        }}
        QTabBar::tab:hover:!selected {{
            background-color: {c.border};
            color: {c.text_primary};
        }}
        QWidget#RightWorkPage, QWidget#RightCodePage {{
            background-color: transparent;
        }}
        QFrame#InlinePreview {{
            background-color: {c.bg_secondary};
            border: 1px solid {c.border};
            border-radius: 8px;
        }}
        """

    def export_theme(self, theme: Theme, path: Path) -> None:
        """导出主题为 JSON 文件（主题分享用）。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(theme.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("主题已导出: %s", path)

    def import_theme(self, path: Path) -> Theme | None:
        """导入外部主题 JSON 文件。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            theme = Theme.from_dict(data, path=str(path), is_builtin=False)
            # 复制到用户主题目录
            dest = get_themes_dir() / f"{path.stem}.json"
            with open(dest, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._themes[theme.name] = theme
            log.info("主题已导入: %s → %s", path, dest)
            return theme
        except (json.JSONDecodeError, OSError) as e:
            log.error("导入主题失败: %s", e)
            return None
