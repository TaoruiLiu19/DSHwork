"""DSH 通信层 (API Adapter)。

封装所有 HTTP/WebSocket 细节，向上暴露纯 Python 接口。
三层架构的最底层，直接与 DSH loopback 和外部 API 交互。

组件：
- http_client: HTTP JSON-RPC 客户端 (requests)
- ws_client: WebSocket 客户端 (websockets)，维护两条连接（全局 + 当前会话）
- version_adapter: 版本适配器，裸协议探测 + 自愈机制
- adapter_cache: 探测结果缓存 (~/.dsh-work/.adapter_cache.json)
- balance_client: 余额查询双通道容错
- reconnect: 断线重连与增量恢复
"""

from .http_client import HttpClient, RpcError
from .ws_client import WebSocketClient, WSMessage, WSEventType
from .version_adapter import VersionAdapter, AdapterProbeResult, CompatibilityMode
from .adapter_cache import AdapterCache
from .balance_client import BalanceClient, BalanceResult, BalanceSource
from .reconnect import ReconnectManager, SessionEventBuffer
from .dsh_service import DshService, SessionInfo, ModelInfo, MessageRecord

__all__ = [
    "HttpClient",
    "RpcError",
    "WebSocketClient",
    "WSMessage",
    "WSEventType",
    "VersionAdapter",
    "AdapterProbeResult",
    "CompatibilityMode",
    "AdapterCache",
    "BalanceClient",
    "BalanceResult",
    "BalanceSource",
    "ReconnectManager",
    "SessionEventBuffer",
    "DshService",
    "SessionInfo",
    "ModelInfo",
    "MessageRecord",
]
