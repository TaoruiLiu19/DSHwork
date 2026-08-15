"""DSH Typert RPC HTTP 客户端。

DSH 0.1.0-rc.6 使用自研 Typert RPC（不是 JSON-RPC 2.0）：
  调用:  POST http://127.0.0.1:3080/api/<method.name>
         Body: {type:"client-request", rpcId:"<uuid>", method:"<method.name>", payload:{...}}
         Content-Type: application/json
         Origin: http://127.0.0.1:3080  （Host 信任围栏）
  响应:  {type:"server-response", rpcId:"<same>", result:{ok:true, value:<业务值>}}
      或 {type:"server-response", rpcId:"<same>", result:{ok:false, error:{code,message,details}}}

HTTP 状态码仅表达载体层：404=未知方法、415=非 JSON、400=非 JSON body、500=handler crash；
业务错误永远是 HTTP 200，通过 result.ok 区分。

封装的核心 RPC 方法：
  session.create / session.list / session.history / session.selectModel /
  session.models / agentPreset.read / host.describe / credentials.describe /
  credentials.getBalance 等。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from .. import constants as C
from ..utils.logger import get_logger

log = get_logger("api.http_client")


class RpcError(Exception):
    """Typert RPC 业务 / 载体调用异常。"""

    def __init__(self, message: str, code: str = "internal", data: Any = None):
        super().__init__(message)
        self.code = code  # 字符串：bad-request / session-not-found / internal ...
        self.data = data

    @property
    def http_code(self) -> int:
        """兼容旧代码里根据 int 码判断的分支。"""
        try:
            return int(self.code)
        except (TypeError, ValueError):
            # 稳定错误名 -> 粗略 HTTP 对应
            mapping = {
                "bad-request": 400,
                "cancelled": 499,
                "session-not-found": 404,
                "model-unavailable": 503,
                "session-conflict": 409,
                "workspace-not-found": 404,
                "directory-unreadable": 500,
                "agent-busy": 503,
                "internal": 500,
            }
            return mapping.get(self.code, 500)


@dataclass
class RpcResponse:
    """RPC 响应封装（保持与旧版兼容，result 字段 = Typert value）。"""

    result: Any
    id: str | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None


class HttpClient:
    """DSH Typert RPC HTTP 客户端。

    封装 requests.Session，使用独立连接池。
    所有 RPC 调用通过 call() 方法统一走 Typert client-request 信封。
    """

    def __init__(self, base_url: str | None = None, api_path: str = C.DSH_API_PREFIX):
        self.base_url = (base_url or C.DSH_BASE_URL).rstrip("/")
        self.api_prefix = api_path.rstrip("/")  # "/api"
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=C.HTTP_POOL_CONNECTIONS,
            pool_maxsize=C.HTTP_POOL_MAXSIZE,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._timeout = C.HTTP_TIMEOUT_SEC
        # Host 信任围栏：Origin 必须与 Host 一致。
        # 默认用常量；用户自定义 base_url 时从 base_url 推导，否则会被围栏拒绝。
        if base_url and base_url.rstrip("/") != C.DSH_BASE_URL:
            self._origin = self.base_url
        else:
            self._origin = C.DSH_ORIGIN_HEADER

    @property
    def endpoint_prefix(self) -> str:
        """URL 前缀：<base>/api"""
        return f"{self.base_url}{self.api_prefix}"

    def call(self, method: str, params: dict | list | None = None, request_id: str | None = None) -> Any:
        """发起 Typert RPC 调用。

        Args:
            method: RPC 方法名，点号分隔，如 "session.create" / "host.describe"
            params: 业务 payload（对象或数组，旧 API 叫 params；这里直接当 payload 发出）
            request_id: 指定 rpcId，不传则生成 UUID

        Returns:
            Typert result.value（业务成功值）。旧代码通常把它当作 session.create 的结果字典等。

        Raises:
            RpcError: 载体或业务错误。错误码是 DSH Typert 稳定字符串（如 bad-request / internal）。
        """
        rpc_id = request_id or str(uuid.uuid4())
        payload = params if params is not None else {}
        envelope = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": payload,
        }
        url = f"{self.endpoint_prefix}/{method}"
        log.debug("RPC → %s payload=%s", method, _truncate_params(payload))
        try:
            resp = self._session.post(
                url,
                json=envelope,
                timeout=self._timeout,
                headers={
                    "Content-Type": "application/json",
                    "Origin": self._origin,
                },
            )
            if resp.status_code == 404:
                # 未知方法名
                raise RpcError(f"未知 RPC 方法: {method}", code="method-not-found")
            resp.raise_for_status()
        except requests.ConnectionError as e:
            log.error("RPC 连接失败 %s: %s", method, e)
            raise RpcError(f"无法连接 DSH ({method}): {e}", code="connection-error") from e
        except requests.Timeout as e:
            log.error("RPC 超时 %s: %s", method, e)
            raise RpcError(f"DSH 响应超时 ({method})", code="timeout") from e
        except RpcError:
            raise
        except requests.HTTPError as e:
            log.error("RPC HTTP 错误 %s: %s", method, e)
            raise RpcError(f"DSH 返回 HTTP {resp.status_code} ({method})", code=str(resp.status_code)) from e

        # 解析 Typert server-response 信封
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise RpcError(f"DSH 响应非 JSON ({method}): {e}", code="bad-response") from e

        # 要求 type == "server-response"（宽松：只要 result 存在即可）
        result = data.get("result")
        if not isinstance(result, dict):
            raise RpcError(
                f"DSH 响应缺少 result 字段 ({method}): {json.dumps(data)[:200]}",
                code="bad-response",
            )

        if result.get("ok") is False:
            err = result.get("error") or {}
            code = err.get("code", "internal")
            msg = err.get("message", "未知错误")
            details = err.get("details", {})
            log.error("RPC 业务错误 %s: [%s] %s  details=%s", method, code, msg, _truncate_params(details))
            raise RpcError(msg, code=str(code), data=details)

        # 成功：返回 unwrapped value
        value = result.get("value")
        return value

    def get(self, path: str, params: dict | None = None) -> Any:
        """发起 GET 请求（余额直连用 / 非 RPC 资源；对 Typert RPC 请用 call()）。

        注意：DSH 0.1.0-rc.6 的 RPC 不支持 GET，全部走 POST /api/<method>。
        此方法仅用于外部 API（DeepSeek balance 直连）和缓存路径校验。
        """
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.base_url}{path}"
        log.debug("GET %s", url)
        try:
            resp = self._session.get(url, params=params, timeout=self._timeout,
                                      headers={"Origin": self._origin})
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError) as e:
            log.debug("GET %s 失败: %s", url, e)
            raise RpcError(f"GET {path} 失败: {e}", code="connection-error") from e

    def raw_get(self, url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response:
        """发起任意 URL 的 GET 请求（余额降级直连用）。"""
        merged_headers = {"Origin": self._origin}
        if headers:
            merged_headers.update(headers)
        return self._session.get(
            url, params=params, timeout=self._timeout, headers=merged_headers
        )

    def close(self) -> None:
        self._session.close()


def _truncate_params(params: Any, limit: int = 200) -> str:
    """日志中截断过长的 params，避免刷屏。"""
    s = repr(params)
    return s if len(s) <= limit else s[:limit] + "..."
