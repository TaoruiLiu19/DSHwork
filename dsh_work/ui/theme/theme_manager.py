"""主题管理器。

主题数据结构（对齐 dsh-web-frontend 的 --dsw-* 设计 token）：
{
  "name": "Web Dark",
  "type": "dark",
  "colors": { ... },
  "background": { "image", "mask_color", "mask_opacity", "background_attachment": "fixed" },
  "effects": { "glass_effect", "glass_blur", "glass_opacity", "bubble_radius", "animation" }
}

v0.4 起内置主题为 Web 版深浅两套（web_light / web_dark），配色全部取自
DeepSeek Harness WebUI（dsh-web-frontend/dist/assets/*.css）的 alias token：
- 浅色: bg-base #FFFFFF / label-primary #0F1115 / brand #4176E6 / border rgba(0,0,0,.1)
- 深色: bg-base #151517 / label-primary #F9FAFB / brand #679EFE / border rgba(255,255,255,.12)
旧版主题名（midnight_ocean / daylight / qinghua / forest_green）在 set_current 时自动迁移。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ...config import get_builtin_themes_dir, get_themes_dir
from ...utils.logger import get_logger

log = get_logger("ui.theme_manager")


@dataclass
class ThemeColors:
    """配色方案（Web 深浅双模式 token，新增字段带默认值，主题文件缺省时不崩溃）。"""

    bg_primary: str = "#151517"        # 应用主背景（Web: --dsw-alias-bg-base）
    bg_secondary: str = "#1B1B1C"      # 面板/浮层背景（Web: --dsw-alias-bg-layer-1）
    bg_sidebar: str = "#151517"        # 侧边栏背景
    bg_hover: str = "rgba(255, 255, 255, 0.08)"    # 悬停态（Web: interactive-bg-hover）
    bg_active: str = "rgba(255, 255, 255, 0.14)"   # 激活态（Web: interactive-bg-active）
    bg_card: str = "#1B1B1C"           # 卡片/代码块背景（Web: markdown-code-block）
    text_primary: str = "#F9FAFB"      # 主文字（Web: label-primary）
    text_secondary: str = "#CFD3D6"    # 次文字（Web: label-secondary）
    text_muted: str = "#ADB2B8"        # 弱文字（Web: label-tertiary）
    text_caption: str = "#81858C"      # 说明文字（Web: label-caption）
    brand_primary: str = "#F9FAFB"     # 品牌实体色（Web: brand-primary，主按钮填充色）
    accent: str = "#679EFE"            # 强调色/链接/激活指示（Web: state-business-primary）
    accent_hover: str = "#5686FE"
    accent_secondary: str = "#679EFE"
    success: str = "#22C55E"
    warning: str = "#F59E0B"
    error: str = "#F25A5A"
    # 线条体系（Web: --dsw-alias-border-l1..l4）
    border: str = "rgba(255, 255, 255, 0.12)"        # 主边框 (l2)
    border_light: str = "rgba(255, 255, 255, 0.20)"  # 强线条 (l4)
    divider: str = "rgba(255, 255, 255, 0.06)"       # 面板间分隔线 (l1)
    gridline: str = "rgba(255, 255, 255, 0.04)"      # 表格网格线
    input_bg: str = "#232324"                        # 输入区底色
    input_border: str = "rgba(255, 255, 255, 0.12)"  # 输入区边框
    tab_border: str = "rgba(255, 255, 255, 0.08)"    # Tab 边框
    btn_border: str = "rgba(255, 255, 255, 0.12)"    # 按钮边框
    # Web 版 markdown 渲染 token
    markdown_code_bg: str = "#1B1B1C"                # 代码块背景
    markdown_code_banner: str = "#232324"            # 代码块语言标签条
    markdown_inline_bg: str = "#232324"              # 行内代码背景
    markdown_block_border: str = "rgba(255, 255, 255, 0.06)"  # 代码块边框
    tooltip_bg: str = "#43454A"                      # 提示气泡背景（Web: tooltip-bg）
    scrollbar_handle: str = "rgba(255, 255, 255, 0.16)"       # 滚动条滑块
    # 会话状态点（Web: running=蓝 / pending=琥珀 / done=绿）
    state_dot_running: str = "#679EFE"
    state_dot_pending: str = "#F59E0B"
    state_dot_done: str = "#22C55E"

    @classmethod
    def from_dict(cls, data: dict) -> ThemeColors:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class ThemeBackground:
    """背景图片配置。"""

    image: str = ""
    mask_color: str = "#000000"
    mask_opacity: float = 0.3
    background_attachment: str = "fixed"  # 仅支持 fixed

    @classmethod
    def from_dict(cls, data: dict) -> ThemeBackground:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class ThemeEffects:
    """视觉效果配置。"""

    glass_effect: bool = True
    glass_blur: int = 20
    glass_opacity: float = 0.75
    bubble_radius: int = 8
    animation: str = "smooth"

    @classmethod
    def from_dict(cls, data: dict) -> ThemeEffects:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass
class Theme:
    """完整主题描述。"""

    name: str = "Web Dark"
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


_theme_singleton: ThemeManager | None = None


class ThemeManager:
    """主题管理器。

    负责加载内置与用户主题、切换主题、生成 QSS 样式表。
    单例：全进程共享一个实例，确保 app.py 的 set_current 能通知到所有组件
    （MainWindow / LeftPanel 等）注册的监听器。
    """

    # 内置主题 key → display_name 的固定映射（Web 版深浅两套，对齐 dsh-web-frontend token）
    BUILTIN_KEY_TO_NAME = {
        "web_light": "Web Light",
        "web_dark": "Web Dark",
    }

    # 旧主题名 → 新 Web 主题迁移映射（v0.3 之前的内置主题已被 Web 主题取代）
    LEGACY_THEME_MIGRATION = {
        "midnight_ocean": "web_dark",
        "forest_green": "web_dark",
        "qinghua": "web_dark",
        "daylight": "web_light",
    }

    # 主题中文显示名（UI 层展示用，内部 key 不变）
    CN_DISPLAY_NAMES = {
        "web_light": "Web 浅色",
        "web_dark": "Web 深色",
    }

    def __new__(cls):
        global _theme_singleton
        if _theme_singleton is None:
            _theme_singleton = super().__new__(cls)
        return _theme_singleton

    def __init__(self):
        # 单例防重入
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._themes: dict[str, Theme] = {}  # display_name → Theme
        self._key_to_name: dict[str, str] = {}  # key_name(snake_case) → display_name
        self._current: Theme | None = None
        self._listeners: list[Callable[[Theme], None]] = []

        # QSS 缓存（key_name → QSS 字符串）
        self._qss_cache: dict[str, str] = {}

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

    def cn_display_name(self, key_name: str) -> str:
        """获取主题中文显示名（UI 层用），无映射时回退 display_name。"""
        return self.CN_DISPLAY_NAMES.get(key_name) or self._key_to_name.get(key_name, key_name)

    def load_all(self) -> dict[str, Theme]:
        """加载所有主题（内置 + 用户自定义）。"""
        self._themes.clear()
        self._key_to_name.clear()
        self._qss_cache.clear()  # 主题重载时清空 QSS 缓存

        # 加载内置主题（先注册固定 key 映射）
        for k, n in self.BUILTIN_KEY_TO_NAME.items():
            self._key_to_name[k] = n

        builtin_dir = get_builtin_themes_dir()
        if builtin_dir.exists():
            for theme_file in builtin_dir.glob("*.json"):
                try:
                    with open(theme_file, encoding="utf-8") as f:
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
                    with open(theme_file, encoding="utf-8") as f:
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

        支持 key_name（如 web_dark）和 display_name（如 Web Dark）。
        旧版内置主题名（midnight_ocean / daylight / qinghua / forest_green）自动迁移到
        对应的 Web 深浅主题。持久化一律用 key_name。
        """
        if name in self.LEGACY_THEME_MIGRATION:
            mapped = self.LEGACY_THEME_MIGRATION[name]
            log.info("旧主题名 %s 已迁移到 %s", name, mapped)
            name = mapped
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
        # 通知监听器（配置持久化由调用方负责，避免双重保存）
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
        """根据主题生成 QSS 样式表，全面对齐 DeepSeek Harness Web 版视觉。

        布局组件 objectName（与 ui/ 层组件一一对应）：
        - WebSidebar / SidebarHeader / SidebarFooter / SessionRow / StatusDot
        - ConversationHeader / MessageRow / MarkdownCodeBlock / ThinkRow / ToolRow
        - ComposerFrame / ComposerTextEdit / SendButton / StatsDock / ContextMeter

        性能优化：返回结果会被 set_current 缓存到 _qss_cache 中。
        """
        t = theme or self._current
        if not t:
            return ""

        # 检查缓存（按 key_name 缓存）
        key_name = self._to_key_name(t.name)
        cached = self._qss_cache.get(key_name)
        if cached is not None:
            return cached

        c = t.colors
        radius = t.effects.bubble_radius
        qss = f"""
        /* ===== 应用骨架 ===== */
        QWidget#MainWindow {{
            background-color: {c.bg_primary};
        }}
        QWidget#WebSidebar {{
            background-color: {c.bg_sidebar};
            border-right: 1px solid {c.divider};
        }}
        QWidget#ConversationHeader {{
            background-color: {c.bg_primary};
            border-bottom: 1px solid {c.divider};
        }}
        QWidget#StatusBar {{
            background-color: {c.bg_secondary};
            border-top: 1px solid {c.divider};
        }}
        QWidget#CenterPanel {{
            background-color: {c.bg_primary};
        }}
        QScrollArea {{
            background: transparent;
        }}
        QScrollArea > QWidget > QWidget {{
            background: transparent;
        }}

        /* ===== 文本 ===== */
        QLabel {{
            color: {c.text_primary};
        }}
        QLabel#Secondary {{
            color: {c.text_secondary};
        }}
        QLabel#Muted {{
            color: {c.text_muted};
        }}
        QLabel#Caption {{
            color: {c.text_caption};
            font-size: 12px;
        }}

        /* ===== 侧边栏（Web Sidebar）===== */
        QWidget#SidebarHeader {{
            background-color: transparent;
        }}
        QPushButton#SidebarBtn {{
            background-color: transparent;
            color: {c.text_secondary};
            border: none;
            border-radius: 6px;
            padding: 5px 8px;
            font-size: 13px;
        }}
        QPushButton#SidebarBtn:hover {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
        }}
        QPushButton#NewSessionBtn {{
            background-color: {c.brand_primary};
            color: {c.bg_primary};
            border: none;
            border-radius: 6px;
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#NewSessionBtn:hover {{
            background-color: {c.text_secondary};
        }}
        QFrame#SessionRow {{
            background-color: transparent;
            border: none;
            border-radius: 8px;
        }}
        QFrame#SessionRow:hover {{
            background-color: {c.bg_hover};
        }}
        QFrame#SessionRow[selected="true"] {{
            background-color: {c.bg_active};
        }}
        QFrame#SessionRow[selected="true"]:hover {{
            background-color: {c.bg_active};
        }}
        QLabel#SessionTitle {{
            color: {c.text_primary};
            font-size: 13px;
        }}
        QLabel#SessionTime {{
            color: {c.text_caption};
            font-size: 11px;
        }}
        QLabel#StatusDot {{
            border-radius: 4px;
            min-width: 8px;
            max-width: 8px;
            min-height: 8px;
            max-height: 8px;
        }}

        /* ===== 对话头（Conversation Header）===== */
        QLabel#ConversationTitle {{
            color: {c.text_primary};
            font-size: 15px;
            font-weight: 600;
        }}
        QPushButton#HeaderBtn {{
            background-color: transparent;
            color: {c.text_secondary};
            border: none;
            border-radius: 6px;
            padding: 5px 10px;
            font-size: 13px;
        }}
        QPushButton#HeaderBtn:hover {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
        }}

        /* ===== 消息流（Web ChatView：全宽行，非气泡）===== */
        QWidget#MessageListContainer {{
            background: transparent;
        }}
        QFrame#MessageRow {{
            background-color: transparent;
            border: none;
        }}
        QFrame#MessageRowUser {{
            background-color: transparent;
            border: none;
        }}
        QLabel#MessageRole {{
            color: {c.text_caption};
            font-size: 12px;
        }}
        QLabel#MessageRoleUser {{
            color: {c.text_caption};
            font-size: 12px;
        }}
        QLabel#MessageContent {{
            color: {c.text_primary};
            font-size: 14px;
        }}
        QLabel#MessageContentUser {{
            color: {c.text_primary};
            font-size: 14px;
        }}
        /* hover 操作图标（copy 等） */
        QPushButton#MsgActionBtn {{
            background-color: transparent;
            color: {c.text_muted};
            border: none;
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 12px;
        }}
        QPushButton#MsgActionBtn:hover {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
        }}

        /* ===== Markdown 渲染（对齐 Web token）===== */
        QFrame#MarkdownCodeBlock {{
            background-color: {c.markdown_code_bg};
            border: 1px solid {c.markdown_block_border};
            border-radius: 8px;
        }}
        QFrame#MarkdownCodeBanner {{
            background-color: {c.markdown_code_banner};
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }}
        QLabel#MarkdownCodeLang {{
            color: {c.text_muted};
            font-size: 12px;
        }}
        QLabel#MarkdownInlineCode {{
            background-color: {c.markdown_inline_bg};
            color: {c.text_primary};
            border-radius: 4px;
            padding: 1px 5px;
            font-family: "Consolas", "Courier New", monospace;
            font-size: 13px;
        }}
        QFrame#ThinkRow, QFrame#ToolRow, QFrame#ContextRow {{
            background-color: {c.bg_secondary};
            border: 1px solid {c.divider};
            border-radius: 8px;
        }}
        QLabel#ThinkTitle {{
            color: {c.text_muted};
            font-size: 12px;
        }}
        QLabel#ToolName {{
            color: {c.text_secondary};
            font-size: 13px;
        }}
        QLabel#TurnTailMeta {{
            color: {c.text_caption};
            font-size: 12px;
        }}

        /* ===== Composer 输入条（Web composer bar）===== */
        QFrame#ComposerFrame {{
            background-color: {c.input_bg};
            border: 1px solid {c.input_border};
            border-radius: 16px;
        }}
        QFrame#ComposerFrame:focus-within {{
            border-color: {c.accent};
        }}
        QPlainTextEdit#ComposerTextEdit, QTextEdit#ComposerTextEdit {{
            background-color: transparent;
            color: {c.text_primary};
            border: none;
            font-size: 14px;
        }}
        QPushButton#ComposerToolBtn {{
            background-color: transparent;
            color: {c.text_muted};
            border: none;
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 15px;
        }}
        QPushButton#ComposerToolBtn:hover {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
        }}
        QPushButton#SendButton {{
            background-color: {c.brand_primary};
            color: {c.bg_primary};
            border: none;
            border-radius: 10px;
            padding: 6px 18px;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#SendButton:hover {{
            background-color: {c.text_secondary};
        }}
        QPushButton#SendButton:disabled {{
            background-color: {c.bg_hover};
            color: {c.text_muted};
        }}
        QPushButton#StopButton {{
            background-color: {c.error};
            color: #FFFFFF;
            border: none;
            border-radius: 10px;
            padding: 6px 18px;
            font-size: 13px;
            font-weight: 600;
        }}
        QFrame#ComposerToolbar {{
            background-color: transparent;
            border-top: 1px solid {c.divider};
        }}
        QFrame#ContextMeter {{
            border-radius: 7px;
            border: 2px solid {c.accent};
        }}

        /* ===== StatsDock（Web 统计条）===== */
        QWidget#StatsDock {{
            background-color: transparent;
        }}
        QLabel#StatsLabel {{
            color: {c.text_caption};
            font-size: 11px;
        }}

        /* ===== 通用控件 ===== */
        QPushButton {{
            background-color: {c.bg_hover};
            color: {c.text_primary};
            border: 1px solid {c.btn_border};
            border-radius: {radius}px;
            padding: 6px 14px;
        }}
        QPushButton:hover {{
            background-color: {c.bg_active};
            border-color: {c.accent};
        }}
        QPushButton#Primary {{
            background-color: {c.brand_primary};
            color: {c.bg_primary};
            border: none;
        }}
        QPushButton#Primary:hover {{
            background-color: {c.text_secondary};
        }}
        QPushButton#Danger {{
            background-color: {c.error};
            color: #FFFFFF;
            border: none;
            border-radius: 6px;
        }}
        QFrame#DropIndicator {{
            background-color: {c.bg_card};
            color: {c.text_muted};
            border: 2px dashed {c.accent};
            border-radius: 8px;
            font-size: 13px;
        }}
        QTextEdit, QPlainTextEdit {{
            background-color: {c.input_bg};
            color: {c.text_primary};
            border: 1px solid {c.input_border};
            border-radius: {radius}px;
            padding: 8px;
        }}
        QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {c.accent};
        }}
        QListWidget, QTreeWidget {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            border: 1px solid {c.input_border};
            border-radius: {radius}px;
        }}
        QListWidget::item:hover, QTreeWidget::item:hover {{
            background-color: {c.bg_hover};
        }}
        QListWidget::item:selected, QTreeWidget::item:selected {{
            background-color: {c.bg_active};
            color: {c.text_primary};
        }}
        QComboBox {{
            background-color: {c.input_bg};
            color: {c.text_primary};
            border: 1px solid {c.input_border};
            border-radius: 6px;
            padding: 4px 10px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {c.bg_secondary};
            color: {c.text_primary};
            selection-background-color: {c.bg_active};
            border: 1px solid {c.input_border};
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
        }}
        QScrollBar::handle:vertical {{
            background: {c.scrollbar_handle};
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
            background: {c.scrollbar_handle};
            border-radius: 5px;
            min-width: 30px;
        }}
        QSplitter::handle {{
            background-color: {c.divider};
        }}
        QSplitter::handle:hover {{
            background-color: {c.accent};
        }}
        QToolTip {{
            background-color: {c.tooltip_bg};
            color: {c.text_primary};
            border: 1px solid {c.border};
            border-radius: 6px;
            padding: 4px 8px;
        }}
        QStatusBar {{
            color: {c.text_secondary};
        }}
        QFrame#ToolCallCard {{
            background-color: {c.bg_card};
            border: 1px solid {c.border};
            border-radius: 8px;
        }}
        QFrame#InputContainer {{
            background-color: {c.bg_secondary};
            border: 1px solid {c.input_border};
            border-radius: 12px;
        }}
        QFrame#InputContainer:focus-within {{
            border-color: {c.accent};
        }}
        QFrame#InputSeparator {{
            background-color: {c.divider};
        }}
        QTabWidget::pane {{
            background-color: {c.bg_primary};
            border: 1px solid {c.tab_border};
            border-radius: 6px;
            top: -1px;
        }}
        QTabBar::tab {{
            background-color: {c.bg_hover};
            color: {c.text_secondary};
            border: 1px solid {c.tab_border};
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
        QHeaderView::section {{
            background-color: {c.bg_secondary};
            color: {c.text_secondary};
            border: none;
            border-bottom: 1px solid {c.divider};
            padding: 6px 8px;
        }}
        QTableWidget, QTableView {{
            gridline-color: {c.gridline};
        }}
        QLineEdit {{
            background-color: {c.input_bg};
            color: {c.text_primary};
            border: 1px solid {c.input_border};
            border-radius: 6px;
            padding: 5px 8px;
        }}
        QLineEdit:focus {{
            border-color: {c.accent};
        }}
        QCheckBox {{
            color: {c.text_primary};
            spacing: 6px;
        }}
        QSpinBox, QDoubleSpinBox {{
            background-color: {c.input_bg};
            color: {c.text_primary};
            border: 1px solid {c.input_border};
            border-radius: 6px;
        }}
        QMessageBox {{
            background-color: {c.bg_secondary};
        }}
        QMessageBox QLabel {{
            color: {c.text_primary};
        }}
        """
        # 缓存生成的 QSS，下次切换同一主题时直接返回
        self._qss_cache[key_name] = qss
        return qss

    def export_theme(self, theme: Theme, path: Path) -> None:
        """导出主题为 JSON 文件（主题分享用）。"""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(theme.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("主题已导出: %s", path)

    def import_theme(self, path: Path) -> Theme | None:
        """导入外部主题 JSON 文件。"""
        try:
            with open(path, encoding="utf-8") as f:
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
