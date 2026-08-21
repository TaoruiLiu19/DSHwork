"""版本适配器探测结果缓存。

首次探测成功后写入 ~/.dsh-work/.adapter_cache.json：
{
  "api_path": "/api/host.describe",
  "version": "0.4.2",
  "capabilities": ["session.create", "session.history", ...],
  "cached_at": "2026-08-14T10:30:00Z"
}

下次启动先读缓存，直接用缓存的 api_path 发送请求。
若缓存命中且请求成功，跳过完整探测，启动速度提升 1-3 秒。
若缓存失效（连接失败或返回格式不匹配），触发完整探测并更新缓存。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import get_adapter_cache_path
from ..utils.logger import get_logger

log = get_logger("api.adapter_cache")


class AdapterCache:
    """适配器探测结果缓存。"""

    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or get_adapter_cache_path()

    def load(self) -> dict[str, Any] | None:
        """读取缓存，不存在或损坏返回 None。"""
        if not self.cache_path.exists():
            return None
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            # 必须包含 api_path 和 version 才视为有效
            if "api_path" not in data or "version" not in data:
                return None
            log.debug("读取适配器缓存: api_path=%s version=%s", data["api_path"], data["version"])
            return data
        except (json.JSONDecodeError, OSError) as e:
            log.debug("适配器缓存损坏，将重新探测: %s", e)
            return None

    def save(
        self,
        api_path: str,
        version: str,
        capabilities: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """写入缓存。"""
        data = {
            "api_path": api_path,
            "version": version,
            "capabilities": capabilities or [],
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            data.update(extra)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            log.info("适配器缓存已写入: api_path=%s version=%s", api_path, version)
        except OSError as e:
            log.warning("写入适配器缓存失败: %s", e)

    def invalidate(self) -> None:
        """删除缓存（探测失效时调用）。"""
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
                log.info("适配器缓存已失效删除")
        except OSError as e:
            log.warning("删除适配器缓存失败: %s", e)

    @property
    def exists(self) -> bool:
        return self.cache_path.exists()
