"""WebSocket 客户端（DSH 0.1.0-rc.6 Typert 协议版）。

DSH 通过两条 WebSocket 连接推送事件：
  ws://host:port/api/events.host —— 主机事件（全局状态变化、广播）
  ws://host:port/api/events.mux  —— 多路复用会话事件流（chunk / tool_call / tool_result / turn_end 等）

连接必须带 Origin: http://host:port 头（Host 信任围栏）。
旧版的 `/ws` 和 `/ws/session/<id>` 端点在 DSH 0.1.0-rc.6 中不存在。

断线重连与增量恢复详见 reconnect.py。
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, ClassVar
from collections.abc import Awaitable

from .. import constants as C
from ..utils.logger import get_logger

log = get_logger("api.ws_client")


class WSEventType(str, Enum):
    """WebSocket 事件类型。"""

    CHUNK = "chunk"                # 流式文本块
    TOOL_CALL = "tool_call"        # 工具调用开始
    TOOL_RESULT = "tool_result"    # 工具调用返回
    TURN_END = "turn_end"          # 一轮对话结束
    TURN_START = "turn_start"      # 一轮对话开始
    STEP_START = "step_start"      # 步骤开始（最小化进度追踪）
    STEP_END = "step_end"          # 步骤结束
    TOKEN_USAGE = "token_usage"    # token 统计
    SESSION_CREATED = "session_created"
    SESSION_DELETED = "session_deleted"
    STATUS = "status"              # 连接状态变化
    DONE = "done"                  # 完成标志
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class WSMessage:
    """WebSocket 入站消息封装。

    每条消息打上本地自增序号 local_seq 并缓存，用于断线重连后的增量恢复。
    """

    local_seq: int
    session_id: str
    event_type: WSEventType
    data: dict[str, Any]
    timestamp: float = field(default_factory=lambda: __import__("time").time())
    # DSH 事件流不一定自带递增 ID，若提供则记录
    event_id: str | None = None

    # DSH domain/action 类型 → 客户端 WSEventType 映射
    _DSH_TYPE_MAP: ClassVar[dict[str, str]] = {
        "assistant/chunk": "chunk",
        "turn/start": "turn_start",
        "turn/end": "turn_end",
        "step/start": "step_start",
        "step/end": "step_end",
        "session/created": "session_created",
        "session/deleted": "session_deleted",
    }

    @classmethod
    def from_raw(cls, raw: dict, local_seq: int) -> WSMessage:
        """从原始 JSON 解析消息。兼容多种帧格式：
        - 直接 {type, session_id, data, event_id}
        - Typert 框架 {payload: {type, ...}}
        - envelope {result: {value: {...}}}
        - DSH events.mux 帧 {type: "session/event", sessionId, event: {type, data}}，
          真实事件在 event 子对象里，类型为 domain/action（如 assistant/chunk）。
          外层 sessionId 优先保留，不要因为解包 event 而丢失。
        """
        # 先保留外层（未剥壳前）的 session 标识，DSH events.mux 结构为：
        #   {type: "session/event", sessionId: "...", event: {type, data}}
        # 外层 sessionId 是该事件真正归属的会话，event.data 里不一定有。
        outer_session_id = (
            raw.get("sessionId")
            or raw.get("session_id")
            or (
                raw.get("payload").get("sessionId")
                if isinstance(raw.get("payload"), dict)
                else None
            )
            or (
                raw.get("payload").get("session_id")
                if isinstance(raw.get("payload"), dict)
                else None
            )
        )

        body = raw
        if isinstance(body.get("payload"), dict):
            body = body["payload"]
        if isinstance(body.get("result"), dict) and isinstance(body["result"].get("value"), (dict, list)):
            val = body["result"]["value"]
            if isinstance(val, dict):
                body = val

        # DSH events.mux：解包 session/event 包裹，取 event.type 与 event.data
        wrapped_event = body.get("event")
        if isinstance(wrapped_event, dict):
            event_data = wrapped_event.get("data")
            if isinstance(event_data, dict):
                body = event_data
            event_type_str = str(wrapped_event.get("type") or body.get("type") or "unknown")
        else:
            event_type_str = str(
                body.get("type")
                or body.get("event")
                or body.get("eventType")
                or raw.get("type")
                or "unknown"
            )

        # 把 DSH 的 domain/action 类型映射到 WSEventType 已知值
        event_type_str = cls._DSH_TYPE_MAP.get(event_type_str, event_type_str)
        try:
            event_type = WSEventType(event_type_str)
        except ValueError:
            event_type = WSEventType.UNKNOWN
        return cls(
            local_seq=local_seq,
            session_id=str(
                # 外层 sessionId 优先级最高（来自 session/event 包裹）
                outer_session_id
                or body.get("session_id")
                or body.get("sessionId")
                or raw.get("session_id")
                or raw.get("sessionId")
                or ""
            ),
            event_type=event_type,
            data=body.get("data") if isinstance(body.get("data"), dict) else dict(body),
            event_id=(
                body.get("event_id")
                or body.get("id")
                or body.get("eventId")
                or raw.get("event_id")
                or raw.get("id")
            ),
        )


class WebSocketClient:
    """WebSocket 客户端，维护两条连接（events.host 全局 + events.mux 会话复用）。

    使用 asyncio + websockets 库，在独立线程中运行事件循环，
    通过回调向业务层推送消息。
    """

    def __init__(self, ws_base_url: str | None = None, origin: str | None = None):
        self.ws_base_url = (ws_base_url or C.DSH_WS_URL).rstrip("/")
        self._host_ws: Any = None     # /api/events.host
        self._mux_ws: Any = None      # /api/events.mux
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._local_seq = 0
        self._current_session_id: str | None = None

        # 回调：业务层注册的消息处理器
        # 签名: (msg: WSMessage) -> None
        self._handlers: list[Callable[[WSMessage], None]] = []

        # 断线重连状态
        self._reconnect_attempts = 0
        self._last_received_event_id: str | None = None
        self._since_timestamp: float | None = None
        # 重连成功回调：业务层注册后用于触发增量恢复（签名: () -> None）
        self.on_reconnected: Callable[[], None] | None = None
        # 是否已进入降级重连模式（达到 MAX_ATTEMPTS 后转为长间隔重试）
        self._degraded_mode = False

        # Origin 头（Host 信任围栏必需）：跟随 ws_base_url，否则自定义端点会被拒绝
        if origin:
            self._origin = origin
        elif ws_base_url and ws_base_url.rstrip("/") != C.DSH_WS_URL:
            # 从 ws:// / wss:// 反推 http:// / https:// origin
            self._origin = ws_base_url.replace("ws://", "http://").replace("wss://", "https://").rstrip("/")
        else:
            self._origin = C.DSH_ORIGIN_HEADER

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_session_id(self) -> str | None:
        return self._current_session_id

    def add_handler(self, handler: Callable[[WSMessage], None]) -> None:
        """注册消息处理器。"""
        self._handlers.append(handler)

    def remove_handler(self, handler: Callable[[WSMessage], None]) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    def start(self) -> None:
        """启动 WebSocket 客户端（在独立线程运行事件循环）。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ws-client")
        self._thread.start()
        log.info("WebSocket 客户端已启动")

    def stop(self) -> None:
        """停止 WebSocket 客户端。"""
        self._running = False
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_all(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
        log.info("WebSocket 客户端已停止")

    async def _close_all(self) -> None:
        if self._host_ws:
            try:
                await self._host_ws.close()
            except Exception:
                pass
            self._host_ws = None
        if self._mux_ws:
            try:
                await self._mux_ws.close()
            except Exception:
                pass
            self._mux_ws = None

    def _run_loop(self) -> None:
        """事件循环线程入口。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main_loop())
        except Exception as e:
            log.error("WebSocket 事件循环异常: %s", e)
        finally:
            self._loop.close()

    async def _main_loop(self) -> None:
        """主循环：维护两条连接，处理重连。"""
        while self._running:
            try:
                await self._connect_host()
                await self._connect_mux()
                # 重连成功：重置计数器、退出降级模式、通知业务层做增量恢复
                was_reconnecting = self._reconnect_attempts > 0 or self._degraded_mode
                self._reconnect_attempts = 0
                if self._degraded_mode:
                    log.info("WebSocket 已从降级模式恢复正常")
                    self._degraded_mode = False
                log.info("两条 WebSocket 均已连接 (host + mux)")
                if was_reconnecting and self.on_reconnected:
                    try:
                        self.on_reconnected()
                    except Exception as e:
                        log.error("on_reconnected 回调异常: %s", e, exc_info=True)
                # 阻塞等待任一连接断开
                while self._running and self._host_ws and self._mux_ws:
                    await asyncio.sleep(1)
            except Exception as e:
                log.warning("WebSocket 连接异常: %s", e)
                if self._running:
                    await self._reconnect()

    def _ws_kwargs(self) -> dict:
        """返回 connect 的公共参数：Origin 头、心跳、超时。"""
        return dict(
            additional_headers={"Origin": self._origin},
            ping_interval=C.WS_HEARTBEAT_INTERVAL_SEC,
            open_timeout=10,
            close_timeout=5,
            max_size=32 * 1024 * 1024,  # 32MB 可容纳大段代码/工具结果
        )

    async def _connect_host(self) -> None:
        """建立 events.host 全局 WebSocket 连接。"""
        import websockets

        url = f"{self.ws_base_url}{C.DSH_WS_HOST_EVENTS_PATH}"
        log.info("连接 Host WebSocket: %s", url)
        self._host_ws = await websockets.connect(url, **self._ws_kwargs())
        log.info("Host WebSocket 已连接")
        asyncio.create_task(self._receive_host())

    async def _connect_mux(self) -> None:
        """建立 events.mux 多路复用会话 WebSocket 连接。"""
        import websockets

        url = f"{self.ws_base_url}{C.DSH_WS_MUX_EVENTS_PATH}"
        log.info("连接 Mux WebSocket: %s", url)
        self._mux_ws = await websockets.connect(url, **self._ws_kwargs())
        log.info("Mux WebSocket 已连接")
        asyncio.create_task(self._receive_mux())

    async def _receive_host(self) -> None:
        """接收全局（events.host）事件。"""
        if not self._host_ws:
            return
        try:
            async for raw_msg in self._host_ws:
                if not self._running:
                    break
                self._dispatch(raw_msg)
        except Exception as e:
            log.warning("Host WebSocket 接收中断: %s", e)
            self._host_ws = None
            # 关闭另一根，触发主循环统一重连
            if self._mux_ws:
                try:
                    await self._mux_ws.close()
                except Exception:
                    pass
                self._mux_ws = None

    async def _receive_mux(self) -> None:
        """接收会话事件流（events.mux，多路复用所有会话）。"""
        if not self._mux_ws:
            return
        try:
            async for raw_msg in self._mux_ws:
                if not self._running:
                    break
                # 分发前如果设置了当前会话，UI 侧会在渲染端按 session_id 过滤
                self._dispatch(raw_msg)
        except Exception as e:
            log.warning("Mux WebSocket 接收中断: %s", e)
            self._mux_ws = None
            if self._host_ws:
                try:
                    await self._host_ws.close()
                except Exception:
                    pass
                self._host_ws = None

    def _dispatch(self, raw_msg: str | bytes) -> None:
        """解析并分发消息到业务层。"""
        try:
            data = json.loads(raw_msg) if isinstance(raw_msg, (str, bytes, bytearray)) else raw_msg
            if isinstance(data, (str, bytes, bytearray)):
                # 还是字符串，双层编码
                data = json.loads(data)
        except (json.JSONDecodeError, TypeError) as e:
            log.debug("无法解析 WS 消息: %s  msg_head=%s", e, repr(str(raw_msg)[:80]))
            return

        self._local_seq += 1
        msg = WSMessage.from_raw(data if isinstance(data, dict) else {}, self._local_seq)

        # 记录最后一条事件 ID 和时间戳（断线重连增量恢复用）
        if msg.event_id:
            self._last_received_event_id = msg.event_id
        self._since_timestamp = msg.timestamp

        # 如果设置了当前会话 ID：只派发该会话的消息 + 全局无会话消息
        # （避免旧会话的流式输出污染 UI）
        if self._current_session_id:
            if msg.session_id and msg.session_id != self._current_session_id:
                return

        # 分发到所有注册的处理器
        for handler in list(self._handlers):
            try:
                handler(msg)
            except Exception as e:
                log.error("消息处理器异常: %s", e, exc_info=True)

    async def _reconnect(self) -> None:
        """断线重连：指数退避，达到上限后转入降级模式（长间隔重试）而非永久死亡。"""
        if self._reconnect_attempts >= C.WS_RECONNECT_MAX_ATTEMPTS:
            # 进入降级模式：不再指数退避，改用最大间隔持续重试
            if not self._degraded_mode:
                self._degraded_mode = True
                log.warning(
                    "WebSocket 重连已达上限 %d 次，进入降级模式：每 %.0f 秒重试一次",
                    C.WS_RECONNECT_MAX_ATTEMPTS, C.WS_RECONNECT_MAX_DELAY_SEC,
                )
            await asyncio.sleep(C.WS_RECONNECT_MAX_DELAY_SEC)
            return

        self._reconnect_attempts += 1
        delay = min(
            C.WS_RECONNECT_BASE_DELAY_SEC * (2 ** (self._reconnect_attempts - 1)),
            C.WS_RECONNECT_MAX_DELAY_SEC,
        )
        log.info(
            "WebSocket 将在 %.1f 秒后重连（第 %d/%d 次）",
            delay, self._reconnect_attempts, C.WS_RECONNECT_MAX_ATTEMPTS,
        )
        await asyncio.sleep(delay)

    def switch_session(self, session_id: str | None) -> None:
        """切换当前活跃会话的过滤。

        DSH 0.1.0-rc.6 的 events.mux 承载所有会话的帧。
        切换瞬间立即清空消息渲染缓冲区（交给上层 UI），
        本客户端只负责过滤：非当前会话且带 session_id 的帧不再派发。
        """
        self._current_session_id = session_id
        log.info("当前过滤会话已切换: %s", session_id or "(none)")

    def send_to_session(self, session_id: str, message: dict) -> None:
        """向会话发送控制消息（目前走 HTTP RPC，保留接口）。"""
        # Typert 协议下排队/取消/打断都通过 HTTP session.updateQueue / session.cancel
        if not self._loop or not self._mux_ws:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._mux_ws.send(json.dumps(message)), self._loop
            )
        except Exception as e:
            log.debug("WS 发送失败（通常不需要，走 HTTP）: %s", e)
