"""用户配置与路径管理。

所有用户数据存放于 ~/.dsh-work/ 目录下：
- config.json         用户配置（模式、主题、工作区、面板宽度等）
- themes/             自定义主题 JSON
- logs/               按天轮转日志
- .adapter_cache.json 版本适配器探测缓存
- .dsh.pid            DSH 进程 PID 文件锁
- offline_cache.db    离线模式 SQLite 缓存
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import constants as C


def get_app_data_dir() -> Path:
    """返回应用数据根目录 ~/.dsh-work/，不存在则创建。"""
    home = Path.home()
    app_dir = home / ".dsh-work"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_themes_dir() -> Path:
    d = get_app_data_dir() / C.THEMES_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_logs_dir() -> Path:
    """返回日志目录（项目根目录下的 logs/ 文件夹），不存在则创建。

    日志保存在项目文件夹中，方便后续其他人遇到问题时直接查看日志排查。
    """
    proj_root = Path(__file__).resolve().parent.parent  # dsh_work/ 的父目录 = 项目根目录
    d = proj_root / C.LOG_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_adapter_cache_path() -> Path:
    return get_app_data_dir() / C.ADAPTER_CACHE_FILENAME


def get_pid_file_path() -> Path:
    """DSH 进程 PID 文件路径（系统临时目录）。"""
    import tempfile

    return Path(tempfile.gettempdir()) / C.DSH_PID_FILENAME


def get_offline_db_path() -> Path:
    return get_app_data_dir() / C.OFFLINE_DB_FILENAME


def get_runtime_dir() -> Path:
    """便携运行时目录 ~/.dsh-work/runtime/（便携 Node + 本地 dsh，P3-2）。"""
    d = get_app_data_dir() / C.RUNTIME_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_user_config_path() -> Path:
    return get_app_data_dir() / C.USER_CONFIG_FILENAME


def get_builtin_themes_dir() -> Path:
    """打包内置主题目录（resources/themes/）。

    PyInstaller 打包后通过 sys._MEIPASS 解析（onedir 指向 _internal/，
    onefile 指向临时解压目录），datas 保留 dsh_work/resources/themes 相对结构；
    源码运行时回退用 __file__ 相对路径。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "dsh_work" / "resources" / "themes"
    return Path(__file__).parent / "resources" / "themes"


@dataclass
class UserConfig:
    """用户持久化配置。

    模式选择、主题、工作区、面板宽度等持久化到此，下次启动自动恢复。
    """

    # 模式：work / code
    mode: str = C.MODE_WORK
    # 当前主题名
    theme: str = C.DEFAULT_THEME
    # 工作区路径
    workspace: str = ""
    # 三栏宽度比例（左、中、右），归一化后存储
    panel_ratios: dict[str, float] = field(
        default_factory=lambda: {"left": 0.18, "center": 0.58, "right": 0.24}
    )
    # 面板折叠状态
    panel_collapsed: dict[str, bool] = field(
        default_factory=lambda: {"left": False, "right": False}
    )
    # 最近使用的模型
    last_model: str = ""
    # 最近使用的 Agent Preset
    last_preset: str = ""
    # 系统托盘：关闭时最小化到托盘
    minimize_to_tray: bool = True
    # 迷你浮窗（默认关）
    mini_float_window: bool = False
    # 迷你浮窗位置（右下角停靠）持久化
    mini_float_pos: Any = None
    # 步骤切换系统通知（默认关）
    step_notification: bool = False
    # 余额来源标注
    balance_source_label: bool = True
    # 可读性自动保护（背景图不透明度 > 0.4 且未开磨砂玻璃时切不透明气泡）
    readability_protection: bool = True
    # 自定义 DSH 端点（兼容降级模式救急用，空字符串表示用默认）
    custom_dsh_endpoint: str = ""
    # 自动检查更新（启动后后台拉取最新版本，有新版弹窗提示，P3-4）
    check_updates: bool = True
    # 用户已跳过的更新版本（点击"暂不更新"后记录，不再弹窗打扰）
    skip_update_version: str = ""
    # 快捷键自定义（预留，首版使用默认）
    shortcuts: dict[str, str] = field(default_factory=dict)
    # 首次启动标记
    first_run: bool = True
    # 上次使用的场景（首次启动引导用）
    last_scenario: str = ""

    # ===== 外置视觉模型（inspect_image 工具）=====
    # OpenAI 兼容视觉端点（支持 qwen-vl / GLM-4V / Ollama / 任意 OpenAI Chat Completions 兼容服务）
    # 示例：
    #   通义千问 VL: https://dashscope.aliyuncs.com/compatible-mode/v1
    #   智谱 GLM-4V: https://open.bigmodel.cn/api/paas/v4
    #   Ollama (本地): http://127.0.0.1:11434/v1
    vision_api_base: str = ""
    # 视觉端点 API Key（Ollama 本地可留空）
    vision_api_key: str = ""
    # 使用的视觉模型名（如 qwen-vl-max / glm-4v-flash / llava:7b）
    vision_model: str = ""
    # 单张图片最大分辨率（长边像素，超出时客户端侧下采样后再上传，省 token）
    vision_max_image_size: int = 1024
    # 视觉工具默认提示词（留空时使用参数传入的 prompt）
    vision_default_prompt: str = "请详细描述这张图片的内容，包括文字、物体、场景、颜色等所有可见信息。"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserConfig:
        cfg = cls()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        path = get_user_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls) -> UserConfig:
        path = get_user_config_path()
        if not path.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            cfg = cls.from_dict(data)
            # v0.4 迁移：旧内置主题名（midnight_ocean/daylight/qinghua/forest_green）
            # → Web 版深浅主题，避免配置指向已移除的主题。
            legacy = {
                "midnight_ocean": C.DEFAULT_THEME,
                "forest_green": C.DEFAULT_THEME,
                "qinghua": C.DEFAULT_THEME,
                "daylight": "web_light",
            }
            if cfg.theme in legacy and legacy[cfg.theme] != cfg.theme:
                cfg.theme = legacy[cfg.theme]
                try:
                    cfg.save()
                except OSError:
                    pass
            return cfg
        except (json.JSONDecodeError, OSError):
            # 配置损坏时回退默认，不崩溃
            cfg = cls()
            return cfg


def ensure_dirs() -> None:
    """启动时确保所有必要目录存在。"""
    get_app_data_dir()
    get_themes_dir()
    get_logs_dir()


def get_dsh_base_url() -> str:
    """获取 DSH base URL，支持用户自定义端点（降级救急）。"""
    cfg = UserConfig.load()
    if cfg.custom_dsh_endpoint:
        return cfg.custom_dsh_endpoint.rstrip("/")
    return C.DSH_BASE_URL


def get_dsh_ws_url() -> str:
    cfg = UserConfig.load()
    if cfg.custom_dsh_endpoint:
        ep = cfg.custom_dsh_endpoint.rstrip("/")
        if ep.startswith("https://"):
            return ep.replace("https://", "wss://", 1)
        if ep.startswith("http://"):
            return ep.replace("http://", "ws://", 1)
        return ep
    return C.DSH_WS_URL


def get_dsh_home_dir() -> Path:
    """DSH CLI 配置/数据根目录（通常 ~/.dsh）。

    优先级：
      1. 环境变量 DSH_HOME（对方 JS 版同样支持，允许便携版/CLI 共享配置）
      2. 默认 ~/.dsh
    目录不存在也不自动创建，只用于读取（如 sessions/ 日志）。
    """
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".dsh"


def get_dsh_sessions_dir() -> Path:
    """DSH 会话持久化日志目录 ~/.dsh/sessions/（供 SessionWatcher 扫描）。

    SessionWatcher 自身会处理目录不存在的情况，这里只返回路径。
    """
    return get_dsh_home_dir() / "sessions"
