"""会话状态机（第 2.3 / 3.3 / 9.2 节）。

管理会话列表、当前活跃会话、消息流、Agent 运行状态。
处理流式输出时的会话切换保护（session_id 校验）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .. import constants as C
# 直接从子模块导入，避免走 api/__init__.py → dsh_service → core/__init__.py 的循环链路
from ..api.dsh_service import DshService, MessageRecord, SessionInfo
from ..api.ws_client import WSMessage, WSEventType
from ..utils.logger import get_logger

log = get_logger("core.session_manager")

# CHUNK 流式通知节流（30ms 合并一次，避免每个 chunk 都全量刷新 UI）
_CHUNK_NOTIFY_INTERVAL_MS = 30


class AgentStatus(str, Enum):
    """Agent 运行状态。"""

    IDLE = "idle"
    RUNNING = "running"
    THINKING = "thinking"   # 模型推理中
    TOOL_EXECUTING = "tool_executing"  # 工具执行中
    ERROR = "error"


@dataclass
class ContextUsage:
    """上下文容量使用情况（第 9.2 节）。"""

    used_tokens: int = 0
    limit_tokens: int = C.DEFAULT_CONTEXT_LENGTH
    # prompt_tokens 来自最近一次模型调用的 token 通知
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def ratio(self) -> float:
        """使用比例 0.0-1.0。"""
        if self.limit_tokens <= 0:
            return 0.0
        return min(self.used_tokens / self.limit_tokens, 1.0)

    @property
    def percentage(self) -> int:
        return int(self.ratio * 100)

    @property
    def color_key(self) -> str:
        """颜色编码：< 70% 蓝，70-90% 橙，> 90% 红。"""
        for threshold, key in C.CONTEXT_COLOR_SEGMENTS:
            if self.ratio < threshold:
                return key
        return "error"

    @property
    def is_warning(self) -> bool:
        return self.ratio >= C.CONTEXT_WARN_THRESHOLD

    @property
    def is_danger(self) -> bool:
        return self.ratio >= C.CONTEXT_DANGER_THRESHOLD


@dataclass
class ToolCallRecord:
    """工具调用记录（用于聚合时间线）。"""

    tool_name: str
    status: str = "running"  # running / success / failed
    params: dict = field(default_factory=dict)
    result: Any = None
    started_at: float = field(default_factory=lambda: time.time())
    finished_at: float = 0.0
    error: str = ""


@dataclass
class SessionState:
    """单个会话的运行时状态。"""

    info: SessionInfo
    messages: list[MessageRecord] = field(default_factory=list)
    agent_status: AgentStatus = AgentStatus.IDLE
    context: ContextUsage = field(default_factory=ContextUsage)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # 当前 turn / step 数（状态栏显示）
    current_turn: int = 0
    current_step: int = 0
    total_steps: int = 0
    step_description: str = ""
    # 流式输出缓冲（未完成的 Assistant 回复）
    streaming_buffer: str = ""
    # 最后一次模型切换时间（用于容量跳变提示）
    last_model_switch_at: float = 0.0
    # 最近一次 RPC 错误（用于 UI 层错误提示）
    last_rpc_error: str = ""
    # 最近一次 WebSocket 事件类型 + 数据（用于 UI 层精确路由：chunk/turn_end/tool_call/tool_result）
    last_event_type: str = ""
    last_event_data: dict = field(default_factory=dict)
    # 本 Turn 暂存的 token 用量快照（TOKEN_USAGE 事件写入，TURN_END 消费后清空）
    pending_token_usage: dict[str, Any] = field(default_factory=dict)
    # 本 Turn 的 purpose / provider（从 turn_start 或发送请求时记录，可空）
    pending_purpose: str = ""
    pending_provider: str = ""
    # 消息时间戳索引：加速历史增量去重（O(1) 查询 vs 全表扫描）
    _msg_ts_index: set[float] = field(default_factory=set)


class SessionManager:
    """会话状态机。

    管理所有会话的状态，处理 WebSocket 事件并更新对应会话。
    流式输出时的会话切换保护：所有 WebSocket 消息处理器携带 session_id 标签，
    UI 渲染层在更新消息流前强制校验。
    """

    def __init__(self, dsh: DshService):
        self.dsh = dsh
        self._sessions: dict[str, SessionState] = {}
        self._current_session_id: str | None = None
        self._listeners: list[Callable[[str, SessionState], None]] = []

        # 注册 WebSocket 消息处理器
        self.dsh.add_ws_handler(self._on_ws_message)
        # 注册重连成功回调：触发增量恢复，补齐断线期间错过的消息
        self.dsh.ws.on_reconnected = self._on_ws_reconnected

        # CHUNK 节流：每个会话独立一个节流定时器 + 待刷新标志
        self._chunk_pending: dict[str, bool] = {}   # session_id -> 是否有待 flush 的 chunk
        self._chunk_timers: dict[str, threading.Timer] = {}
        self._chunk_lock = threading.Lock()

    # ===== 消息时间戳索引辅助（保持 set 与 list 同步）=====

    def _rebuild_ts_index(self, state: SessionState) -> None:
        """从 messages 列表重建时间戳索引（加载历史后调用）。"""
        state._msg_ts_index = {m.timestamp for m in state.messages}

    def _append_messages(self, state: SessionState, new_msgs: list[MessageRecord]) -> None:
        """追加消息并同步维护索引，去重。"""
        deduped: list[MessageRecord] = []
        for m in new_msgs:
            if m.timestamp not in state._msg_ts_index:
                state._msg_ts_index.add(m.timestamp)
                deduped.append(m)
        if deduped:
            state.messages.extend(deduped)
        return deduped

    def _on_ws_reconnected(self) -> None:
        """WebSocket 重连成功后，对当前会话做增量恢复。

        1. 通过 ReconnectManager.on_reconnect 获取断线前最后的 event_id / timestamp
        2. 调用 dsh.get_history(session_id, since=..., since_timestamp=...) 拉取增量
        3. 按时间戳去重后合并到 state.messages，并通知 UI 刷新
        """
        sid = self._current_session_id
        if not sid:
            return
        state = self._sessions.get(sid)
        if not state:
            return
        try:
            params = self.dsh.reconnect.on_reconnect(sid)
            since = params.get("since")
            since_ts = params.get("since_timestamp")
            incremental = self.dsh.get_history(
                sid,
                since=since,
                since_timestamp=since_ts if since_ts and since_ts > 0 else None,
                limit=C.SESSION_LAZY_LOAD_BATCH,
            )
            if not incremental:
                return
            # 优化：使用 _msg_ts_index O(1) 去重（原本每次重建 set 全表扫描）
            new_msgs = self._append_messages(state, incremental)
            if not new_msgs:
                return
            state.messages.sort(key=lambda m: m.timestamp)
            # 标记一次"批量更新"事件，让 UI 重载消息流
            state.last_event_type = "history_sync"
            state.last_event_data = {"added": len(new_msgs)}
            self._notify(sid)
            log.info("重连增量恢复完成: session=%s 新增 %d 条消息", sid, len(new_msgs))
        except Exception as e:
            log.error("重连增量恢复失败 session=%s: %s", sid, e, exc_info=True)

    @property
    def current_session_id(self) -> str | None:
        return self._current_session_id

    @property
    def current_session(self) -> SessionState | None:
        if not self._current_session_id:
            return None
        return self._sessions.get(self._current_session_id)

    @property
    def sessions(self) -> dict[str, SessionState]:
        return self._sessions

    def add_listener(self, listener: Callable[[str, SessionState], None]) -> None:
        """注册会话状态变更监听器。"""
        self._listeners.append(listener)

    def _notify(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state:
            for listener in list(self._listeners):
                try:
                    listener(session_id, state)
                except Exception as e:
                    log.error("会话监听器异常: %s", e)

    # ===== CHUNK 节流（防止高频小块导致 UI 过度重绘）=====

    def _notify_chunk(self, session_id: str) -> None:
        """CHUNK 专用通知：30ms 内的多次 chunk 合并成一次 UI 刷新。"""
        with self._chunk_lock:
            # 已有定时器在倒计时，只打标记即可（会在 flush 时一并通知）
            if session_id in self._chunk_timers:
                self._chunk_pending[session_id] = True
                return
            # 无定时器：立即通知（首次 chunk 立即显示，减少延迟感），并启动定时器合并后续
            self._chunk_pending[session_id] = False
            self._notify(session_id)

            def _flush():
                with self._chunk_lock:
                    self._chunk_timers.pop(session_id, None)
                    pending = self._chunk_pending.pop(session_id, False)
                if pending:
                    self._notify(session_id)

            t = threading.Timer(_CHUNK_NOTIFY_INTERVAL_MS / 1000.0, _flush)
            t.daemon = True
            self._chunk_timers[session_id] = t
            t.start()

    def _cancel_chunk_timer(self, session_id: str) -> None:
        """会话切换 / TURN_END 时取消 chunk 定时器，确保最后一块立即送达。"""
        with self._chunk_lock:
            t = self._chunk_timers.pop(session_id, None)
            if t is not None:
                t.cancel()
            pending = self._chunk_pending.pop(session_id, False)
        if pending:
            self._notify(session_id)

    # ===== 会话管理 =====

    async def refresh_sessions(self) -> list[SessionInfo]:
        """刷新会话列表。"""
        try:
            infos = self.dsh.list_sessions()
            for info in infos:
                if info.id not in self._sessions:
                    self._sessions[info.id] = SessionState(info=info)
                else:
                    self._sessions[info.id].info = info
            return infos
        except Exception as e:
            log.error("刷新会话列表失败: %s", e)
            return []

    def create_session(self, title: str = "", model: str = "", workspace: str = "",
                       agent_preset: str = "") -> SessionState:
        """创建新会话并设为当前会话。

        优先通过 DSH RPC 创建，agentPreset 字段决定使用哪个 DSH 原生预设
        （standard / code / minimal / cordis）。
        若 DSH 不可用（离线模式 / 连接失败 / RPC 错误），
        回退为"本地草稿会话"——生成稳定的本地 sessionId，UI 立即响应，
        用户看到 ＋ 按钮点击后确实打开了新会话（而不是静默无反应）。
        """
        import time
        import uuid
        from ..api.dsh_service import SessionInfo

        # 1. 正常路径：通过 DSH RPC 创建（含 agentPreset）
        try:
            info = self.dsh.create_session(title=title, model=model, workspace=workspace,
                                            agent_preset=agent_preset)
            state = SessionState(info=info)
            self._sessions[info.id] = state
            self.switch_to(info.id)
            return state
        except Exception as e:
            log.warning("通过 DSH 创建会话失败，降级为本地草稿会话: %s", e)

        # 2. 回退：本地草稿会话（DSH 不可用时，保证 ＋ 按钮永远有反应）
        now = time.time()
        local_id = "local-" + uuid.uuid4().hex[:12]
        info = SessionInfo(
            id=local_id,
            title=title or "本地草稿（待同步）",
            created_at=now,
            updated_at=now,
            model=model,
            message_count=0,
        )
        state = SessionState(info=info)
        state.local_draft = True
        self._sessions[info.id] = state
        self.switch_to(info.id)
        return state

    def switch_to(self, session_id: str) -> None:
        """切换当前活跃会话。

        流式输出时的会话切换保护：
        1. 切换瞬间立即清空消息渲染缓冲区
        2. 切换 WebSocket 流到新会话
        3. 加载新会话历史（懒加载：先加载最近 50 条）
        """
        if session_id == self._current_session_id:
            return

        old_id = self._current_session_id
        self._current_session_id = session_id

        # 切换前清理旧会话的 CHUNK 节流定时器
        if old_id:
            self._cancel_chunk_timer(old_id)

        # 清空旧会话的流式缓冲
        if old_id and old_id in self._sessions:
            self._sessions[old_id].streaming_buffer = ""

        # 切换 WebSocket 流
        self.dsh.switch_session(session_id)

        # 加载历史（懒加载）
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                info=SessionInfo(id=session_id)
            )
        state = self._sessions[session_id]
        if not state.messages:
            self._load_history(session_id, limit=C.SESSION_LAZY_LOAD_BATCH)

        self._notify(session_id)
        log.info("切换会话: %s → %s", old_id, session_id)

    def _load_history(self, session_id: str, limit: int = 50) -> None:
        """加载会话历史（懒加载）。"""
        try:
            messages = self.dsh.get_history(session_id, limit=limit)
            if session_id in self._sessions:
                state = self._sessions[session_id]
                state.messages = messages
                # 重建时间戳索引
                self._rebuild_ts_index(state)
        except Exception as e:
            log.warning("加载会话历史失败 session=%s: %s", session_id, e)

    def load_more_history(self, session_id: str) -> int:
        """向上滚动时追加加载更多历史。"""
        state = self._sessions.get(session_id)
        if not state:
            return 0
        try:
            messages = self.dsh.get_history(
                session_id,
                limit=C.SESSION_LAZY_LOAD_BATCH,
            )
            # 优化：使用 _msg_ts_index 去重，同时把更早的消息插到头部时同步更新索引
            new_msgs: list[MessageRecord] = []
            for m in messages:
                if m.timestamp not in state._msg_ts_index:
                    state._msg_ts_index.add(m.timestamp)
                    new_msgs.append(m)
            if new_msgs:
                state.messages = new_msgs + state.messages
                self._notify(session_id)
            return len(new_msgs)
        except Exception as e:
            log.warning("追加加载历史失败: %s", e)
            return 0

    # ===== 消息发送 =====

    def send_message(self, content: str, attachments: list[str] | None = None) -> None:
        """发送消息到当前会话。

        Agent 运行时输入框变为"追加消息"模式——输入的内容通过 session.updateQueue 发送，
        排队等待下一轮处理，而非立即触发新 turn。

        失败时回滚乐观添加的用户消息，避免 UI 上出现"消息发出去了但没响应"
        的假成功状态（导致界面留白/变白）。
        """
        self._last_send_ok = False
        if not self._current_session_id:
            return
        state = self.current_session
        if not state:
            return

        # 本地立即添加用户消息（乐观更新）
        optimistic_msg = MessageRecord(
            role="user",
            content=content,
            timestamp=time.time(),
        )
        state.messages.append(optimistic_msg)
        state._msg_ts_index.add(optimistic_msg.timestamp)   # 同步索引
        append_index = len(state.messages) - 1

        rpc_error: Exception | None = None

        if state.agent_status == AgentStatus.RUNNING:
            # Agent 运行中：追加到队列
            try:
                self.dsh.update_queue(self._current_session_id, content)
                self._last_send_ok = True
            except Exception as e:
                log.error("追加消息到队列失败: %s", e)
                rpc_error = e
        else:
            # 空闲：发送新消息
            state.agent_status = AgentStatus.RUNNING
            state.current_turn += 1
            try:
                self.dsh.send_message(self._current_session_id, content, attachments)
                self._last_send_ok = True
            except Exception as e:
                log.error("发送消息失败: %s", e)
                rpc_error = e
                state.agent_status = AgentStatus.ERROR

        # ---- 失败回滚：移除乐观添加的消息 ----
        if not self._last_send_ok:
            if 0 <= append_index < len(state.messages) and \
                    state.messages[append_index] is optimistic_msg:
                popped = state.messages.pop(append_index)
                state._msg_ts_index.discard(popped.timestamp)   # 同步索引回滚
            if state.agent_status == AgentStatus.RUNNING:
                state.agent_status = AgentStatus.IDLE
                if state.current_turn > 0:
                    state.current_turn -= 1
            # 将原始异常携带到状态上，供 UI 层读取提示
            state.last_rpc_error = str(rpc_error) if rpc_error else ""

        self._notify(self._current_session_id)

    def cancel_agent(self) -> None:
        """停止 Agent 执行。"""
        if not self._current_session_id:
            return
        try:
            self.dsh.cancel_session(self._current_session_id)
            state = self.current_session
            if state:
                state.agent_status = AgentStatus.IDLE
                self._notify(self._current_session_id)
        except Exception as e:
            log.error("停止 Agent 失败: %s", e)

    def delete_session(self, session_id: str) -> bool:
        """删除会话：调用 DSH RPC + 清理本地状态。

        Returns: True 表示 DSH 端删除成功（本地状态总会清理）。
        """
        ok = self.dsh.delete_session(session_id)
        # 本地清理（无论 DSH 是否成功，都从本地移除，避免列表残留）
        if session_id in self._sessions:
            del self._sessions[session_id]
        if self._current_session_id == session_id:
            self._current_session_id = None
        # 清理重连缓冲
        self.dsh.reconnect.clear_session(session_id)
        log.info("会话已删除: %s (dsh_ok=%s)", session_id, ok)
        return ok

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话：调用 DSH RPC + 更新本地状态。"""
        ok = self.dsh.rename_session(session_id, title)
        state = self._sessions.get(session_id)
        if state:
            state.info.title = title
            self._notify(session_id)
        log.info("会话已重命名: %s → %s (dsh_ok=%s)", session_id, title, ok)
        return ok

    # ===== 模型切换 =====

    def select_model(self, model_id: str) -> None:
        """切换模型，更新上下文容量基准。"""
        if not self._current_session_id:
            return
        try:
            self.dsh.select_model(self._current_session_id, model_id)
            state = self.current_session
            if state:
                state.info.model = model_id
                state.last_model_switch_at = time.time()
                # 更新上下文上限（模型切换时容量基准随之更新）
                state.context.limit_tokens = C.MODEL_CONTEXT_LENGTH_FALLBACK.get(
                    model_id, C.DEFAULT_CONTEXT_LENGTH
                )
                self._notify(self._current_session_id)
        except Exception as e:
            log.error("切换模型失败: %s", e)

    # ===== WebSocket 事件处理 =====

    def _on_ws_message(self, msg: WSMessage) -> None:
        """处理 WebSocket 消息。

        流式输出时的会话切换保护：
        if incoming_event.session_id != self.current_session_id: discard
        """
        # 全局事件（无 session_id）直接处理
        if not msg.session_id:
            self._handle_global_event(msg)
            return

        # 会话切换保护：丢弃非当前会话的事件
        if msg.session_id != self._current_session_id:
            log.debug("丢弃非当前会话事件 session=%s current=%s", msg.session_id, self._current_session_id)
            return

        # 增量恢复去重
        if not self.dsh.reconnect.on_message(msg):
            return  # 重复消息，已去重

        self._handle_session_event(msg)

    def _handle_global_event(self, msg: WSMessage) -> None:
        """处理全局事件（会话创建/删除、连接状态变化）。"""
        if msg.event_type == WSEventType.SESSION_CREATED:
            data = msg.data
            info = DshService._parse_session(data)
            if info.id and info.id not in self._sessions:
                self._sessions[info.id] = SessionState(info=info)
                log.info("新会话已创建: %s", info.id)
        elif msg.event_type == WSEventType.SESSION_DELETED:
            sid = msg.data.get("session_id", "")
            if sid in self._sessions:
                del self._sessions[sid]
                if self._current_session_id == sid:
                    self._current_session_id = None
                log.info("会话已删除: %s", sid)

    def _handle_session_event(self, msg: WSMessage) -> None:
        """处理当前会话的事件流。"""
        state = self.current_session
        if not state:
            return

        if msg.event_type == WSEventType.CHUNK:
            # 流式文本块：追加到 streaming_buffer
            chunk = msg.data.get("content") or msg.data.get("text", "")
            state.streaming_buffer += chunk
            state.agent_status = AgentStatus.THINKING
            state.last_event_type = "chunk"
            state.last_event_data = {"chunk": chunk}
            # 优化：CHUNK 节流，30ms 合并一次 UI 刷新（避免每个小 chunk 都重绘）
            self._notify_chunk(msg.session_id)

        elif msg.event_type == WSEventType.TURN_START:
            state.agent_status = AgentStatus.RUNNING
            state.current_turn = msg.data.get("turn", state.current_turn + 1)
            state.streaming_buffer = ""
            state.last_event_type = "turn_start"
            state.last_event_data = {}
            self._cancel_chunk_timer(msg.session_id)   # 清理旧定时器
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TURN_END or msg.event_type == WSEventType.DONE:
            # 一轮对话结束：先 flush 掉 CHUNK 节流，确保最后一块立即送达
            self._cancel_chunk_timer(msg.session_id)
            # 一轮对话结束：将 streaming_buffer 固化为消息
            finalized_msg = None
            if state.streaming_buffer:
                finalized_msg = MessageRecord(
                    role="assistant",
                    content=state.streaming_buffer,
                    timestamp=time.time(),
                )
                state.messages.append(finalized_msg)
                state._msg_ts_index.add(finalized_msg.timestamp)   # 同步索引
                state.streaming_buffer = ""
            # —— 用量与消耗追踪：写入本 Turn 的 token 记录（插件级完整维度）——
            snap: dict = state.pending_token_usage
            extra_data = msg.data if isinstance(msg.data, dict) else {}
            # 如果 data 里有 finish reason / token 字段，补充
            for src_key, dst_key in [
                ("finishReason", "finish_reason"), ("finish_reason", "finish_reason"),
                ("reason", "finish_reason"),
            ]:
                v = extra_data.get(src_key)
                if v and dst_key not in snap:
                    snap[dst_key] = str(v)
            if snap:
                try:
                    if hasattr(self.dsh, "usage") and self.dsh.usage is not None:
                        model = snap.get("model") or getattr(state.info, "model", "") or ""
                        provider = snap.get("provider") or state.pending_provider or ""
                        purpose = snap.get("purpose") or state.pending_purpose or ""
                        # 记录时间：优先取事件 msg.timestamp（秒级）转 ms，否则当前
                        ts_ms = int((msg.timestamp or time.time()) * 1000)
                        self.dsh.usage.add_record(
                            time_ms=ts_ms,
                            model=str(model),
                            provider=str(provider),
                            purpose=str(purpose),
                            input_tokens=int(snap.get("input_tokens", 0)),
                            output_tokens=int(snap.get("output_tokens", 0)),
                            cache_read_tokens=int(snap.get("cache_read_tokens", 0)),
                            cache_write_tokens=int(snap.get("cache_write_tokens", 0)),
                            reasoning_tokens=int(snap.get("reasoning_tokens", 0)),
                            finish_reason=str(snap.get("finish_reason", "")),
                        )
                except Exception as e:
                    log.warning("写入用量记录失败: %s", e)
                # 消费后清空快照
                state.pending_token_usage = {}
                state.pending_purpose = ""
                state.pending_provider = ""
            state.agent_status = AgentStatus.IDLE
            state.last_event_type = "turn_end"
            state.last_event_data = {"message": finalized_msg}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TOOL_CALL:
            tool_name = msg.data.get("tool", msg.data.get("name", "unknown"))
            params = msg.data.get("params") or msg.data.get("arguments", {})
            record = ToolCallRecord(
                tool_name=tool_name,
                params=params,
            )
            state.tool_calls.append(record)
            state.agent_status = AgentStatus.TOOL_EXECUTING
            state.last_event_type = "tool_call"
            state.last_event_data = {"tool_name": tool_name, "params": params}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TOOL_RESULT:
            # 更新最后一个同名工具调用的结果
            tool_name = msg.data.get("tool", "")
            status = msg.data.get("status", "success")
            result = msg.data.get("result")
            error = msg.data.get("error", "")
            for record in reversed(state.tool_calls):
                if record.tool_name == tool_name and record.status == "running":
                    record.status = status
                    record.result = result
                    record.error = error
                    record.finished_at = time.time()
                    break
            state.last_event_type = "tool_result"
            state.last_event_data = {
                "tool_name": tool_name,
                "status": status,
                "result": result,
                "error": error,
            }
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.STEP_START:
            state.current_step = msg.data.get("step", state.current_step + 1)
            state.total_steps = msg.data.get("total_steps", 0)
            state.step_description = msg.data.get("description", "")
            state.last_event_type = "step_start"
            state.last_event_data = {"description": state.step_description}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.STEP_END:
            state.step_description = msg.data.get("description", "")
            state.last_event_type = "step_end"
            state.last_event_data = {"description": state.step_description}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TOKEN_USAGE:
            data = dict(msg.data) if isinstance(msg.data, dict) else {}
            # 上下文容量：最近一次模型调用的 prompt_tokens + 本轮 completion_tokens
            prompt = int(data.get("prompt_tokens", 0))
            completion = int(data.get("completion_tokens", 0))
            state.context.prompt_tokens = prompt
            state.context.completion_tokens = completion
            # 已用上下文 = prompt_tokens + completion_tokens（不累加历轮）
            state.context.used_tokens = prompt + completion
            # 保存完整 token 快照（兼容 snake_case / camelCase，给 UsageTracker 用）
            snap: dict = state.pending_token_usage
            for src_key, dst_key in [
                ("prompt_tokens", "input_tokens"), ("inputTokens", "input_tokens"),
                ("input_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"), ("outputTokens", "output_tokens"),
                ("output_tokens", "output_tokens"),
                ("cacheReadTokens", "cache_read_tokens"), ("cache_read_tokens", "cache_read_tokens"),
                ("cacheWriteTokens", "cache_write_tokens"), ("cache_write_tokens", "cache_write_tokens"),
                ("reasoningTokens", "reasoning_tokens"), ("reasoning_tokens", "reasoning_tokens"),
                ("finishReason", "finish_reason"), ("finish_reason", "finish_reason"),
                ("model", "model"), ("provider", "provider"), ("purpose", "purpose"),
            ]:
                if src_key in data and data[src_key] is not None:
                    snap[dst_key] = data[src_key]
            state.last_event_type = "token_usage"
            state.last_event_data = {}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.ERROR:
            state.agent_status = AgentStatus.ERROR
            log.error("会话错误 session=%s: %s", msg.session_id, msg.data)
            state.last_event_type = "error"
            state.last_event_data = dict(msg.data) if isinstance(msg.data, dict) else {}
            self._notify(msg.session_id)

    # ===== 离线缓存持久化（第 8.4 节）=====
    # 每次 Agent Turn 完全结束时，将该 Turn 的完整上下文序列化为 JSON，存入本地 SQLite。
    # 仅保留最近 5000 条消息，超出按 LRU 淘汰。

    def get_status_text(self) -> str:
        """获取状态栏 Agent 状态文本。"""
        state = self.current_session
        if not state:
            return "Idle"
        if state.agent_status == AgentStatus.RUNNING:
            return f"Running · Turn {state.current_turn}"
        if state.agent_status == AgentStatus.THINKING:
            return f"Thinking · Turn {state.current_turn}"
        if state.agent_status == AgentStatus.TOOL_EXECUTING:
            return f"Tool · Step {state.current_step}"
        if state.agent_status == AgentStatus.ERROR:
            return "Error"
        return "Idle"
