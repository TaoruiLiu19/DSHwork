"""余额查询双通道容错。

余额查询完全依赖 DSH 新增的 credentials.getBalance RPC 是高风险设计——
DSH 可能跳票或延迟支持该接口。因此采用双通道容错：

| 通道            | 方式                                  | 触发条件                     | 密钥处理                              |
|-----------------|---------------------------------------|------------------------------|---------------------------------------|
| 首选：DSH 代理  | 调用 credentials.getBalance RPC       | DSH 支持该接口               | DSH 持有 Key 明文，客户端只拿脱敏数字 |
| 降级：平台直连  | GET https://api.deepseek.com/user/balance | DSH 返回 404 或 MethodNotFound | 仅本次会话内存中临时读取用户输入的 Key，不落盘 |

状态栏明确标注余额来源——"来自 DSH"或"来自平台直连"——让用户知情。
降级通道的临时 Key 仅在内存中存活，会话结束即清除，绝不写入磁盘或日志。

两条通道都不经过版本适配器，使用独立的 HTTP 连接池。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from .. import constants as C
from ..utils.logger import get_logger
from .http_client import HttpClient, RpcError

log = get_logger("api.balance_client")


class BalanceSource(str, Enum):
    """余额来源。"""

    DSH_PROXY = "dsh"       # 首选通道：DSH 代理
    PLATFORM_DIRECT = "platform"  # 降级通道：平台直连
    UNAVAILABLE = "unavailable"   # 不可用


@dataclass
class BalanceResult:
    """余额查询结果。"""

    balance: float
    currency: str = "CNY"
    source: BalanceSource = BalanceSource.UNAVAILABLE
    is_available: bool = False
    error: str = ""
    queried_at: float = 0.0

    @property
    def source_label(self) -> str:
        """状态栏标注文案。"""
        if self.source == BalanceSource.DSH_PROXY:
            return "来自 DSH"
        if self.source == BalanceSource.PLATFORM_DIRECT:
            return "来自平台直连"
        return "不可用"


class BalanceClient:
    """余额查询客户端，双通道容错。

    使用方法：
        client = BalanceClient(dsh_http_client)
        result = client.query()          # 首选 DSH 代理
        if not result.is_available:
            result = client.query_direct(api_key)  # 降级直连
    """

    def __init__(self, dsh_http: HttpClient):
        # 首选通道复用 DSH HTTP 客户端（DSH loopback）
        self._dsh_http = dsh_http
        # 降级通道使用独立 HTTP 连接池，不经过版本适配器、不复用 DSH loopback 连接池
        self._direct_session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=C.HTTP_POOL_CONNECTIONS,
            pool_maxsize=C.HTTP_POOL_MAXSIZE,
        )
        self._direct_session.mount("https://", adapter)
        self._direct_session.mount("http://", adapter)

        # 缓存：定期刷新（每 5 分钟），不实时轮询
        self._cached: BalanceResult | None = None
        self._last_query_time: float = 0
        self._lock = threading.Lock()
        # 异步查询线程池：复用线程，避免每次都 new Thread()（降低线程创建开销）
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="balance-query")

        # 降级通道临时 Key：仅内存中存活，会话结束即清除
        self._temp_api_key: str | None = None

    def set_temp_api_key(self, key: str) -> None:
        """设置降级通道临时 API Key（仅内存，不落盘）。"""
        self._temp_api_key = key
        log.info("已设置降级通道临时 API Key（仅内存）")

    def clear_temp_api_key(self) -> None:
        """清除临时 API Key（会话结束时调用）。"""
        self._temp_api_key = None
        log.info("已清除降级通道临时 API Key")

    def query(self, force: bool = False) -> BalanceResult:
        """查询余额，首选 DSH 代理通道。

        Args:
            force: 强制刷新，忽略缓存

        Returns:
            BalanceResult，is_available=False 表示不可用
        """
        with self._lock:
            # 缓存检查：每 5 分钟刷新一次
            if not force and self._cached and self._cached.is_available:
                elapsed = time.time() - self._last_query_time
                if elapsed < C.BALANCE_REFRESH_INTERVAL_SEC:
                    return self._cached

            # 首选通道：DSH 代理
            result = self._query_via_dsh()
            if result.is_available:
                self._cached = result
                self._last_query_time = time.time()
                return result

            # 降级通道：平台直连（需要临时 Key）
            if self._temp_api_key:
                result = self._query_via_platform(self._temp_api_key)
                if result.is_available:
                    self._cached = result
                    self._last_query_time = time.time()
                    return result

            # 两通道都不可用
            unavailable = BalanceResult(
                balance=0.0,
                source=BalanceSource.UNAVAILABLE,
                is_available=False,
                error=result.error or "两通道均不可用",
                queried_at=time.time(),
            )
            self._cached = unavailable
            return unavailable

    def _query_via_dsh(self) -> BalanceResult:
        """首选通道：调用 credentials.getBalance RPC。"""
        try:
            result = self._dsh_http.call("credentials.getBalance")
            if not isinstance(result, dict):
                return BalanceResult(
                    balance=0.0,
                    source=BalanceSource.UNAVAILABLE,
                    is_available=False,
                    error="DSH 返回格式异常",
                    queried_at=time.time(),
                )
            # DSH 持有 Key 明文，客户端只拿脱敏数字
            balance = float(result.get("balance", 0))
            currency = result.get("currency", "CNY")
            log.info("余额查询成功（DSH 代理）: %.2f %s", balance, currency)
            return BalanceResult(
                balance=balance,
                currency=currency,
                source=BalanceSource.DSH_PROXY,
                is_available=True,
                queried_at=time.time(),
            )
        except RpcError as e:
            # DSH 返回 404 或 MethodNotFound 时降级
            log.info("DSH 代理通道不可用，将尝试降级: %s", e)
            return BalanceResult(
                balance=0.0,
                source=BalanceSource.UNAVAILABLE,
                is_available=False,
                error=str(e),
                queried_at=time.time(),
            )
        except Exception as e:
            log.warning("DSH 代理余额查询异常: %s", e)
            return BalanceResult(
                balance=0.0,
                source=BalanceSource.UNAVAILABLE,
                is_available=False,
                error=str(e),
                queried_at=time.time(),
            )

    def _query_via_platform(self, api_key: str) -> BalanceResult:
        """降级通道：直连 DeepSeek 平台 API。

        GET https://api.deepseek.com/user/balance
        临时 Key 仅在内存中存活，会话结束即清除，绝不写入磁盘或日志。
        """
        url = f"{C.DEEPSEEK_API_BASE}{C.DEEPSEEK_BALANCE_PATH}"
        try:
            resp = self._direct_session.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=C.HTTP_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()
            # 平台 API 返回格式适配
            balance = float(data.get("balance") or data.get("amount", 0))
            currency = data.get("currency", "CNY")
            log.info("余额查询成功（平台直连）: %.2f %s", balance, currency)
            return BalanceResult(
                balance=balance,
                currency=currency,
                source=BalanceSource.PLATFORM_DIRECT,
                is_available=True,
                queried_at=time.time(),
            )
        except requests.RequestException as e:
            log.warning("平台直连余额查询失败: %s", e)
            return BalanceResult(
                balance=0.0,
                source=BalanceSource.UNAVAILABLE,
                is_available=False,
                error=f"平台直连失败: {e}",
                queried_at=time.time(),
            )
        except (ValueError, KeyError) as e:
            log.warning("平台直连响应解析失败: %s", e)
            return BalanceResult(
                balance=0.0,
                source=BalanceSource.UNAVAILABLE,
                is_available=False,
                error=f"响应解析失败: {e}",
                queried_at=time.time(),
            )

    def query_async(self, callback: Callable[[BalanceResult], None], force: bool = False) -> None:
        """异步查询余额，结果通过回调返回（避免阻塞 UI）。"""
        def _worker():
            try:
                result = self.query(force=force)
            except Exception as e:  # 兜底：回调异常不导致线程池崩溃
                log.warning("异步余额查询异常: %s", e)
                result = BalanceResult(
                    balance=0.0,
                    source=BalanceSource.UNAVAILABLE,
                    is_available=False,
                    error=str(e),
                    queried_at=time.time(),
                )
            try:
                callback(result)
            except Exception as e:
                log.warning("余额查询回调异常: %s", e)

        self._executor.submit(_worker)

    @property
    def cached(self) -> BalanceResult | None:
        return self._cached

    def close(self) -> None:
        """关闭客户端，清除临时 Key。"""
        self.clear_temp_api_key()
        self._executor.shutdown(wait=False)
        self._direct_session.close()
        log.info("余额客户端已关闭")
