"""断线重连与增量恢复。

成对重连只是第一步，重连后如何恢复状态决定了会不会丢事件或闪旧数据。
改为增量恢复：

1. 客户端在通信层为每条入站 WS 消息打上本地自增序号 local_seq 并缓存，
   同时记录该会话最后一条事件的 last_received_event_id（若 DSH 提供）
   与到达时间戳 since_timestamp。
2. 双 WS 重建成功后，不立即全量拉取，而是发送
   session.history(session_id, since=last_received_event_id)，
   仅补齐断线期间的增量事件。
3. DSH 事件流不一定自带递增 ID——当 since 不被支持时，回退为按
   since_timestamp 拉取，再由客户端按时间戳 + 内容哈希对本地缓存去重，
   防止断线前已渲染的消息被重复插入。
4. 去重发生在业务逻辑层而非 UI 层：UI 永远只收到去重后的追加事件，
   避免重连瞬间界面闪动。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..utils.logger import get_logger
from .ws_client import WSMessage

log = get_logger("api.reconnect")


@dataclass
class SessionEventBuffer:
    """会话事件缓冲区。

    缓存断线前的消息，重连后用于增量恢复与去重。
    去重发生在业务逻辑层而非 UI 层。
    """

    session_id: str
    # 本地自增序号 → 消息
    messages: list[WSMessage] = field(default_factory=list)
    # 内容哈希集合，用于去重
    _content_hashes: set[str] = field(default_factory=set)
    # 最后一条 DSH 事件 ID（若提供）
    last_received_event_id: str | None = None
    # 最后一条消息到达时间戳
    since_timestamp: float = 0.0
    # 最大缓存条数（防止内存膨胀）
    max_size: int = 500

    def append(self, msg: WSMessage) -> bool:
        """追加消息，返回是否为新增（True）或重复（False）。

        去重逻辑：按内容哈希判断。
        """
        content_hash = self._compute_hash(msg)
        if content_hash in self._content_hashes:
            log.debug("去重：跳过重复消息 seq=%d", msg.local_seq)
            return False

        self.messages.append(msg)
        self._content_hashes.add(content_hash)

        # 更新游标
        if msg.event_id:
            self.last_received_event_id = msg.event_id
        self.since_timestamp = msg.timestamp

        # LRU 淘汰
        if len(self.messages) > self.max_size:
            old = self.messages.pop(0)
            old_hash = self._compute_hash(old)
            self._content_hashes.discard(old_hash)

        return True

    def clear(self) -> None:
        """清空缓冲区（切换会话时调用）。"""
        self.messages.clear()
        self._content_hashes.clear()
        self.last_received_event_id = None
        self.since_timestamp = 0.0

    @staticmethod
    def _compute_hash(msg: WSMessage) -> str:
        """计算消息内容哈希（时间戳 + 内容），用于去重。"""
        # 使用 event_type + data 的 JSON 字符串 + 时间戳（秒级精度）
        import json
        content = json.dumps(
            {"type": msg.event_type.value, "data": msg.data},
            ensure_ascii=False,
            sort_keys=True,
        )
        # 时间戳取秒级精度，避免毫秒差异导致去重失败
        ts = int(msg.timestamp)
        return hashlib.md5(f"{ts}:{content}".encode("utf-8")).hexdigest()


class ReconnectManager:
    """断线重连管理器。

    协调 WebSocketClient 的重连与 SessionManager 的增量恢复。
    """

    def __init__(self):
        # session_id → 事件缓冲区
        self._buffers: dict[str, SessionEventBuffer] = {}

    def get_buffer(self, session_id: str) -> SessionEventBuffer:
        """获取或创建会话缓冲区。"""
        if session_id not in self._buffers:
            self._buffers[session_id] = SessionEventBuffer(session_id=session_id)
        return self._buffers[session_id]

    def on_message(self, msg: WSMessage) -> bool:
        """消息到达时调用，返回是否应分发给 UI（去重后）。"""
        if not msg.session_id:
            return True  # 全局事件不过滤
        buf = self.get_buffer(msg.session_id)
        return buf.append(msg)

    def on_reconnect(self, session_id: str) -> dict[str, Any]:
        """重连成功后，返回增量恢复参数。

        业务层用这些参数调用 session.history(session_id, since=...) 补齐断线期间事件。
        """
        buf = self._buffers.get(session_id)
        if not buf:
            return {"session_id": session_id}

        params: dict[str, Any] = {"session_id": session_id}
        if buf.last_received_event_id:
            params["since"] = buf.last_received_event_id
        elif buf.since_timestamp > 0:
            params["since_timestamp"] = buf.since_timestamp
        log.info(
            "重连增量恢复: session=%s since=%s timestamp=%s",
            session_id, buf.last_received_event_id, buf.since_timestamp,
        )
        return params

    def clear_session(self, session_id: str) -> None:
        """清空指定会话的缓冲区。"""
        if session_id in self._buffers:
            self._buffers[session_id].clear()

    def clear_all(self) -> None:
        """清空所有缓冲区。"""
        for buf in self._buffers.values():
            buf.clear()
        self._buffers.clear()
