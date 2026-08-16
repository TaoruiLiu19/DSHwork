r"""跨平台路径助手。

提供用户数据目录、缓存目录、配置目录等统一入口，供各模块使用。
Windows 下走 %APPDATA%\DSHWork；macOS ~/Library/Application Support/DSHWork；
Linux ~/.local/share/DSHWork。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_APP_NAME = "DSHWork"


class Paths:
    """静态路径集合。"""

    @staticmethod
    def app_data_root() -> Path:
        """跨平台用户数据根目录（不存在则创建）。"""
        if sys.platform.startswith("win"):
            base = os.environ.get("APPDATA")
            if base:
                root = Path(base) / _APP_NAME
            else:
                root = Path.home() / "AppData" / "Roaming" / _APP_NAME
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support" / _APP_NAME
        else:
            base = os.environ.get("XDG_DATA_HOME")
            if base:
                root = Path(base) / _APP_NAME
            else:
                root = Path.home() / ".local" / "share" / _APP_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def user_data() -> Path:
        """主用户数据目录（写权限稳定；会话级、全局级数据都放下面子目录）。"""
        return Paths.app_data_root()

    @staticmethod
    def config_dir() -> Path:
        """配置文件目录。"""
        if sys.platform.startswith("linux"):
            base = os.environ.get("XDG_CONFIG_HOME")
            p = Path(base) / _APP_NAME if base else Path.home() / ".config" / _APP_NAME
        else:
            p = Paths.app_data_root() / "config"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def cache_dir() -> Path:
        """缓存目录（可安全清理）。"""
        if sys.platform.startswith("linux"):
            base = os.environ.get("XDG_CACHE_HOME")
            p = Path(base) / _APP_NAME if base else Path.home() / ".cache" / _APP_NAME
        else:
            p = Paths.app_data_root() / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def log_dir() -> Path:
        p = Paths.app_data_root() / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def update_dir() -> Path:
        """客户端/官方 dsh 自更新包暂存目录。"""
        p = Paths.cache_dir() / "updates"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def migrations_dir() -> Path:
        """一键迁移临时目录（Codex / Claude Code → DSH Work）。"""
        p = Paths.cache_dir() / "migrations"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def port_preview_dir() -> Path:
        """端口预览用临时 HTML / 映射缓存目录。"""
        p = Paths.cache_dir() / "port-preview"
        p.mkdir(parents=True, exist_ok=True)
        return p
