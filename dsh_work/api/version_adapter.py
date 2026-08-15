"""版本适配器与自愈机制（适配 DSH 0.1.0-rc.6 Typert RPC）。

核心设计：在通信层内部实现版本适配器。不依赖固定 URL GET 探测，
而是直接用 Typert RPC 调用 host.describe 等方法拿到 version 字段。

探测过程记录三个信息：
- method: 成功响应的 RPC 方法名（如 host.describe）
- version: 解析到的版本号
- capabilities: 响应中声明的能力列表 / 或主机能力键（canOpenPath 等）

若探测全部失败，进入"兼容降级模式"：仅保留纯文本对话的 HTTP 请求，
关闭 WebSocket 流式与工具调用可视化，确保最基本的聊天功能可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .. import constants as C
from ..utils.logger import get_logger
from .adapter_cache import AdapterCache
from .http_client import HttpClient, RpcError

log = get_logger("api.version_adapter")


class CompatibilityMode(Enum):
    """兼容模式。"""

    FULL = "full"            # 完整功能，所有 RPC 可用
    DEGRADED = "degraded"    # 兼容降级模式，仅纯文本对话
    OFFLINE = "offline"      # DSH 不可连接，离线模式


@dataclass
class AdapterProbeResult:
    """探测结果。"""

    success: bool
    api_path: str = ""         # 保留字段，意义为"成功探测到的 RPC 方法名"
    version: str = ""
    capabilities: list[str] = field(default_factory=list)
    mode: CompatibilityMode = CompatibilityMode.OFFLINE
    error: str = ""


class VersionAdapter:
    """版本适配器：Typert RPC host.describe 探测 + 缓存 + 自愈。

    使用方法：
        adapter = VersionAdapter(http_client)
        result = adapter.probe()
        if result.success:
            ...
    """

    def __init__(self, http_client: HttpClient, cache: AdapterCache | None = None):
        self.http = http_client
        self.cache = cache or AdapterCache()
        self._result: AdapterProbeResult | None = None

    @property
    def result(self) -> AdapterProbeResult | None:
        return self._result

    @property
    def mode(self) -> CompatibilityMode:
        return self._result.mode if self._result else CompatibilityMode.OFFLINE

    @property
    def version(self) -> str:
        return self._result.version if self._result else "unknown"

    @property
    def api_path(self) -> str:
        """成功探测到的 RPC 方法名（如 host.describe），兼容旧字段命名。"""
        return self._result.api_path if self._result else ""

    @property
    def capabilities(self) -> list[str]:
        return self._result.capabilities if self._result else []

    def has_capability(self, name: str) -> bool:
        """检查 DSH 是否声明了某项能力。"""
        return name in self.capabilities

    def probe(self, use_cache: bool = True) -> AdapterProbeResult:
        """执行版本探测。

        优先读缓存，缓存命中且验证成功则跳过完整探测。
        缓存失效时触发完整探测并更新缓存。
        """
        # Step 1: 尝试缓存
        if use_cache:
            cached = self.cache.load()
            if cached:
                result = self._verify_cached(cached)
                if result.success:
                    self._result = result
                    log.info(
                        "适配器探测成功（缓存命中）: version=%s rpc=%s",
                        result.version, result.api_path,
                    )
                    return result
                else:
                    log.info("适配器缓存失效，触发完整探测")
                    self.cache.invalidate()

        # Step 2: 完整 RPC 探测
        result = self._full_probe()
        if result.success:
            self.cache.save(
                api_path=result.api_path,  # 存为 method 名
                version=result.version,
                capabilities=result.capabilities,
            )
        self._result = result
        return result

    def _verify_cached(self, cached: dict) -> AdapterProbeResult:
        """用缓存的 method（旧叫 api_path）发一次 RPC 验证缓存有效性。"""
        method = cached.get("api_path", "") or "host.describe"
        try:
            data = self.http.call(method, {})
            if not isinstance(data, dict):
                return AdapterProbeResult(
                    success=False, mode=CompatibilityMode.DEGRADED, error="缓存验证响应非 dict"
                )
            version = data.get("version") or cached.get("version", "")
            if not version:
                return AdapterProbeResult(
                    success=False, mode=CompatibilityMode.DEGRADED, error="缓存验证响应无 version 字段"
                )
            caps = cached.get("capabilities", [])
            if not caps and isinstance(data, dict):
                caps = self._extract_capabilities(data)
            return AdapterProbeResult(
                success=True,
                api_path=method,
                version=str(version),
                capabilities=list(caps),
                mode=CompatibilityMode.FULL,
            )
        except RpcError as e:
            return AdapterProbeResult(
                success=False, mode=CompatibilityMode.DEGRADED, error=str(e)
            )

    def _full_probe(self) -> AdapterProbeResult:
        """按候选 RPC 方法逐个尝试，取最先返回 dict 且含 version 字段的。"""
        for method in C.ADAPTER_PROBE_METHODS:
            try:
                data = self.http.call(method, {})
                if not isinstance(data, dict):
                    continue
                version = data.get("version")
                if not version:
                    # settings.describe / credentials.describe 可能不带 version
                    continue
                caps = self._extract_capabilities(data)
                log.info(
                    "适配器探测成功（完整探测）: method=%s version=%s capabilities=%d",
                    method, version, len(caps),
                )
                return AdapterProbeResult(
                    success=True,
                    api_path=method,
                    version=str(version),
                    capabilities=caps,
                    mode=CompatibilityMode.FULL,
                )
            except RpcError as e:
                # 单个方法失败，继续尝试下一个（但不是"连接失败"类错误才继续；
                # 连接失败类错误直接抛到外层 _is_dsh_reachable 再判断）
                if "连接" in str(e) or "connection" in str(e).lower() or "timeout" in str(e).lower():
                    log.debug("探测方法 %s 疑似连接异常: %s", method, e)
                    break
                log.debug("探测方法 %s 不合法: %s", method, e)
                continue
            except Exception as e:
                log.debug("探测方法 %s 异常: %s", method, e)
                continue

        # 所有路径探测失败——检查是否完全不可连接
        if not self._is_dsh_reachable():
            log.warning("DSH 不可连接，进入离线模式")
            return AdapterProbeResult(
                success=False,
                mode=CompatibilityMode.OFFLINE,
                error="DSH 进程不可连接",
            )

        log.warning("DSH 可连接但所有探测方法失败，进入兼容降级模式")
        return AdapterProbeResult(
            success=False,
            mode=CompatibilityMode.DEGRADED,
            error="所有探测方法均未返回合法 version 字段",
        )

    @staticmethod
    def _extract_capabilities(value: dict[str, Any]) -> list[str]:
        """从 host.describe.value / 等响应中提取能力字符串列表。"""
        caps: list[str] = []
        if not isinstance(value, dict):
            return caps
        # canOpenPath / pickDirectory / ... 这类 bool key
        for key, val in value.items():
            if isinstance(val, bool) and val is True:
                caps.append(key)
        # 已有 capabilities 数组（未来版本可能提供）
        existing = value.get("capabilities")
        if isinstance(existing, list):
            for c in existing:
                if isinstance(c, str) and c not in caps:
                    caps.append(c)
        return caps

    def _is_dsh_reachable(self) -> bool:
        """检查 DSH loopback 是否可连接（TCP 探测 + 轻量 HTTP 请求）。"""
        import socket

        try:
            with socket.create_connection(
                (C.DSH_DEFAULT_HOST, C.DSH_DEFAULT_PORT), timeout=2
            ):
                pass
        except OSError:
            return False

        # 端口通了，但还要确认是 DSH（返回 HTML 带 __DSH_BOOT__ 或对 /api 有非 5xx 响应）
        try:
            r = self.http.raw_get(C.DSH_BASE_URL + "/api")
            if r.status_code in (404, 405, 415, 200):
                return True
            if 400 <= r.status_code < 500:
                # 任何客户端错误意味着服务器端存在且响应
                return True
        except Exception:
            pass
        return True  # 端口已开就认为可达（至少是未知服务）
